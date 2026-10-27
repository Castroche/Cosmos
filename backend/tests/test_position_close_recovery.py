"""Uncertain venue closes recover through attributable fills, without new orders."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from cosmos_quant.domain import Fill, Order, OrderSide, OrderStatus, OrderType
from cosmos_quant.exchanges.base import PositionProtectionError
from cosmos_quant.trading_intelligence import PositionStateV2
from test_pooled_positions import campaign, close_fills


@pytest.mark.parametrize("attributable", [True, False])
def test_legacy_missing_close_acknowledgement_requires_owned_client_id(
    tmp_path, monkeypatch, attributable,
):
    store, position, _plan, exchange, reconciler, completed = campaign(tmp_path)
    close_fills(exchange)
    client_id = next(iter(reconciler.close_client_ids(position))) if attributable else "unowned"
    exchange.fills = [
        fill.model_copy(update={"client_order_id": client_id})
        if fill.id == "exit" else fill
        for fill in exchange.fills
    ]

    async def no_native_ids(*_):
        return set()

    monkeypatch.setattr(exchange, "protection_exit_order_ids", no_native_ids)
    result = asyncio.run(reconciler.reconcile(exchange))
    if attributable:
        assert result["closed"] == 1
        assert store.get_position(position.position_id).state == PositionStateV2.CLOSED
        assert "native-exit" in store.get_position(position.position_id).exit_order_ids
        assert len(completed) == 1
        assert asyncio.run(reconciler.reconcile(exchange))["closed"] == 0
    else:
        assert result["closed"] == 0
        assert store.get_position(position.position_id).version == position.version
        assert not completed


def test_filled_close_survives_protection_cleanup_error_without_resubmission(
    tmp_path, monkeypatch,
):
    store, position, plan, exchange, reconciler, completed = campaign(tmp_path)
    store.update_position(
        position.position_id, expected_version=position.version,
        updates={"exit_requested_at": datetime.now(UTC), "exit_reason": "test exit"},
    )
    submitted = []

    async def no_native_ids(*_):
        return set()

    async def reduce(_plan, _entries, intent, quantity):
        submitted.append(intent)
        exchange.quantity = Decimal("0")
        acknowledged = Order(
            id="filled-close", client_order_id=intent.idempotency_key,
            plan_id=plan.plan_id, symbol=intent.symbol, side=OrderSide.SELL,
            order_type=OrderType.MARKET, status=OrderStatus.FILLED, reduce_only=True,
            quantity=quantity, filled_quantity=quantity, average_fill_price="103",
        )
        exchange.fills.append(Fill(
            id="close-fill", order_id=acknowledged.id, client_order_id=intent.idempotency_key,
            symbol=intent.symbol, side=OrderSide.SELL, quantity=quantity,
            price="103", fee="1", timestamp=datetime.now(UTC),
        ))
        raise PositionProtectionError(
            "close filled but protection cleanup unavailable", acknowledged
        )

    monkeypatch.setattr(exchange, "protection_exit_order_ids", no_native_ids)
    monkeypatch.setattr(exchange, "reduce_position_safely", reduce)
    with pytest.raises(RuntimeError, match="failed reconciliation"):
        asyncio.run(reconciler.reconcile(exchange))
    pending = store.get_position(position.position_id)
    assert "filled-close" in pending.exit_order_ids
    assert store.get_artifact("filled-close") is not None
    assert pending.state != PositionStateV2.CLOSED
    assert asyncio.run(reconciler.reconcile(exchange))["closed"] == 1
    assert len(submitted) == 1
    assert len(completed) == 1
    assert store.get_position(position.position_id).state == PositionStateV2.CLOSED


def test_unattributable_quantity_cannot_remain_silently_pending_forever(tmp_path, monkeypatch):
    store, position, _plan, exchange, reconciler, _completed = campaign(tmp_path)
    exchange.quantity = Decimal("0")
    halted = []
    monkeypatch.setattr(reconciler, "halt", lambda *args: halted.append(args))
    assert asyncio.run(reconciler.reconcile(exchange))["failed"] == 0
    clock = datetime.now(UTC) + timedelta(minutes=3)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock if tz else clock.replace(tzinfo=None)

    monkeypatch.setattr("cosmos_quant.v2_position_runtime.datetime", Clock)
    with pytest.raises(RuntimeError, match="failed reconciliation"):
        asyncio.run(reconciler.reconcile(exchange))
    assert halted
    assert store.get_position(position.position_id).state != PositionStateV2.CLOSED
    assert store.get_position(position.position_id).remaining_quantity == 15
