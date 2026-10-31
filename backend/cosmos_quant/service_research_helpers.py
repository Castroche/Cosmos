from __future__ import annotations

import asyncio
import json
from datetime import datetime
from decimal import Decimal
from hashlib import sha256

from fastapi import HTTPException
from pydantic import ValidationError

from .api_contracts import (
    CausalPolicyBacktestRequest,
    CausalPolicyCampaignEntry,
    CausalPolicyCampaignSpec,
    ResearchDatasetBundle,
)
from .backtest import BacktestResult, CausalStrategyPrior, causal_library_policy_fingerprint
from .configuration import UserConfiguration
from .domain import (
    BasisPoint,
    Candle,
    FundingRatePoint,
    LiquidationFlowPoint,
    OpenInterestPoint,
    OrderFlowBar,
    PortfolioState,
    PositioningPoint,
    Side,
    StrategyPlan,
    TradingMode,
)
from .indicators import atr
from .research import ResearchDataset, verify_research_dataset
from .research_campaign import campaign_candidate_fingerprint
from .research_lifecycle import StrategyCandidate
from .signals import select_strategy_kind
from .strategy_library import library_entry
from .timeframes import (
    closed_candles,
    confirmation_timeframe,
    periods_per_year,
    resample_closed_candles,
)

logger = __import__("logging").getLogger(__name__)


