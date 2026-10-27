from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from time import perf_counter
from typing import Any

import httpx
from pydantic import BaseModel, Field

from .ai_config_service import AIConfigurationService
from .ai_configuration import (
    AIProvider,
    AIProviderConfiguration,
    AIProviderRuntimeState,
    AIReasoningEffort,
)
from .ai_usage import AICompletionResult, AIUsage, AIUsageEvent, estimate_deepseek_cost_cny

logger = logging.getLogger(__name__)

MAX_STRUCTURED_OUTPUT_TOKENS = 10_000


class AIErrorCode(StrEnum):
    NOT_CONFIGURED = "not_configured"
    AUTHENTICATION = "authentication"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    INVALID_RESPONSE = "invalid_response"
    CIRCUIT_OPEN = "circuit_open"
    QUALITY_GATED = "quality_gated"
    CONFIGURATION = "configuration"


class AIProviderTestResult(BaseModel):
    ok: bool
    provider: AIProvider
    model: str
    latency_ms: int
    model_count: int
    models: list[str]
    selected_model_available: bool | None
    failure_code: AIErrorCode | None = None
    message: str
    tested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AIProviderError(RuntimeError):
    def __init__(
        self,
        provider: AIProvider,
        code: AIErrorCode,
        message: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
        diagnostic_message: str = "",
    ):
        super().__init__(message)
        self.provider = provider
        self.code = code
        self.public_message = message
        self.retryable = retryable
        self.status_code = status_code
        self.diagnostic_message = diagnostic_message[:500]
        self.usage = AIUsage()
        self.provider_request_id: str | None = None
        self.model = ""
        self.latency_ms = 0
        self.finish_reason: str | None = None
        self.response_char_count = 0
        self.operation = "structured_generation"
        self.thinking_enabled: bool | None = None
        self.reasoning_effort: AIReasoningEffort | None = None
        self.prompt_version = "v1"
        self.schema_version = "v1"
        self.created_at = datetime.now(UTC)


def _endpoint(base_url: str, resource: str) -> str:
    base = base_url.rstrip("/")
    path = resource if resource.startswith("/") else f"/{resource}"
    if base.endswith("/v1") and path.startswith("/v1/"):
        path = path[3:]
    return f"{base}{path}"


def _usage(
    payload: dict[str, Any],
    *,
    provider: AIProvider,
    model: str,
    created_at: datetime,
) -> AIUsage:
    usage = payload.get("usage") or payload.get("usageMetadata") or {}
    input_tokens = int(
        usage.get("input_tokens", usage.get("prompt_tokens", usage.get("promptTokenCount", 0))) or 0
    )
    output_tokens = int(
        usage.get(
            "output_tokens",
            usage.get("completion_tokens", usage.get("candidatesTokenCount", 0)),
        )
        or 0
    )
    input_details = usage.get("input_tokens_details") or {}
    completion_details = (
        usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}
    )
    cache_hit_input_tokens = int(
        usage.get(
            "prompt_cache_hit_tokens",
            input_details.get("cached_tokens", usage.get("cachedContentTokenCount", 0)),
        )
        or 0
    )
    explicit_miss = usage.get("prompt_cache_miss_tokens")
    cache_miss_input_tokens = (
        int(explicit_miss or 0)
        if explicit_miss is not None
        else max(input_tokens - cache_hit_input_tokens, 0)
    )
    reasoning_tokens = int(completion_details.get("reasoning_tokens", 0) or 0)
    estimated_cost_cny = None
    pricing_tier = None
    if provider == AIProvider.DEEPSEEK:
        estimated_cost_cny, pricing_tier = estimate_deepseek_cost_cny(
            model=model,
            at=created_at,
            cache_hit_input_tokens=cache_hit_input_tokens,
            cache_miss_input_tokens=cache_miss_input_tokens,
            output_tokens=output_tokens,
        )
    return AIUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=int(usage.get("total_tokens", usage.get("totalTokenCount", 0)))
        or input_tokens + output_tokens,
        cache_hit_input_tokens=cache_hit_input_tokens,
        cache_miss_input_tokens=cache_miss_input_tokens,
        reasoning_tokens=reasoning_tokens,
        visible_output_tokens=max(output_tokens - reasoning_tokens, 0),
        estimated_cost_cny=estimated_cost_cny,
        pricing_tier=pricing_tier,
    )


