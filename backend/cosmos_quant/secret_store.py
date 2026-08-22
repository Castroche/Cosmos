from __future__ import annotations

from pathlib import Path

import keyring
from keyring.errors import KeyringError


class SecretStore:
    """Store exchange credentials in the operating-system credential vault."""

    FIELDS = ("api_key", "secret", "passphrase")

    def __init__(self, identity: Path):
        self.service = f"cosmos-quant:{identity.resolve()}"

    def _read(self, scope: str) -> dict[str, str]:
        return {
            field: keyring.get_password(self.service, f"{scope}:{field}") or ""
            for field in self.FIELDS
        }

    def get_credentials(self, scope: str) -> dict[str, str]:
        return self._read(scope)

    def set_credentials(self, scope: str, credentials: dict[str, str | None]) -> None:
        try:
            for field in self.FIELDS:
                value = credentials.get(field)
                if value:
                    keyring.set_password(self.service, f"{scope}:{field}", value)
        except KeyringError as exc:
            raise RuntimeError("unable to write credentials to the OS vault") from exc

    def clear_credentials(self, scope: str) -> None:
        for field in self.FIELDS:
            try:
                keyring.delete_password(self.service, f"{scope}:{field}")
            except KeyringError:
                continue

    def state(self, scope: str) -> dict[str, bool]:
        credentials = self.get_credentials(scope)
        return {f"{field}_configured": bool(value) for field, value in credentials.items()}

    def migrate_legacy(self, exchange_id: str, target_scope: str) -> None:
        """Move credentials from the pre-1.0 exchange-only key exactly once."""
        if any(self._read(target_scope).values()):
            return
        legacy = self._read(exchange_id)
        if not any(legacy.values()):
            return
        self.set_credentials(target_scope, legacy)
        self.clear_credentials(exchange_id)
