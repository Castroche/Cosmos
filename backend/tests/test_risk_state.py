import asyncio
from decimal import Decimal

import pytest
from cosmos_quant import api
from cosmos_quant.demo import demo_candles, snapshot_from_candles
from cosmos_quant.domain import PortfolioState
from cosmos_quant.exchanges.paper import PaperExchange
from cosmos_quant.execution import ExecutionEngine
from cosmos_quant.risk import RiskEngine
from cosmos_quant.risk_state import RiskStateService
from cosmos_quant.storage import SqliteStore
from cosmos_quant.strategy import ExplainableTrendStrategy


def test_manual_risk_halt_is_durable_and_requires_confirmation(tmp_path):
    store = SqliteStore(tmp_path / "cosmos.db")
    first = RiskStateService(store)
    first.halt("operator emergency stop")

    restored = RiskStateService(store)
    assert restored.get().halted
    assert restored.get().reason == "operator emergency stop"

    with pytest.raises(ValueError):
        restored.resume("WRONG")
    assert restored.resume("RESUME TRADING").halted is False


def test_manual_halt_cannot_be_downgraded_into_auto_recoverable_source(tmp_path):
    state = RiskStateService(SqliteStore(tmp_path / "cosmos.db"))
    manual = state.halt("operator certification hold", source="manual")

    assert (
        state.halt(
            "temporary maintenance failure",
            source="maintenance",
        )
        == manual
    )
    assert state.recover_maintenance() == manual
    assert (
        state.halt(
            "temporary reconciliation timeout",
            source="reconciliation_transport",
        )
        == manual
    )
    assert state.recover_reconciliation_transport() == manual
    assert (
        state.halt(
            "daily drawdown threshold",
            source="daily_drawdown",
        )
        == manual
    )
    assert state.get().source == "manual"
    assert state.get().reason == "operator certification hold"


def test_higher_priority_manual_halt_replaces_auto_recoverable_source(tmp_path):
    state = RiskStateService(SqliteStore(tmp_path / "cosmos.db"))
    state.halt("temporary maintenance failure", source="maintenance")

    manual = state.halt("operator certification hold", source="manual")

    assert manual.source == "manual"
    assert manual.reason == "operator certification hold"
    assert state.recover_maintenance() == manual


def test_only_legacy_missing_champion_halt_is_migrated(tmp_path):
    state = RiskStateService(SqliteStore(tmp_path / "cosmos.db"))
    state.halt(
        "No cross-market robust Champion; block all new risk until strategy "
        "certification is enforced",
        source="manual",
    )

    recovered = state.recover_legacy_strategy_governance_halt()

    assert recovered.halted is False

    operator_halt = state.halt("operator emergency stop", source="manual")
    assert state.recover_legacy_strategy_governance_halt() == operator_halt

    state.resume("RESUME TRADING")
    uncertain = state.halt(
        "exchange order outcome is uncertain",
        source="execution_uncertain",
    )
    assert state.recover_legacy_strategy_governance_halt() == uncertain


def test_daily_drawdown_failure_latches_risk_halt(tmp_path):
    async def scenario():
        candles = demo_candles()
        market = snapshot_from_candles(candles)
        plan = ExplainableTrendStrategy().generate(candles, market, Decimal("100000"))
        exchange = PaperExchange()
        exchange.update_market(market)
        state = RiskStateService(SqliteStore(tmp_path / "cosmos.db"))
        engine = ExecutionEngine(exchange, RiskEngine(), state)
        portfolio = PortfolioState(
            equity=Decimal("95000"),
            cash=Decimal("95000"),
            daily_start_equity=Decimal("100000"),
            gross_exposure=Decimal("0"),
        )

        result = await engine.submit_plan(plan, portfolio)

        assert result.order is None
        assert state.get().halted
        assert state.get().source == "daily_drawdown"

    asyncio.run(scenario())


