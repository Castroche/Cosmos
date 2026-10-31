from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from uuid import uuid4

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from .agent.schemas import TradeEpisodeStatus
from .api_contracts import GenerateRequest, HaltRequest, ResumeRequest
from .backtest import BacktestResult
from .configuration import StrategyKind, UserConfiguration
from .domain import (
    AuditEvent,
    Order,
    OrderStatus,
    PlanStatus,
    PortfolioState,
    PositionSizing,
    Side,
    StrategyPlan,
    TradingMode,
)
from .plan_lifecycle import EntrySignalState, assess_entry_signal
from .research import ResearchDataset
from .research_lifecycle import StrategyCandidate, validate_compiled_champion
from .risk import RiskEngine, RiskLimits
from .signals import NoTradeSignal, score_market_state
from .timeframes import closed_candles
from .v2_position_runtime import V2PositionReconciler

logger = __import__("logging").getLogger(__name__)


class AccountService:
    async def require_local_origin(self, request: Request, call_next):
        path = request.url.path
        if request.method == "OPTIONS":
            return await call_next(request)
        protected = path.startswith("/api/") or path in {
            "/metrics",
            "/docs",
            "/docs/oauth2-redirect",
            "/redoc",
            "/openapi.json",
        }
        if protected and not self.local_access.trusted_request(
            request.headers.get("origin"),
            request.headers.get("sec-fetch-site"),
            str(request.url),
        ):
            return JSONResponse({"detail": "请求来源不受信任"}, status_code=403)
        return await call_next(request)

    def reconciliation_protected_plan_ids(self) -> set[str]:
        plan_ids = {
            plan.id
            for plan in self.plans.values()
            if plan.status
            in {
                PlanStatus.APPROVED,
                PlanStatus.ACTIVE,
                PlanStatus.PAUSED,
            }
        }
        plan_ids.update(
            episode.plan_id
            for episode in self.agent_store.list_episodes(500)
            if episode.status
            in {
                TradeEpisodeStatus.RESERVED,
                TradeEpisodeStatus.PENDING_ENTRY,
                TradeEpisodeStatus.OPEN,
                TradeEpisodeStatus.UNCERTAIN,
                TradeEpisodeStatus.AWAITING_REVIEW,
            }
        )
        configuration = self.configuration_service.current_configuration()
        if (
            self.runtime.order_ledger is not None
            and configuration.exchange_id == "okx"
            and configuration.trading_mode == TradingMode.SANDBOX
        ):
            for position in self.trading_intelligence_store.list_positions():
                plan_ids.add(position.plan_id)
                self.runtime.order_ledger.managed_auxiliary_client_ids.update(
                    V2PositionReconciler.close_client_ids(position)
                )
                for order_id in position.exit_order_ids:
                    artifact = self.trading_intelligence_store.get_artifact(order_id)
                    if not artifact or artifact["artifact_type"] != "ExchangeOrder":
                        continue
                    exit_order = Order.model_validate(artifact["payload"])
                    if exit_order.client_order_id:
                        self.runtime.order_ledger.managed_auxiliary_client_ids.add(
                            exit_order.client_order_id
                        )
                for order_id in position.entry_order_ids:
                    artifact = self.trading_intelligence_store.get_artifact(order_id)
                    if not artifact or artifact["artifact_type"] != "ExchangeOrder":
                        continue
                    order = Order.model_validate(artifact["payload"])
                    if order.plan_id != position.plan_id:
                        order = order.model_copy(update={"plan_id": position.plan_id})
                        self.trading_intelligence_store.save_artifact(
                            order.id,
                            "ExchangeOrder",
                            order,
                        )
                    existing = self.runtime.order_ledger.records.get(order.id)
                    if (
                        existing
                        and existing.order.plan_id == order.plan_id
                        and existing.protection_verified
                    ):
                        continue
                    self.runtime.order_ledger.record(order, protection_verified=True)
        return plan_ids

    def audit_value(self, value) -> str | int | float | bool | None:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        return json.dumps(
            value,
            default=str,
            ensure_ascii=False,
            sort_keys=True,
        )

    def add_audit(self, category: str, message: str, severity: str = "info", **data) -> None:
        event = AuditEvent(
            id=f"evt_{uuid4().hex[:12]}",
            category=category,
            severity=severity,
            message=message,
            data={key: self.audit_value(value) for key, value in data.items()},
        )
        self.audit.insert(0, event)
        del self.audit[100:]
        self.store.append_audit(event)

    def risk_engine(self) -> RiskEngine:
        configuration = self.configuration_service.current_configuration()
        preferences = configuration.risk
        state = self.risk_state_service.get()
        return RiskEngine(
            RiskLimits(
                max_risk_per_trade=preferences.risk_per_trade,
                max_daily_drawdown=preferences.max_daily_drawdown,
                max_gross_exposure=preferences.max_gross_exposure,
                max_portfolio_risk=preferences.max_portfolio_risk,
                existing_position_stress=preferences.existing_position_stress,
                max_directional_exposure=preferences.max_directional_exposure,
                max_leverage=preferences.max_leverage,
                max_symbol_exposure=preferences.max_symbol_exposure,
                max_open_positions=preferences.max_open_positions,
                min_reward_risk=preferences.minimum_reward_risk,
                max_spread_bps=preferences.max_spread_bps,
                max_abs_funding_rate=preferences.max_abs_funding_rate,
                execution_leverage=(
                    configuration.contract_leverage
                    if configuration.market_type == "swap"
                    else Decimal("1")
                ),
                allow_short=(configuration.market_type == "swap"),
                operational_halt=state.halted,
                halt_reason=state.reason,
            )
        )

    def constrain_plan_to_portfolio(
        self,
        plan: StrategyPlan,
        portfolio: PortfolioState,
        configuration: UserConfiguration,
    ) -> StrategyPlan:
        """Shrink a new entry to current exposure and margin capacity before review."""
        entry_reference = (plan.entry.zone.lower + plan.entry.zone.upper) / Decimal("2")
        if entry_reference <= 0 or portfolio.equity <= 0:
            return plan
        existing_symbol_exposure = sum(
            (
                abs(position.quantity * position.mark_price)
                for position in portfolio.open_positions
                if position.symbol == plan.symbol
            ),
            Decimal("0"),
        )
        gross_capacity = max(
            portfolio.equity * configuration.risk.max_gross_exposure - portfolio.gross_exposure,
            Decimal("0"),
        )
        symbol_capacity = max(
            portfolio.equity * configuration.risk.max_symbol_exposure - existing_symbol_exposure,
            Decimal("0"),
        )
        estimated_existing_risk = (
            portfolio.gross_exposure * configuration.risk.existing_position_stress
        )
        portfolio_risk_capacity = max(
            portfolio.equity * configuration.risk.max_portfolio_risk - estimated_existing_risk,
            Decimal("0"),
        )
        risk_scale = (
            min(portfolio_risk_capacity / plan.sizing.risk_amount, Decimal("1"))
            if plan.sizing.risk_amount > 0
            else Decimal("0")
        )
        same_direction_exposure = sum(
            (
                abs(position.quantity * position.mark_price)
                for position in portfolio.open_positions
                if position.side == plan.side
            ),
            Decimal("0"),
        )
        directional_capacity = max(
            portfolio.equity * configuration.risk.max_directional_exposure
            - same_direction_exposure,
            Decimal("0"),
        )
        available = (
            portfolio.available_margin if portfolio.available_margin is not None else portfolio.cash
        )
        leverage = plan.leverage if plan.market_type == "swap" else Decimal("1")
        capital_capacity = max(available * leverage, Decimal("0"))
        capacity = min(
            gross_capacity,
            symbol_capacity,
            capital_capacity,
            directional_capacity,
            plan.sizing.notional * risk_scale,
        )
        if capacity <= 0 or plan.sizing.notional <= capacity:
            return plan
        buffered_capacity = capacity * Decimal("0.98")
        quantity = (buffered_capacity / entry_reference).quantize(
            Decimal("0.000001"), rounding=ROUND_DOWN
        )
        if quantity <= 0:
            return plan
        notional = (quantity * entry_reference).quantize(Decimal("0.01"))
        stop_distance = abs(entry_reference - plan.exit.stop_loss)
        risk_amount = (quantity * stop_distance).quantize(Decimal("0.01"))
        actual_risk_fraction = risk_amount / portfolio.equity
        plan.sizing = PositionSizing(
            risk_fraction=actual_risk_fraction,
            risk_amount=risk_amount,
            suggested_quantity=quantity,
            notional=notional,
        )
        plan.reasons.append(
            f"仓位受当前敞口与可用保证金约束，名义价值收缩至 {notional:f} USDT，"
            f"实际风险为权益的 {actual_risk_fraction * 100:.3f}%"
        )
        return plan

    async def generate_strategy(self, request: GenerateRequest):
        configuration = self.configuration_service.current_configuration()
        if (
            request.strategy_spec
            and request.strategy_spec.get("source") in {"ai", "ml"}
            and not request.research_candidate_id
        ):
            raise HTTPException(
                422,
                "AI/ML StrategySpec 只能进入研究候选库，不能直接生成交易计划",
            )
        compiled_candidate: StrategyCandidate | None = None
        spec_source = request.strategy_spec.get("source") if request.strategy_spec else None
        if spec_source in {"ai", "ml"}:
            payload = await asyncio.to_thread(
                self.store.load_strategy_candidate,
                request.research_candidate_id,
            )
            if not payload:
                raise HTTPException(422, "指定的研究 Champion 不存在。")
            compiled_candidate = self.parse_strategy_candidate_payload(
                payload,
                audit_context="compiled_candidate_plan_generation",
            )
            if compiled_candidate is None:
                raise HTTPException(
                    422,
                    "指定的研究 Champion 使用旧版策略合约，必须重新研究和编译。",
                )
            if compiled_candidate.spec != request.strategy_spec:
                raise HTTPException(422, "请求 StrategySpec 与研究 Champion 不一致。")
            dataset_payload = (
                await asyncio.to_thread(
                    self.store.load_research_dataset,
                    compiled_candidate.dataset_id,
                )
                if compiled_candidate.dataset_id
                else None
            )
            if not dataset_payload or compiled_candidate.execution_artifact is None:
                raise HTTPException(422, "研究 Champion 尚未形成可执行验证工件。")
            dataset = ResearchDataset.model_validate(dataset_payload)
            try:
                validate_compiled_champion(
                    compiled_candidate,
                    compiled_candidate.execution_artifact,
                    dataset_id=dataset.id,
                    data_fingerprint=dataset.data_fingerprint,
                )
            except ValueError as exc:
                raise HTTPException(422, f"研究 Champion 验证失败：{exc}") from exc
        elif request.research_candidate_id:
            raise HTTPException(422, "只有 AI/ML Champion 需要研究执行工件。")
        requested_symbol = request.symbol or configuration.default_symbol
        requested_timeframe = request.timeframe or configuration.default_timeframe
        try:
            market, history = await self.runtime.market_context(
                limit=configuration.strategy.research_bars,
                symbol=requested_symbol,
                timeframe=requested_timeframe,
            )
            portfolio = await self.runtime.portfolio()
        except Exception as exc:
            raise HTTPException(503, f"无法基于真实行情生成策略：{exc}") from exc
        strategy_history = closed_candles(
            history,
            requested_timeframe,
            market.timestamp,
        )
        if len(strategy_history) < 55:
            raise HTTPException(409, "已收盘 K 线不足 55 根，不能生成策略")
        try:
            plan = self.strategy_factory.generate(
                request.strategy_kind,
                candles=strategy_history,
                snapshot=market,
                equity=self.strategy_sizing_equity(configuration, portfolio, request.equity),
                risk_fraction=request.risk_fraction or configuration.risk.risk_per_trade,
                exchange=configuration.exchange_id,
                timeframe=requested_timeframe,
                minimum_reward_risk=configuration.risk.minimum_reward_risk,
                allow_short=configuration.market_type == "swap",
                confirmation_candles=self.confirmation_history(
                    strategy_history, requested_timeframe
                ),
                require_confirmation=self.requires_higher_confirmation(configuration),
                strategy_spec=request.strategy_spec,
            )
        except NoTradeSignal as exc:
            raise HTTPException(
                409,
                detail={
                    "code": "no_trade",
                    "message": "当前市场条件未达到策略门槛，系统建议观望",
                    "signal": exc.signal.model_dump(mode="json"),
                },
            ) from exc
        except ValueError as exc:
            raise HTTPException(422, f"策略规格无效：{exc}") from exc
        parent = self.plans.get(request.revision_of) if request.revision_of else None
        if request.revision_of and not parent:
            raise HTTPException(404, "要修订的策略版本不存在")
        plan.lineage_id = parent.lineage_id or parent.id if parent else plan.id
        plan.supersedes_plan_id = parent.id if parent else None
        plan.version = parent.version + 1 if parent else 1
        plan.algorithm = request.strategy_kind.value
        if compiled_candidate is not None:
            plan.research_candidate_id = compiled_candidate.id
            plan.validated_strategy_artifact = compiled_candidate.execution_artifact.model_dump(
                mode="json"
            )
            plan.reasons.append(
                "采用已通过不可变数据集、稳健性与漂移验证的 AI/ML Champion；"
                f"候选 {compiled_candidate.id}，执行工件 "
                f"{compiled_candidate.execution_artifact.fingerprint}。"
            )
        plan.trading_mode = configuration.trading_mode
        plan.market_type = configuration.market_type
        plan.margin_mode = (
            str(configuration.margin_mode) if configuration.market_type == "swap" else "cash"
        )
        plan.leverage = (
            configuration.contract_leverage if configuration.market_type == "swap" else Decimal("1")
        )
        if configuration.market_type == "swap":
            plan.reasons.append(
                f"USDT 永续采用 {plan.leverage:f}x "
                f"{'全仓' if plan.margin_mode == 'cross' else '逐仓'}保证金；"
                "仓位按交易所账户权益和止损距离计算"
            )
        plan = self.constrain_plan_to_portfolio(plan, portfolio, configuration)
        plan.source_prompt = request.prompt.strip()
        plan.thesis = f"{request.prompt.strip()} {plan.thesis}".strip()
        plan.minimum_reward_risk = configuration.risk.minimum_reward_risk
        self.plans[plan.id] = plan
        self.store.save_strategy(plan)
        self.add_audit("strategy", "已基于真实行情生成新策略", plan_id=plan.id)
        return plan

    async def cancel_order(self, order_id: str):
        """Cancel a tracked working order without changing any open position."""

        await self.runtime.initialize()
        if not self.runtime.execution or not self.runtime.order_ledger:
            raise HTTPException(503, "订单网关尚未就绪")
        record = self.runtime.order_ledger.records.get(order_id)
        if not record:
            raise HTTPException(404, "订单不在当前交易环境的受控账本中")
        if record.order.status not in {
            OrderStatus.PENDING,
            OrderStatus.OPEN,
            OrderStatus.PARTIALLY_FILLED,
        }:
            raise HTTPException(409, f"订单当前状态 {record.order.status} 不允许撤销")
        try:
            canceled = await self.runtime.execution.cancel_order(
                record.order.id, record.order.symbol
            )
            if canceled.status != OrderStatus.CANCELED:
                canceled = await self.runtime.execution.fetch_order(
                    record.order.id, record.order.symbol
                )
            if canceled.status != OrderStatus.CANCELED:
                raise RuntimeError(f"交易所返回状态 {canceled.status}，撤单尚未确认")
        except Exception as exc:
            self.add_audit(
                "execution",
                "人工撤单失败",
                severity="warning",
                order_id=record.order.id,
                reason=str(exc)[:180],
            )
            raise HTTPException(502, f"交易所撤单失败：{exc}") from exc
        self.runtime.order_ledger.record(canceled, protection_verified=record.protection_verified)
        self.add_audit(
            "execution",
            "操作者已撤销未成交订单",
            order_id=canceled.id,
            symbol=canceled.symbol,
        )
        return canceled

    async def backtest_strategy(
        self,
        plan_id: str,
        research_trial_count: int = 1,
    ):
        plan = self.plans.get(plan_id)
        if not plan:
            raise HTTPException(404, "strategy plan not found")
        configuration = self.configuration_service.current_configuration()
        try:
            market, history = await self.runtime.market_context(
                limit=self.effective_research_bars(
                    configuration.strategy.research_bars,
                    plan.timeframe,
                ),
                symbol=plan.symbol,
                timeframe=plan.timeframe,
            )
        except Exception as exc:
            raise HTTPException(503, f"真实历史行情不可用：{exc}") from exc
        history = closed_candles(history, plan.timeframe, market.timestamp)
        (
            dataset,
            funding_rates,
            open_interest,
            positioning,
            basis,
        ) = await self.materialize_research_dataset(
            history,
            exchange=plan.exchange,
            market_type=plan.market_type,
            symbol=plan.symbol,
            timeframe=plan.timeframe,
        )
        result = await self.execute_research_backtest(
            dataset,
            history,
            initial_equity=(plan.sizing.risk_amount / plan.sizing.risk_fraction),
            fee_bps=configuration.fee_bps,
            slippage_bps=configuration.slippage_bps,
            strategy_kind=StrategyKind(plan.algorithm),
            symbol=plan.symbol,
            timeframe=plan.timeframe,
            exchange=plan.exchange,
            risk_fraction=plan.sizing.risk_fraction,
            minimum_reward_risk=plan.minimum_reward_risk,
            allow_short=plan.market_type == "swap",
            require_confirmation=self.requires_higher_confirmation(
                configuration, plan.trading_mode
            ),
            validation=configuration.strategy.validation,
            strategy_spec=plan.strategy_spec or None,
            funding_rates=funding_rates,
            open_interest=open_interest,
            positioning=positioning,
            basis=basis,
            side_filter=Side(plan.side),
            research_trial_count=max(research_trial_count, 1),
        )
        result.strategy_fingerprint = plan.fingerprint
        self.store.save_backtest(plan.id, result)
        self.add_audit("backtest", "回测已完成", plan_id=plan.id, trades=result.total_trades)
        return result

    async def latest_strategy_backtest(self, plan_id: str):
        plan = self.plans.get(plan_id)
        if not plan:
            raise HTTPException(404, "strategy plan not found")
        payload = self.store.load_latest_backtest(plan.id)
        if not payload:
            return None
        artifact = BacktestResult.model_validate(payload)
        if artifact.strategy_fingerprint != plan.fingerprint:
            return None
        if artifact.cost_model.get("direction_filter") != str(plan.side):
            return None
        return artifact

    async def strategy_market_fit(self, plan_id: str):
        """Explain whether a fixed strategy still fits the latest closed market data."""

        plan = self.plans.get(plan_id)
        if not plan:
            raise HTTPException(404, "strategy plan not found")
        configuration = self.configuration_service.current_configuration()
        try:
            market, history = await self.runtime.market_context(
                limit=min(configuration.strategy.research_bars, 500),
                symbol=plan.symbol,
                timeframe=plan.timeframe,
            )
        except Exception as exc:
            raise HTTPException(503, f"最新市场适配检查所需行情不可用：{exc}") from exc
        completed = closed_candles(history, plan.timeframe, market.timestamp)
        assessment = score_market_state(completed)
        signal = await self.resolve_current_plan_signal(plan, completed, market)
        lifecycle = assess_entry_signal(plan, signal)
        direction_matches = signal.actionable and str(signal.direction) == str(plan.side)
        family_matches = assessment.selected_kind.value == plan.algorithm
        if direction_matches and family_matches and not assessment.ambiguous:
            verdict = "aligned"
            summary = "策略家族、方向与最新已收盘行情一致，可继续交由风险闸门判断"
        elif lifecycle.state == EntrySignalState.INVALIDATED:
            verdict = "conflict"
            summary = "最新确定性信号与计划方向冲突，不应执行该版本"
        else:
            verdict = "expired" if lifecycle.state == EntrySignalState.EXPIRED else "degraded"
            summary = "最新行情尚未形成与该计划一致的清晰信号，保持等待"
        quality = self.runtime.market_quality
        return {
            "plan_id": plan.id,
            "strategy_fingerprint": plan.fingerprint,
            "symbol": plan.symbol,
            "timeframe": plan.timeframe,
            "checked_at": datetime.now(UTC),
            "data_as_of": market.timestamp,
            "verdict": verdict,
            "summary": summary,
            "entry_lifecycle": lifecycle,
            "direction_matches": direction_matches,
            "family_matches": family_matches,
            "signal": signal,
            "assessment": assessment,
            "market_quality": quality,
        }

    async def current_risk(self):
        try:
            market, _history = await self.runtime.market_context()
            portfolio: PortfolioState = await self.runtime.portfolio()
        except Exception as exc:
            raise HTTPException(503, f"风险检查所需数据不可用：{exc}") from exc
        return self.risk_engine().evaluate(None, portfolio, market)

    async def get_risk_state(self):
        return self.risk_state_service.get()

    async def halt_trading(self, request: HaltRequest):
        state = self.risk_state_service.halt(request.reason)
        # New submissions now fail their generation check. Drain the one already
        # admitted to the exchange boundary before acknowledging the stop.
        async with self.v2_submission_lock:
            pass
        self.add_audit("risk", "已触发全局交易停机", severity="critical", reason=state.reason)
        return state

    async def resume_trading(self, request: ResumeRequest):
        try:
            state = self.risk_state_service.resume(request.confirmation)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        self.add_audit("risk", "已人工恢复新开仓权限", severity="warning")
        return state
