import asyncio
import inspect

from app_fixture import services as api
from httpx import ASGITransport, AsyncClient


def request(method: str, path: str, json=None):
    async def run():
        transport = ASGITransport(app=api.app)
        headers = {}
        async with AsyncClient(
            transport=transport, base_url="http://test", headers=headers
        ) as client:
            return await client.request(method, path, json=json)

    return asyncio.run(run())


def test_legacy_realtime_mutations_are_gone() -> None:
    paths = (
        "/api/agent/goals/legacy/run-once",
        "/api/live-authorization/enable",
        "/api/live-unlock/preview",
        "/api/strategies/legacy/approve",
        "/api/strategies/legacy/activate",
        "/api/strategies/legacy/execute",
    )
    assert all(request("POST", path).status_code == 404 for path in paths)


def test_migration_summary_confirms_no_legacy_scheduler() -> None:
    response = request("GET", "/api/trading-intelligence/migration-summary")
    assert response.status_code == 200
    payload = response.json()
    assert payload["legacy_strategy_scheduler_running"] is False
    assert payload["legacy_agent_scheduler_running"] is False
    assert payload["legacy_mutations"] == "not_registered_http_404"
    assert payload["legacy_runtime_modules_present"] is False
    assert payload["legacy_evidence"] == "read_only"


def test_dashboard_cannot_scan_generate_or_backtest_legacy_plans() -> None:
    source = inspect.getsource(api.dashboard)
    assert "ensure_plan" not in source
    assert "strategy_factory" not in source
    assert "execute_research_backtest" not in source
    assert "materialize_research_dataset" not in source
    assert not hasattr(api, "ensure_plan")
