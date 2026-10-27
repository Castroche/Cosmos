"""Portfolio sizing inputs: reconcile venue positions with pending entry reservations."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from .domain import PortfolioState
from .trading_intelligence import PositionRuntimeV2, PositionStateV2


def portfolio_risk_snapshot(
    portfolio: PortfolioState,
    managed: list[PositionRuntimeV2],
    *,
    existing_position_stress: Decimal,
    pending_entries: list[Any] | None = None,
    mode: str | None = None,
) -> dict:
    """Gross directional risk plus unfilled reservations, with one slot per direction.

    Entry reservations contain original order amounts. Release only the fraction
    atomically booked into a position, not venue fills that remain unaccounted.
    """
    zero = Decimal("0")
    scopes: dict[tuple[str, str], dict] = {}
    for item in portfolio.open_positions:
        if item.quantity <= 0:
            continue
        side = str(item.side).upper()
        scopes[(item.symbol.removesuffix(":USDT"), side)] = {
            "symbol": item.symbol.removesuffix(":USDT"), "side": side,
            "quantity": item.quantity, "entry": item.average_entry,
            "mark": item.mark_price, "stop": item.stop_loss, "pending": False,
        }
    for item in managed:
        if item.state in {PositionStateV2.CLOSED, PositionStateV2.FLAT}:
            continue
        key = (item.symbol.removesuffix(":USDT"), item.side)
        existing = scopes.get(key)
        # Reserve the unfilled part until the order is reconciled or cancelled.
        quantity = (
            item.original_quantity
            if item.state in {PositionStateV2.OPENING, PositionStateV2.PARTIALLY_FILLED}
            else item.remaining_quantity
        )
        if existing:
            existing["quantity"] = max(existing["quantity"], quantity)
            existing["stop"] = item.current_stop
        elif quantity > 0:
            scopes[key] = {
                "symbol": key[0], "side": item.side, "quantity": quantity,
                "entry": item.average_entry, "mark": item.average_entry,
                "stop": item.current_stop, "pending": True,
            }
        if key in scopes:
            scopes[key]["position_id"] = item.position_id
            scopes[key]["position_version"] = item.version
    rows = []
    for item in scopes.values():
        stop = item["stop"]
        # Equity is marked to market: protect the current equity-to-stop loss,
        # including profits at risk. Missing stops retain the configured fallback.
        distance = (
            max(item["mark"] - stop if item["side"] == "LONG" else stop - item["mark"], zero)
            if stop is not None else None
        )
        risk = (
            distance * item["quantity"] if distance is not None
            else portfolio.equity * existing_position_stress
        )
        rows.append({**item, "mark_price": item["mark"],
                     "risk_amount": risk, "estimated": stop is None})
    risk_amount = sum((row["risk_amount"] for row in rows), zero)
    pending_risk = zero
    pending_margin = zero
    position_scopes = set(scopes)
    seen_entries: set[str] = set()
    reservations = []
    for entry in pending_entries or []:
        item = entry if isinstance(entry, dict) else entry.model_dump()
        if str(item["status"]) not in {
            "SUBMITTING", "OPEN", "PARTIALLY_FILLED", "CANCEL_PENDING", "UNCERTAIN",
        }:
            continue
        if mode is not None and str(item["mode"]) != str(mode):
            continue
        entry_id = item.get("entry_id")
        if entry_id is not None:
            if entry_id in seen_entries:
                continue
            seen_entries.add(entry_id)
        quantity = Decimal(str(item["quantity"]))
        unfilled = max(quantity - Decimal(str(item.get("accounted_quantity", zero))), zero)
        if unfilled == 0:
            continue
        fraction = unfilled / quantity
        reserved_risk = Decimal(str(item["reserved_risk"])) * fraction
        reserved_risk += Decimal(str(item.get("reserved_stop_risk", zero))) * fraction
        reserved_margin = Decimal(str(item["reserved_margin"])) * fraction
        pending_risk += reserved_risk
        pending_margin += reserved_margin
        position_scopes.add((item["symbol"].removesuffix(":USDT"), str(item["side"])))
        reservations.append({"entry_id": entry_id, "position_id": item.get("position_id"),
                             "risk_amount": reserved_risk, "margin_amount": reserved_margin})
    risk_amount += pending_risk
    return {
        "risk_amount": risk_amount,
        "risk_fraction": risk_amount / portfolio.equity if portfolio.equity > 0 else Decimal("1"),
        "open_positions": len(position_scopes),
        "positions": rows,
        "pending_risk": pending_risk,
        "pending_margin": pending_margin,
        "reserved_risk": pending_risk,
        "reserved_margin": pending_margin,
        "pending_entries": reservations,
    }
