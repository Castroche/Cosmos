from pathlib import Path

import pytest
from cosmos_quant.config_service import ConfigurationService
from cosmos_quant.configuration import (
    CommissioningResult,
    ConfigurationUpdate,
    CredentialInput,
    UserConfiguration,
)
from cosmos_quant.storage import SqliteStore


class MemorySecrets:
    def __init__(self):
        self.values = {}

    def get_credentials(self, exchange_id):
        return self.values.get(exchange_id, {"api_key": "", "secret": "", "passphrase": ""})

    def set_credentials(self, exchange_id, credentials):
        current = self.get_credentials(exchange_id).copy()
        current.update({key: value for key, value in credentials.items() if value})
        self.values[exchange_id] = current

    def clear_credentials(self, exchange_id):
        self.values.pop(exchange_id, None)

    def state(self, exchange_id):
        values = self.get_credentials(exchange_id)
        return {f"{key}_configured": bool(value) for key, value in values.items()}


def service(tmp_path: Path):
    return ConfigurationService(SqliteStore(tmp_path / "cosmos.db"), MemorySecrets())


def test_live_mode_credentials_do_not_enable_legacy_live_switch(tmp_path):
    view = service(tmp_path).save(
        ConfigurationUpdate(
            configuration=UserConfiguration(trading_mode="live", live_trading_enabled=True),
            credentials=CredentialInput(
                api_key="live-key",
                secret="live-secret",
                passphrase="live-passphrase",
            ),
            live_unlock_phrase="I ACCEPT LIVE TRADING RISK",
        )
    )
    assert view.configuration.live_trading_enabled is False
    assert view.configuration.automatic_execution_enabled is False


def test_live_mode_requires_exact_unlock_phrase(tmp_path):
    config = UserConfiguration(trading_mode="live", live_trading_enabled=True)
    with pytest.raises(ValueError, match="确认短语"):
        service(tmp_path).save(
            ConfigurationUpdate(
                configuration=config,
                credentials=CredentialInput(
                    api_key="live-key",
                    secret="live-secret",
                    passphrase="live-passphrase",
                ),
                live_unlock_phrase="close enough",
            )
        )


def test_private_mode_requires_credentials(tmp_path):
    config = UserConfiguration(trading_mode="sandbox")
    with pytest.raises(ValueError, match="API Key"):
        service(tmp_path).save(ConfigurationUpdate(configuration=config))


def test_credentials_are_not_written_to_sqlite(tmp_path):
    database = tmp_path / "cosmos.db"
    configuration_service = service(tmp_path)
    configuration_service.save(
        ConfigurationUpdate(
            configuration=UserConfiguration(),
            credentials=CredentialInput(
                api_key="secret-api-key", secret="secret-value", passphrase="passphrase"
            ),
        )
    )
    assert b"secret-api-key" not in database.read_bytes()
    assert b"secret-value" not in database.read_bytes()


def test_proxy_url_accepts_local_http_proxy():
    configuration = UserConfiguration(network_proxy_url=" http://127.0.0.1:7890/ ")
    assert configuration.network_proxy_url == "http://127.0.0.1:7890"


@pytest.mark.parametrize(
    "proxy_url",
    ["127.0.0.1:7890", "socks5://127.0.0.1:7890", "http://user:pass@proxy:8080"],
)
def test_proxy_url_rejects_unsafe_or_unsupported_values(proxy_url):
    with pytest.raises(ValueError, match="proxy"):
        UserConfiguration(network_proxy_url=proxy_url)


def test_legacy_automatic_exchange_execution_is_forced_off(tmp_path):
    configuration_service = service(tmp_path)
    configuration = UserConfiguration(trading_mode="sandbox", automatic_execution_enabled=True)
    view = configuration_service.save(
        ConfigurationUpdate(
            configuration=configuration,
            credentials=CredentialInput(
                api_key="sandbox-key",
                secret="sandbox-secret",
                passphrase="sandbox-passphrase",
            ),
        )
    )
    assert view.configuration.automatic_execution_enabled is False


def test_matching_sandbox_commissioning_unlocks_execution(tmp_path):
    configuration_service = service(tmp_path)
    configuration = UserConfiguration(trading_mode="sandbox", market_type="spot")
    configuration_service.save(
        ConfigurationUpdate(
            configuration=configuration,
            credentials=CredentialInput(
                api_key="sandbox-key",
                secret="sandbox-secret",
                passphrase="sandbox-passphrase",
            ),
        )
    )
    result = CommissioningResult(
        exchange="okx",
        market_type="spot",
        symbol="BTC/USDT",
        credential_fingerprint=configuration_service.credential_fingerprint(configuration),
        protection_verified=True,
        cancellation_confirmed=True,
        order_ids=["acceptance-order"],
        message="passed",
    )
    configuration_service.store.save_commissioning("okx", "spot", result)

    unlocked = configuration_service.save(
        ConfigurationUpdate(
            configuration=configuration.model_copy(update={"automatic_execution_enabled": True})
        )
    )

    assert unlocked.execution_ready
    assert unlocked.commissioning is not None
    assert unlocked.commissioning.order_ids == ["acceptance-order"]


def test_rotated_sandbox_api_key_invalidates_commissioning(tmp_path):
    configuration_service = service(tmp_path)
    configuration = UserConfiguration(trading_mode="sandbox")
    configuration_service.save(
        ConfigurationUpdate(
            configuration=configuration,
            credentials=CredentialInput(
                api_key="old-key",
                secret="sandbox-secret",
                passphrase="sandbox-passphrase",
            ),
        )
    )
    result = CommissioningResult(
        exchange="okx",
        market_type="spot",
        symbol="BTC/USDT",
        credential_fingerprint=configuration_service.credential_fingerprint(configuration),
        protection_verified=True,
        cancellation_confirmed=True,
        order_ids=["old-order"],
        message="passed",
    )
    configuration_service.store.save_commissioning("okx", "spot", result)
    configuration_service.save(
        ConfigurationUpdate(
            configuration=configuration,
            credentials=CredentialInput(api_key="new-key"),
        )
    )

    assert not configuration_service.get().execution_ready


def test_sandbox_and_live_credentials_are_isolated(tmp_path):
    configuration_service = service(tmp_path)
    sandbox = UserConfiguration(trading_mode="sandbox")
    configuration_service.save(
        ConfigurationUpdate(
            configuration=sandbox,
            credentials=CredentialInput(
                api_key="sandbox-key",
                secret="sandbox-secret",
                passphrase="sandbox-passphrase",
            ),
        )
    )
    live = UserConfiguration(trading_mode="live", live_trading_enabled=True)
    configuration_service.save(
        ConfigurationUpdate(
            configuration=live,
            credentials=CredentialInput(
                api_key="live-key",
                secret="live-secret",
                passphrase="live-passphrase",
            ),
            live_unlock_phrase="I ACCEPT LIVE TRADING RISK",
        )
    )

    assert configuration_service.credentials_for(sandbox)["api_key"] == "sandbox-key"
    assert configuration_service.credentials_for(live)["api_key"] == "live-key"
    view = configuration_service.get()
    assert view.credential_scopes["okx:sandbox"].api_key_configured
    assert view.credential_scopes["okx:live"].api_key_configured
