from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from .domain import TradingMode


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    trading_mode: TradingMode = TradingMode.PAPER
    cosmos_api_host: str = "127.0.0.1"
    cosmos_api_port: int = 8000
    cosmos_cors_origins: str = "http://localhost:5173"
    cosmos_data_dir: Path = Path("data")
    cosmos_automation_start_paused: bool = False
    exchange_id: str = "okx"
    exchange_sandbox: bool = True
    exchange_api_key: str = ""
    exchange_secret: str = ""
    exchange_passphrase: str = ""
    live_trading_enabled: bool = False
    live_unlock_phrase: str = ""

    @property
    def cors_origins(self) -> list[str]:
        return [item.strip() for item in self.cosmos_cors_origins.split(",") if item.strip()]