class ResearchHelpersService:
    def parse_strategy_candidate_payload(
        self,
        payload: dict,
        *,
        audit_context: str,
        emit_audit: bool = True,
    ) -> StrategyCandidate | None:
        """Fail closed on stale immutable candidates without breaking the pool.

        StrategySpec defaults can evolve between application versions. Persisted
        candidates keep their original fingerprint and must never be silently
        reinterpreted under a newer contract. They are isolated from execution and
        research selection while the remaining valid candidates continue to work.
        """

        try:
            return StrategyCandidate.model_validate(payload)
        except ValidationError as exc:
            candidate_id = str(payload.get("id") or "unknown")
            if emit_audit and candidate_id not in self.stale_candidate_audit_ids:
                self.stale_candidate_audit_ids.add(candidate_id)
                self.add_audit(
                    "research",
                    "旧版策略候选与当前 StrategySpec 合约不兼容，已失效隔离",
                    severity="warning",
                    candidate_id=candidate_id,
                    context=audit_context,
                    error=str(exc)[:500],
                )
            return None

    def agent_trailing_state(
        self,
        plan: StrategyPlan,
        history: list[Candle],
        market_timestamp: datetime,
        market_price: Decimal,
        entry_time: datetime,
        entry_price: Decimal,
    ) -> dict:
        """Evaluate the executable, closed-bar Agent-managed trailing stop.

        The exchange-native hard stop remains active throughout. This soft trailing
        level may only request a safe full exit after its declared activation rule.
        """

        state = {
            "configured": bool(
                plan.exit.trailing_stop_atr is not None or plan.exit.break_even_after_first_target
            ),
            "activation": str(plan.exit.trailing_activation),
            "active": False,
            "break_even_active": False,
            "history_complete": False,
            "targets_reached": 0,
            "favorable_price": None,
            "stop_price": None,
            "breached": False,
        }
        if not state["configured"]:
            return state
        closed = closed_candles(history, plan.timeframe, market_timestamp)
        eligible = [candle for candle in closed if candle.timestamp >= entry_time]
        if not eligible:
            return state
        state["history_complete"] = entry_time >= history[0].timestamp
        if not state["history_complete"]:
            return state
        favorable_price = (
            max(candle.high for candle in eligible)
            if plan.side == Side.LONG
            else min(candle.low for candle in eligible)
        )
        targets_reached = sum(
            1
            for target in plan.exit.take_profits
            if (
                favorable_price >= target.price
                if plan.side == Side.LONG
                else favorable_price <= target.price
            )
        )
        favorable_move = (
            favorable_price - entry_price
            if plan.side == Side.LONG
            else entry_price - favorable_price
        )
        initial_risk = abs(entry_price - plan.exit.stop_loss)
        trailing_active = bool(
            plan.exit.trailing_stop_atr is not None
            and len(eligible) >= 15
            and plan.exit.trailing_is_active(
                targets_reached=targets_reached,
                favorable_move=max(favorable_move, Decimal("0")),
                initial_risk=initial_risk,
            )
        )
        break_even_active = bool(plan.exit.break_even_after_first_target and targets_reached >= 1)
        active = trailing_active or break_even_active
        state.update(
            {
                "active": active,
                "break_even_active": break_even_active,
                "targets_reached": targets_reached,
                "favorable_price": str(favorable_price),
            }
        )
        if not active:
            return state
        stop_price = plan.exit.stop_loss
        if break_even_active:
            stop_price = (
                max(stop_price, entry_price)
                if plan.side == Side.LONG
                else min(stop_price, entry_price)
            )
        if trailing_active:
            assert plan.exit.trailing_stop_atr is not None
            trail_distance = atr(eligible[-15:], 14) * plan.exit.trailing_stop_atr
            stop_price = (
                max(stop_price, favorable_price - trail_distance)
                if plan.side == Side.LONG
                else min(stop_price, favorable_price + trail_distance)
            )
        breached = (
            market_price <= stop_price if plan.side == Side.LONG else market_price >= stop_price
        )
        state.update(
            {
                "stop_price": str(stop_price),
                "breached": breached,
            }
        )
        return state

    def effective_research_bars(self, configured_bars: int, timeframe: str) -> int:
        """Give 1h/4h research at least one year and permit multi-year generations."""

        if timeframe not in {"1h", "4h"}:
            return configured_bars
        return min(
            50000,
            max(configured_bars, int(periods_per_year(timeframe)) + 1),
        )

    def confirmation_history(self, history, timeframe: str):
        target = confirmation_timeframe(timeframe)
        if target == timeframe:
            return None
        return resample_closed_candles(history, timeframe, target)

    def configured_strategy_kind(self, configuration: UserConfiguration, history):
        if configuration.strategy.automatic_regime_selection:
            return select_strategy_kind(history)
        return configuration.strategy.default_kind

    def requires_higher_confirmation(
        self,
        configuration: UserConfiguration,
        trading_mode: TradingMode | str | None = None,
    ) -> bool:
        mode = TradingMode(trading_mode or configuration.trading_mode)
        return (
            configuration.strategy.validation.require_higher_timeframe_confirmation
            and mode != TradingMode.PAPER
        )

    def research_universe_request_fingerprint(self, request_payload: dict) -> str:
        encoded = json.dumps(
            request_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(encoded).hexdigest()[:20]

    def _validate_sealed_blind_scope(
        self,
        development: list[ResearchDataset],
        blind: list[ResearchDataset],
    ) -> str | None:
        """Validate one chronological, source-matched, one-time holdout contract."""

        if not blind:
            if any(item.research_role == "blind" or item.blind_sealed for item in development):
                raise ValueError("sealed blind datasets cannot be used for strategy selection")
            return None
        if any(
            item.research_role != "blind"
            or not item.blind_sealed
            or not item.blind_set_id
            or not item.source_dataset_id
            for item in blind
        ):
            raise ValueError("blind datasets must be source-bound, fingerprinted and sealed")
        blind_set_ids = {item.blind_set_id for item in blind}
        if len(blind_set_ids) != 1:
            raise ValueError("blind datasets must belong to one sealed blind set")
        blind_set_id = next(iter(blind_set_ids))
        if any(
            item.research_role != "development"
            or item.blind_sealed
            or item.blind_set_id != blind_set_id
            or not item.source_dataset_id
            for item in development
        ):
            raise ValueError(
                "blind evaluation requires development splits from the same sealed set"
            )
        development_by_source = {item.source_dataset_id: item for item in development}
        blind_by_source = {item.source_dataset_id: item for item in blind}
        if (
            len(development_by_source) != len(development)
            or len(blind_by_source) != len(blind)
            or set(development_by_source) != set(blind_by_source)
        ):
            raise ValueError(
                "development and blind datasets must contain one matching split per source market"
            )
        for source_id, development_item in development_by_source.items():
            blind_item = blind_by_source[source_id]
            development_identity = (
                development_item.exchange,
                development_item.symbol,
                development_item.timeframe,
                development_item.market_type,
            )
            blind_identity = (
                blind_item.exchange,
                blind_item.symbol,
                blind_item.timeframe,
                blind_item.market_type,
            )
            if development_identity != blind_identity:
                raise ValueError("development and blind market identities do not match")
            if (
                development_item.split_boundary is None
                or development_item.split_boundary != blind_item.split_boundary
                or development_item.embargo_bars != blind_item.embargo_bars
                or development_item.data_end >= blind_item.data_start
                or blind_item.data_start < blind_item.split_boundary
            ):
                raise ValueError("development/blind chronological split contract is invalid")
        return str(blind_set_id)

    def _sealed_blind_evidence_fingerprint(
        self,
        *,
        strategy_fingerprint: str,
        candidate,
        datasets: list[ResearchDataset],
    ) -> str:
        payload = {
            "disclosure": "pass-fail-only-v1",
            "strategy_fingerprint": strategy_fingerprint,
            "candidate_aggregate_fingerprint": campaign_candidate_fingerprint(candidate),
            "blind_dataset_fingerprints": sorted(item.data_fingerprint for item in datasets),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return sha256(encoded).hexdigest()

    def strategy_sizing_equity(
        self,
        configuration: UserConfiguration,
        portfolio: PortfolioState,
        requested_equity: Decimal | None,
    ) -> Decimal:
        """Only paper trading may use an operator-supplied hypothetical equity."""
        if configuration.trading_mode == TradingMode.PAPER:
            return requested_equity or configuration.paper_equity
        return portfolio.equity

    async def load_verified_research_dataset(self, dataset_id: str) -> ResearchDatasetBundle:
        dataset_payload = await asyncio.to_thread(
            self.store.load_research_dataset,
            dataset_id,
        )
        if not dataset_payload:
            raise HTTPException(404, f"research dataset not found: {dataset_id}")
        dataset = ResearchDataset.model_validate(dataset_payload)
        (
            candle_payloads,
            funding_payloads,
            open_interest_payloads,
            positioning_payloads,
            basis_payloads,
            order_flow_payloads,
            liquidation_flow_payloads,
        ) = await asyncio.gather(
            asyncio.to_thread(self.store.load_research_dataset_candles, dataset_id),
            asyncio.to_thread(self.store.load_research_dataset_funding, dataset_id),
            asyncio.to_thread(self.store.load_research_dataset_open_interest, dataset_id),
            asyncio.to_thread(self.store.load_research_dataset_positioning, dataset_id),
            asyncio.to_thread(self.store.load_research_dataset_basis, dataset_id),
            asyncio.to_thread(self.store.load_research_dataset_order_flow, dataset_id),
            asyncio.to_thread(self.store.load_research_dataset_liquidation_flow, dataset_id),
        )
        candles = [Candle.model_validate(item) for item in candle_payloads]
        funding = [FundingRatePoint.model_validate(item) for item in funding_payloads]
        open_interest = [OpenInterestPoint.model_validate(item) for item in open_interest_payloads]
        positioning = [PositioningPoint.model_validate(item) for item in positioning_payloads]
        basis = [BasisPoint.model_validate(item) for item in basis_payloads]
        order_flow = [OrderFlowBar.model_validate(item) for item in order_flow_payloads]
        liquidation_flow = [
            LiquidationFlowPoint.model_validate(item) for item in liquidation_flow_payloads
        ]
        verification = verify_research_dataset(
            dataset,
            candles,
            funding,
            open_interest,
            positioning,
            basis,
            order_flow,
            liquidation_flow,
        )
        if not verification["verified"]:
            raise HTTPException(409, f"research dataset integrity failed: {dataset_id}")
        return (
            dataset,
            candles,
            funding,
            open_interest,
            positioning,
            basis,
            order_flow,
            liquidation_flow,
        )

    def causal_strategy_prior_from_library_entry(self, entry) -> CausalStrategyPrior:
        return CausalStrategyPrior(
            id=entry.id,
            family=entry.family,
            archetype=entry.archetype,
            preferred_regimes=entry.preferred_regimes,
            avoid_regimes=entry.avoid_regimes,
            required_data=entry.required_data,
            spec=entry.spec,
            spec_fingerprint=entry.validated_spec.fingerprint,
        )

    def build_causal_policy_campaign_entry(
        self,
        policy: CausalPolicyCampaignSpec,
    ) -> CausalPolicyCampaignEntry:
        entries = [library_entry(entry_id) for entry_id in policy.library_entry_ids]
        priors = tuple(self.causal_strategy_prior_from_library_entry(entry) for entry in entries)
        policy_fingerprint = causal_library_policy_fingerprint(
            list(priors),
            maximum_candidates_per_bar=policy.maximum_candidates_per_bar,
            abstain_on_ambiguous_regime=policy.abstain_on_ambiguous_regime,
            minimum_regime_confidence=policy.minimum_regime_confidence,
            minimum_signal_confidence=policy.minimum_signal_confidence,
            minimum_directional_consensus=policy.minimum_directional_consensus,
        )
        return CausalPolicyCampaignEntry(
            id=f"causal_policy:{policy.id}",
            policy_id=policy.id,
            priors=priors,
            maximum_candidates_per_bar=policy.maximum_candidates_per_bar,
            abstain_on_ambiguous_regime=policy.abstain_on_ambiguous_regime,
            minimum_regime_confidence=policy.minimum_regime_confidence,
            minimum_signal_confidence=policy.minimum_signal_confidence,
            minimum_directional_consensus=policy.minimum_directional_consensus,
            policy_fingerprint=policy_fingerprint,
        )

    async def backtest_causal_policy_entry_on_dataset(
        self,
        entry: CausalPolicyCampaignEntry,
        dataset: ResearchDataset,
        candles: list[Candle],
        funding_rates: list[FundingRatePoint],
        open_interest: list[OpenInterestPoint],
        *,
        positioning: list[PositioningPoint],
        basis: list[BasisPoint],
        order_flow: list[OrderFlowBar],
        liquidation_flow: list[LiquidationFlowPoint],
        research_trial_count: int,
    ) -> BacktestResult:
        configuration = self.configuration_service.current_configuration()
        result = await self.execute_research_backtest(
            dataset,
            candles,
            initial_equity=configuration.paper_equity,
            fee_bps=configuration.fee_bps,
            slippage_bps=configuration.slippage_bps,
            symbol=dataset.symbol,
            timeframe=dataset.timeframe,
            exchange=dataset.exchange,
            risk_fraction=configuration.risk.risk_per_trade,
            minimum_reward_risk=configuration.risk.minimum_reward_risk,
            allow_short=dataset.market_type == "swap",
            require_confirmation=False,
            validation=configuration.strategy.validation,
            funding_rates=funding_rates,
            open_interest=open_interest,
            positioning=positioning,
            basis=basis,
            order_flow=order_flow,
            liquidation_flow=liquidation_flow,
            causal_strategy_priors=list(entry.priors),
            causal_policy_max_candidates_per_bar=(entry.maximum_candidates_per_bar),
            causal_policy_abstain_on_ambiguous_regime=(entry.abstain_on_ambiguous_regime),
            causal_policy_minimum_regime_confidence=(entry.minimum_regime_confidence),
            causal_policy_minimum_signal_confidence=(entry.minimum_signal_confidence),
            causal_policy_minimum_directional_consensus=(entry.minimum_directional_consensus),
            research_trial_count=research_trial_count,
            require_regime_family_fit=True,
        )
        if result.strategy_fingerprint != entry.policy_fingerprint:
            raise RuntimeError("causal policy backtest fingerprint binding failed")
        result.integrity_checks.update(
            {
                "causal_policy_campaign_hypothesis_pre_registered": True,
                "causal_policy_campaign_certificate_eligible_path": True,
            }
        )
        return result

    async def backtest_causal_strategy_policy(
        self,
        request: CausalPolicyBacktestRequest,
    ) -> BacktestResult:
        (
            dataset,
            candles,
            funding,
            open_interest,
            positioning,
            basis,
            order_flow,
            liquidation_flow,
        ) = await self.load_verified_research_dataset(request.dataset_id)
        if dataset.research_role == "blind" or dataset.blind_sealed:
            raise HTTPException(422, "sealed blind datasets cannot be used for policy research")
        try:
            entry = self.build_causal_policy_campaign_entry(
                CausalPolicyCampaignSpec(
                    id="single_dataset_development_screen",
                    library_entry_ids=request.library_entry_ids,
                    maximum_candidates_per_bar=request.maximum_candidates_per_bar,
                    abstain_on_ambiguous_regime=(request.abstain_on_ambiguous_regime),
                    minimum_regime_confidence=request.minimum_regime_confidence,
                    minimum_signal_confidence=request.minimum_signal_confidence,
                    minimum_directional_consensus=(request.minimum_directional_consensus),
                )
            )
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        historical_campaigns = await asyncio.to_thread(self.store.list_research_campaigns, 1000)
        minimum_trial_count = (
            max(
                (
                    int(item.get("effective_trial_count") or item.get("trial_count") or 1)
                    for item in historical_campaigns
                ),
                default=0,
            )
            + 1
        )
        effective_trial_count = max(request.research_trial_count, minimum_trial_count)
        result = await self.backtest_causal_policy_entry_on_dataset(
            entry,
            dataset,
            candles,
            funding,
            open_interest,
            positioning=positioning,
            basis=basis,
            order_flow=order_flow,
            liquidation_flow=liquidation_flow,
            research_trial_count=effective_trial_count,
        )
        result.cost_model["causal_policy_effective_trial_count"] = str(effective_trial_count)
        result.cost_model["causal_policy_evidence_role"] = (
            "single_dataset_development_screen_not_certificate_eligible"
        )
        result.integrity_checks["causal_policy_campaign_certificate_eligible_path"] = False
        result.integrity_checks["causal_policy_campaign_required_for_certificate"] = True
        self.add_audit(
            "research",
            "Agent因果动态选策回测完成",
            dataset_id=dataset.id,
            policy_fingerprint=result.strategy_fingerprint,
            prior_count=len(entry.priors),
            effective_trial_count=effective_trial_count,
            total_trades=result.total_trades,
            robustness_passed=result.robustness_passed,
        )
        return result

    def campaign_blind_opening_allowed(
        self,
        *,
        provisional_selected,
        research_pbo,
        blind_dataset_ids: list[str],
        survivorship_bias_control_passed: bool,
    ) -> bool:
        """Keep sealed evidence closed until selection bias is controlled.

        Development-only exploration may omit a point-in-time universe. A sealed
        blind set is a one-shot evidence asset, however, so a development winner
        and PBO pass are insufficient unless the exact market universe also proves
        point-in-time selection and historical/prospective survivorship control.
        """

        return bool(
            provisional_selected is not None
            and research_pbo.passed
            and blind_dataset_ids
            and survivorship_bias_control_passed
        )
