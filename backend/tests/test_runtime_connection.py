import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from cosmos_quant.configuration import UserConfiguration
from cosmos_quant.domain import Candle, MarketSnapshot
from cosmos_quant.exchanges.ccxt_adapter import ExchangeConfigurationError
from cosmos_quant.runtime import RuntimeManager


def test_private_failure_preserves_successful_public_connection(monkeypatch):
    now = datetime.now(UTC)

    class FakeAdapter:
        def __init__(self, *args, **kwargs):
            pass

        async def market_snapshot(self, symbol):
            return MarketSnapshot(
                symbol=symbol,
                timestamp=now,
                bid=Decimal("99999"),
                ask=Decimal("100001"),
                last=Decimal("100000"),
                source="okx",
            )

        async def fetch_candles(self, symbol, timeframe, limit):
            candle = Candle(
                timestamp=now,
                open=Decimal("100000"),
                high=Decimal("100001"),
                low=Decimal("99999"),
                close=Decimal("100000"),
                volume=Decimal("1"),
            )
            return [candle] * limit

        async def portfolio(self):
            raise ExchangeConfigurationError(
                "okx_account_mode_spot",
                "OKX account is still in spot mode",
                "Enable Futures mode in OKX Demo Trading",
            )

        async def close(self):
            pass

    monkeypatch.setattr("cosmos_quant.runtime.CcxtExchangeAdapter", FakeAdapter)
    service = SimpleNamespace(store=object())
    runtime = RuntimeManager(service)

    result = asyncio.run(
        runtime.test_connection(
            UserConfiguration(trading_mode="sandbox", market_type="swap"),
            {
                "api_key": "sandbox-key",
                "secret": "sandbox-secret",
                "passphrase": "sandbox-passphrase",
            },
        )
    )

    assert not result.ok
    assert result.public_market_ok
    assert result.private_account_ok is False
    assert not result.protected_order_ok
    assert result.last_price == Decimal("100000")
    assert result.candle_count == 60
    assert result.failure_code == "okx_account_mode_spot"
    assert result.required_action == "Enable Futures mode in OKX Demo Trading"
    assert "spot mode" in result.message


def test_reconciliation_transient_failure_blocks_entries_without_immediate_halt():
    class FailingLedger:
        async def reconcile(self, exchange):
            del exchange
            raise TimeoutError("temporary private REST timeout")

    class RiskState:
        def __init__(self):
            self.reasons = []

        def halt(self, reason, source):
            self.reasons.append((reason, source))

    runtime = RuntimeManager(SimpleNamespace(store=object()), RiskState())
    runtime.execution = object()
    runtime.order_ledger = FailingLedger()

    asyncio.run(runtime.reconcile_orders(raise_on_error=False))
    asyncio.run(runtime.reconcile_orders(raise_on_error=False))

    health = runtime.reconciliation_health
    assert not health.healthy
    assert not health.entry_allowed
    assert health.consecutive_failures == 2
    assert runtime.risk_state.reasons == []

    asyncio.run(runtime.reconcile_orders(raise_on_error=False))

    assert runtime.reconciliation_health.consecutive_failures == 3
    assert len(runtime.risk_state.reasons) == 1
    assert runtime.risk_state.reasons[0][1] == "reconciliation_transport"


def test_successful_reconciliation_clears_transient_failure_gate():
    from cosmos_quant.order_ledger import ReconciliationReport

    class FlakyLedger:
        def __init__(self):
            self.fail = True

        async def reconcile(self, exchange):
            del exchange
            if self.fail:
                raise TimeoutError("temporary private REST timeout")
            return ReconciliationReport()

    ledger = FlakyLedger()
    runtime = RuntimeManager(SimpleNamespace(store=object()))
    runtime.execution = object()
    runtime.order_ledger = ledger

    asyncio.run(runtime.reconcile_orders(raise_on_error=False))
    assert not runtime.reconciliation_entry_allowed

    ledger.fail = False
    report = asyncio.run(runtime.reconcile_orders())

    assert report is not None
    assert runtime.reconciliation_health.healthy
    assert runtime.reconciliation_entry_allowed
    assert runtime.reconciliation_health.last_error == ""


def test_unknown_exchange_position_triggers_integrity_halt():
    from cosmos_quant.order_ledger import ReconciliationReport

    class Ledger:
        async def reconcile(self, exchange):
            del exchange
            return ReconciliationReport(
                unknown_position_symbols=["BTC/USDT"],
            )

    class RiskState:
        def __init__(self):
            self.reasons = []

        def halt(self, reason, source):
            self.reasons.append((reason, source))

    risk = RiskState()
    runtime = RuntimeManager(SimpleNamespace(store=object()), risk)
    runtime.execution = object()
    runtime.order_ledger = Ledger()

    asyncio.run(runtime.reconcile_orders())

    assert len(risk.reasons) == 1
    assert risk.reasons[0][1] == "reconciliation_integrity"