def _parse_json_text(text: str, provider: AIProvider) -> dict[str, Any]:
    normalized = text.strip()
    if normalized.startswith("```"):
        normalized = normalized.removeprefix("```json").removeprefix("```")
        normalized = normalized.removesuffix("```").strip()
    try:
        parsed = json.loads(normalized)
    except json.JSONDecodeError as exc:
        raise AIProviderError(
            provider,
            AIErrorCode.INVALID_RESPONSE,
            "模型没有返回可校验的 JSON 结果",
            retryable=True,
            diagnostic_message=(
                f"JSONDecodeError line={exc.lineno} column={exc.colno} "
                f"position={exc.pos} chars={len(normalized)}"
            ),
        ) from exc
    except TypeError as exc:
        raise AIProviderError(
            provider,
            AIErrorCode.INVALID_RESPONSE,
            "模型没有返回可校验的 JSON 结果",
            retryable=True,
            diagnostic_message=f"TypeError chars={len(normalized)}",
        ) from exc
    if not isinstance(parsed, dict):
        raise AIProviderError(
            provider,
            AIErrorCode.INVALID_RESPONSE,
            "模型返回的结构必须是 JSON 对象",
            retryable=True,
            diagnostic_message=f"top_level_type={type(parsed).__name__}",
        )
    return parsed


def _finish_reason(payload: dict[str, Any]) -> str | None:
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        value = choices[0].get("finish_reason")
        if value is not None:
            return str(value)[:100]
    for key in ("stop_reason", "status"):
        value = payload.get(key)
        if value is not None:
            return str(value)[:100]
    return None


