import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from cosmos_quant import ai_gateway as gateway_module
from cosmos_quant.ai_config_service import AIConfigurationService
from cosmos_quant.ai_configuration import (
    AIConfiguration,
    AIConfigurationUpdate,
    AIProvider,
    AIProviderConfiguration,
)
from cosmos_quant.ai_gateway import (
    AIErrorCode,
    AIGateway,
    AIProviderError,
    AnthropicMessagesAdapter,
    GeminiInteractionsAdapter,
    OpenAIChatAdapter,
    OpenAIResponsesAdapter,
)
from cosmos_quant.ai_usage import AIUsage, AIUsageEvent, estimate_deepseek_cost_cny
from cosmos_quant.storage import SqliteStore


class MemorySecrets:
    def __init__(self):
        self.values = {}

    def get_credentials(self, scope):
        return self.values.get(scope, {"api_key": "", "secret": "", "passphrase": ""})

    def set_credentials(self, scope, credentials):
        self.values[scope] = {**self.get_credentials(scope), **credentials}

    def clear_credentials(self, scope):
        self.values.pop(scope, None)


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.mark.parametrize(
    (
        "provider",
        "adapter_type",
        "expected_path",
        "response",
        "auth_header",
        "format_type",
    ),
    [
        (
            AIProvider.OPENAI,
            OpenAIResponsesAdapter,
            "/v1/responses",
            {"model": "test-model", "output_text": '{"status":"ok"}'},
            "authorization",
            None,
        ),
        (
            AIProvider.ANTHROPIC,
            AnthropicMessagesAdapter,
            "/v1/messages",
            {"model": "test-model", "content": [{"type": "text", "text": '{"status":"ok"}'}]},
            "x-api-key",
            None,
        ),
        (
            AIProvider.GOOGLE,
            GeminiInteractionsAdapter,
            "/v1beta/interactions",
            {"model": "test-model", "outputs": [{"text": '{"status":"ok"}'}]},
            "x-goog-api-key",
            None,
        ),
        (
            AIProvider.DEEPSEEK,
            OpenAIChatAdapter,
            "/chat/completions",
            {"model": "test-model", "choices": [{"message": {"content": '{"status":"ok"}'}}]},
            "authorization",
            "json_object",
        ),
        (
            AIProvider.XAI,
            OpenAIChatAdapter,
            "/v1/chat/completions",
            {"model": "test-model", "choices": [{"message": {"content": '{"status":"ok"}'}}]},
            "authorization",
            "json_schema",
        ),
        (
            AIProvider.MISTRAL,
            OpenAIChatAdapter,
            "/v1/chat/completions",
            {"model": "test-model", "choices": [{"message": {"content": '{"status":"ok"}'}}]},
            "authorization",
            "json_schema",
        ),
        (
            AIProvider.QWEN,
            OpenAIChatAdapter,
            "/compatible-mode/v1/chat/completions",
            {"model": "test-model", "choices": [{"message": {"content": '{"status":"ok"}'}}]},
            "authorization",
            "json_object",
        ),
        (
            AIProvider.CUSTOM,
            OpenAIChatAdapter,
            "/v1/chat/completions",
            {"model": "test-model", "choices": [{"message": {"content": '{"status":"ok"}'}}]},
            "authorization",
            "json_schema",
        ),
    ],
)
def test_provider_request_contracts(
    provider, adapter_type, expected_path, response, auth_header, format_type
):
    seen = {}

    def handler(request: httpx.Request):
        seen["path"] = request.url.path
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=response)

    defaults = AIConfiguration().providers
    configuration = next(item for item in defaults if item.provider == provider).model_copy(
        update={"model": "test-model"}
    )
    adapter = adapter_type(
        configuration,
        "secret-value",
        transport=httpx.MockTransport(handler),
    )
    result = run(
        adapter.generate_json(
            system="system",
            prompt="json test",
            schema={"type": "object"},
            schema_name="test",
            max_tokens=32,
        )
    )
    run(adapter.close())

    assert result.data == {"status": "ok"}
    assert seen["path"] == expected_path
    assert seen["headers"][auth_header]
    if format_type:
        assert seen["body"]["response_format"]["type"] == format_type
    if provider == AIProvider.DEEPSEEK:
        assert seen["body"]["thinking"] == {"type": "enabled"}
        assert seen["body"]["reasoning_effort"] == "high"
    assert "secret-value" not in result.model_dump_json()


