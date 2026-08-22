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
