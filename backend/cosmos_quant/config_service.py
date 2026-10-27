from __future__ import annotations

from hashlib import sha256

from .configuration import (
    CommissioningResult,
    ConfigurationUpdate,
    ConfigurationView,
    CredentialState,
    UserConfiguration,
)
from .domain import TradingMode
from .secret_store import SecretStore
from .storage import SqliteStore


class ConfigurationService:
    def __init__(self, store: SqliteStore, secrets: SecretStore):
        self.store = store
        self.secrets = secrets

    def get(self) -> ConfigurationView:
        configuration, updated_at = self.store.load_configuration()
        self._migrate_legacy_credentials(configuration)
        commissioning = self.commissioning_for(configuration)
        scope = self.credential_scope(configuration)
        return ConfigurationView(
            configuration=configuration,
            credentials=CredentialState(**self.secrets.state(scope)),
            credential_scopes=self._credential_scopes(),
            commissioning=commissioning,
            execution_ready=self.is_execution_ready(configuration, commissioning),
            updated_at=updated_at,
        )

    def current_configuration(self) -> UserConfiguration:
        configuration, _ = self.store.load_configuration()
        return configuration

    def save(self, update: ConfigurationUpdate) -> ConfigurationView:
        configuration = update.configuration.model_copy(
            update={
                "live_trading_enabled": False,
                "automatic_execution_enabled": False,
            }
        )
        self._migrate_legacy_credentials(configuration)
        if (
            configuration.trading_mode == TradingMode.LIVE
            and update.credentials
            and any(
                (
                    update.credentials.api_key,
                    update.credentials.secret,
                    update.credentials.passphrase,
                )
            )
            and update.live_unlock_phrase != "I ACCEPT LIVE TRADING RISK"
        ):
            raise ValueError("保存实盘凭证的确认短语不正确")
        if update.credentials:
            credential_change = update.credentials.clear_existing or any(
                (
                    update.credentials.api_key,
                    update.credentials.secret,
                    update.credentials.passphrase,
                )
            )
            if (
                credential_change
                and configuration.trading_mode != TradingMode.PAPER
                and configuration.automatic_execution_enabled
            ):
                raise ValueError("更换交易凭证前必须先关闭自动执行")
            if update.credentials.clear_existing:
                self.secrets.clear_credentials(self.credential_scope(configuration))
            self.secrets.set_credentials(
                self.credential_scope(configuration),
                update.credentials.model_dump(exclude={"clear_existing"}),
            )
        credential_state = self.secrets.state(self.credential_scope(configuration))
        if configuration.trading_mode != TradingMode.PAPER:
            if (
                not credential_state["api_key_configured"]
                or not credential_state["secret_configured"]
            ):
                raise ValueError("测试网或实盘模式必须配置 API Key 和 Secret")
            if (
                configuration.exchange_id == "okx"
                and not credential_state["passphrase_configured"]
            ):
                raise ValueError("OKX 测试网或实盘模式必须配置 Passphrase")
        commissioning = self.commissioning_for(configuration)
        if (
            configuration.trading_mode != TradingMode.PAPER
            and configuration.automatic_execution_enabled
            and not self.is_execution_ready(configuration, commissioning)
        ):
            raise ValueError("启用自动执行前必须先通过测试网原子保护单验收")
        updated_at = self.store.save_configuration(configuration)
        return ConfigurationView(
            configuration=configuration,
            credentials=CredentialState(**credential_state),
            credential_scopes=self._credential_scopes(),
            commissioning=commissioning,
            execution_ready=self.is_execution_ready(configuration, commissioning),
            updated_at=updated_at,
        )

    def credentials_for(self, configuration: UserConfiguration) -> dict[str, str]:
        self._migrate_legacy_credentials(configuration)
        return self.secrets.get_credentials(self.credential_scope(configuration))

    def restore_credentials(
        self, configuration: UserConfiguration, credentials: dict[str, str]
    ) -> None:
        """Replace one credential scope, used to roll back failed runtime changes."""
        scope = self.credential_scope(configuration)
        self.secrets.clear_credentials(scope)
        self.secrets.set_credentials(scope, credentials)

    @staticmethod
    def credential_scope(configuration: UserConfiguration) -> str:
        return f"{configuration.exchange_id}:{configuration.trading_mode}"

    def _migrate_legacy_credentials(self, configuration: UserConfiguration) -> None:
        if configuration.trading_mode == TradingMode.PAPER:
            return
        migrate = getattr(self.secrets, "migrate_legacy", None)
        if migrate:
            migrate(
                configuration.exchange_id,
                self.credential_scope(configuration),
            )

    def _credential_scopes(self) -> dict[str, CredentialState]:
        return {
            f"{exchange}:{mode}": CredentialState(**self.secrets.state(f"{exchange}:{mode}"))
            for exchange in ("okx", "binance")
            for mode in ("paper", "sandbox", "live")
        }

    def credential_fingerprint(self, configuration: UserConfiguration) -> str:
        api_key = self.credentials_for(configuration).get("api_key", "")
        return sha256(api_key.encode()).hexdigest()[:16] if api_key else ""

    def commissioning_for(self, configuration: UserConfiguration) -> CommissioningResult | None:
        payload = self.store.load_commissioning(
            configuration.exchange_id, str(configuration.market_type)
        )
        return CommissioningResult.model_validate(payload) if payload else None

    def is_execution_ready(
        self,
        configuration: UserConfiguration,
        commissioning: CommissioningResult | None = None,
    ) -> bool:
        if configuration.trading_mode == TradingMode.PAPER:
            return True
        commissioning = commissioning or self.commissioning_for(configuration)
        if not commissioning or not commissioning.passed:
            return False
        if configuration.trading_mode == TradingMode.SANDBOX:
            return commissioning.credential_fingerprint == self.credential_fingerprint(
                configuration
            )
        return True
