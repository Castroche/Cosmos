"""UI metadata requests must stay independent of native Quant model loading."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

from cosmos_quant import api
from cosmos_quant.configuration import UserConfiguration
from cosmos_quant.demo import demo_candles
from cosmos_quant.domain import MarketSnapshot
from cosmos_quant.market_data import MarketDataService
from cosmos_quant.storage import SqliteStore
from cosmos_quant.trading_intelligence import (
    AutomationRuntimeV2,
    WaitPlan,
    WaitRuntimeStatus,
    WaitRuntimeV2,
)
from cosmos_quant.trading_intelligence_automation import TradingIntelligenceAutomation
from cosmos_quant.trading_intelligence_engine import EventDrivenTradingEngine
from cosmos_quant.trading_intelligence_store import TradingIntelligenceStore


def test_quant_metadata_does_not_construct_predictors(monkeypatch):
    artifact = SimpleNamespace(
        model_fingerprint="model", data_as_of=None, validation={}
    )
    monkeypatch.setattr(api, "load_quant_v1_artifact", lambda _symbol: artifact)
    monkeypatch.setattr(api, "quant_model_is_eligible", lambda _artifact: True)

    def forbid_predictor(_symbol):
        raise AssertionError("metadata endpoint loaded a native predictor")

    monkeypatch.setattr(api, "load_quant_v1_model", forbid_predictor)
    result = asyncio.run(api.trading_intelligence_quant_models())
    assert len(result) == 3
    assert all(row["health"] == "healthy" for row in result)


def test_concurrent_quant_loads_share_one_predictor(monkeypatch):
    artifact = object()
    created = []
    monkeypatch.setattr(api, "quant_model_cache", {})
    monkeypatch.setattr(api, "load_quant_v1_artifact", lambda _symbol: artifact)

    def build(value):
        model = SimpleNamespace(artifact=value)
        created.append(model)
        return model

    monkeypatch.setattr(api, "LightGBMQuantIntelligenceV1", build)
    with ThreadPoolExecutor(max_workers=6) as workers:
        models = list(workers.map(api.load_quant_v1_model, [api.V2_SYMBOLS[0]] * 6))
    assert len(created) == 1
    assert all(model is created[0] for model in models)


def test_hot_state_reads_continue_while_an_application_writer_is_busy(tmp_path):
    store = SqliteStore(tmp_path / "state.db")
    store.save_configuration(UserConfiguration(default_symbol="SOL/USDT"))
    store.save_risk_state('{"halted":true}')
    ti = TradingIntelligenceStore(store)
    ti.save_automation_runtime(AutomationRuntimeV2(enabled=True))
    with ThreadPoolExecutor(max_workers=1) as workers, store._lock:
        def read():
            return store.load_configuration(), store.load_risk_state(), ti.automation_runtime()

        configuration, risk, automation = workers.submit(read).result(timeout=2)
        assert configuration[0].default_symbol == "SOL/USDT"
        assert risk == '{"halted":true}'
        assert automation.enabled


def test_slow_candle_persistence_does_not_block_the_event_loop(tmp_path, monkeypatch):
    store = SqliteStore(tmp_path / "market.db")
    candles = demo_candles(80)
    release = Event()

    class Adapter:
        async def market_snapshot(self, symbol):
            return MarketSnapshot(
                symbol=symbol, timestamp=candles[-1].timestamp, source="test",
                bid=candles[-1].close, ask=candles[-1].close, last=candles[-1].close,
            )

        async def fetch_candles(self, *_args):
            return candles

    async def run():
        loop = asyncio.get_running_loop()
        writing = asyncio.Event()
        original = store.save_candles

        def persist(*args):
            loop.call_soon_threadsafe(writing.set)
            assert release.wait(2), "event loop could not release a slow disk write"
            original(*args)

        monkeypatch.setattr(store, "save_candles", persist)
        fetch = asyncio.create_task(
            MarketDataService(store).fetch(Adapter(), "okx", "BTC/USDT", "1h", 80)
        )
        try:
            await asyncio.wait_for(writing.wait(), 1)
        finally:
            release.set()
        _market, history, quality = await asyncio.wait_for(fetch, 2)
        assert len(history) == 80
        assert not quality.is_cached

    asyncio.run(run())


def test_account_resume_finishes_while_a_symbol_decision_is_busy(tmp_path):
    from test_immediate_contract import market

    store = TradingIntelligenceStore(SqliteStore(tmp_path / "resume.db"))
    store.save_market_state(market())
    store.save_automation_runtime(AutomationRuntimeV2(enabled=True))
    calls = []

    async def decide(*_args):
        raise AssertionError("account resume waited for a fresh LLM decision")

    async def reconcile():
        calls.append("reconciled")

    service = TradingIntelligenceAutomation(
        store, EventDrivenTradingEngine(store), decide=decide, execute=decide,
        reconcile=reconcile,
    )

    async def run():
        async with service._locks[api.V2_SYMBOLS[0]]:
            await asyncio.wait_for(service.resume(evaluate_market_states=False), 1)

    asyncio.run(run())
    assert calls == ["reconciled"]
    assert service.runtime().enabled


def test_review_poll_keeps_legacy_evidence_out_of_the_decoded_result(tmp_path):
    store = SqliteStore(tmp_path / "review.db")
    legacy = {"id": "legacy", "backtest": {"trades": ["evidence"] * 5000}}
    current = {"id": "current", "research_context": {"symbol": "BTC/USDT"}}
    with store.connect() as connection:
        for payload in (legacy, current):
            connection.execute(
                "INSERT INTO strategy_candidates "
                "(candidate_id,spec_fingerprint,family,status,source,payload,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (payload["id"], payload["id"], "trend", "proposed", "system",
                 json.dumps(payload), "2026-10-09T00:00:00+00:00"),
            )
    rows = {row["id"]: row for row in store.list_strategy_review_candidate_payloads()}
    assert rows["legacy"] == {"id": "legacy"}
    assert rows["current"] == current
    assert store.load_strategy_candidate("legacy") == legacy


def test_active_price_wakes_are_not_evicted_by_newer_terminal_history(tmp_path):
    from datetime import UTC, datetime, timedelta

    store = TradingIntelligenceStore(SqliteStore(tmp_path / "waits.db"))
    now = datetime.now(UTC)
    active = WaitRuntimeV2(
        episode_id="active", symbol=api.V2_SYMBOLS[0],
        wait_plan=WaitPlan(
            reason="future price", expires_at=now + timedelta(hours=1),
            wake_conditions=[{"kind": "PRICE_CROSSES_ABOVE", "action": "RECALL_LLM",
                              "value": "100"}],
        ),
    )
    store.save_wait_runtime(active)
    terminal = active.model_copy(update={"status": WaitRuntimeStatus.FIRED})
    with store.store.connect() as connection:
        for i in range(1001):
            item = terminal.model_copy(update={"wait_runtime_id": f"terminal-{i}"})
            connection.execute(
                "INSERT INTO ti_artifacts_v2 "
                "(artifact_id,artifact_type,content_hash,payload,created_at) VALUES (?,?,?,?,?)",
                (item.wait_runtime_id, "WaitRuntimeV2", str(i), item.model_dump_json(),
                 (now + timedelta(seconds=1)).isoformat()),
            )
    found = store.list_wait_runtimes(symbol=api.V2_SYMBOLS[0], active_only=True)
    assert [item.wait_runtime_id for item in found] == [active.wait_runtime_id]
