from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
from pathlib import Path

from cosmos_quant.api import create_app
from cosmos_quant.application_services import ApplicationServices
from cosmos_quant.settings import Settings
from cosmos_quant.storage import SqliteStore
from fastapi.testclient import TestClient


def test_existing_openapi_contract_is_preserved():
    baseline = json.loads((Path(__file__).parent / "fixtures/api_contract_v2.json").read_text())
    schema = create_app().openapi()
    for key, source in [("paths", schema["paths"]), ("schemas", schema["components"]["schemas"])]:
        for name, digest in baseline[key].items():
            assert (
                hashlib.sha256(json.dumps(source[name], sort_keys=True).encode()).hexdigest()
                == digest
            ), name


def test_import_does_not_initialize_runtime_or_data(tmp_path):
    env = {**os.environ, "COSMOS_DATA_DIR": str(tmp_path / "untouched")}
    subprocess.run(
        [
            os.sys.executable,
            "-c",
            "from cosmos_quant.settings import Settings; "
            "Settings.__init__ = lambda self, **kwargs: "
            '(_ for _ in ()).throw(AssertionError("import read settings")); '
            "import cosmos_quant.api",
        ],
        env=env,
        check=True,
        capture_output=True,
    )
    assert not (tmp_path / "untouched").exists()


def test_application_instances_and_dependencies_are_isolated(tmp_path):
    left = ApplicationServices(Settings(cosmos_data_dir=tmp_path / "left"))
    right = ApplicationServices(Settings(cosmos_data_dir=tmp_path / "right"))
    assert left.store.path != right.store.path
    assert left.v2_entry_allocation_lock is not right.v2_entry_allocation_lock
    assert left.critical_workers is not right.critical_workers
    assert left.learning_shadow is not right.learning_shadow
    app = create_app(services=left)
    client = TestClient(app, base_url="http://test")
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/trading-intelligence/learning/settings").json()["enabled"]
    settings = {"enabled": False, "shadow_enabled": False, "pinned_version": None}
    assert (
        client.put("/api/trading-intelligence/learning/settings", json=settings).status_code == 200
    )
    assert right.store.learning.settings()["enabled"]
    assert client.get("/api/trading-intelligence/learning/versions").json()["items"] == []
    assert client.get("/api/trading-intelligence/learning/comparisons").json()["items"] == []
    assert client.get("/api/trading-intelligence/learning/summary").json()["daily_used"] == 0
    assert client.get("/api/retired-nonexistent-route").status_code == 404


def test_repositories_preserve_transaction_owner_and_migration_backup(tmp_path):
    path = tmp_path / "account.db"
    store = SqliteStore(path)
    assert store.execution.store is store
    assert store.research.store is store
    with store.connect() as c:
        c.execute("DELETE FROM schema_migrations WHERE version=11")
        c.execute("DROP TABLE ti_learning_evidence")
    upgraded = SqliteStore(path)
    assert list((tmp_path / "backups").glob("*pre-schema-v11*"))
    assert upgraded.integrity_check() == "ok"
    before = len(list((tmp_path / "backups").glob("*")))
    SqliteStore(path)
    assert len(list((tmp_path / "backups").glob("*"))) == before


def test_domain_services_do_not_import_http_entrypoint():
    root = Path(__file__).parents[1] / "cosmos_quant"
    for path in [*root.glob("service_*.py"), *root.glob("repository_*.py")]:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module not in {"api", "cosmos_quant.api"}, path
            assert not isinstance(node, ast.Global), path


def test_lifespan_owns_and_cancels_background_workers(tmp_path, monkeypatch):
    import asyncio

    from cosmos_quant import service_lifecycle
    from cosmos_quant.demo import demo_candles, snapshot_from_candles
    from cosmos_quant.runtime import NetworkFreeRuntime

    services = ApplicationServices(
        Settings(cosmos_data_dir=tmp_path, cosmos_automation_start_paused=True)
    )
    candles = demo_candles()
    services.runtime = NetworkFreeRuntime(
        services.configuration_service, candles, snapshot_from_candles(candles)
    )
    closed = []

    class Collector:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        async def close(self):
            closed.append(True)

    async def parked():
        await asyncio.Event().wait()

    monkeypatch.setattr(service_lifecycle, "BinanceLiquidationCollector", Collector)
    for name in (
        "maintenance_loop",
        "catalog_journal_collection_loop",
        "official_event_collection_loop",
        "trading_intelligence_market_state_loop",
        "trading_intelligence_position_loop",
        "strategy_review_worker_loop",
    ):
        monkeypatch.setattr(services, name, parked)
    app = create_app(services=services)
    with TestClient(app, base_url="http://test") as client:
        assert client.get("/api/health").status_code == 200
        assert len(services.critical_workers) == 3
        assert services.learning_worker_task is not None
        assert not services.learning_shadow.accepting
    assert services.critical_workers == {}
    assert services.learning_worker_task is None
    assert services.learning_shadow.task is None
    assert closed == [True]