def test_deepseek_usage_exposes_cache_reasoning_and_visible_output_tokens():
    response = {
        "id": "request_test",
        "model": "deepseek-v4-flash",
        "choices": [
            {"message": {"content": '{"status":"ok"}'}, "finish_reason": "stop"}
        ],
        "usage": {
            "prompt_tokens": 1_000,
            "prompt_cache_hit_tokens": 600,
            "prompt_cache_miss_tokens": 400,
            "completion_tokens": 200,
            "completion_tokens_details": {"reasoning_tokens": 150},
            "total_tokens": 1_200,
        },
    }

    def handler(_request: httpx.Request):
        return httpx.Response(200, json=response)

    configuration = next(
        item for item in AIConfiguration().providers if item.provider == AIProvider.DEEPSEEK
    )
    adapter = OpenAIChatAdapter(
        configuration,
        "secret-value",
        transport=httpx.MockTransport(handler),
    )
    result = run(
        adapter.generate_json(
            system="system",
            prompt="prompt",
            schema={"type": "object"},
            schema_name="test",
        )
    )
    run(adapter.close())

    assert result.provider_request_id == "request_test"
    assert result.finish_reason == "stop"
    assert result.response_char_count == len('{"status":"ok"}')
    assert result.usage.cache_hit_input_tokens == 600
    assert result.usage.cache_miss_input_tokens == 400
    assert result.usage.reasoning_tokens == 150
    assert result.usage.visible_output_tokens == 50
    assert result.usage.cache_hit_rate == pytest.approx(0.6)
    assert result.usage.estimated_cost_cny is not None


@pytest.mark.parametrize(
    ("at", "expected_cost", "expected_tier"),
    [
        (datetime(2026, 8, 14, 12, tzinfo=UTC), Decimal("3.02000000"), "legacy"),
        (datetime(2026, 8, 17, 2, tzinfo=UTC), Decimal("12.10000000"), "peak"),
        (datetime(2026, 8, 17, 12, tzinfo=UTC), Decimal("6.05000000"), "off_peak"),
        (datetime(2026, 9, 10, 6, tzinfo=UTC), Decimal("1.50600000"), "peak"),
        (datetime(2026, 9, 10, 12, tzinfo=UTC), Decimal("0.75300000"), "off_peak"),
    ],
)
def test_deepseek_cost_estimator_tracks_effective_peak_pricing(
    at, expected_cost, expected_tier
):
    cost, tier = estimate_deepseek_cost_cny(
        model=(
            "deepseek-flash"
            if at >= datetime(2026, 9, 10, 4, tzinfo=UTC)
            else "deepseek-v4-flash"
        ),
        at=at,
        cache_hit_input_tokens=1_000_000,
        cache_miss_input_tokens=1_000_000,
        output_tokens=1_000_000,
    )

    assert cost == expected_cost
    assert tier == expected_tier


