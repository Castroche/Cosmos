from cosmos_quant import secret_store as secret_store_module
from cosmos_quant.secret_store import SecretStore


def test_legacy_exchange_credentials_migrate_to_one_mode_only(tmp_path, monkeypatch):
    vault: dict[tuple[str, str], str] = {}

    monkeypatch.setattr(
        secret_store_module.keyring,
        "get_password",
        lambda service, username: vault.get((service, username)),
    )
    monkeypatch.setattr(
        secret_store_module.keyring,
        "set_password",
        lambda service, username, value: vault.__setitem__((service, username), value),
    )
    monkeypatch.setattr(
        secret_store_module.keyring,
        "delete_password",
        lambda service, username: vault.pop((service, username), None),
    )
    store = SecretStore(tmp_path / "cosmos.db")
    store.set_credentials(
        "okx",
        {"api_key": "legacy-key", "secret": "legacy-secret", "passphrase": "pass"},
    )

    store.migrate_legacy("okx", "okx:sandbox")

    assert store.get_credentials("okx:sandbox")["api_key"] == "legacy-key"
    assert store.get_credentials("okx:live")["api_key"] == ""
    assert store.get_credentials("okx")["api_key"] == ""
