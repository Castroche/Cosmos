from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from uuid import uuid4

from pydantic import BaseModel, Field

from ..domain import (
    Candle,
    Fill,
    MarketSnapshot,
    Order,
    OrderIntent,
    OrderSide,
    OrderStatus,
    OrderType,
    PortfolioState,
    Position,
    PositionSide,
    Side,
    StrategyPlan,
    TradingMode,
)
from ..storage import SqliteStore
from .base import ExchangeAdapter, PositionProtectionError

MONEY_QUANTUM = Decimal("0.00000001")


def _money(value: Decimal) -> Decimal:
    """Normalize account values to the precision guaranteed by the domain model."""
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


class PaperPosition(BaseModel):
    symbol: str
    position_side: PositionSide | None = None
    signed_quantity: Decimal = Decimal("0")
    average_entry: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")


class PaperTarget(BaseModel):
    price: Decimal
    close_fraction: Decimal
    triggered: bool = False


class PaperBracket(BaseModel):
    plan_id: str
    symbol: str
    side: Side
    entry_order_id: str
    entry_filled: dict[str, Decimal] = Field(default_factory=dict)
    position_side: PositionSide | None = None
    stop_loss: Decimal
    targets: list[PaperTarget]
    original_quantity: Decimal = Decimal("0")
    remaining_quantity: Decimal = Decimal("0")
    active: bool = False
    completed: bool = False


class PaperAccountState(BaseModel):
    cash: Decimal
    daily_start_equity: Decimal
    orders: list[Order] = Field(default_factory=list)
    positions: list[PaperPosition] = Field(default_factory=list)
    brackets: list[PaperBracket] = Field(default_factory=list)
    idempotency: dict[str, str] = Field(default_factory=dict)
    session_date: date = Field(default_factory=lambda: datetime.now(UTC).date())