class BaseProviderAdapter:
    def __init__(
        self,
        configuration: AIProviderConfiguration,
        api_key: str,
        *,
        proxy_url: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.configuration = configuration
        self.provider = configuration.provider
        self.api_key = api_key
        client_options: dict[str, Any] = {
            "timeout": httpx.Timeout(configuration.timeout_seconds),
            "follow_redirects": False,
            "transport": transport,
            "trust_env": True,
        }
        if proxy_url and transport is None:
            client_options["proxy"] = proxy_url
        self.client = httpx.AsyncClient(**client_options)

    async def close(self) -> None:
        await self.client.aclose()

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    async def _request(
        self,
        method: str,
        resource: str,
        *,
        json_body: dict[str, Any] | None = None,
        attempts: int = 3,
    ) -> dict[str, Any]:
        if not self.api_key:
            raise AIProviderError(
                self.provider,
                AIErrorCode.NOT_CONFIGURED,
                "该厂商尚未配置 API Key",
            )
        for attempt in range(attempts):
            caught: Exception | None = None
            try:
                response = await self.client.request(
                    method,
                    _endpoint(self.configuration.base_url, resource),
                    headers={"Accept": "application/json", **self.headers()},
                    json=json_body,
                )
            except httpx.TimeoutException as exc:
                caught = exc
                error = AIProviderError(
                    self.provider,
                    AIErrorCode.TIMEOUT,
                    "AI 厂商请求超时",
                    retryable=True,
                )
            except httpx.RequestError as exc:
                caught = exc
                error = AIProviderError(
                    self.provider,
                    AIErrorCode.UNAVAILABLE,
                    "无法连接 AI 厂商服务",
                    retryable=True,
                )
            else:
                if response.is_success:
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        raise AIProviderError(
                            self.provider,
                            AIErrorCode.INVALID_RESPONSE,
                            "AI 厂商返回了无效响应",
                        ) from exc
                    if not isinstance(payload, dict):
                        raise AIProviderError(
                            self.provider,
                            AIErrorCode.INVALID_RESPONSE,
                            "AI 厂商返回了无效响应",
                        )
                    return payload
                if response.status_code in {401, 403}:
                    raise AIProviderError(
                        self.provider,
                        AIErrorCode.AUTHENTICATION,
                        "API Key 无效、无权限或账户未开通该模型",
                        status_code=response.status_code,
                    )
                if response.status_code == 429:
                    error = AIProviderError(
                        self.provider,
                        AIErrorCode.RATE_LIMITED,
                        "AI 厂商触发限流或额度不足",
                        retryable=True,
                        status_code=429,
                    )
                elif response.status_code >= 500:
                    error = AIProviderError(
                        self.provider,
                        AIErrorCode.UNAVAILABLE,
                        "AI 厂商服务暂时不可用",
                        retryable=True,
                        status_code=response.status_code,
                    )
                else:
                    raise AIProviderError(
                        self.provider,
                        AIErrorCode.CONFIGURATION,
                        f"AI 请求被厂商拒绝（HTTP {response.status_code}）",
                        status_code=response.status_code,
                    )
            if attempt + 1 >= attempts or not error.retryable:
                raise error from caught
            await asyncio.sleep(min(0.25 * (2**attempt) + random.uniform(0, 0.1), 1.0))
        raise AssertionError("unreachable")

    async def list_models(self) -> list[str]:
        payload = await self._request("GET", "/models")
        candidates = payload.get("data", payload.get("models", []))
        models: list[str] = []
        if isinstance(candidates, list):
            for item in candidates:
                if isinstance(item, str):
                    models.append(item)
                elif isinstance(item, dict):
                    model_id = item.get("id") or item.get("name")
                    if isinstance(model_id, str):
                        models.append(model_id.removeprefix("models/"))
        return sorted(set(models))[:200]

    def _completion_result(
        self,
        *,
        payload: dict[str, Any],
        text: str,
        started: float,
        operation: str,
        thinking_enabled: bool | None,
        reasoning_effort: AIReasoningEffort | None,
        prompt_version: str,
        schema_version: str,
    ) -> AICompletionResult:
        created_at = datetime.now(UTC)
        model = str(payload.get("model") or self.configuration.model)
        usage = _usage(
            payload,
            provider=self.provider,
            model=model,
            created_at=created_at,
        )
        latency_ms = round((perf_counter() - started) * 1000)
        provider_request_id = str(payload["id"]) if payload.get("id") else None
        finish_reason = _finish_reason(payload)
        try:
            data = _parse_json_text(text, self.provider)
        except AIProviderError as exc:
            exc.usage = usage
            exc.provider_request_id = provider_request_id
            exc.model = model
            exc.latency_ms = latency_ms
            exc.finish_reason = finish_reason
            exc.response_char_count = len(text)
            exc.operation = operation
            exc.thinking_enabled = thinking_enabled
            exc.reasoning_effort = reasoning_effort
            exc.prompt_version = prompt_version
            exc.schema_version = schema_version
            exc.created_at = created_at
            raise
        return AICompletionResult(
            provider=self.provider,
            model=model,
            data=data,
            usage=usage,
            latency_ms=latency_ms,
            provider_request_id=provider_request_id,
            finish_reason=finish_reason,
            response_char_count=len(text),
            operation=operation,
            thinking_enabled=thinking_enabled,
            reasoning_effort=reasoning_effort,
            prompt_version=prompt_version,
            schema_version=schema_version,
            created_at=created_at,
        )

    async def generate_json(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        schema_name: str,
        max_tokens: int = 1800,
        thinking_enabled: bool | None = None,
        reasoning_effort: AIReasoningEffort | None = None,
        operation: str = "structured_generation",
        prompt_version: str = "v1",
        schema_version: str = "v1",
    ) -> AICompletionResult:
        raise NotImplementedError


class OpenAIResponsesAdapter(BaseProviderAdapter):
    async def generate_json(
        self,
        *,
        system,
        prompt,
        schema,
        schema_name,
        max_tokens=1800,
        thinking_enabled=None,
        reasoning_effort=None,
        operation="structured_generation",
        prompt_version="v1",
        schema_version="v1",
    ):
        started = perf_counter()
        payload = await self._request(
            "POST",
            "/responses",
            json_body={
                "model": self.configuration.model,
                "instructions": system,
                "input": prompt,
                "max_output_tokens": max_tokens,
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": schema_name,
                        "schema": schema,
                        "strict": True,
                    }
                },
            },
        )
        text = payload.get("output_text")
        if not isinstance(text, str):
            text = next(
                (
                    block.get("text")
                    for item in payload.get("output", [])
                    if isinstance(item, dict)
                    for block in item.get("content", [])
                    if isinstance(block, dict) and isinstance(block.get("text"), str)
                ),
                "",
            )
        return self._completion_result(
            payload=payload,
            text=text,
            started=started,
            operation=operation,
            thinking_enabled=None,
            reasoning_effort=None,
            prompt_version=prompt_version,
            schema_version=schema_version,
        )


