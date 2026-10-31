from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial

from .alpha_factory import (
    AlphaModelArtifactV1,
    DevelopmentAuditCertificateV1,
    FullStrategyCertificateV2,
    SandboxAdmissionCertificateV1,
    assess_crypto_5m_contract,
)
from .alpha_runtime import estimate_alpha_round_trip_cost, evaluate_certified_alpha_signal
from .configuration import StrategyKind
from .domain import Candle, PlanStatus, Side, StrategyPlan, TradingMode
from .indicators import atr
from .plan_lifecycle import assess_entry_signal
from .risk import RiskCheck, RiskDecision
from .timeframes import closed_candles

logger = __import__("logging").getLogger(__name__)


class ResearchAdmissionRuntimeService:
    def _certified_alpha_model_trio(
        self,
        admission: SandboxAdmissionCertificateV1,
    ) -> tuple[AlphaModelArtifactV1, AlphaModelArtifactV1, AlphaModelArtifactV1]:
        artifacts: list[AlphaModelArtifactV1] = []
        for model_id in admission.model_artifact_ids:
            payload = self.store.load_alpha_model_artifact(model_id)
            if payload is None:
                raise RuntimeError(f"certified alpha model artifact is missing: {model_id}")
            artifacts.append(AlphaModelArtifactV1.model_validate(payload))
        linear = next(
            (
                item
                for item in artifacts
                if item.model_family in {"elastic_net_regression", "logistic_classification"}
            ),
            None,
        )
        gbdt = next(
            (
                item
                for item in artifacts
                if item.model_family in {"lightgbm_regression", "lightgbm_classification"}
            ),
            None,
        )
        ensemble = next(
            (item for item in artifacts if item.model_family == "ensemble_50_50"),
            None,
        )
        if linear is None or gbdt is None or ensemble is None:
            raise RuntimeError("certified alpha model trio is incomplete")
        if ensemble.fingerprint != admission.artifact_fingerprint:
            raise RuntimeError("certified alpha ensemble fingerprint drifted")
        return linear, gbdt, ensemble

    def _development_audit_for_admission(
        self,
        admission: SandboxAdmissionCertificateV1,
    ) -> DevelopmentAuditCertificateV1:
        if not admission.development_audit_certificate_id:
            raise RuntimeError("sandbox admission predates server-bound development audit evidence")
        status = self.store.load_research_certificate_status(
            admission.development_audit_certificate_id
        )
        if not status or status["certificate_type"] != "development_audit_v1" or status["revoked"]:
            raise RuntimeError("valid DevelopmentAuditCertificateV1 is unavailable")
        audit = DevelopmentAuditCertificateV1.model_validate(status["certificate"])
        if (
            audit.strategy_ir_hash != admission.strategy_ir_hash
            or audit.artifact_fingerprint != admission.artifact_fingerprint
            or audit.data_contract_fingerprint != admission.data_contract_fingerprint
            or set(audit.model_artifact_ids) != set(admission.model_artifact_ids)
            or audit.evidence != admission.audit_evidence
        ):
            raise RuntimeError("sandbox admission development audit binding drifted")
        return audit

    def _alpha_admission_for_plan(self, plan: StrategyPlan) -> SandboxAdmissionCertificateV1:
        admission_id = plan.sandbox_admission_certificate_id
        if not admission_id and plan.full_strategy_certificate_id:
            full_status = self.store.load_research_certificate_status(
                plan.full_strategy_certificate_id
            )
            if (
                not full_status
                or full_status["certificate_type"] != "full_strategy_v2"
                or full_status["revoked"]
            ):
                raise RuntimeError("valid FullStrategyCertificateV2 is unavailable")
            full = FullStrategyCertificateV2.model_validate(full_status["certificate"])
            admission_id = full.sandbox_admission_certificate_id
        status = self.store.load_research_certificate_status(admission_id or "")
        if not status or status["certificate_type"] != "sandbox_admission_v1" or status["revoked"]:
            raise RuntimeError("valid SandboxAdmissionCertificateV1 is unavailable")
        admission = SandboxAdmissionCertificateV1.model_validate(status["certificate"])
        self._development_audit_for_admission(admission)
        return admission

    async def evaluate_certified_alpha_current_signal(
        self,
        admission: SandboxAdmissionCertificateV1,
        history: list[Candle],
        market,
        *,
        exchange: str,
        notional: Decimal,
    ):
        linear, gbdt, ensemble = await asyncio.to_thread(
            self._certified_alpha_model_trio,
            admission,
        )
        required = tuple(
            sorted(
                {
                    feature
                    for item in (linear, gbdt, ensemble)
                    for feature in item.required_data_features
                }
            )
        )
        datasets = await asyncio.to_thread(self.store.list_research_datasets, 2_000)
        contract = assess_crypto_5m_contract(datasets, required_features=required)
        if (
            contract.status != "READY"
            or contract.fingerprint != admission.data_contract_fingerprint
        ):
            raise RuntimeError("certified alpha data contract changed or is no longer READY")
        market_key = f"{exchange}:{market.symbol}"
        readiness = next((item for item in contract.markets if item.market == market_key), None)
        if readiness is None or readiness.status != "READY" or not readiness.dataset_id:
            raise RuntimeError(f"certified alpha market is not READY: {market_key}")
        tail = await asyncio.to_thread(
            partial(
                self.store.load_alpha_runtime_feature_tail,
                readiness.dataset_id,
                limit=5_000,
            )
        )
        completed = closed_candles(history, "5m", market.timestamp)[-4_096:]
        if len(completed) < 55:
            raise RuntimeError("certified alpha runtime requires at least 55 closed 5m candles")
        latest_quote_volume = completed[-1].close * completed[-1].volume
        configuration = self.configuration_service.current_configuration()
        estimated_cost = estimate_alpha_round_trip_cost(
            fee_bps=configuration.fee_bps,
            slippage_bps=configuration.slippage_bps,
            spread_bps=market.spread_bps,
            notional=max(notional, Decimal("0")),
            latest_quote_volume=latest_quote_volume,
        )
        return await asyncio.to_thread(
            partial(
                evaluate_certified_alpha_signal,
                linear,
                gbdt,
                ensemble,
                candles=completed,
                feature_tail=tail,
                snapshot=market,
                estimated_round_trip_cost=estimated_cost,
            )
        )

    def evaluate_current_plan_signal(self, plan: StrategyPlan, history, market):
        configuration = self.configuration_service.current_configuration()
        completed = closed_candles(history, plan.timeframe, market.timestamp)
        if plan.research_profile == "crypto_5m_v1":
            if plan.timeframe != "5m":
                raise RuntimeError("crypto_5m_v1 execution is frozen to the 5m timeframe")

            async def evaluate_alpha():
                admission = await asyncio.to_thread(self._alpha_admission_for_plan, plan)
                signal, _ = await self.evaluate_certified_alpha_current_signal(
                    admission,
                    completed,
                    market,
                    exchange=plan.exchange,
                    notional=plan.sizing.notional,
                )
                return signal

            return evaluate_alpha()
        return self.strategy_factory.evaluate(
            StrategyKind(plan.algorithm),
            candles=completed,
            snapshot=market,
            timeframe=plan.timeframe,
            allow_short=plan.market_type == "swap",
            confirmation_candles=self.confirmation_history(completed, plan.timeframe),
            require_confirmation=self.requires_higher_confirmation(
                configuration, plan.trading_mode
            ),
            strategy_spec=plan.strategy_spec or None,
        )

    async def resolve_current_plan_signal(self, plan: StrategyPlan, history, market):
        result = self.evaluate_current_plan_signal(plan, history, market)
        return await result if inspect.isawaitable(result) else result

    def add_signal_gate(self, decision: RiskDecision, plan: StrategyPlan, signal) -> RiskDecision:
        assessment = assess_entry_signal(plan, signal)
        alpha_requires_current_direction = (
            plan.research_profile == "crypto_5m_v1" and not signal.actionable
        )
        aligned = assessment.execution_allowed and not alpha_requires_current_direction
        check = RiskCheck(
            code="signal_lifecycle",
            passed=aligned,
            blocking=assessment.blocking or alpha_requires_current_direction,
            message=(
                "crypto_5m_v1 当前集成信号为 flat；方向一致、两倍成本优势和七类状态门"
                "必须逐笔同时通过，禁止新增风险。"
                if alpha_requires_current_direction
                else assessment.reason
            ),
        )
        return RiskDecision(
            approved=decision.approved and aligned,
            checks=[*decision.checks, check],
        )

    async def process_sandbox_alpha_handoffs(self) -> int:
        """Materialize at most one current-signal plan for each admitted strategy."""

        configuration = self.configuration_service.current_configuration()
        if configuration.exchange_id != "okx" or configuration.trading_mode != TradingMode.SANDBOX:
            return 0
        handoffs = await asyncio.to_thread(self.store.list_sandbox_admission_handoffs, limit=200)
        retryable = {
            "admitted_waiting_current_signal",
            "queued_until_okx_sandbox",
            "waiting_current_signal",
            "runtime_data_blocked",
            "active_instance_exists",
        }
        created = 0
        for handoff in handoffs:
            if handoff.get("status") not in retryable:
                continue
            certificate_id = str(handoff.get("certificate_id") or "")
            status = await asyncio.to_thread(
                self.store.load_research_certificate_status, certificate_id
            )
            if (
                not status
                or status["certificate_type"] != "sandbox_admission_v1"
                or status["revoked"]
            ):
                continue
            admission = SandboxAdmissionCertificateV1.model_validate(status["certificate"])
            try:
                await asyncio.to_thread(self._development_audit_for_admission, admission)
            except RuntimeError as exc:
                await asyncio.to_thread(
                    self.store.save_sandbox_admission_handoff,
                    admission,
                    status="development_audit_invalid",
                    details={"reason": str(exc)},
                )
                continue
            existing = [
                item
                for item in self.plans.values()
                if item.sandbox_admission_certificate_id == admission.id
                and item.status != PlanStatus.ARCHIVED
            ]
            if existing:
                await asyncio.to_thread(
                    partial(
                        self.store.save_sandbox_admission_handoff,
                        admission,
                        status="active_instance_exists",
                        details={"plan_id": existing[0].id},
                    )
                )
                continue
            try:
                portfolio = await self.runtime.portfolio()
                candidates: list[tuple[Decimal, StrategyPlan, dict]] = []
                symbols = [
                    symbol
                    for symbol in configuration.strategy.watchlist
                    if symbol in {"BTC/USDT", "ETH/USDT", "SOL/USDT"}
                ]
                for symbol in symbols:
                    market, history = await self.runtime.market_context(
                        limit=4_096,
                        symbol=symbol,
                        timeframe="5m",
                    )
                    completed = closed_candles(history, "5m", market.timestamp)
                    if len(completed) < 55:
                        continue
                    volatility = atr(completed, 14)
                    stop_distance = max(volatility * Decimal("1.8"), market.last * Decimal("0.002"))
                    provisional_notional = (
                        portfolio.equity
                        * admission.maximum_risk_per_trade
                        / stop_distance
                        * market.last
                    )
                    signal, inference = await self.evaluate_certified_alpha_current_signal(
                        admission,
                        completed,
                        market,
                        exchange="okx",
                        notional=provisional_notional,
                    )
                    if not signal.actionable:
                        continue
                    if configuration.market_type == "spot" and signal.direction == "short":
                        continue
                    linear, _, _ = await asyncio.to_thread(
                        self._certified_alpha_model_trio, admission
                    )
                    ir_payload = linear.canonical_ir_payload or {}
                    family = str(ir_payload.get("family") or StrategyKind.TREND.value)
                    try:
                        strategy_kind = StrategyKind(family)
                    except ValueError:
                        strategy_kind = StrategyKind.TREND
                    plan = self.strategy_factory.generate_unchecked(
                        strategy_kind,
                        candles=completed,
                        snapshot=market,
                        equity=portfolio.equity,
                        risk_fraction=admission.maximum_risk_per_trade,
                        exchange="okx",
                        timeframe="5m",
                        minimum_reward_risk=configuration.risk.minimum_reward_risk,
                        allow_short=configuration.market_type == "swap",
                        side_override=Side(str(signal.direction)),
                    )
                    plan = plan.model_copy(
                        update={
                            "lineage_id": admission.id,
                            "algorithm": strategy_kind.value,
                            "trading_mode": TradingMode.SANDBOX,
                            "market_type": configuration.market_type,
                            "margin_mode": (
                                str(configuration.margin_mode)
                                if configuration.market_type == "swap"
                                else "cash"
                            ),
                            "leverage": (
                                configuration.contract_leverage
                                if configuration.market_type == "swap"
                                else Decimal("1")
                            ),
                            "status": PlanStatus.ACTIVE,
                            "regime": signal.regime,
                            "confidence": signal.confidence,
                            "signal_candle_at": signal.signal_candle_at,
                            "data_as_of": signal.data_as_of,
                            "strategy_spec": {},
                            "strategy_spec_fingerprint": admission.strategy_ir_hash,
                            "research_profile": "crypto_5m_v1",
                            "alpha_artifact_fingerprint": admission.artifact_fingerprint,
                            "sandbox_admission_certificate_id": admission.id,
                            "validated_strategy_artifact": {
                                "profile": "crypto_5m_v1",
                                "model_artifact_ids": list(admission.model_artifact_ids),
                                "ensemble_artifact_fingerprint": admission.artifact_fingerprint,
                                "data_contract_fingerprint": admission.data_contract_fingerprint,
                                "current_inference": inference.model_dump(mode="json"),
                            },
                            "exit": plan.exit.model_copy(
                                update={"maximum_holding_bars": inference.horizon_bars}
                            ),
                            "reasons": [*signal.reasons, *plan.reasons],
                            "source_prompt": "certified_alpha_runtime_only",
                            "decision_origin": "alpha_factory",
                            "approved_at": datetime.now(UTC),
                        }
                    )
                    plan.approved_fingerprint = plan.fingerprint
                    candidates.append((signal.confidence, plan, inference.model_dump(mode="json")))
                if not candidates:
                    await asyncio.to_thread(
                        partial(
                            self.store.save_sandbox_admission_handoff,
                            admission,
                            status="waiting_current_signal",
                            details={"last_checked_at": datetime.now(UTC).isoformat()},
                        )
                    )
                    continue
                _, plan, inference_payload = max(candidates, key=lambda item: item[0])
                self.plans[plan.id] = plan
                await asyncio.to_thread(self.store.save_strategy, plan)
                await asyncio.to_thread(
                    partial(
                        self.store.save_sandbox_admission_handoff,
                        admission,
                        status="active_instance_exists",
                        details={
                            "plan_id": plan.id,
                            "symbol": plan.symbol,
                            "inference": inference_payload,
                        },
                    )
                )
                self.add_audit(
                    "research",
                    "已认证 crypto_5m_v1 当前信号自动物化为 OKX 模拟盘活动计划",
                    certificate_id=admission.id,
                    plan_id=plan.id,
                    symbol=plan.symbol,
                )
                created += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await asyncio.to_thread(
                    partial(
                        self.store.save_sandbox_admission_handoff,
                        admission,
                        status="runtime_data_blocked",
                        details={
                            "last_checked_at": datetime.now(UTC).isoformat(),
                            "error": str(exc)[:500],
                        },
                    )
                )
        return created
