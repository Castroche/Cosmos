from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

from .domain import Order, OrderStatus
from .exchanges.base import ExchangeAdapter
from .storage import SqliteStore


class ReconciliationStatus(StrEnum):
    CONFIRMED = "confirmed"
    UNCERTAIN = "uncertain"


class OrderRecord(BaseModel):
    exchange_id: str
    order: Order
    protection_verified: bool = False
    reconciliation_status: ReconciliationStatus = ReconciliationStatus.CONFIRMED
    last_reconciled_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_error: str = ""


class PrivateOrderEvent(BaseModel):
    order_id: str
    client_order_id: str = ""
    status: OrderStatus
    filled_quantity: Decimal | None = None
    average_fill_price: Decimal | None = None
    fee: Decimal | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ReconciliationReport(BaseModel):
    confirmed: int = 0
    uncertain_order_ids: list[str] = Field(default_factory=list)
    unknown_open_order_ids: list[str] = Field(default_factory=list)
    unknown_position_symbols: list[str] = Field(default_factory=list)
    unprotected_order_ids: list[str] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ReconciliationHealth(BaseModel):
    healthy: bool = True
    entry_allowed: bool = True
    consecutive_failures: int = 0
    failure_threshold: int = 3
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_error: str = ""


class OrderLedger:
    """Durable order truth reconciled against the exchange after every restart."""

    def __init__(self, store: SqliteStore, exchange_id: str):
        self.store = store
        self.exchange_id = exchange_id
        self.records: dict[str, OrderRecord] = {
            item["order"]["id"]: OrderRecord.model_validate(item)
            for item in store.load_order_records(exchange_id)
        }

    def record(self, order: Order, protection_verified: bool = False) -> OrderRecord:
        existing = self.records.get(order.id) or self._by_client(order.client_order_id)
        if existing and order.plan_id == "unknown":
            order.plan_id = existing.order.plan_id
        record = OrderRecord(
            exchange_id=self.exchange_id,
            order=order,
            protection_verified=protection_verified
            or bool(existing and existing.protection_verified),
            reconciliation_status=ReconciliationStatus.CONFIRMED,
        )
        self.records[order.id] = record
        self.store.save_order_record(self.exchange_id, record)
        return record

    def apply_event(self, event: PrivateOrderEvent) -> bool:
        record = self.records.get(event.order_id) or self._by_client(event.client_order_id)
        if not record:
            return False
        updates = {
            "status": event.status,
            "updated_at": event.updated_at,
        }
        if event.filled_quantity is not None:
            updates["filled_quantity"] = event.filled_quantity
        if event.average_fill_price is not None:
            updates["average_fill_price"] = event.average_fill_price
        if event.fee is not None:
            updates["fee"] = event.fee
        record.order = record.order.model_copy(update=updates)
        record.reconciliation_status = ReconciliationStatus.CONFIRMED
        record.last_reconciled_at = datetime.now(UTC)
        record.last_error = ""
        self.store.save_order_record(self.exchange_id, record)
        return True

    async def reconcile(
        self,
        exchange: ExchangeAdapter,
        *,
        protected_plan_ids: set[str] | None = None,
    ) -> ReconciliationReport:
        report = ReconciliationReport()
        open_orders = await exchange.open_orders()
        open_ids = set()
        for order in open_orders:
            open_ids.add(order.id)
            existing = self.records.get(order.id) or self._by_client(order.client_order_id)
            if not existing:
                report.unknown_open_order_ids.append(order.id)
                self.record(order, protection_verified=False)
            else:
                self.record(order, protection_verified=existing.protection_verified)
            report.confirmed += 1

        for record in list(self.records.values()):
            if (
                record.order.status
                not in {
                    OrderStatus.PENDING,
                    OrderStatus.OPEN,
                    OrderStatus.PARTIALLY_FILLED,
                }
                or record.order.id in open_ids
            ):
                continue
            try:
                remote = await exchange.fetch_order(record.order.id, record.order.symbol)
            except Exception as exc:
                record.reconciliation_status = ReconciliationStatus.UNCERTAIN
                record.last_reconciled_at = datetime.now(UTC)
                record.last_error = str(exc)[:300]
                self.store.save_order_record(self.exchange_id, record)
                report.uncertain_order_ids.append(record.order.id)
                continue
            self.record(remote, protection_verified=record.protection_verified)
            report.confirmed += 1

        open_position_symbols: set[str] | None = None
        if protected_plan_ids is not None:
            portfolio = await exchange.portfolio()
            open_position_symbols = {
                position.symbol for position in portfolio.open_positions if position.quantity > 0
            }
            attributed_position_symbols = {
                record.order.symbol
                for record in self.records.values()
                if record.order.plan_id in protected_plan_ids
                and record.order.status
                in {
                    OrderStatus.PARTIALLY_FILLED,
                    OrderStatus.FILLED,
                }
            }
            report.unknown_position_symbols = sorted(
                open_position_symbols - attributed_position_symbols
            )

        for record in list(self.records.values()):
            if record.order.status not in {
                OrderStatus.OPEN,
                OrderStatus.PARTIALLY_FILLED,
                OrderStatus.FILLED,
            }:
                continue
            if (
                record.order.status == OrderStatus.FILLED
                and protected_plan_ids is not None
                and (
                    record.order.plan_id not in protected_plan_ids
                    or record.order.symbol not in open_position_symbols
                )
            ):
                continue
            try:
                verified = await exchange.verify_protection(record.order)
            except Exception as exc:
                record.reconciliation_status = ReconciliationStatus.UNCERTAIN
                record.last_reconciled_at = datetime.now(UTC)
                record.last_error = str(exc)[:300]
                self.store.save_order_record(self.exchange_id, record)
                raise RuntimeError(
                    "protection verification is temporarily unavailable for "
                    f"order {record.order.id}: {str(exc)[:180]}"
                ) from exc
            record.protection_verified = verified
            record.reconciliation_status = ReconciliationStatus.CONFIRMED
            record.last_reconciled_at = datetime.now(UTC)
            record.last_error = ""
            self.store.save_order_record(self.exchange_id, record)
            if not verified:
                report.unprotected_order_ids.append(record.order.id)
        return report

    def recent_orders(self, limit: int = 20) -> list[Order]:
        ordered = sorted(
            self.records.values(),
            key=lambda item: item.order.updated_at,
            reverse=True,
        )
        return [record.order for record in ordered[:limit]]

    def _by_client(self, client_order_id: str) -> OrderRecord | None:
        if not client_order_id:
            return None
        return next(
            (
                record
                for record in self.records.values()
                if record.order.client_order_id == client_order_id
            ),
            None,
        )