class AnthropicMessagesAdapter(BaseProviderAdapter):
    def headers(self) -> dict[str, str]:
        return {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}

    async def generate_json(
        self,
        *,
        system,
        prompt,
        schema,
        schema_name,
        max_tokens=1800,
        thinking_enabled=None,
        reasoning_effort=None,
        operation="structured_generation",
        prompt_version="v1",
        schema_version="v1",
    ):
        started = perf_counter()
        payload = await self._request(
            "POST",
            "/messages",
            json_body={
                "model": self.configuration.model,
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "output_config": {"format": {"type": "json_schema", "schema": schema}},
            },
        )
        text = next(
            (
                item.get("text")
                for item in payload.get("content", [])
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            ),
            "",
        )
        return self._completion_result(
            payload=payload,
            text=text,
            started=started,
            operation=operation,
            thinking_enabled=None,
            reasoning_effort=None,
            prompt_version=prompt_version,
            schema_version=schema_version,
        )


class GeminiInteractionsAdapter(BaseProviderAdapter):
    def headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self.api_key}

    async def generate_json(
        self,
        *,
        system,
        prompt,
        schema,
        schema_name,
        max_tokens=1800,
        thinking_enabled=None,
        reasoning_effort=None,
        operation="structured_generation",
        prompt_version="v1",
        schema_version="v1",
    ):
        started = perf_counter()
        payload = await self._request(
            "POST",
            "/interactions",
            json_body={
                "model": self.configuration.model,
                "system_instruction": system,
                "input": prompt,
                "response_format": {
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": schema,
                },
                "generation_config": {"max_output_tokens": max_tokens},
            },
        )
        outputs = payload.get("outputs", [])
        text = payload.get("output_text") or next(
            (
                item.get("text")
                for item in reversed(outputs)
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            ),
            "",
        )
        return self._completion_result(
            payload=payload,
            text=text,
            started=started,
            operation=operation,
            thinking_enabled=None,
            reasoning_effort=None,
            prompt_version=prompt_version,
            schema_version=schema_version,
        )


class OpenAIChatAdapter(BaseProviderAdapter):
    async def generate_json(
        self,
        *,
        system,
        prompt,
        schema,
        schema_name,
        max_tokens=1800,
        thinking_enabled=None,
        reasoning_effort=None,
        operation="structured_generation",
        prompt_version="v1",
        schema_version="v1",
    ):
        started = perf_counter()
        native_schema = self.provider in {
            AIProvider.XAI,
            AIProvider.MISTRAL,
            AIProvider.CUSTOM,
        }
        response_format: dict[str, Any]
        if native_schema:
            response_format = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": schema, "strict": True},
            }
        else:
            response_format = {"type": "json_object"}
            system = (
                f"{system}\nReturn JSON only. It must satisfy this JSON Schema:\n"
                f"{json.dumps(schema, ensure_ascii=False, separators=(',', ':'))}"
            )
        request_body = {
            "model": self.configuration.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_tokens,
            "response_format": response_format,
        }
        effective_thinking = None
        effective_effort = None
        if self.provider == AIProvider.DEEPSEEK:
            effective_thinking = (
                self.configuration.thinking_enabled
                if thinking_enabled is None
                else thinking_enabled
            )
            request_body["thinking"] = {
                "type": "enabled" if effective_thinking else "disabled"
            }
            if effective_thinking:
                effective_effort = reasoning_effort or self.configuration.reasoning_effort
                request_body["reasoning_effort"] = str(effective_effort)
        payload = await self._request(
            "POST",
            "/chat/completions",
            json_body=request_body,
        )
        choices = payload.get("choices", [])
        text = next(
            (
                item.get("message", {}).get("content")
                for item in choices
                if isinstance(item, dict)
                and isinstance(item.get("message", {}).get("content"), str)
            ),
            "",
        )
        return self._completion_result(
            payload=payload,
            text=text,
            started=started,
            operation=operation,
            thinking_enabled=effective_thinking,
            reasoning_effort=effective_effort,
            prompt_version=prompt_version,
            schema_version=schema_version,
        )