def test_provider_errors_are_sanitized_and_never_include_response_body():
    def handler(_request: httpx.Request):
        return httpx.Response(401, json={"error": "leaked-vendor-detail"})

    configuration = next(
        item for item in AIConfiguration().providers if item.provider == AIProvider.DEEPSEEK
    )
    adapter = OpenAIChatAdapter(
        configuration,
        "top-secret",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(AIProviderError) as captured:
        run(adapter.list_models())
    run(adapter.close())

    assert captured.value.code == AIErrorCode.AUTHENTICATION
    assert "top-secret" not in str(captured.value)
    assert "leaked-vendor-detail" not in str(captured.value)


def test_fixed_provider_rejects_non_official_host():
    with pytest.raises(ValueError, match="official provider host"):
        AIProviderConfiguration(
            provider=AIProvider.OPENAI,
            model="gpt-test",
            base_url="https://attacker.example/v1",
        )


def test_gateway_failover_uses_next_enabled_provider(tmp_path, monkeypatch):
    secrets = MemorySecrets()
    service = AIConfigurationService(SqliteStore(tmp_path / "cosmos.db"), secrets)
    configuration = AIConfiguration()
    configuration.providers = [
        item.model_copy(
            update={"enabled": item.provider in {AIProvider.OPENAI, AIProvider.DEEPSEEK}}
        )
        for item in configuration.providers
    ]
    for provider in (AIProvider.OPENAI, AIProvider.DEEPSEEK):
        secrets.set_credentials(service.credential_scope(provider), {"api_key": f"key-{provider}"})
    service.save(AIConfigurationUpdate(configuration=configuration))

    class FakeAdapter:
        def __init__(self, provider):
            self.provider = provider

        async def generate_json(self, **_kwargs):
            if self.provider == AIProvider.OPENAI:
                raise AIProviderError(
                    self.provider,
                    AIErrorCode.UNAVAILABLE,
                    "temporarily unavailable",
                    retryable=True,
                )
            return gateway_module.AICompletionResult(
                provider=self.provider,
                model="fallback-model",
                data={"status": "ok"},
                latency_ms=5,
            )

        async def close(self):
            return None

    monkeypatch.setattr(
        gateway_module,
        "create_adapter",
        lambda configuration, *_args, **_kwargs: FakeAdapter(configuration.provider),
    )
    gateway = AIGateway(service)
    result, attempted = run(
        gateway.generate_json(
            system="system",
            prompt="prompt",
            schema={"type": "object"},
            schema_name="test",
        )
    )

    assert result.provider == AIProvider.DEEPSEEK
    assert attempted == [AIProvider.OPENAI, AIProvider.DEEPSEEK]


def test_agent_decision_quality_gate_skips_bad_provider_for_healthy_fallback(
    tmp_path, monkeypatch
):
    secrets = MemorySecrets()
    service = AIConfigurationService(SqliteStore(tmp_path / "cosmos.db"), secrets)
    configuration = AIConfiguration()
    configuration.primary_provider = AIProvider.DEEPSEEK
    configuration.providers = [
        item.model_copy(
            update={"enabled": item.provider in {AIProvider.OPENAI, AIProvider.DEEPSEEK}}
        )
        for item in configuration.providers
    ]
    for provider in (AIProvider.OPENAI, AIProvider.DEEPSEEK):
        secrets.set_credentials(service.credential_scope(provider), {"api_key": "test-key"})
    service.save(AIConfigurationUpdate(configuration=configuration))
    baseline = datetime.now(UTC) - timedelta(minutes=2)
    for index in range(5):
        service.record_usage(
            AIUsageEvent(
                provider=AIProvider.DEEPSEEK,
                model="deepseek-v4-flash",
                operation="agent_decision",
                succeeded=index == 0,
                error_code=None if index == 0 else "invalid_response",
                usage=AIUsage(),
                latency_ms=100,
                created_at=baseline + timedelta(seconds=index),
            )
        )

    called = []

    class FakeAdapter:
        def __init__(self, provider):
            self.provider = provider

        async def generate_json(self, **_kwargs):
            called.append(self.provider)
            return gateway_module.AICompletionResult(
                provider=self.provider,
                model="healthy-model",
                data={"status": "ok"},
                latency_ms=5,
                operation="agent_decision",
            )

        async def close(self):
            return None

    monkeypatch.setattr(
        gateway_module,
        "create_adapter",
        lambda configuration, *_args, **_kwargs: FakeAdapter(configuration.provider),
    )
    gateway = AIGateway(service)
    result, attempted = run(
        gateway.generate_json(
            system="system",
            prompt="prompt",
            schema={"type": "object"},
            schema_name="test",
            operation="agent_decision",
        )
    )

    assert result.provider == AIProvider.OPENAI
    assert attempted == [AIProvider.DEEPSEEK, AIProvider.OPENAI]
    assert called == [AIProvider.OPENAI]
    deepseek = gateway.health_snapshot()[str(AIProvider.DEEPSEEK)]
    assert deepseek["automatic_agent_decisions_allowed"] is False
    assert deepseek["agent_decision_quality"]["status"] == "quality_blocked"


def test_gateway_circuit_exposes_diagnostics_and_recovers_half_open(tmp_path, monkeypatch):
    secrets = MemorySecrets()
    service = AIConfigurationService(SqliteStore(tmp_path / "cosmos.db"), secrets)
    configuration = AIConfiguration()
    configuration.providers = [
        item.model_copy(update={"enabled": item.provider == AIProvider.DEEPSEEK})
        for item in configuration.providers
    ]
    secrets.set_credentials(
        service.credential_scope(AIProvider.DEEPSEEK),
        {"api_key": "key-deepseek"},
    )
    service.save(AIConfigurationUpdate(configuration=configuration))
    available = False

    class FakeAdapter:
        async def generate_json(self, **_kwargs):
            if not available:
                raise AIProviderError(
                    AIProvider.DEEPSEEK,
                    AIErrorCode.UNAVAILABLE,
                    "temporary provider outage",
                    retryable=True,
                )
            return gateway_module.AICompletionResult(
                provider=AIProvider.DEEPSEEK,
                model="deepseek-v4-flash",
                data={"status": "ok"},
                latency_ms=3,
            )

        async def close(self):
            return None

    monkeypatch.setattr(gateway_module, "create_adapter", lambda *_args, **_kwargs: FakeAdapter())
    gateway = AIGateway(service)
    for _ in range(3):
        with pytest.raises(AIProviderError):
            run(
                gateway.generate_json(
                    system="system",
                    prompt="prompt",
                    schema={"type": "object"},
                    schema_name="test",
                )
            )
    health = gateway.health_snapshot()[str(AIProvider.DEEPSEEK)]
    assert health["status"] == "circuit_open"
    assert health["consecutive_failures"] == 3
    assert health["last_error_code"] == str(AIErrorCode.UNAVAILABLE)
    assert health["deterministic_fallback_available"] is True

    opened_at = gateway.circuits[AIProvider.DEEPSEEK].opened_at
    for _ in range(5):
        with pytest.raises(AIProviderError) as captured:
            run(
                gateway.generate_json(
                    system="system",
                    prompt="prompt",
                    schema={"type": "object"},
                    schema_name="test",
                )
            )
        assert captured.value.code == AIErrorCode.CIRCUIT_OPEN
    cooling = gateway.health_snapshot()[str(AIProvider.DEEPSEEK)]
    assert cooling["consecutive_failures"] == 3
    assert gateway.circuits[AIProvider.DEEPSEEK].opened_at == opened_at

    gateway.circuits[AIProvider.DEEPSEEK].opened_at = datetime.now(UTC) - timedelta(seconds=61)
    available = True
    result, _ = run(
        gateway.generate_json(
            system="system",
            prompt="prompt",
            schema={"type": "object"},
            schema_name="test",
        )
    )
    assert result.data == {"status": "ok"}
    recovered = gateway.health_snapshot()[str(AIProvider.DEEPSEEK)]
    assert recovered["status"] == "ready"
    assert recovered["consecutive_failures"] == 0
    assert recovered["half_open_attempts"] == 1


def test_invalid_json_persists_failure_usage_and_finish_reason(tmp_path, monkeypatch):
    secrets = MemorySecrets()
    store = SqliteStore(tmp_path / "cosmos.db")
    service = AIConfigurationService(store, secrets)
    configuration = AIConfiguration()
    configuration.providers = [
        item.model_copy(update={"enabled": item.provider == AIProvider.DEEPSEEK})
        for item in configuration.providers
    ]
    secrets.set_credentials(
        service.credential_scope(AIProvider.DEEPSEEK),
        {"api_key": "key-deepseek"},
    )
    service.save(AIConfigurationUpdate(configuration=configuration))

    response = {
        "id": "request_invalid",
        "model": "deepseek-v4-flash",
        "choices": [
            {"message": {"content": '{"status":'}, "finish_reason": "length"}
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 1200,
            "total_tokens": 1300,
        },
    }

    class InvalidAdapter:
        async def generate_json(self, **kwargs):
            configuration = service.provider_configuration(AIProvider.DEEPSEEK)
            adapter = OpenAIChatAdapter(
                configuration,
                "key-deepseek",
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(200, json=response)
                ),
            )
            try:
                return await adapter.generate_json(**kwargs)
            finally:
                await adapter.close()

        async def close(self):
            return None

    monkeypatch.setattr(
        gateway_module,
        "create_adapter",
        lambda *_args, **_kwargs: InvalidAdapter(),
    )
    gateway = AIGateway(service)
    with pytest.raises(AIProviderError) as captured:
        run(
            gateway.generate_json(
                system="system",
                prompt="prompt",
                schema={"type": "object"},
                schema_name="test",
            )
        )

    assert captured.value.code == AIErrorCode.INVALID_RESPONSE
    report = service.usage_report(days=1)
    assert report.summary.failed_request_count == 1
    assert report.recent[0].succeeded is False
    assert report.recent[0].finish_reason == "length"
    assert report.recent[0].usage.output_tokens == 1200
    assert "JSONDecodeError" in (report.recent[0].error_detail or "")


def test_gateway_rejects_output_budget_above_hard_limit(tmp_path):
    service = AIConfigurationService(SqliteStore(tmp_path / "cosmos.db"), MemorySecrets())
    gateway = AIGateway(service)
    with pytest.raises(ValueError, match="10000"):
        run(
            gateway.generate_json(
                system="system",
                prompt="prompt",
                schema={"type": "object"},
                schema_name="test",
                max_tokens=10_001,
            )
        )
