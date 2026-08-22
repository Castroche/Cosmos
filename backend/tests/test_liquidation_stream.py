import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from cosmos_quant.domain import OrderSide, Side
from cosmos_quant.liquidation_stream import parse_binance_liquidation
from cosmos_quant.market_data_capture import (
    CaptureSessionStatus,
    MarketDataCaptureSession,
    calculate_capture_coverage,
)
from cosmos_quant.storage import SqliteStore


def test_binance_liquidation_is_normalized_and_deduplicatable():
    payload = {
        "e": "forceOrder",
        "E": 1568014460893,
        "o": {
            "s": "BTCUSDT",
            "S": "SELL",
            "o": "LIMIT",
            "f": "IOC",
            "q": "0.014",
            "p": "9910",
            "ap": "9910",
            "X": "FILLED",
            "l": "0.014",
            "z": "0.014",
            "T": 1568014460893,
        },
        "ps": "BTCUSDT",
        "st": 1,
    }

    first = parse_binance_liquidation(
        json.dumps(payload),
        allowed_symbols={"BTC/USDT"},
    )
    second = parse_binance_liquidation(payload, allowed_symbols={"BTC/USDT"})

    assert first is not None
    assert second is not None
    assert first.id == second.id
    assert first.order_side == OrderSide.SELL
    assert first.liquidated_side == Side.LONG
    assert str(first.quote_value) == "138.740"


def test_binance_liquidation_filters_coin_m_and_untracked_symbols():
    payload = {
        "e": "forceOrder",
        "E": 1,
        "o": {
            "s": "ETHUSDT",
            "S": "BUY",
            "q": "1",
            "p": "1",
            "ap": "1",
            "X": "FILLED",
            "l": "1",
            "z": "1",
            "T": 1,
        },
        "st": 2,
    }

    assert parse_binance_liquidation(payload, allowed_symbols={"BTC/USDT"}) is None


def test_liquidation_event_storage_is_idempotent(tmp_path):
    payload = {
        "e": "forceOrder",
        "E": 1568014460893,
        "o": {
            "s": "BTCUSDT",
            "S": "BUY",
            "q": "2",
            "p": "100",
            "ap": "99",
            "X": "FILLED",
            "l": "1",
            "z": "2",
            "T": 1568014460893,
        },
        "st": 1,
    }
    event = parse_binance_liquidation(payload)
    assert event is not None
    store = SqliteStore(tmp_path / "liquidations.db")

    store.save_liquidation_event(event)
    store.save_liquidation_event(event)

    restored = store.list_liquidation_events(symbol="BTC/USDT")
    assert len(restored) == 1
    assert restored[0]["id"] == event.id


def _capture_session(
    session_id: str,
    start: datetime,
    end: datetime | None,
    *,
    status: CaptureSessionStatus = CaptureSessionStatus.CLOSED,
) -> MarketDataCaptureSession:
    return MarketDataCaptureSession(
        id=session_id,
        source="binance",
        stream="!forceOrder@arr",
        symbols=["BTC/USDT", "ETH/USDT"],
        sampling="latest_per_symbol_within_1000ms",
        status=status,
        started_at=start,
        last_heartbeat_at=end or start,
        ended_at=end,
    )


def test_capture_coverage_uses_connection_intervals_not_event_frequency():
    now = datetime(2026, 8, 15, tzinfo=UTC)
    start = now - timedelta(days=90)
    first_end = now - timedelta(days=45)
    second_start = first_end + timedelta(seconds=60)
    sessions = [
        _capture_session("capture_first", start, first_end),
        _capture_session("capture_second", second_start, now),
    ]

    coverage = calculate_capture_coverage(
        sessions,
        source="binance",
        stream="!forceOrder@arr",
        symbol="BTC/USDT",
        now=now,
    )

    assert coverage.passed is True
    assert coverage.session_count == 2
    assert coverage.maximum_gap_seconds == Decimal("60.000")
    # Coverage is valid even if a quiet symbol emitted no liquidation event.
    assert coverage.coverage_ratio >= Decimal("0.999")


def test_capture_coverage_fails_closed_on_material_disconnect_gap():
    now = datetime(2026, 8, 15, tzinfo=UTC)
    start = now - timedelta(days=90)
    sessions = [
        _capture_session("capture_first", start, now - timedelta(days=2)),
        _capture_session("capture_second", now - timedelta(days=1), now),
    ]

    coverage = calculate_capture_coverage(
        sessions,
        source="binance",
        stream="!forceOrder@arr",
        symbol="BTC/USDT",
        now=now,
    )

    assert coverage.passed is False
    assert coverage.material_gap_count == 1
    assert coverage.maximum_gap_seconds == Decimal("86400.000")


def test_capture_sessions_are_durable_and_crash_reconciled(tmp_path):
    store = SqliteStore(tmp_path / "captures.db")
    started_at = datetime(2026, 8, 15, tzinfo=UTC)
    heartbeat_at = started_at + timedelta(seconds=30)
    session = _capture_session(
        "capture_crashed",
        started_at,
        None,
        status=CaptureSessionStatus.CONNECTED,
    )
    store.start_market_data_capture_session(session)
    assert store.heartbeat_market_data_capture_session(session.id, heartbeat_at) is True

    interrupted = store.interrupt_open_market_data_capture_sessions(
        source="binance",
        stream="!forceOrder@arr",
        reason="test restart",
    )
    restored = MarketDataCaptureSession.model_validate(
        store.list_market_data_capture_sessions()[0]
    )

    assert interrupted == 1
    assert restored.status == CaptureSessionStatus.INTERRUPTED
    assert restored.ended_at == heartbeat_at
    assert restored.error == "test restart"
    assert store.heartbeat_market_data_capture_session(session.id, heartbeat_at) is False
