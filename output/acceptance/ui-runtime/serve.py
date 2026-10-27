"""Run a local acceptance instance without modifying the operator database or vault."""
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))
DATA = Path(__file__).resolve().parent / "data"
os.environ["COSMOS_DATA_DIR"] = str(DATA)
os.environ["COSMOS_AUTOMATION_START_PAUSED"] = "true"
os.environ["TRADING_MODE"] = "paper"
for name in ("EXCHANGE_API_KEY", "EXCHANGE_SECRET", "EXCHANGE_PASSPHRASE"):
    os.environ[name] = ""

import keyring

vault = {}
keyring.get_password = lambda service, name: vault.get((service, name))
keyring.set_password = lambda service, name, value: vault.__setitem__((service, name), value)
keyring.delete_password = lambda service, name: vault.pop((service, name), None)

from cosmos_quant.configuration import UserConfiguration
from cosmos_quant.storage import SqliteStore

config = UserConfiguration()
source = ROOT / "data" / "cosmos.db"
if source.exists():
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as connection:
        row = connection.execute("SELECT payload FROM app_configuration WHERE id=1").fetchone()
    if row:
        payload = json.loads(row[0])
        payload.update(trading_mode="paper", automatic_execution_enabled=False,
                       live_trading_enabled=False)
        config = UserConfiguration.model_validate(payload)
store = SqliteStore(DATA / "cosmos.db")
store.save_configuration(config)

import uvicorn

if __name__ == "__main__":
    uvicorn.run("cosmos_quant.api:app", host="127.0.0.1", port=8010, log_level="warning")
