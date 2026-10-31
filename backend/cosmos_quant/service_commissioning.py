from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from hashlib import sha256
from uuid import uuid4

from fastapi import HTTPException

from cosmos_quant.exchanges.ccxt_adapter import CcxtExchangeAdapter

from .api_contracts import CommissioningRequest, MultiLegCommissioningRequest
from .configuration import CommissioningResult, StrategyKind
from .domain import (
    EntryRule,
    ExitPlan,
    Order,
    OrderIntent,
    OrderSide,
    OrderStatus,
    OrderType,
    PlanStatus,
    PortfolioState,
    PositionSizing,
    PriceZone,
    ProfitTarget,
    Side,
    StrategyPlan,
    TradingMode,
)
from .multi_leg_execution import (
    MultiLegCommissioningResult,
    MultiLegExecutionCoordinator,
    MultiLegExecutionRequest,
    MultiLegExecutionStatus,
    MultiLegOrderSpec,
    build_multi_leg_commissioning_result,
)
from .order_ledger import OrderLedger
from .timeframes import closed_candles

logger = __import__("logging").getLogger(__name__)


class CommissioningService:
    def _multi_leg_scope_exposure(
        self,
        portfolio: PortfolioState,
        symbol: str,
    ) -> Decimal:
        return sum(
            (
                position.quantity if str(position.side) == str(Side.LONG) else -position.quantity
                for position in portfolio.open_positions
                if position.symbol == symbol
            ),
            Decimal("0"),
        )

    async def _sandbox_multi_leg_adapters(
        self,
        request: MultiLegCommissioningRequest,
    ) -> tuple[dict[str, CcxtExchangeAdapter], dict[str, OrderLedger]]:
        configuration = self.configuration_service.current_configuration()
        credentials = self.configuration_service.credentials_for(configuration)
        adapters: dict[str, CcxtExchangeAdapter] = {}
        ledgers: dict[str, OrderLedger] = {}
        for leg in request.legs:
            if leg.adapter_key in adapters:
                continue
            adapter = self.CcxtExchangeAdapter(
                configuration.exchange_id,
                credentials,
                sandbox=True,
                live_enabled=False,
                market_type=leg.market_type,
                margin_mode=str(configuration.margin_mode),
                contract_leverage=configuration.contract_leverage,
                store=self.store,
                account_id=(f"{configuration.exchange_id}:sandbox:{leg.market_type}:multi_leg"),
                proxy_url=configuration.network_proxy_url,
            )
            adapters[leg.adapter_key] = adapter
            ledgers[leg.adapter_key] = OrderLedger(
                self.store,
                f"{configuration.exchange_id}:sandbox:{leg.market_type}:multi_leg",
            )
        return adapters, ledgers

    async def commission_multi_leg(self, request: MultiLegCommissioningRequest):
        if self.multi_leg_commissioning_lock.locked():
            raise HTTPException(409, "multi-leg commissioning is already running")
        async with self.multi_leg_commissioning_lock:
            return await self._commission_multi_leg(request)

    async def _commission_multi_leg(
        self,
        request: MultiLegCommissioningRequest,
    ) -> MultiLegCommissioningResult:
        try:
            request.validate_protocol_shape()
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        configuration = self.configuration_service.current_configuration()
        if configuration.trading_mode != TradingMode.SANDBOX:
            raise HTTPException(409, "multi-leg commissioning can run only in sandbox mode")
        expected = (
            "TEST MULTI LEG "
            + request.strategy_kind.upper()
            + " "
            + ",".join(sorted(leg.symbol for leg in request.legs))
        )
        if request.confirmation != expected:
            raise HTTPException(400, f"confirmation must be exactly: {expected}")
        if self.risk_state_service.get().halted:
            raise HTTPException(409, "global risk halt must be cleared before commissioning")
        credential_fingerprint = self.configuration_service.credential_fingerprint(configuration)
        for market_type in sorted({leg.market_type for leg in request.legs}):
            scoped_configuration = configuration.model_copy(update={"market_type": market_type})
            existing = self.configuration_service.commissioning_for(scoped_configuration)
            if not existing or not existing.passed:
                raise HTTPException(
                    409,
                    f"single-market sandbox commissioning has not passed for {market_type}",
                )
            if existing.credential_fingerprint != credential_fingerprint:
                raise HTTPException(409, f"{market_type} commissioning used other credentials")
            if market_type == "swap" and not existing.agent_position_management_passed:
                raise HTTPException(
                    409,
                    "swap position-management commissioning must pass first",
                )

        adapters, ledgers = await self._sandbox_multi_leg_adapters(request)
        scope_map = {
            leg.id: f"{configuration.exchange_id}:{leg.market_type}:{leg.symbol}"
            for leg in request.legs
        }
        starting_exposure: dict[str, Decimal] = {}
        try:
            for adapter in adapters.values():
                await adapter.validate_account_configuration()
            portfolios = {
                key: portfolio
                for key, portfolio in zip(
                    adapters,
                    await asyncio.gather(*(item.portfolio() for item in adapters.values())),
                    strict=True,
                )
            }
            open_orders_by_adapter = {
                key: orders
                for key, orders in zip(
                    adapters,
                    await asyncio.gather(*(item.open_orders() for item in adapters.values())),
                    strict=True,
                )
            }
            for leg in request.legs:
                scope = scope_map[leg.id]
                starting_exposure[scope] = self._multi_leg_scope_exposure(
                    portfolios[leg.adapter_key],
                    leg.symbol,
                )
            no_preexisting_scope_orders = not any(
                order.symbol == leg.symbol
                for leg in request.legs
                for order in open_orders_by_adapter[leg.adapter_key]
            )
            if any(value != 0 for value in starting_exposure.values()):
                raise HTTPException(
                    409,
                    "commissioning scopes must start flat; existing positions were found",
                )
            if not no_preexisting_scope_orders:
                raise HTTPException(
                    409,
                    "commissioning scopes must have no pre-existing open orders",
                )
            snapshots = await asyncio.gather(
                *(adapters[leg.adapter_key].market_snapshot(leg.symbol) for leg in request.legs)
            )
            quantities = await asyncio.gather(
                *(
                    adapters[leg.adapter_key].minimum_commission_quantity(
                        leg.symbol,
                        snapshot.ask if leg.side == OrderSide.BUY else snapshot.bid,
                    )
                    for leg, snapshot in zip(request.legs, snapshots, strict=True)
                )
            )
            run_id = f"multileg_commission_{uuid4().hex}"
            execution_request = MultiLegExecutionRequest(
                id=run_id,
                strategy_kind=request.strategy_kind,
                campaign_id=f"commissioning_{uuid4().hex}",
                strategy_fingerprint=sha256(run_id.encode()).hexdigest(),
                campaign_certificate_fingerprint=sha256(
                    f"certificate:{run_id}".encode()
                ).hexdigest(),
                blind_evidence_fingerprint=sha256(
                    f"not-research-evidence:{run_id}".encode()
                ).hexdigest(),
                execution_purpose="commissioning",
                trading_mode="sandbox",
                legs=[
                    MultiLegOrderSpec(
                        id=leg.id,
                        adapter_key=leg.adapter_key,
                        exchange=configuration.exchange_id,
                        market_type=leg.market_type,
                        symbol=leg.symbol,
                        side=leg.side,
                        quantity=quantity.quantize(
                            Decimal("0.00000001"),
                            rounding=ROUND_UP,
                        ),
                        compensation_reduce_only=leg.market_type != "spot",
                    )
                    for leg, quantity in zip(request.legs, quantities, strict=True)
                ],
            )
            coordinator = MultiLegExecutionCoordinator(
                self.store,
                adapters,
                risk_state=self.risk_state_service,
                reconciliation_attempts=8,
                reconciliation_delay_seconds=0.5,
                order_sink=lambda adapter_key, order: ledgers[adapter_key].record(
                    order,
                    protection_verified=False,
                ),
            )

            def validate_commissioning(candidate: MultiLegExecutionRequest) -> None:
                if (
                    candidate.execution_purpose != "commissioning"
                    or candidate.fingerprint != execution_request.fingerprint
                ):
                    raise ValueError("commissioning request identity changed")

            entered = await coordinator.execute(
                execution_request,
                validate_certificate=validate_commissioning,
            )
            if entered.status != MultiLegExecutionStatus.HEDGED:
                raise RuntimeError(
                    f"multi-leg sandbox entry did not reach HEDGED: {entered.status}"
                )
            closed = await coordinator.close(
                entered.id,
                validate_certificate=validate_commissioning,
            )
            ending_portfolios = {
                key: portfolio
                for key, portfolio in zip(
                    adapters,
                    await asyncio.gather(*(item.portfolio() for item in adapters.values())),
                    strict=True,
                )
            }
            ending_exposure = {
                scope_map[leg.id]: self._multi_leg_scope_exposure(
                    ending_portfolios[leg.adapter_key],
                    leg.symbol,
                )
                for leg in request.legs
            }
            remaining_orders = await asyncio.gather(
                *(item.open_orders() for item in adapters.values())
            )
            no_remaining_scope_orders = not any(
                order.symbol in {leg.symbol for leg in request.legs}
                for orders in remaining_orders
                for order in orders
            )
            recorded_order_ids = {
                record.order.id for ledger in ledgers.values() for record in ledger.records.values()
            }
            expected_order_ids = {
                order.id
                for state in closed.legs
                for order in (state.entry_order, state.compensation_order)
                if order is not None
            }
            result = build_multi_leg_commissioning_result(
                exchange=configuration.exchange_id,
                credential_fingerprint=credential_fingerprint,
                strategy_kind=request.strategy_kind,
                run=entered,
                closed_run=closed,
                starting_exposure=starting_exposure,
                ending_exposure=ending_exposure,
                no_preexisting_scope_orders=no_preexisting_scope_orders,
                no_remaining_scope_orders=no_remaining_scope_orders,
                order_ledger_recorded=expected_order_ids <= recorded_order_ids,
                message=(
                    "sandbox entry, hedge reconciliation, coordinated close, flat-position "
                    "recheck, and durable order recording completed"
                ),
            )
            if not result.passed:
                self.risk_state_service.halt(
                    "multi-leg commissioning ended without complete flat-state proof",
                    source="multi_leg_commissioning",
                )
            await asyncio.to_thread(self.store.save_multi_leg_commissioning, result)
            return result
        except HTTPException:
            raise
        except Exception as exc:
            self.risk_state_service.halt(
                f"multi-leg commissioning failed; manually reconcile venue: {str(exc)[:180]}",
                source="multi_leg_commissioning",
            )
            self.add_audit(
                "commissioning",
                "multi-leg sandbox commissioning failed",
                severity="critical",
                strategy_kind=request.strategy_kind,
                reason=str(exc)[:240],
            )
            raise HTTPException(502, str(exc)) from exc
        finally:
            await asyncio.gather(
                *(adapter.close() for adapter in adapters.values()),
                return_exceptions=True,
            )

    async def list_multi_leg_commissioning(self):
        payloads = await asyncio.to_thread(self.store.list_multi_leg_commissioning)
        return [MultiLegCommissioningResult.model_validate(payload) for payload in payloads]

    async def commission_protection(self, request: CommissioningRequest):
        configuration = self.configuration_service.current_configuration()
        if configuration.trading_mode != TradingMode.SANDBOX:
            raise HTTPException(409, "原子保护单验收只能在交易所测试网模式执行")
        expected = f"TEST PROTECTED ORDER {configuration.default_symbol}"
        if request.confirmation != expected:
            raise HTTPException(400, f"confirmation must be exactly: {expected}")
        await self.runtime.initialize(force=True)
        execution = self.runtime.execution
        if not isinstance(execution, self.CcxtExchangeAdapter):
            raise HTTPException(409, "当前执行网关不是交易所测试网")
        orders: list[Order] = []
        canceled_ids: set[str] = set()
        try:
            market, history = await self.runtime.market_context()
            strategy_history = closed_candles(
                history,
                configuration.default_timeframe,
                market.timestamp,
            )
            await execution.validate_account_configuration()
            entry_price = market.last * Decimal("0.80")
            stop_price = market.last * Decimal("0.75")
            target_price = market.last * Decimal("1.20")
            quantity = await execution.minimum_commission_quantity(
                configuration.default_symbol, entry_price
            )
            quantity = quantity.quantize(Decimal("0.00000001"), rounding=ROUND_UP)
            risk_amount = (quantity * (entry_price - stop_price)).quantize(
                Decimal("0.00000001"), rounding=ROUND_UP
            )
            notional = (quantity * entry_price).quantize(Decimal("0.00000001"), rounding=ROUND_UP)
            template = self.strategy_factory.generate_unchecked(
                StrategyKind.TREND,
                candles=strategy_history,
                snapshot=market,
                equity=max(configuration.paper_equity, Decimal("100")),
                risk_fraction=Decimal("0.001"),
                exchange=configuration.exchange_id,
                timeframe=configuration.default_timeframe,
                minimum_reward_risk=Decimal("1"),
            )
            plan = template.model_copy(
                update={
                    "id": f"commission-{uuid4().hex}",
                    "name": "交易所原子保护单验收",
                    "side": Side.LONG,
                    "trading_mode": configuration.trading_mode,
                    "market_type": configuration.market_type,
                    "margin_mode": str(configuration.margin_mode),
                    "leverage": configuration.contract_leverage,
                    "status": PlanStatus.ACTIVE,
                    "entry": EntryRule(
                        order_type=OrderType.LIMIT,
                        zone=PriceZone(lower=entry_price, upper=entry_price),
                        trigger="仅用于测试网验收；限价远离市价，验证后立即撤销",
                        expires_at=datetime.now(UTC) + timedelta(minutes=5),
                    ),
                    "exit": ExitPlan(
                        stop_loss=stop_price,
                        take_profits=[
                            ProfitTarget(
                                price=target_price,
                                close_fraction=Decimal("1"),
                                rationale="测试交易所原生止盈保护",
                            )
                        ],
                        invalidation="验收结束立即撤销",
                    ),
                    "sizing": PositionSizing(
                        risk_fraction=Decimal("0.001"),
                        risk_amount=risk_amount,
                        suggested_quantity=quantity,
                        notional=notional,
                    ),
                    "minimum_reward_risk": Decimal("1"),
                    "thesis": "测试网原子保护单协议验收，不属于交易策略信号。",
                    "reasons": ["确认入场、止盈和止损由交易所作为一个受保护结构接受"],
                }
            )
            intent = OrderIntent(
                idempotency_key=f"cxtest{uuid4().hex[:16]}",
                plan_id=plan.id,
                plan_fingerprint=plan.fingerprint,
                symbol=plan.symbol,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                quantity=quantity,
                limit_price=entry_price,
            )
            orders = await execution.place_protected_order(plan, intent)
            for order in orders:
                if not await execution.verify_protection(order):
                    raise RuntimeError(f"订单 {order.id} 未查询到完整止盈止损保护")
            for order in reversed(orders):
                canceled = await execution.cancel_order(order.id, order.symbol)
                if canceled.status != OrderStatus.CANCELED:
                    canceled = await execution.fetch_order(order.id, order.symbol)
                if canceled.status != OrderStatus.CANCELED:
                    raise RuntimeError(
                        f"测试订单 {order.id} 未确认撤销，当前状态 {canceled.status}"
                    )
                canceled_ids.add(order.id)
        except Exception as exc:
            for order in reversed(orders):
                if order.id not in canceled_ids:
                    with suppress(Exception):
                        await execution.cancel_order(order.id, order.symbol)
            self.risk_state_service.halt(
                f"测试网保护单验收失败，必须人工核对交易所挂单：{str(exc)[:160]}",
                source="commissioning",
            )
            self.add_audit(
                "commissioning",
                "测试网原子保护单验收失败",
                severity="critical",
                reason=str(exc)[:180],
            )
            raise HTTPException(502, str(exc)) from exc
        result = CommissioningResult(
            exchange=configuration.exchange_id,
            market_type=configuration.market_type,
            symbol=configuration.default_symbol,
            credential_fingerprint=self.configuration_service.credential_fingerprint(configuration),
            protection_verified=True,
            cancellation_confirmed=len(canceled_ids) == len(orders),
            order_ids=[order.id for order in orders],
            message="测试网原子保护单已由交易所确认，且所有测试订单均已撤销",
        )
        self.store.save_commissioning(
            configuration.exchange_id, str(configuration.market_type), result
        )
        self.add_audit(
            "commissioning",
            "测试网原子保护单验收通过",
            exchange=configuration.exchange_id,
            market_type=str(configuration.market_type),
            order_count=len(orders),
        )
        return result

    async def commission_agent_position_management(self, request: CommissioningRequest):
        if self.agent_commissioning_lock.locked():
            raise HTTPException(409, "Agent 仓位管理验收正在执行，请等待当前验收完成")
        async with self.agent_commissioning_lock:
            return await self._commission_agent_position_management(request)

    async def _commission_agent_position_management(self, request: CommissioningRequest):
        """Exercise an actual OKX Demo fill, partial reduction, and final exit."""

        configuration = self.configuration_service.current_configuration()
        if (
            configuration.trading_mode != TradingMode.SANDBOX
            or configuration.exchange_id != "okx"
            or str(configuration.market_type) != "swap"
        ):
            raise HTTPException(409, "Agent 仓位管理验收只能在 OKX USDT 永续测试网执行")
        expected = f"TEST AGENT REDUCTION {configuration.default_symbol}"
        if request.confirmation != expected:
            raise HTTPException(400, f"confirmation must be exactly: {expected}")
        risk_state = self.risk_state_service.get()
        if risk_state.halted:
            raise HTTPException(
                409,
                "全局交易仍处于 HALT；请先稳定通过对账、人工核对账户并解除停机",
            )
        existing = self.configuration_service.commissioning_for(configuration)
        if not existing or not existing.passed:
            raise HTTPException(409, "请先通过测试网原子保护挂单与撤单验收")
        if existing.credential_fingerprint != self.configuration_service.credential_fingerprint(
            configuration
        ):
            raise HTTPException(409, "当前测试网凭证与原子保护单验收凭证不一致")

        await self.runtime.initialize(force=True)
        execution = self.runtime.execution
        if not isinstance(execution, self.CcxtExchangeAdapter):
            raise HTTPException(409, "当前执行网关不是 OKX 测试网")
        portfolio = await self.runtime.portfolio()
        if any(
            position.symbol == configuration.default_symbol and position.quantity > 0
            for position in portfolio.open_positions
        ):
            raise HTTPException(409, "验收前默认品种必须没有任何已有持仓")
        open_orders = await execution.open_orders()
        if any(order.symbol == configuration.default_symbol for order in open_orders):
            raise HTTPException(409, "验收前默认品种必须没有任何已有普通挂单")

        plan: StrategyPlan | None = None
        submitted_orders: list[Order] = []
        entry_orders: list[Order] = []
        reduction: Order | None = None
        final_exit: Order | None = None
        try:
            market, history = await self.runtime.market_context()
            strategy_history = closed_candles(
                history, configuration.default_timeframe, market.timestamp
            )
            await execution.validate_account_configuration()
            entry_price = market.ask * Decimal("1.01")
            stop_price = market.bid * Decimal("0.95")
            first_target = market.ask * Decimal("1.05")
            second_target = market.ask * Decimal("1.10")
            minimum = await execution.minimum_commission_quantity(
                configuration.default_symbol, entry_price
            )
            quantity = (minimum * Decimal("8")).quantize(Decimal("0.00000001"), rounding=ROUND_UP)
            risk_amount = (quantity * (entry_price - stop_price)).quantize(
                Decimal("0.00000001"), rounding=ROUND_UP
            )
            notional = (quantity * entry_price).quantize(Decimal("0.00000001"), rounding=ROUND_UP)
            template = self.strategy_factory.generate_unchecked(
                StrategyKind.TREND,
                candles=strategy_history,
                snapshot=market,
                equity=max(configuration.paper_equity, Decimal("100")),
                risk_fraction=Decimal("0.001"),
                exchange=configuration.exchange_id,
                timeframe=configuration.default_timeframe,
                minimum_reward_risk=Decimal("1"),
            )
            plan = template.model_copy(
                update={
                    "id": f"agent-commission-{uuid4().hex}",
                    "name": "OKX Agent 仓位管理验收",
                    "side": Side.LONG,
                    "trading_mode": TradingMode.SANDBOX,
                    "market_type": configuration.market_type,
                    "margin_mode": str(configuration.margin_mode),
                    "leverage": configuration.contract_leverage,
                    "status": PlanStatus.ACTIVE,
                    "entry": EntryRule(
                        order_type=OrderType.LIMIT,
                        zone=PriceZone(lower=entry_price, upper=entry_price),
                        trigger="仅用于 OKX Demo 成交后保护单重整验收",
                        expires_at=datetime.now(UTC) + timedelta(minutes=2),
                    ),
                    "exit": ExitPlan(
                        stop_loss=stop_price,
                        take_profits=[
                            ProfitTarget(
                                price=first_target,
                                close_fraction=Decimal("0.5"),
                                rationale="验收第一组附加保护单",
                            ),
                            ProfitTarget(
                                price=second_target,
                                close_fraction=Decimal("0.5"),
                                rationale="验收第二组附加保护单",
                            ),
                        ],
                        invalidation="验收结束后必须完全平仓",
                    ),
                    "sizing": PositionSizing(
                        risk_fraction=Decimal("0.001"),
                        risk_amount=risk_amount,
                        suggested_quantity=quantity,
                        notional=notional,
                    ),
                    "minimum_reward_risk": Decimal("1"),
                    "thesis": "测试网仓位管理协议验收，不属于交易策略信号。",
                    "reasons": ["验证部分减仓、保护单重整、全平和空仓回查"],
                }
            )
            intent = OrderIntent(
                idempotency_key=f"cxagenttest{uuid4().hex[:14]}",
                plan_id=plan.id,
                plan_fingerprint=plan.fingerprint,
                symbol=plan.symbol,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                quantity=quantity,
                limit_price=entry_price,
            )
            submitted_orders = await execution.place_protected_order(plan, intent)

            async def wait_for_fill(order: Order) -> Order:
                latest = order
                for attempt in range(8):
                    if latest.status == OrderStatus.FILLED:
                        return latest
                    if latest.status in {OrderStatus.CANCELED, OrderStatus.REJECTED}:
                        break
                    if attempt < 7:
                        await asyncio.sleep(0.5)
                        latest = await execution.fetch_order(order.id, order.symbol)
                raise RuntimeError(f"测试网入场订单 {order.id} 未完整成交，状态 {latest.status}")

            entry_orders = list(
                await asyncio.gather(*(wait_for_fill(item) for item in submitted_orders))
            )
            current_quantity = sum((order.filled_quantity for order in entry_orders), Decimal("0"))
            if current_quantity <= 0:
                raise RuntimeError("测试网验收没有取得可核验的成交数量")
            partial_quantity = (entry_orders[-1].filled_quantity / Decimal("2")).quantize(
                Decimal("0.00000001"), rounding=ROUND_DOWN
            )
            if partial_quantity <= 0:
                raise RuntimeError("测试仓位不足以执行一次有效的部分减仓")
            reduction = await execution.reduce_position_safely(
                plan,
                entry_orders,
                OrderIntent(
                    idempotency_key=f"cxagentreduce{uuid4().hex[:12]}",
                    plan_id=plan.id,
                    plan_fingerprint=plan.fingerprint,
                    symbol=plan.symbol,
                    side=OrderSide.SELL,
                    order_type=OrderType.MARKET,
                    quantity=partial_quantity,
                    reduce_only=True,
                ),
                current_quantity,
            )
            remaining = current_quantity - reduction.filled_quantity
            if remaining <= 0:
                raise RuntimeError("部分减仓意外关闭了全部测试仓位")
            if not await execution.verify_position_protection(plan, entry_orders, remaining):
                raise RuntimeError("部分减仓后的原生保护数量未通过独立回查")
            final_exit = await execution.reduce_position_safely(
                plan,
                entry_orders,
                OrderIntent(
                    idempotency_key=f"cxagentexit{uuid4().hex[:14]}",
                    plan_id=plan.id,
                    plan_fingerprint=plan.fingerprint,
                    symbol=plan.symbol,
                    side=OrderSide.SELL,
                    order_type=OrderType.MARKET,
                    quantity=remaining,
                    reduce_only=True,
                ),
                remaining,
            )
            final_portfolio = await self.runtime.portfolio()
            position_flat = not any(
                position.symbol == plan.symbol and position.quantity > 0
                for position in final_portfolio.open_positions
            )
            protection_flat = await execution.verify_position_protection(
                plan, entry_orders, Decimal("0")
            )
            if not position_flat or not protection_flat:
                raise RuntimeError("最终空仓或保护单清理未通过独立回查")
        except Exception as exc:
            cleanup_errors: list[str] = []
            for submitted in submitted_orders:
                try:
                    latest = await execution.fetch_order(submitted.id, submitted.symbol)
                    if latest.status in {
                        OrderStatus.OPEN,
                        OrderStatus.PARTIALLY_FILLED,
                        OrderStatus.PENDING,
                    }:
                        await execution.cancel_order(latest.id, latest.symbol)
                except Exception as cleanup_exc:
                    cleanup_errors.append(f"cancel {submitted.id}: {str(cleanup_exc)[:80]}")
            if plan and submitted_orders:
                try:
                    cleanup_portfolio = await self.runtime.portfolio()
                    cleanup_quantity = sum(
                        (
                            position.quantity
                            for position in cleanup_portfolio.open_positions
                            if position.symbol == plan.symbol and position.side == plan.side
                        ),
                        Decimal("0"),
                    )
                    if cleanup_quantity > 0:
                        try:
                            await execution.reduce_position_safely(
                                plan,
                                submitted_orders,
                                OrderIntent(
                                    idempotency_key=f"cxagentcleanup{uuid4().hex[:11]}",
                                    plan_id=plan.id,
                                    plan_fingerprint=plan.fingerprint,
                                    symbol=plan.symbol,
                                    side=OrderSide.SELL,
                                    order_type=OrderType.MARKET,
                                    quantity=cleanup_quantity,
                                    reduce_only=True,
                                ),
                                cleanup_quantity,
                            )
                        except Exception as protected_cleanup_exc:
                            cleanup_errors.append(
                                f"protected cleanup: {str(protected_cleanup_exc)[:100]}"
                            )
                            refreshed = await self.runtime.portfolio()
                            cleanup_quantity = sum(
                                (
                                    position.quantity
                                    for position in refreshed.open_positions
                                    if position.symbol == plan.symbol and position.side == plan.side
                                ),
                                Decimal("0"),
                            )
                            if cleanup_quantity > 0:
                                emergency = await execution.place_order(
                                    OrderIntent(
                                        idempotency_key=(f"cxagentemergency{uuid4().hex[:9]}"),
                                        plan_id=plan.id,
                                        plan_fingerprint=plan.fingerprint,
                                        symbol=plan.symbol,
                                        side=OrderSide.SELL,
                                        order_type=OrderType.MARKET,
                                        quantity=cleanup_quantity,
                                        reduce_only=True,
                                    )
                                )
                                if emergency.status != OrderStatus.FILLED:
                                    emergency = await execution.fetch_order(
                                        emergency.id, emergency.symbol
                                    )
                                if emergency.status != OrderStatus.FILLED:
                                    cleanup_errors.append(
                                        f"emergency exit status: {emergency.status}"
                                    )
                    if not await execution.clear_position_protection(plan, submitted_orders):
                        cleanup_errors.append("native protection cleanup unconfirmed")
                    cleanup_portfolio = await self.runtime.portfolio()
                    if any(
                        position.symbol == plan.symbol and position.quantity > 0
                        for position in cleanup_portfolio.open_positions
                    ):
                        cleanup_errors.append("position cleanup unconfirmed")
                except Exception as cleanup_exc:
                    cleanup_errors.append(f"cleanup failure: {str(cleanup_exc)[:120]}")
            cleanup_status = (
                "；自动清理已确认"
                if not cleanup_errors
                else "；自动清理未完全确认：" + " | ".join(cleanup_errors)[:240]
            )
            self.risk_state_service.halt(
                "Agent 测试网仓位管理验收失败，必须人工核对持仓和 Algo 单："
                f"{str(exc)[:150]}{cleanup_status}",
                source="agent_commissioning",
            )
            self.add_audit(
                "commissioning",
                "Agent 测试网仓位管理验收失败",
                severity="critical",
                reason=str(exc)[:180],
                entry_order_ids=",".join(order.id for order in submitted_orders),
                reduction_order_id=reduction.id if reduction else "",
                exit_order_id=final_exit.id if final_exit else "",
                cleanup_status=cleanup_status,
            )
            raise HTTPException(502, str(exc)) from exc

        result = existing.model_copy(
            update={
                "symbol": configuration.default_symbol,
                "agent_reduction_verified": True,
                "agent_exit_verified": True,
                "position_flat_confirmed": True,
                "order_ids": list(
                    dict.fromkeys(
                        [
                            *existing.order_ids,
                            *(order.id for order in entry_orders),
                            reduction.id,
                            final_exit.id,
                        ]
                    )
                ),
                "message": ("OKX Demo 已确认真实成交、部分减仓保护重整、最终平仓和空仓状态"),
                "tested_at": datetime.now(UTC),
            }
        )
        self.store.save_commissioning(
            configuration.exchange_id, str(configuration.market_type), result
        )
        self.add_audit(
            "commissioning",
            "Agent 测试网仓位管理验收通过",
            exchange=configuration.exchange_id,
            market_type=str(configuration.market_type),
            order_count=len(entry_orders) + 2,
        )
        return result
