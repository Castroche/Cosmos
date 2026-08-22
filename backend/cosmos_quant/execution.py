from __future__ import annotations

import asyncio
from contextlib import suppress
from decimal import Decimal
from hashlib import sha256

from .domain import (
    MarketSnapshot,
    OrderIntent,
    OrderSide,
    OrderType,
    PortfolioState,
    StrategyPlan,
)
from .exchanges.base import ExchangeAdapter
from .risk import RiskCheck, RiskDecision, RiskEngine
from .risk_state import RiskStateService


class ExecutionResult:
    def __init__(self, decision: RiskDecision, orders=None):
        self.decision = decision
        self.orders = orders or []

    @property
    def order(self):
        return self.orders[0] if self.orders else None


class ProtectionVerificationError(RuntimeError):
    def __init__(self, message: str, orders):
        super().__init__(message)
        self.orders = list(orders)


class ExecutionEngine:
    def __init__(
        self,
        exchange: ExchangeAdapter,
        risk: RiskEngine,
        risk_state: RiskStateService | None = None,
    ):
        self.exchange = exchange
        self.risk = risk
        self.risk_state = risk_state

    async def submit_plan(
        self,
        plan: StrategyPlan,
        portfolio: PortfolioState | None = None,
        market: MarketSnapshot | None = None,
    ) -> ExecutionResult:
        market = market or await self.exchange.market_snapshot(plan.symbol)
        portfolio = portfolio or await self.exchange.portfolio()
        decision = self.risk.evaluate(plan, portfolio, market)
        daily_drawdown = next(
            (check for check in decision.checks if check.code == "daily_drawdown"),
            None,
        )
        if daily_drawdown and not daily_drawdown.passed and self.risk_state:
            self.risk_state.halt(daily_drawdown.message, source="daily_drawdown")
        if not decision.approved:
            return ExecutionResult(decision)
        if not await self.exchange.protection_available(plan.symbol):
            decision.checks.append(
                RiskCheck(
                    code="protection_orders",
                    passed=False,
                    message="交易场所尚未验证原生止损/止盈保护，禁止裸仓入场",
                )
            )
            decision.approved = False
            return ExecutionResult(decision)
        raw_key = f"{plan.id}:{plan.fingerprint}:entry:v1"
        intent = OrderIntent(
            idempotency_key=f"cx{sha256(raw_key.encode()).hexdigest()[:20]}",
            plan_id=plan.id,
            plan_fingerprint=plan.fingerprint,
            symbol=plan.symbol,
            side=OrderSide.BUY if plan.side == "long" else OrderSide.SELL,
            order_type=OrderType.LIMIT,
            quantity=plan.sizing.suggested_quantity,
            limit_price=(plan.entry.zone.lower + plan.entry.zone.upper) / Decimal("2"),
        )
        orders = await self.exchange.place_protected_order(plan, intent)
        try:
            for order in orders:
                verified = False
                for attempt in range(3):
                    try:
                        verified = await self.exchange.verify_protection(order)
                    except Exception:
                        if attempt == 2:
                            raise
                    if verified:
                        break
                    if attempt < 2:
                        await asyncio.sleep(0.25 * (attempt + 1))
                if not verified:
                    raise RuntimeError(f"交易所未确认订单 {order.id} 的原生止盈止损保护")
        except Exception as exc:
            for order in reversed(orders):
                with suppress(Exception):
                    await self.exchange.cancel_order(order.id, order.symbol)
            raise ProtectionVerificationError(
                f"保护单提交后复核失败，已尝试撤销全部入场单：{exc}", orders
            ) from exc
        return ExecutionResult(decision, orders)
