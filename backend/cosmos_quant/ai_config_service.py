from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from .ai_configuration import (
    AIConfiguration,
    AIConfigurationUpdate,
    AIConfigurationView,
    AIProvider,
    AIProviderCredentialState,
    AIProviderRuntimeState,
)
from .ai_usage import AIUsageEvent, AIUsageReport, AIUsageSummary
from .secret_store import SecretStore
from .storage import SqliteStore


class AIConfigurationService:
    AGENT_DECISION_QUALITY_SAMPLE_SIZE = 20
    AGENT_DECISION_QUALITY_MINIMUM_SAMPLES = 5
    AGENT_DECISION_MINIMUM_SUCCESS_RATE = 0.80
    AGENT_DECISION_GATE_COOLDOWN = timedelta(minutes=15)

    def __init__(self, store: SqliteStore, secrets: SecretStore):
        self.store = store
        self.secrets = secrets

    @staticmethod
    def credential_scope(provider: AIProvider | str) -> str:
        return f"ai:{provider}"

    def get(self) -> AIConfigurationView:
        payload, updated_at = self.store.load_ai_configuration()
        configuration = AIConfiguration.model_validate(payload) if payload else AIConfiguration()
        return self._view(configuration, updated_at)

    def save(self, update: AIConfigurationUpdate) -> AIConfigurationView:
        changed_scope: str | None = None
        previous_credentials: dict[str, str] | None = None
        try:
            if update.credential:
                changed_scope = self.credential_scope(update.credential.provider)
                previous_credentials = self.secrets.get_credentials(changed_scope).copy()
                if update.credential.clear_existing:
                    self.secrets.clear_credentials(changed_scope)
                if update.credential.api_key:
                    self.secrets.set_credentials(
                        changed_scope, {"api_key": update.credential.api_key}
                    )

            for provider in update.configuration.providers:
                if provider.enabled and not self._has_key(provider.provider):
                    raise ValueError(f"启用 {provider.provider} 前必须先配置该厂商的 API Key")

            updated_at = self.store.save_ai_configuration(update.configuration)
        except Exception:
            if changed_scope is not None and previous_credentials is not None:
                self.secrets.clear_credentials(changed_scope)
                self.secrets.set_credentials(changed_scope, previous_credentials)
            raise
        return self._view(update.configuration, updated_at)

    def _has_key(self, provider: AIProvider) -> bool:
        return bool(self.secrets.get_credentials(self.credential_scope(provider)).get("api_key"))

    def credentials_for(self, provider: AIProvider) -> dict[str, str]:
        """Internal-only credential access; API response models never expose this value."""
        return self.secrets.get_credentials(self.credential_scope(provider)).copy()

    def provider_configuration(self, provider: AIProvider):
        configuration = self.get().configuration
        return next(item for item in configuration.providers if item.provider == provider)

    def save_runtime(self, provider: AIProvider, state: AIProviderRuntimeState) -> None:
        self.store.save_ai_provider_runtime(provider, state)

    def record_usage(self, event: AIUsageEvent) -> None:
        self.store.save_ai_usage_event(event)

    def usage_report(
        self,
        *,
        days: int = 30,
        provider: AIProvider | None = None,
        recent_limit: int = 100,
    ) -> AIUsageReport:
        until = datetime.now(UTC)
        since = until - timedelta(days=days)
        events = [
            AIUsageEvent.model_validate(item)
            for item in self.store.load_ai_usage_events(
                since=since,
                until=until,
                provider=provider,
            )
        ]
        summary = AIUsageSummary(
            since=since,
            until=until,
            request_count=len(events),
            failed_request_count=sum(not item.succeeded for item in events),
            input_tokens=sum(item.usage.input_tokens for item in events),
            output_tokens=sum(item.usage.output_tokens for item in events),
            total_tokens=sum(item.usage.total_tokens for item in events),
            cache_hit_input_tokens=sum(
                item.usage.cache_hit_input_tokens for item in events
            ),
            cache_miss_input_tokens=sum(
                item.usage.cache_miss_input_tokens for item in events
            ),
            reasoning_tokens=sum(item.usage.reasoning_tokens for item in events),
            visible_output_tokens=sum(
                item.usage.visible_output_tokens for item in events
            ),
            estimated_cost_cny=sum(
                (
                    item.usage.estimated_cost_cny
                    for item in events
                    if item.usage.estimated_cost_cny is not None
                ),
                Decimal("0"),
            ),
            unpriced_request_count=sum(
                item.usage.estimated_cost_cny is None for item in events
            ),
        )
        return AIUsageReport(summary=summary, recent=events[:recent_limit])

    def operation_quality(
        self,
        *,
        provider: AIProvider,
        operation: str,
        days: int = 7,
        sample_size: int = AGENT_DECISION_QUALITY_SAMPLE_SIZE,
    ) -> dict:
        """Summarize recent structured-output reliability for automatic routing.

        A low rolling success rate is diagnostic by itself.  It becomes a routing
        block only when the newest request also failed and is still inside a short
        cooldown.  That permits an automatic half-open probe after the cooldown and
        lets one successful probe restore routing without deleting audit history.
        """

        until = datetime.now(UTC)
        since = until - timedelta(days=days)
        events = [
            AIUsageEvent.model_validate(item)
            for item in self.store.load_ai_usage_events(
                since=since,
                until=until,
                provider=provider,
            )
            if item.get("operation") == operation
        ][: max(sample_size, 1)]
        request_count = len(events)
        succeeded = sum(item.succeeded for item in events)
        failed = request_count - succeeded
        success_rate = succeeded / request_count if request_count else None
        invalid_responses = sum(
            not item.succeeded and item.error_code == "invalid_response"
            for item in events
        )
        latencies = sorted(item.latency_ms for item in events)
        median_latency_ms = (
            latencies[len(latencies) // 2] if latencies else None
        )
        p95_latency_ms = (
            latencies[max(int(len(latencies) * 0.95) - 1, 0)]
            if latencies
            else None
        )
        latest = events[0] if events else None
        cooldown_until = (
            latest.created_at + self.AGENT_DECISION_GATE_COOLDOWN
            if latest and not latest.succeeded
            else None
        )
        statistically_degraded = bool(
            request_count >= self.AGENT_DECISION_QUALITY_MINIMUM_SAMPLES
            and success_rate is not None
            and success_rate < self.AGENT_DECISION_MINIMUM_SUCCESS_RATE
        )
        automatic_allowed = not (
            statistically_degraded
            and latest is not None
            and not latest.succeeded
            and cooldown_until is not None
            and cooldown_until > until
        )
        if not request_count:
            status = "warming_up"
            reason = "尚无自动结构化决策样本"
        elif automatic_allowed and not statistically_degraded:
            status = "healthy"
            reason = "最近结构化输出成功率达到自动路由门槛"
        elif automatic_allowed:
            status = "probe_allowed"
            reason = "历史成功率偏低，但允许一次自动恢复探测"
        else:
            status = "quality_blocked"
            reason = "最近结构化输出成功率过低，自动调用已暂停并使用确定性降级"
        return {
            "operation": operation,
            "window_days": days,
            "sample_size": sample_size,
            "request_count": request_count,
            "succeeded_request_count": succeeded,
            "failed_request_count": failed,
            "invalid_response_count": invalid_responses,
            "success_rate": success_rate,
            "minimum_success_rate": self.AGENT_DECISION_MINIMUM_SUCCESS_RATE,
            "median_latency_ms": median_latency_ms,
            "p95_latency_ms": p95_latency_ms,
            "latest_succeeded": latest.succeeded if latest else None,
            "latest_at": latest.created_at if latest else None,
            "cooldown_until": cooldown_until if not automatic_allowed else None,
            "automatic_allowed": automatic_allowed,
            "status": status,
            "reason": reason,
        }

    def _view(self, configuration: AIConfiguration, updated_at):
        return AIConfigurationView(
            configuration=configuration,
            credentials={
                provider: AIProviderCredentialState(api_key_configured=self._has_key(provider))
                for provider in AIProvider
            },
            runtime={
                provider: AIProviderRuntimeState.model_validate(
                    self.store.load_ai_provider_runtime(provider) or {}
                )
                for provider in AIProvider
            },
            updated_at=updated_at,
        )
