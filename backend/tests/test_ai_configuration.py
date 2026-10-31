import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app_fixture import services as api
from cosmos_quant.ai_config_service import AIConfigurationService
from cosmos_quant.ai_configuration import (
    AIConfiguration,
    AIConfigurationUpdate,
    AICredentialInput,
    AIProvider,
    AIProviderConfiguration,
)
from cosmos_quant.ai_usage import AIUsage, AIUsageEvent
from cosmos_quant.storage import SqliteStore
from httpx import ASGITransport, AsyncClient


class MemorySecrets:
    def __init__(self):
        self.values: dict[str, dict[str, str]] = {}

    def get_credentials(self, scope):
        return self.values.get(scope, {"api_key": "", "secret": "", "passphrase": ""})

    def set_credentials(self, scope, credentials):
        current = self.get_credentials(scope).copy()
        current.update({key: value for key, value in credentials.items() if value})
        self.values[scope] = current

    def clear_credentials(self, scope):
        self.values.pop(scope, None)


def enable(configuration: AIConfiguration, provider: AIProvider) -> AIConfiguration:
    return configuration.model_copy(
        deep=True,
        update={
            "providers": [
                item.model_copy(update={"enabled": item.provider == provider})
                for item in configuration.providers
            ]
        },
    )


def request(method: str, path: str, json=None):
    async def run():
        transport = ASGITransport(app=api.app)
        headers = {}
        async with AsyncClient(
            transport=transport, base_url="http://test", headers=headers
        ) as client:
            return await client.request(method, path, json=json)

    return asyncio.run(run())


def test_ai_api_key_is_vault_only_and_never_returned(tmp_path):
    database = tmp_path / "cosmos.db"
    secrets = MemorySecrets()
    service = AIConfigurationService(SqliteStore(database), secrets)
    configuration = enable(AIConfiguration(), AIProvider.OPENAI)

    view = service.save(
        AIConfigurationUpdate(
            configuration=configuration,
            credential=AICredentialInput(
                provider=AIProvider.OPENAI, api_key="sk-private-test-value"
            ),
        )
    )

    assert view.credentials[AIProvider.OPENAI].api_key_configured is True
    assert "sk-private-test-value" not in view.model_dump_json()
    assert b"sk-private-test-value" not in database.read_bytes()
    assert service.get().configuration.providers[0].enabled is True


def test_enabled_provider_requires_a_key(tmp_path):
    service = AIConfigurationService(SqliteStore(tmp_path / "cosmos.db"), MemorySecrets())

    with pytest.raises(ValueError, match="API Key"):
        service.save(
            AIConfigurationUpdate(configuration=enable(AIConfiguration(), AIProvider.ANTHROPIC))
        )


def test_ai_endpoint_rejects_insecure_external_base_url():
    with pytest.raises(ValueError, match="HTTPS"):
        AIProviderConfiguration(
            provider=AIProvider.CUSTOM,
            model="local-model",
            base_url="http://example.com/v1",
        )

    local = AIProviderConfiguration(
        provider=AIProvider.CUSTOM,
        model="local-model",
        base_url="http://127.0.0.1:11434/v1",
    )
    assert local.base_url == "http://127.0.0.1:11434/v1"


def test_ai_settings_api_exposes_state_but_not_secret(tmp_path, monkeypatch):
    service = AIConfigurationService(SqliteStore(tmp_path / "cosmos.db"), MemorySecrets())
    monkeypatch.setattr(api, "ai_configuration_service", service)
    configuration = enable(AIConfiguration(), AIProvider.GOOGLE)
    payload = {
        "configuration": configuration.model_dump(mode="json"),
        "credential": {"provider": "google", "api_key": "google-test-secret"},
    }

    saved = request("PUT", "/api/ai/settings", json=payload)
    fetched = request("GET", "/api/ai/settings")

    assert saved.status_code == 200
    assert fetched.status_code == 200
    assert fetched.json()["credentials"]["google"]["api_key_configured"] is True
    assert "google-test-secret" not in fetched.text


def test_ai_usage_report_aggregates_cache_reasoning_and_cost(tmp_path, monkeypatch):
    service = AIConfigurationService(SqliteStore(tmp_path / "cosmos.db"), MemorySecrets())
    event = AIUsageEvent(
        provider=AIProvider.DEEPSEEK,
        model="deepseek-v4-flash",
        operation="agent_decision",
        thinking_enabled=True,
        reasoning_effort="high",
        prompt_version="agent_reasoner_v2",
        schema_version="agent_reasoning_output_v2",
        usage=AIUsage(
            input_tokens=1_000,
            output_tokens=200,
            total_tokens=1_200,
            cache_hit_input_tokens=600,
            cache_miss_input_tokens=400,
            reasoning_tokens=150,
            visible_output_tokens=50,
            estimated_cost_cny=Decimal("0.001"),
            pricing_tier="legacy",
        ),
        latency_ms=100,
        created_at=datetime.now(UTC),
    )
    service.record_usage(event)

    report = service.usage_report(days=1, provider=AIProvider.DEEPSEEK)

    assert report.summary.request_count == 1
    assert report.summary.cache_hit_rate == pytest.approx(0.6)
    assert report.summary.reasoning_tokens == 150
    assert report.summary.visible_output_tokens == 50
    assert report.summary.estimated_cost_cny == Decimal("0.001")
    assert report.recent[0].operation == "agent_decision"

    monkeypatch.setattr(api, "ai_configuration_service", service)
    response = request("GET", "/api/ai/usage?days=1&provider=deepseek")
    assert response.status_code == 200
    assert response.json()["summary"]["request_count"] == 1
    assert response.json()["recent"][0]["reasoning_effort"] == "high"


def test_operation_quality_blocks_bad_latest_sample_and_recovers_on_success(tmp_path):
    service = AIConfigurationService(SqliteStore(tmp_path / "cosmos.db"), MemorySecrets())
    baseline = datetime.now(UTC) - timedelta(minutes=5)
    for index in range(5):
        succeeded = index == 0
        service.record_usage(
            AIUsageEvent(
                provider=AIProvider.DEEPSEEK,
                model="deepseek-v4-flash",
                operation="agent_decision",
                succeeded=succeeded,
                error_code=None if succeeded else "invalid_response",
                usage=AIUsage(),
                latency_ms=100 + index,
                created_at=baseline + timedelta(seconds=index),
            )
        )

    blocked = service.operation_quality(
        provider=AIProvider.DEEPSEEK,
        operation="agent_decision",
    )

    assert blocked["request_count"] == 5
    assert blocked["success_rate"] == pytest.approx(0.2)
    assert blocked["invalid_response_count"] == 4
    assert blocked["automatic_allowed"] is False
    assert blocked["status"] == "quality_blocked"

    service.record_usage(
        AIUsageEvent(
            provider=AIProvider.DEEPSEEK,
            model="deepseek-v4-flash",
            operation="agent_decision",
            succeeded=True,
            usage=AIUsage(),
            latency_ms=90,
            created_at=datetime.now(UTC),
        )
    )
    recovered = service.operation_quality(
        provider=AIProvider.DEEPSEEK,
        operation="agent_decision",
    )

    assert recovered["automatic_allowed"] is True
    assert recovered["status"] == "probe_allowed"
    assert recovered["latest_succeeded"] is True