def create_adapter(
    configuration: AIProviderConfiguration,
    api_key: str,
    *,
    proxy_url: str = "",
    transport: httpx.AsyncBaseTransport | None = None,
) -> BaseProviderAdapter:
    adapter_type: type[BaseProviderAdapter]
    if configuration.provider == AIProvider.OPENAI:
        adapter_type = OpenAIResponsesAdapter
    elif configuration.provider == AIProvider.ANTHROPIC:
        adapter_type = AnthropicMessagesAdapter
    elif configuration.provider == AIProvider.GOOGLE:
        adapter_type = GeminiInteractionsAdapter
    else:
        adapter_type = OpenAIChatAdapter
    return adapter_type(
        configuration,
        api_key,
        proxy_url=proxy_url,
        transport=transport,
    )


@dataclass
class CircuitState:
    failures: int = 0
    opened_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error_code: AIErrorCode | None = None
    last_error_message: str = ""
    half_open_attempts: int = 0
    half_open_in_flight: bool = False


class AIGateway:
    def __init__(self, service: AIConfigurationService, *, proxy_url: str = ""):
        self.service = service
        self.proxy_url = proxy_url
        self.circuits: dict[AIProvider, CircuitState] = {}

    def _adapter(self, provider: AIProvider) -> BaseProviderAdapter:
        configuration = self.service.provider_configuration(provider)
        api_key = self.service.credentials_for(provider).get("api_key", "")
        return create_adapter(configuration, api_key, proxy_url=self.proxy_url)

    def _ensure_circuit(self, provider: AIProvider) -> None:
        circuit = self.circuits.setdefault(provider, CircuitState())
        if circuit.opened_at:
            cooldown_active = datetime.now(UTC) - circuit.opened_at < timedelta(seconds=60)
            if cooldown_active or circuit.half_open_in_flight:
                raise AIProviderError(
                    provider,
                    AIErrorCode.CIRCUIT_OPEN,
                    "该 AI 厂商连续失败，熔断器正在冷却",
                    retryable=True,
                )
            circuit.half_open_in_flight = True
            circuit.half_open_attempts += 1

    def _record(
        self,
        provider: AIProvider,
        *,
        success: bool,
        error: AIProviderError | None = None,
    ) -> None:
        circuit = self.circuits.setdefault(provider, CircuitState())
        if error is not None and error.code == AIErrorCode.CIRCUIT_OPEN:
            return
        if success:
            circuit.failures = 0
            circuit.opened_at = None
            circuit.half_open_in_flight = False
            circuit.last_success_at = datetime.now(UTC)
            circuit.last_error_code = None
            circuit.last_error_message = ""
            return
        circuit.failures += 1
        circuit.half_open_in_flight = False
        circuit.last_failure_at = datetime.now(UTC)
        if error is not None:
            circuit.last_error_code = error.code
            circuit.last_error_message = error.public_message[:500]
        if circuit.failures >= 3:
            circuit.opened_at = datetime.now(UTC)

    def health_snapshot(self) -> dict[str, dict]:
        now = datetime.now(UTC)
        snapshot: dict[str, dict] = {}
        for provider in self.routing_order():
            circuit = self.circuits.setdefault(provider, CircuitState())
            decision_quality = self.service.operation_quality(
                provider=provider,
                operation="agent_decision",
            )
            cooldown_until = (
                circuit.opened_at + timedelta(seconds=60) if circuit.opened_at else None
            )
            snapshot[str(provider)] = {
                "status": (
                    "circuit_open"
                    if cooldown_until and cooldown_until > now
                    else "degraded"
                    if circuit.failures
                    else "ready"
                ),
                "consecutive_failures": circuit.failures,
                "opened_at": circuit.opened_at,
                "cooldown_until": cooldown_until,
                "last_failure_at": circuit.last_failure_at,
                "last_success_at": circuit.last_success_at,
                "last_error_code": (
                    str(circuit.last_error_code) if circuit.last_error_code else None
                ),
                "last_error_message": circuit.last_error_message,
                "half_open_attempts": circuit.half_open_attempts,
                "half_open_in_flight": circuit.half_open_in_flight,
                "deterministic_fallback_available": True,
                "agent_decision_quality": decision_quality,
                "automatic_agent_decisions_allowed": decision_quality[
                    "automatic_allowed"
                ],
            }
        return snapshot

    async def _record_usage(self, result: AICompletionResult) -> None:
        event = AIUsageEvent(
            provider_request_id=result.provider_request_id,
            provider=result.provider,
            model=result.model,
            operation=result.operation,
            thinking_enabled=result.thinking_enabled,
            reasoning_effort=result.reasoning_effort,
            prompt_version=result.prompt_version,
            schema_version=result.schema_version,
            succeeded=True,
            finish_reason=result.finish_reason,
            response_char_count=result.response_char_count,
            usage=result.usage,
            latency_ms=result.latency_ms,
            created_at=result.created_at,
        )
        try:
            await asyncio.to_thread(self.service.record_usage, event)
        except Exception:
            logger.exception("failed to persist AI usage event %s", event.id)

    async def _record_failure_usage(self, error: AIProviderError) -> None:
        if not error.provider_request_id and error.usage.total_tokens <= 0:
            return
        event = AIUsageEvent(
            provider_request_id=error.provider_request_id,
            provider=error.provider,
            model=error.model or self.service.provider_configuration(error.provider).model,
            operation=error.operation,
            thinking_enabled=error.thinking_enabled,
            reasoning_effort=error.reasoning_effort,
            prompt_version=error.prompt_version,
            schema_version=error.schema_version,
            succeeded=False,
            error_code=str(error.code),
            error_detail=error.diagnostic_message or error.public_message,
            finish_reason=error.finish_reason,
            response_char_count=error.response_char_count,
            usage=error.usage,
            latency_ms=error.latency_ms,
            created_at=error.created_at,
        )
        try:
            await asyncio.to_thread(self.service.record_usage, event)
        except Exception:
            logger.exception("failed to persist failed AI usage event %s", event.id)

    async def test_provider(self, provider: AIProvider) -> AIProviderTestResult:
        adapter = self._adapter(provider)
        started = perf_counter()
        try:
            models = await adapter.list_models()
            smoke = await adapter.generate_json(
                system="You are a connectivity verifier. Return only the requested object.",
                prompt='Return {"status":"ok"}.',
                schema={
                    "type": "object",
                    "properties": {"status": {"type": "string", "const": "ok"}},
                    "required": ["status"],
                    "additionalProperties": False,
                },
                schema_name="connection_test",
                max_tokens=128,
                thinking_enabled=False,
                operation="provider_connection_test",
                prompt_version="connection_test_v2",
                schema_version="connection_test_v1",
            )
            await self._record_usage(smoke)
            if smoke.data.get("status") != "ok":
                raise AIProviderError(
                    provider,
                    AIErrorCode.INVALID_RESPONSE,
                    "模型连通，但结构化输出校验失败",
                )
            selected = adapter.configuration.model
            available = selected in models if models else None
            result = AIProviderTestResult(
                ok=True,
                provider=provider,
                model=smoke.model,
                latency_ms=round((perf_counter() - started) * 1000),
                model_count=len(models),
                models=models,
                selected_model_available=available,
                message="鉴权、模型目录和结构化输出均已通过",
            )
            self._record(provider, success=True)
        except AIProviderError as exc:
            self._record(provider, success=False, error=exc)
            await self._record_failure_usage(exc)
            result = AIProviderTestResult(
                ok=False,
                provider=provider,
                model=adapter.configuration.model,
                latency_ms=round((perf_counter() - started) * 1000),
                model_count=0,
                models=[],
                selected_model_available=None,
                failure_code=exc.code,
                message=exc.public_message,
            )
        finally:
            await adapter.close()
        self.service.save_runtime(
            provider,
            AIProviderRuntimeState(
                status="ready" if result.ok else "error",
                latency_ms=result.latency_ms,
                tested_at=result.tested_at,
                model_count=result.model_count,
                models=result.models,
                selected_model_available=result.selected_model_available,
                failure_code=str(result.failure_code) if result.failure_code else None,
                message=result.message,
            ),
        )
        return result

    async def discover_models(self, provider: AIProvider) -> list[str]:
        adapter = self._adapter(provider)
        try:
            models = await adapter.list_models()
        finally:
            await adapter.close()
        previous = self.service.get().runtime[provider]
        self.service.save_runtime(
            provider,
            previous.model_copy(
                update={
                    "model_count": len(models),
                    "models": models,
                    "selected_model_available": adapter.configuration.model in models
                    if models
                    else None,
                    "message": "模型目录已刷新；连接状态以最近一次完整验收为准",
                }
            ),
        )
        return models

    def routing_order(self, requested: AIProvider | None = None) -> list[AIProvider]:
        view = self.service.get()
        enabled = [item for item in view.configuration.providers if item.enabled]
        if requested is not None:
            return [requested]
        primary = view.configuration.primary_provider
        ordered = sorted(enabled, key=lambda item: item.priority)
        enabled_ids = {item.provider for item in enabled}
        if primary not in enabled_ids:
            return [item.provider for item in ordered]
        return [primary, *(item.provider for item in ordered if item.provider != primary)]

    async def generate_json(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        schema_name: str,
        requested_provider: AIProvider | None = None,
        max_tokens: int = 1800,
        thinking_enabled: bool | None = None,
        reasoning_effort: AIReasoningEffort | None = None,
        operation: str = "structured_generation",
        prompt_version: str = "v1",
        schema_version: str = "v1",
    ) -> tuple[AICompletionResult, list[AIProvider]]:
        if max_tokens < 1 or max_tokens > MAX_STRUCTURED_OUTPUT_TOKENS:
            raise ValueError(
                f"max_tokens must be between 1 and {MAX_STRUCTURED_OUTPUT_TOKENS}"
            )
        route = self.routing_order(requested_provider)
        if not route:
            raise AIProviderError(
                AIProvider.CUSTOM,
                AIErrorCode.NOT_CONFIGURED,
                "没有已启用的 AI 厂商，请先在 AI 服务设置中启用至少一个厂商",
            )
        attempted: list[AIProvider] = []
        last_error: AIProviderError | None = None
        for provider in route:
            attempted.append(provider)
            try:
                if operation == "agent_decision":
                    quality = self.service.operation_quality(
                        provider=provider,
                        operation=operation,
                    )
                    if not quality["automatic_allowed"]:
                        last_error = AIProviderError(
                            provider,
                            AIErrorCode.QUALITY_GATED,
                            "该模型最近结构化决策成功率过低，自动调用已暂停",
                            retryable=True,
                            diagnostic_message=(
                                f"success_rate={quality['success_rate']} "
                                f"samples={quality['request_count']} "
                                f"cooldown_until={quality['cooldown_until']}"
                            ),
                        )
                        continue
                self._ensure_circuit(provider)
                adapter = self._adapter(provider)
                try:
                    result = await adapter.generate_json(
                        system=system,
                        prompt=prompt,
                        schema=schema,
                        schema_name=schema_name,
                        max_tokens=max_tokens,
                        thinking_enabled=thinking_enabled,
                        reasoning_effort=reasoning_effort,
                        operation=operation,
                        prompt_version=prompt_version,
                        schema_version=schema_version,
                    )
                finally:
                    await adapter.close()
                self._record(provider, success=True)
                await self._record_usage(result)
                return result, attempted
            except AIProviderError as exc:
                last_error = exc
                if exc.code != AIErrorCode.CIRCUIT_OPEN:
                    self._record(provider, success=False, error=exc)
                    await self._record_failure_usage(exc)
                if requested_provider is not None or not exc.retryable:
                    break
        if last_error:
            raise last_error
        raise AssertionError("AI routing terminated without a result")