class PaperExchange(ExchangeAdapter):
    def __init__(
        self,
        initial_cash: Decimal = Decimal("100000"),
        fee_bps: Decimal = Decimal("6"),
        slippage_bps: Decimal = Decimal("2"),
        market_type: str = "spot",
        margin_mode: str = "cross",
        contract_leverage: Decimal = Decimal("3"),
        store: SqliteStore | None = None,
        account_id: str = "paper:default",
    ):
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.daily_start_equity = initial_cash
        self.fee_bps = fee_bps
        self.slippage_bps = slippage_bps
        self.market_type = market_type
        self.margin_mode = margin_mode
        self.contract_leverage = Decimal(str(contract_leverage))
        self.store = store
        self.account_id = account_id
        self.snapshots: dict[str, MarketSnapshot] = {}
        self.candles: dict[str, list[Candle]] = {}
        self.orders: dict[str, Order] = {}
        self.positions: dict[str, PaperPosition] = {}
        self.brackets: dict[str, PaperBracket] = {}
        self.idempotency: dict[str, str] = {}
        self.session_date = datetime.now(UTC).date()
        self._restore()

    def _restore(self) -> None:
        if not self.store:
            return
        payload = self.store.load_paper_state(self.account_id)
        if not payload:
            return
        state = PaperAccountState.model_validate_json(payload)
        self.cash = state.cash
        self.daily_start_equity = state.daily_start_equity
        self.orders = {order.id: order for order in state.orders}
        self.positions = {
            self._position_key(position.symbol, position.position_side): position
            for position in state.positions
        }
        self.brackets = {bracket.plan_id: bracket for bracket in state.brackets}
        self.idempotency = state.idempotency
        self.session_date = state.session_date

    def _persist(self) -> None:
        if not self.store:
            return
        state = PaperAccountState(
            cash=self.cash,
            daily_start_equity=self.daily_start_equity,
            orders=list(self.orders.values()),
            positions=list(self.positions.values()),
            brackets=list(self.brackets.values()),
            idempotency=self.idempotency,
            session_date=self.session_date,
        )
        self.store.save_paper_state(self.account_id, state.model_dump_json())

    def update_market(self, snapshot: MarketSnapshot) -> None:
        self.snapshots[snapshot.symbol] = snapshot
        self._roll_daily_session()
        self._match_open_orders(snapshot)
        self._process_brackets(snapshot)

    def seed_candles(self, symbol: str, candles: list[Candle]) -> None:
        self.candles[symbol] = candles

    def _roll_daily_session(self) -> None:
        today = datetime.now(UTC).date()
        if self.session_date >= today:
            return
        inventory = Decimal("0")
        unrealized = Decimal("0")
        for position in self.positions.values():
            snapshot = self.snapshots.get(position.symbol)
            mark = snapshot.last if snapshot else position.average_entry
            if self.market_type == "spot":
                inventory += position.signed_quantity * mark
            else:
                unrealized += (mark - position.average_entry) * position.signed_quantity
        self.daily_start_equity = _money(
            self.cash + inventory if self.market_type == "spot" else self.cash + unrealized
        )
        self.session_date = today
        self._persist()

    async def health(self) -> dict[str, str | bool]:
        return {
            "ok": True,
            "mode": "paper",
            "venue": "COSMOS persistent paper exchange",
        }

    async def market_snapshot(self, symbol: str) -> MarketSnapshot:
        if symbol not in self.snapshots:
            raise LookupError(f"no market snapshot for {symbol}")
        return self.snapshots[symbol]

    async def fetch_candles(
        self, symbol: str, timeframe: str = "1h", limit: int = 500
    ) -> list[Candle]:
        del timeframe
        if symbol not in self.candles:
            raise LookupError(f"no candle history for {symbol}")
        return self.candles[symbol][-limit:]

    async def portfolio(self) -> PortfolioState:
        gross_exposure = Decimal("0")
        unrealized_total = Decimal("0")
        position_models: list[Position] = []
        spot_inventory_value = Decimal("0")
        for state in self.positions.values():
            if state.signed_quantity == 0:
                continue
            snapshot = self.snapshots.get(state.symbol)
            mark = snapshot.last if snapshot else state.average_entry
            unrealized = _money((mark - state.average_entry) * state.signed_quantity)
            notional = _money(abs(state.signed_quantity * mark))
            gross_exposure += notional
            unrealized_total += unrealized
            if self.market_type == "spot":
                spot_inventory_value += _money(state.signed_quantity * mark)
            bracket = next(
                (
                    item
                    for item in self.brackets.values()
                    if item.symbol == state.symbol
                    and item.active
                    and not item.completed
                    and item.position_side == state.position_side
                    and (item.side == Side.LONG) == (state.signed_quantity > 0)
                ),
                None,
            )
            next_target = (
                next((target.price for target in bracket.targets if not target.triggered), None)
                if bracket
                else None
            )
            margin_used = (
                _money(notional / self.contract_leverage) if self.market_type == "swap" else None
            )
            liquidation_price = None
            if self.market_type == "swap":
                liquidation_distance = Decimal("0.80") / self.contract_leverage
                liquidation_price = _money(
                    state.average_entry
                    * (
                        Decimal("1") - liquidation_distance
                        if state.signed_quantity > 0
                        else Decimal("1") + liquidation_distance
                    )
                )
            position_models.append(
                Position(
                    symbol=state.symbol,
                    side=Side.LONG if state.signed_quantity > 0 else Side.SHORT,
                    quantity=_money(abs(state.signed_quantity)),
                    average_entry=_money(state.average_entry),
                    mark_price=mark,
                    unrealized_pnl=unrealized,
                    stop_loss=bracket.stop_loss if bracket else None,
                    take_profit=next_target,
                    leverage=(self.contract_leverage if self.market_type == "swap" else None),
                    liquidation_price=liquidation_price,
                    margin_mode=(self.margin_mode if self.market_type == "swap" else None),
                    margin_used=margin_used,
                )
            )
        equity = _money(
            self.cash + spot_inventory_value
            if self.market_type == "spot"
            else self.cash + unrealized_total
        )
        used_margin = _money(
            gross_exposure / self.contract_leverage if self.market_type == "swap" else Decimal("0")
        )
        return PortfolioState(
            equity=equity,
            cash=_money(self.cash),
            daily_start_equity=_money(self.daily_start_equity),
            gross_exposure=_money(gross_exposure),
            available_margin=_money(
                max(equity - used_margin, Decimal("0")) if self.market_type == "swap" else self.cash
            ),
            used_margin=used_margin,
            open_positions=position_models,
        )

    async def place_order(self, intent: OrderIntent) -> Order:
        if intent.quantity <= 0:
            raise ValueError("order quantity must be positive")
        if intent.order_type == OrderType.LIMIT and (
            intent.limit_price is None or intent.limit_price <= 0
        ):
            raise ValueError("limit orders require a positive limit_price")
        if intent.order_type == OrderType.MARKET and intent.limit_price is not None:
            raise ValueError("market orders must not carry limit_price")
        if intent.position_side:
            if self.market_type != "swap":
                raise ValueError("position_side requires a swap account")
            opening_side = (
                OrderSide.BUY if intent.position_side == PositionSide.LONG else OrderSide.SELL
            )
            if (intent.side == opening_side) == intent.reduce_only:
                raise ValueError("side/position_side does not match opening or reducing intent")
        if intent.idempotency_key in self.idempotency:
            previous = self.orders[self.idempotency[intent.idempotency_key]]
            if any(
                getattr(previous, field) != getattr(intent, field)
                for field in (
                    "plan_id",
                    "symbol",
                    "side",
                    "position_side",
                    "order_type",
                    "quantity",
                    "limit_price",
                    "reduce_only",
                )
            ):
                raise ValueError("idempotency key reused for a different order")
            return previous
        snapshot = await self.market_snapshot(intent.symbol)
        order = Order(
            id=f"ord_{uuid4().hex[:12]}",
            client_order_id=intent.idempotency_key,
            plan_id=intent.plan_id,
            symbol=intent.symbol,
            side=intent.side,
            position_side=intent.position_side,
            reduce_only=intent.reduce_only,
            order_type=intent.order_type,
            status=OrderStatus.OPEN,
            quantity=intent.quantity,
            limit_price=intent.limit_price,
            mode=TradingMode.PAPER,
        )
        self.orders[order.id] = order
        self.idempotency[intent.idempotency_key] = order.id
        if intent.order_type == OrderType.MARKET:
            self._fill(order, snapshot, reduce_only=intent.reduce_only)
        else:
            self._match(order, snapshot)
        self._persist()
        return self.orders[order.id]

    async def cancel_order(self, order_id: str, symbol: str) -> Order:
        order = self.orders[order_id]
        if order.symbol != symbol:
            raise ValueError("symbol does not match order")
        if order.status in {OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED}:
            order.status = OrderStatus.CANCELED
            order.updated_at = datetime.now(UTC)
            bracket = self.brackets.get(order.plan_id)
            if bracket and not bracket.active:
                bracket.completed = True
            self._persist()
        return order

    async def open_orders(self) -> list[Order]:
        return [
            order
            for order in self.orders.values()
            if order.status
            in {
                OrderStatus.OPEN,
                OrderStatus.PARTIALLY_FILLED,
            }
        ]

    async def fetch_order(self, order_id: str, symbol: str) -> Order:
        order = self.orders[order_id]
        if order.symbol != symbol:
            raise ValueError("symbol does not match order")
        return order

    async def find_order_by_client_id(
        self,
        client_order_id: str,
        symbol: str,
    ) -> Order | None:
        order_id = self.idempotency.get(client_order_id)
        if order_id is None:
            return None
        order = self.orders[order_id]
        return order if order.symbol == symbol else None

    async def fetch_fills(self, symbol: str, since: datetime, limit: int = 200) -> list[Fill]:
        filled = [
            order
            for order in self.orders.values()
            if order.symbol == symbol
            and order.status == OrderStatus.FILLED
            and order.updated_at >= since
        ]
        filled.sort(key=lambda order: order.updated_at)
        return [
            Fill(
                id=f"fill_{order.id}",
                order_id=order.id,
                client_order_id=order.client_order_id,
                plan_id=order.plan_id,
                symbol=order.symbol,
                side=order.side,
                quantity=order.filled_quantity,
                price=order.average_fill_price or Decimal("0"),
                fee=order.fee,
                fee_currency=order.symbol.split("/", 1)[1].split(":", 1)[0],
                timestamp=order.updated_at,
            )
            for order in filled[-min(max(limit, 1), 1000) :]
        ]

    async def reduce_position_safely(
        self,
        plan: StrategyPlan,
        entry_orders: list[Order],
        intent: OrderIntent,
        current_quantity: Decimal,
    ) -> Order:
        if not intent.reduce_only or intent.order_type != OrderType.MARKET:
            raise PositionProtectionError("position reduction requires a reduce-only market order")
        if intent.quantity > current_quantity:
            raise PositionProtectionError("reduction exceeds the attributed position")
        if intent.idempotency_key in self.idempotency:
            return await self.place_order(intent)
        if entry_orders and intent.position_side is None:
            intent = intent.model_copy(update={"position_side": entry_orders[0].position_side})
        await self.sync_position_protection(plan, entry_orders, current_quantity)
        bracket = self.brackets.get(plan.id)
        if not bracket or not bracket.active or bracket.completed:
            raise PositionProtectionError("paper position has no active protection bracket")
        if abs(bracket.remaining_quantity - current_quantity) > MONEY_QUANTUM:
            raise PositionProtectionError(
                "paper protection quantity does not match the attributed position"
            )
        order = await self.place_order(intent)
        if order.status != OrderStatus.FILLED:
            raise PositionProtectionError(
                f"paper reduce-only order ended in status {order.status}", order
            )
        self.reduce_plan_bracket(plan.id, order.filled_quantity)
        expected = _money(max(current_quantity - order.filled_quantity, Decimal("0")))
        bracket = self.brackets[plan.id]
        if bracket.remaining_quantity != expected:
            raise PositionProtectionError(
                "paper protection did not converge to the remaining position", order
            )
        if expected > 0 and (not bracket.active or bracket.completed):
            raise PositionProtectionError(
                "paper protection became inactive while position remains", order
            )
        if expected == 0 and (bracket.active or not bracket.completed):
            raise PositionProtectionError("paper protection remained active after full exit", order)
        return order

    async def verify_position_protection(
        self,
        plan: StrategyPlan,
        entry_orders: list[Order],
        current_quantity: Decimal,
    ) -> bool:
        if any(
            order.symbol != plan.symbol
            or order.side != (OrderSide.BUY if plan.side == Side.LONG else OrderSide.SELL)
            for order in entry_orders
        ):
            return False
        bracket = self.brackets.get(plan.id)
        if current_quantity <= 0:
            return bool(not bracket or (bracket.completed and not bracket.active))
        return bool(
            bracket
            and bracket.active
            and not bracket.completed
            and abs(bracket.remaining_quantity - current_quantity) <= MONEY_QUANTUM
        )

    async def verify_protection(self, order: Order) -> bool:
        return any(
            (bracket.entry_order_id == order.id or order.id in bracket.entry_filled)
            and (order.filled_quantity == 0 or (bracket.active and not bracket.completed))
            for bracket in self.brackets.values()
        )

    async def protection_available(self, symbol: str, position_side=None) -> bool:
        del symbol, position_side
        return True

    async def place_protected_order(self, plan: StrategyPlan, intent: OrderIntent) -> list[Order]:
        if (
            intent.reduce_only
            or intent.symbol != plan.symbol
            or intent.side != (OrderSide.BUY if plan.side == Side.LONG else OrderSide.SELL)
        ):
            raise ValueError("protected entry must open the plan's symbol and direction")
        order = await self.place_order(intent)
        if order.status == OrderStatus.REJECTED:
            raise RuntimeError("纸面账户拒绝入场订单，未创建保护单")
        await self.attach_bracket(plan, order)
        return [order]

    async def attach_bracket(self, plan: StrategyPlan, entry_order: Order) -> None:
        for bracket in self.brackets.values():
            if entry_order.id == bracket.entry_order_id or entry_order.id in bracket.entry_filled:
                self._activate_bracket_if_filled(bracket)
                self._persist()
                return
        if plan.id in self.brackets:
            bracket = self.brackets[plan.id]
            if bracket.position_side != entry_order.position_side or bracket.side != plan.side:
                raise PositionProtectionError("cannot merge different paper position legs")
            self._activate_bracket_if_filled(bracket)
            bracket.entry_filled[entry_order.id] = Decimal("0")
            self._activate_bracket_if_filled(bracket)
            self._persist()
            return
        self.brackets[plan.id] = PaperBracket(
            plan_id=plan.id,
            symbol=plan.symbol,
            side=plan.side,
            entry_order_id=entry_order.id,
            position_side=entry_order.position_side,
            entry_filled={entry_order.id: Decimal("0")},
            stop_loss=plan.exit.stop_loss,
            targets=[
                PaperTarget(price=target.price, close_fraction=target.close_fraction)
                for target in plan.exit.take_profits
            ],
        )
        self._activate_bracket_if_filled(self.brackets[plan.id])
        self._persist()

    def reduce_plan_bracket(self, plan_id: str, quantity: Decimal) -> None:
        """Keep simulated protection quantity aligned after an explicit reduce-only exit."""

        bracket = self.brackets.get(plan_id)
        if not bracket or bracket.completed:
            return
        bracket.remaining_quantity = _money(
            max(bracket.remaining_quantity - quantity, Decimal("0"))
        )
        if bracket.remaining_quantity <= 0:
            bracket.active = False
            bracket.completed = True
        self._persist()

    def _activate_bracket_if_filled(self, bracket: PaperBracket) -> None:
        if not bracket.entry_filled:
            bracket.entry_filled[bracket.entry_order_id] = (
                bracket.original_quantity if bracket.active or bracket.completed else Decimal("0")
            )
        for entry_id, accounted in list(bracket.entry_filled.items()):
            entry = self.orders.get(entry_id)
            if entry and entry.filled_quantity > accounted:
                delta = entry.filled_quantity - accounted
                bracket.original_quantity += delta
                bracket.remaining_quantity += delta
                bracket.entry_filled[entry_id] = entry.filled_quantity
                bracket.active = True
                bracket.completed = False

    async def rebalance_position_protection(
        self, plan: StrategyPlan, entry_orders: list[Order], current_quantity: Decimal
    ) -> bool:
        if not entry_orders:
            raise PositionProtectionError("paper protection sync requires attributed entry orders")
        side = OrderSide.BUY if plan.side == Side.LONG else OrderSide.SELL
        leg = entry_orders[0].position_side
        if any(
            entry.symbol != plan.symbol
            or entry.side != side
            or entry.position_side != leg
            or entry.reduce_only
            for entry in entry_orders
        ):
            raise PositionProtectionError("paper protection pool mixes symbols or position legs")
        state = self.positions.get(self._position_key(plan.symbol, leg))
        if current_quantity < 0 or current_quantity > abs(state.signed_quantity if state else 0):
            raise PositionProtectionError("paper protection quantity exceeds actual position")
        # Keep all entry identities accounted so subsequent fills add only their delta.
        accounted = {entry.id: entry.filled_quantity for entry in entry_orders}
        for bracket in self.brackets.values():
            if bracket.entry_order_id in accounted or set(bracket.entry_filled) & accounted.keys():
                bracket.remaining_quantity = Decimal("0")
                bracket.active = False
                bracket.completed = True
        self.brackets[plan.id] = PaperBracket(
            plan_id=plan.id,
            symbol=plan.symbol,
            side=plan.side,
            position_side=leg,
            entry_order_id=entry_orders[0].id,
            entry_filled=accounted,
            stop_loss=plan.exit.stop_loss,
            targets=[
                PaperTarget(price=t.price, close_fraction=t.close_fraction)
                for t in plan.exit.take_profits
            ],
            original_quantity=current_quantity,
            remaining_quantity=current_quantity,
            active=current_quantity > 0,
            completed=current_quantity == 0,
        )
        self._persist()
        return True

    async def clear_position_protection(
        self, plan: StrategyPlan, entry_orders: list[Order]
    ) -> bool:
        return await self.sync_position_protection(plan, entry_orders, Decimal("0"))

    @staticmethod
    def _position_key(symbol: str, position_side: PositionSide | None) -> str:
        return f"{symbol}:{position_side.value}" if position_side else symbol

    def _match_open_orders(self, snapshot: MarketSnapshot) -> None:
        changed = False
        for order in list(self.orders.values()):
            if order.status == OrderStatus.OPEN and order.symbol == snapshot.symbol:
                previous = order.status
                self._match(order, snapshot)
                changed = changed or previous != order.status
        for bracket in self.brackets.values():
            self._activate_bracket_if_filled(bracket)
        if changed:
            self._persist()

    def _match(self, order: Order, snapshot: MarketSnapshot) -> None:
        if order.limit_price is None:
            return
        can_fill = (order.side == OrderSide.BUY and snapshot.ask <= order.limit_price) or (
            order.side == OrderSide.SELL and snapshot.bid >= order.limit_price
        )
        if can_fill:
            self._fill(order, snapshot)

    def _fill(
        self,
        order: Order,
        snapshot: MarketSnapshot,
        *,
        reduce_only: bool = False,
    ) -> None:
        multiplier = self.slippage_bps / Decimal("10000")
        raw_fill = (
            snapshot.ask * (Decimal("1") + multiplier)
            if order.side == OrderSide.BUY
            else snapshot.bid * (Decimal("1") - multiplier)
        )
        fill = raw_fill.quantize(Decimal("0.01"))
        if order.order_type == OrderType.LIMIT and order.limit_price is not None:
            fill = (
                min(fill, order.limit_price)
                if order.side == OrderSide.BUY
                else max(fill, order.limit_price)
            )
        notional = _money(fill * order.quantity)
        position_key = self._position_key(order.symbol, order.position_side)
        position = self.positions.get(
            position_key,
            PaperPosition(
                symbol=order.symbol,
                position_side=order.position_side,
            ),
        )
        signed_delta = order.quantity if order.side == OrderSide.BUY else -order.quantity
        if (reduce_only or order.reduce_only) and (
            position.signed_quantity == 0
            or position.signed_quantity * signed_delta >= 0
            or abs(signed_delta) > abs(position.signed_quantity)
        ):
            order.status = OrderStatus.REJECTED
            order.updated_at = datetime.now(UTC)
            return
        if self.market_type == "spot":
            if signed_delta > 0 and notional > self.cash:
                order.status = OrderStatus.REJECTED
                order.updated_at = datetime.now(UTC)
                return
            if signed_delta < 0 and abs(signed_delta) > max(position.signed_quantity, 0):
                order.status = OrderStatus.REJECTED
                order.updated_at = datetime.now(UTC)
                return
        else:
            current_symbol_notional = abs(position.signed_quantity * fill)
            next_symbol_notional = abs((position.signed_quantity + signed_delta) * fill)
            other_notional = sum(
                (
                    abs(state.signed_quantity)
                    * (
                        self.snapshots[state.symbol].last
                        if state.symbol in self.snapshots
                        else state.average_entry
                    )
                    for state in self.positions.values()
                    if self._position_key(state.symbol, state.position_side) != position_key
                ),
                Decimal("0"),
            )
            projected_margin = (other_notional + next_symbol_notional) / self.contract_leverage
            current_unrealized = sum(
                (
                    (
                        self.snapshots[state.symbol].last
                        if state.symbol in self.snapshots
                        else state.average_entry
                    )
                    - state.average_entry
                )
                * state.signed_quantity
                for state in self.positions.values()
            )
            equity = self.cash + current_unrealized
            if projected_margin > equity and next_symbol_notional > current_symbol_notional:
                order.status = OrderStatus.REJECTED
                order.updated_at = datetime.now(UTC)
                return
        realized = self._apply_position_fill(position, signed_delta, fill)
        self.positions[position_key] = position
        fee = _money(abs(notional) * self.fee_bps / Decimal("10000"))
        if self.market_type == "spot":
            self.cash += -notional if signed_delta > 0 else notional
            self.cash -= fee
        else:
            self.cash += realized - fee
        self.cash = _money(self.cash)
        order.status = OrderStatus.FILLED
        order.filled_quantity = order.quantity
        order.average_fill_price = fill
        order.fee = fee
        order.updated_at = datetime.now(UTC)

    @staticmethod
    def _apply_position_fill(
        position: PaperPosition,
        signed_delta: Decimal,
        fill_price: Decimal,
    ) -> Decimal:
        current = position.signed_quantity
        if current == 0 or current * signed_delta > 0:
            total = abs(current) + abs(signed_delta)
            position.average_entry = _money(
                (position.average_entry * abs(current) + fill_price * abs(signed_delta)) / total
            )
            position.signed_quantity = _money(current + signed_delta)
            return Decimal("0")
        closing = min(abs(current), abs(signed_delta))
        direction = Decimal("1") if current > 0 else Decimal("-1")
        realized = _money((fill_price - position.average_entry) * closing * direction)
        new_quantity = _money(current + signed_delta)
        position.realized_pnl = _money(position.realized_pnl + realized)
        position.signed_quantity = new_quantity
        if new_quantity == 0:
            position.average_entry = Decimal("0")
        elif current * new_quantity < 0:
            position.average_entry = fill_price
        return realized

    def _process_brackets(self, snapshot: MarketSnapshot) -> None:
        changed = False
        for bracket in list(self.brackets.values()):
            if bracket.symbol != snapshot.symbol or bracket.completed:
                continue
            self._activate_bracket_if_filled(bracket)
            if not bracket.active:
                continue
            stop_hit = (
                snapshot.bid <= bracket.stop_loss
                if bracket.side == Side.LONG
                else snapshot.ask >= bracket.stop_loss
            )
            if stop_hit and bracket.remaining_quantity > 0:
                self._exit_bracket(bracket, bracket.remaining_quantity, snapshot, "stop")
                bracket.remaining_quantity = Decimal("0")
                bracket.completed = True
                bracket.active = False
                changed = True
                continue
            for target in bracket.targets:
                target_hit = (
                    snapshot.bid >= target.price
                    if bracket.side == Side.LONG
                    else snapshot.ask <= target.price
                )
                if target.triggered or not target_hit or bracket.remaining_quantity <= 0:
                    continue
                pending_targets = [item for item in bracket.targets if not item.triggered]
                quantity = (
                    bracket.remaining_quantity
                    if len(pending_targets) == 1
                    else min(
                        _money(bracket.original_quantity * target.close_fraction),
                        bracket.remaining_quantity,
                    )
                )
                self._exit_bracket(bracket, quantity, snapshot, "target")
                bracket.remaining_quantity = _money(bracket.remaining_quantity - quantity)
                target.triggered = True
                changed = True
            if bracket.remaining_quantity <= 0:
                bracket.completed = True
                bracket.active = False
        if changed:
            self._persist()

    def _exit_bracket(
        self,
        bracket: PaperBracket,
        quantity: Decimal,
        snapshot: MarketSnapshot,
        reason: str,
    ) -> None:
        side = OrderSide.SELL if bracket.side == Side.LONG else OrderSide.BUY
        order = Order(
            id=f"ord_{uuid4().hex[:12]}",
            client_order_id=f"cx_{bracket.plan_id}_{reason}_{uuid4().hex[:6]}",
            plan_id=bracket.plan_id,
            symbol=bracket.symbol,
            side=side,
            position_side=bracket.position_side,
            reduce_only=True,
            order_type=OrderType.MARKET,
            status=OrderStatus.OPEN,
            quantity=quantity,
            mode=TradingMode.PAPER,
        )
        self.orders[order.id] = order
        self._fill(order, snapshot, reduce_only=True)
