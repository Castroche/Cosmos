import asyncio
import json
from datetime import UTC, datetime
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
from cosmos_quant.ai_usage import estimate_deepseek_cost_cny
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
        "choices": [{"message": {"content": '{"status":"ok"}'}}],
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
    ],
)
def test_deepseek_cost_estimator_tracks_effective_peak_pricing(
    at, expected_cost, expected_tier
):
    cost, tier = estimate_deepseek_cost_cny(
        model="deepseek-v4-flash",
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