def test_clean_reconciliation_recovers_only_transport_halts(tmp_path):
    state = RiskStateService(SqliteStore(tmp_path / "cosmos.db"))
    state.halt(
        "订单对账连续失败 3 次：temporary timeout",
        source="reconciliation_transport",
    )

    recovered = state.recover_reconciliation_transport()

    assert recovered.halted is False

    state.halt(
        "订单状态不确定、存在外部挂单或保护单缺失，需人工核对",
        source="reconciliation_integrity",
    )
    still_halted = state.recover_reconciliation_transport()
    assert still_halted.halted is True
    assert still_halted.source == "reconciliation_integrity"


def test_clean_demo_reconciliation_can_recover_integrity_latch(tmp_path):
    state = RiskStateService(SqliteStore(tmp_path / "cosmos.db"))
    state.halt(
        "订单状态不确定、存在外部挂单或保护单缺失，需人工核对",
        source="reconciliation_integrity",
    )

    recovered = state.recover_demo_reconciliation_integrity()

    assert recovered.halted is False

    manual = state.halt("operator emergency stop", source="manual")
    assert state.recover_demo_reconciliation_integrity() == manual


def test_clean_reconciliation_recovers_legacy_transport_halt(tmp_path):
    state = RiskStateService(SqliteStore(tmp_path / "cosmos.db"))
    state.halt(
        "订单对账失败：okx GET /api/v5/trade/orders-pending",
        source="reconciliation",
    )

    recovered = state.recover_reconciliation_transport()

    assert recovered.halted is False


def test_exact_position_protection_revalidation_recovers_only_read_halt(tmp_path):
    state = RiskStateService(SqliteStore(tmp_path / "cosmos.db"))
    state.halt(
        "open-position protection is uncertain: temporary fills-history timeout",
        source="protection_reconciliation",
    )

    recovered = state.recover_protection_reconciliation()

    assert recovered.halted is False

    state.halt(
        "Agent position protection is uncertain after an interrupted reduction",
        source="protection_rebalance",
    )
    still_halted = state.recover_protection_reconciliation()
    assert still_halted.halted is True
    assert still_halted.source == "protection_rebalance"

    state.halt("operator emergency stop", source="manual")
    manual = state.recover_protection_reconciliation()
    assert manual.halted is True
    assert manual.source == "manual"


def test_successful_maintenance_recovers_only_maintenance_halt(tmp_path):
    state = RiskStateService(SqliteStore(tmp_path / "cosmos.db"))
    state.halt(
        "自动备份连续失败 3 次：temporary file lock",
        source="maintenance",
    )

    recovered = state.recover_maintenance()

    assert recovered.halted is False

    state.halt(
        "订单状态不确定、存在外部挂单或保护单缺失，需人工核对",
        source="reconciliation_integrity",
    )
    integrity = state.recover_maintenance()
    assert integrity.halted is True
    assert integrity.source == "reconciliation_integrity"


def test_backup_maintenance_requires_three_consecutive_failures(monkeypatch):
    class Backup:
        calls = 0

        def create_if_due(self):
            self.calls += 1
            raise RuntimeError("temporary file lock")

    class RiskState:
        halt_calls = []
        recovery_calls = 0

        def halt(self, reason, source):
            self.halt_calls.append((reason, source))

        def recover_maintenance(self):
            self.recovery_calls += 1

    backup = Backup()
    risk_state = RiskState()

    async def stop_after_third_attempt(_seconds):
        if backup.calls >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(api, "backup_service", backup)
    monkeypatch.setattr(api, "risk_state_service", risk_state)
    monkeypatch.setattr(api.asyncio, "sleep", stop_after_third_attempt)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(api.maintenance_loop())

    assert backup.calls == 3
    assert len(risk_state.halt_calls) == 1
    assert risk_state.halt_calls[0][1] == "maintenance"
    assert "连续失败 3 次" in risk_state.halt_calls[0][0]
