"""Account recovery races use temporary SQLite and a fake Demo venue only."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest
from cosmos_quant import api
from cosmos_quant.trading_intelligence import AutomationRuntimeV2, PositionStateV2
from cosmos_quant.v2_position_runtime import PositionReconciliationIntegrityError
from test_pooled_positions import campaign, close_fills


@pytest.fixture
def recovery_account(tmp_path, monkeypatch):
    store, position, _plan, exchange, _reconciler, completed = campaign(tmp_path)
    store.save_automation_runtime(AutomationRuntimeV2(enabled=True))
    halted = []
    recovered = []

    async def on_terminal(*_args):
        pass

    async def on_completed(symbol, event_id):
        completed.append((symbol, event_id))

    async def close():
        pass

    configuration = SimpleNamespace(
        margin_mode="cross",
        contract_leverage=Decimal("1"),
        risk=SimpleNamespace(max_leverage=Decimal("3")),
    )
    monkeypatch.setattr(api, "trading_intelligence_store", store)
    monkeypatch.setattr(api, "v2_entry_allocation_lock", asyncio.Lock())
    monkeypatch.setattr(
        api,
        "trading_intelligence_automation",
        SimpleNamespace(runtime=store.automation_runtime, on_entry_terminal=on_terminal),
    )
    monkeypatch.setattr(
        api,
        "risk_state_service",
        SimpleNamespace(
            entry_generation=0,
            get=lambda: SimpleNamespace(halted=False),
            halt=lambda *args: halted.append(args),
            recover_protection_reconciliation=lambda: recovered.append(True),
        ),
    )
    monkeypatch.setattr(
        api.configuration_service, "current_configuration", lambda: configuration
    )
    monkeypatch.setattr(api, "enqueue_v2_trade_completed", on_completed)
    monkeypatch.setattr(api, "build_v2_demo_adapter", lambda: exchange)
    monkeypatch.setattr(exchange, "close", close, raising=False)
    return store, position, exchange, completed, halted, recovered


def test_startup_and_background_worker_close_a_pool_once(recovery_account, monkeypatch):
    store, position, exchange, completed, halted, recovered = recovery_account
    close_fills(exchange)

    async def run():
        reading_order = asyncio.Event()
        release = asyncio.Event()
        worker_reconciled = asyncio.Event()
        original_fetch = exchange.fetch_order
        reads = 0

        async def fetch_order(*args):
            nonlocal reads
            reads += 1
            if reads == 1:
                reading_order.set()
                await release.wait()
            return await original_fetch(*args)

        def recover():
            recovered.append(True)
            if len(recovered) == 2:
                worker_reconciled.set()

        monkeypatch.setattr(exchange, "fetch_order", fetch_order)
        monkeypatch.setattr(api.risk_state_service, "recover_protection_reconciliation", recover)
        startup = asyncio.create_task(api.reconcile_v2_before_resume())
        await asyncio.wait_for(reading_order.wait(), 2)
        worker = asyncio.create_task(api.trading_intelligence_position_loop())
        try:
            await asyncio.sleep(0)
            assert reads == 1
            assert not worker.done()
            release.set()
            await asyncio.wait_for(startup, 2)
            await asyncio.wait_for(worker_reconciled.wait(), 2)
        finally:
            release.set()
            for task in (startup, worker):
                task.cancel()
            await asyncio.gather(startup, worker, return_exceptions=True)

    asyncio.run(run())
    closed = store.get_position(position.position_id)
    assert closed.state == PositionStateV2.CLOSED
    assert closed.remaining_quantity == 0
    events = store.list_events(aggregate_id=position.position_id)
    assert len([e for e in events if e["event_type"] == "TRADE_COMPLETED"]) == 1
    assert not [e for e in events if e["event_type"] == "POSITION_RECONCILIATION_FAILED"]
    assert len(completed) == 1
    assert not halted


def test_entry_allocation_waits_for_complete_position_recovery(recovery_account, monkeypatch):
    store, position, exchange, _completed, halted, _recovered = recovery_account
    close_fills(exchange)

    async def run():
        reading_order = asyncio.Event()
        release = asyncio.Event()
        original_fetch = exchange.fetch_order
        allocated_states = []

        async def fetch_order(*args):
            reading_order.set()
            await release.wait()
            return await original_fetch(*args)

        async def allocate(*_args):
            allocated_states.append(store.get_position(position.position_id).state)

        monkeypatch.setattr(exchange, "fetch_order", fetch_order)
        monkeypatch.setattr(api, "_execute_trading_intelligence_plan", allocate)
        recovery = asyncio.create_task(api.reconcile_v2_before_resume())
        await asyncio.wait_for(reading_order.wait(), 2)
        entry = asyncio.create_task(
            api.execute_trading_intelligence_plan("next-plan", SimpleNamespace())
        )
        try:
            await asyncio.sleep(0)
            assert not allocated_states
            release.set()
            await asyncio.wait_for(asyncio.gather(recovery, entry), 2)
            assert allocated_states == [PositionStateV2.CLOSED]
        finally:
            release.set()
            for task in (recovery, entry):
                task.cancel()
            await asyncio.gather(recovery, entry, return_exceptions=True)

    asyncio.run(run())
    assert not halted


def test_uncertain_entry_keeps_recovery_fail_closed(recovery_account, monkeypatch):
    store, position, exchange, completed, _halted, recovered = recovery_account
    close_fills(exchange)

    async def uncertain(*_args, **_kwargs):
        return {"uncertain": 1}

    monkeypatch.setattr(api.EntryOrderService, "reconcile", uncertain)
    with pytest.raises(PositionReconciliationIntegrityError, match="entry order remains uncertain"):
        asyncio.run(api.reconcile_v2_demo_state(exchange))
    assert store.get_position(position.position_id).version == position.version
    assert not completed and not recovered
    assert not api.v2_entry_allocation_lock.locked()


def test_protection_failure_still_halts_and_releases_account_lock(
    recovery_account, monkeypatch
):
    _store, _position, exchange, _completed, halted, recovered = recovery_account
    exchange.protected = False

    async def unprotected(*_args):
        return False

    monkeypatch.setattr(exchange, "sync_position_protection", unprotected)
    with pytest.raises(RuntimeError, match="failed reconciliation"):
        asyncio.run(api.reconcile_v2_demo_state(exchange))
    assert halted and not recovered
    assert not api.v2_entry_allocation_lock.locked()


@pytest.mark.parametrize("path", ["startup", "worker"])
@pytest.mark.parametrize("venue_has_position", [False, True])
def test_stale_protection_halt_requires_venue_revalidation(
    recovery_account, monkeypatch, path, venue_has_position
):
    store, _position, exchange, _completed, _halted, _recovered = recovery_account
    close_fills(exchange)
    asyncio.run(api.reconcile_v2_demo_state(exchange))
    store.save_automation_runtime(AutomationRuntimeV2(enabled=False))
    exchange.quantity = Decimal("15") if venue_has_position else Decimal("0")
    risk = SimpleNamespace(halted=True, source="protection_reconciliation")
    reads = []

    async def run():
        checked = asyncio.Event()
        original_portfolio = exchange.portfolio

        async def portfolio():
            reads.append(True)
            return await original_portfolio()

        def recover():
            risk.halted = False
            checked.set()

        def halt(*_args, **_kwargs):
            checked.set()

        monkeypatch.setattr(exchange, "portfolio", portfolio)
        monkeypatch.setattr(api.risk_state_service, "get", lambda: risk)
        monkeypatch.setattr(api.risk_state_service, "recover_protection_reconciliation", recover)
        monkeypatch.setattr(api.risk_state_service, "halt", halt)
        if path == "startup":
            if venue_has_position:
                with pytest.raises(PositionReconciliationIntegrityError, match="outside the v2"):
                    await api.reconcile_v2_before_resume()
            else:
                await api.reconcile_v2_before_resume()
        else:
            worker = asyncio.create_task(api.trading_intelligence_position_loop())
            try:
                await asyncio.wait_for(checked.wait(), 2)
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(run())
    assert reads
    assert risk.halted is venue_has_position
