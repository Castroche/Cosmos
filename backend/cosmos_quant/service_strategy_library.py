from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from typing import Literal

from fastapi import HTTPException
from pydantic import ValidationError

from .alpha_factory import (
    AlphaModelArtifactV1,
    alpha_transaction_cost_fingerprint,
    assess_crypto_5m_contract,
    dependency_versions,
)
from .autonomous_research import AutonomousResearchRunRequest, FastGateEngine, ResearchRunResult
from .carry import CARRY_STRATEGY_LIBRARY
from .carry_campaign import carry_strategy_id
from .configuration import StrategyKind, UserConfiguration
from .entry_profiles import DEFAULT_ENTRY_PROFILE_ID, get_entry_profile
from .indicators import INDICATOR_CATALOG, atr
from .operator_workspace import (
    PersonalStrategyCreate,
    PersonalStrategyRecord,
    PersonalStrategyUpdate,
    StrategyLibraryItem,
    StrategyLibraryPage,
    StrategyLibraryPreference,
    stable_fingerprint,
)
from .relative_value import PAIR_STRATEGY_LIBRARY
from .relative_value_campaign import pair_strategy_id
from .research import ResearchDataset
from .research_certificates import GeneratorEnvelopeCertificate
from .research_lifecycle import CandidateResearchContext, CandidateStatus, StrategyCandidate
from .signals import select_strategy_kind
from .strategy_dsl import compile_strategy_dsl
from .strategy_library import library_entry, list_strategy_library
from .strategy_spec import default_strategy_spec, strategy_spec_json_schema, validate_strategy_spec
from .timeframes import closed_candles

logger = __import__("logging").getLogger(__name__)


class StrategyLibraryService:
    async def list_strategies(self):
        return list(self.plans.values())

    async def strategy_spec_contract(self):
        return {
            "schema": strategy_spec_json_schema(),
            "defaults": {
                kind.value: default_strategy_spec(kind).model_dump(mode="json")
                for kind in StrategyKind
            },
        }

    async def create_autonomous_research_run(self, request: AutonomousResearchRunRequest):
        if request.research_profile == "crypto_5m_v1":
            ir = compile_strategy_dsl(request.dsl)
            artifacts: list[AlphaModelArtifactV1] = []
            for model_id in request.model_artifact_ids:
                payload = await asyncio.to_thread(self.store.load_alpha_model_artifact, model_id)
                if payload is None:
                    raise HTTPException(409, f"alpha model artifact not found: {model_id}")
                artifacts.append(AlphaModelArtifactV1.model_validate(payload))
            families = {item.model_family for item in artifacts}
            if not (
                families & {"elastic_net_regression", "logistic_classification"}
                and families & {"lightgbm_regression", "lightgbm_classification"}
                and "ensemble_50_50" in families
            ):
                raise HTTPException(409, "crypto_5m_v1 requires one native model trio")
            lineage = {
                (
                    item.profile,
                    item.horizon_bars,
                    item.feature_names,
                    item.ir_fingerprint,
                    item.cost_model_fingerprint,
                )
                for item in artifacts
            }
            if len(lineage) != 1 or any(
                item.ir_fingerprint != ir.fingerprint for item in artifacts
            ):
                raise HTTPException(409, "autonomous run DSL and frozen model lineage differ")
            if any(not item.walk_forward_passed for item in artifacts):
                raise HTTPException(
                    409,
                    "autonomous run models did not independently pass walk-forward",
                )
            expected_cost = alpha_transaction_cost_fingerprint(
                fee_bps=self.configuration_service.current_configuration().fee_bps,
                slippage_bps=self.configuration_service.current_configuration().slippage_bps,
            )
            if {item.cost_model_fingerprint for item in artifacts} != {expected_cost}:
                raise HTTPException(409, "autonomous run model cost lineage changed")
            if any(item.dependency_versions != dependency_versions() for item in artifacts):
                raise HTTPException(409, "autonomous run model dependency versions drifted")
            required = tuple(
                sorted({feature for item in artifacts for feature in item.required_data_features})
            )
            datasets = await asyncio.to_thread(self.store.list_research_datasets, 2_000)
            contract = assess_crypto_5m_contract(datasets, required_features=required)
            selected_fingerprints = {
                item.get("data_fingerprint")
                for item in datasets
                if item.get("id") in {market.dataset_id for market in contract.markets}
            }
            if (
                contract.status != "READY"
                or request.snapshot.dataset_fingerprint not in selected_fingerprints
            ):
                raise HTTPException(409, "autonomous run snapshot is outside READY crypto_5m_v1")
        envelope = None
        if request.generator_envelope_id:
            envelope_payload = await asyncio.to_thread(
                self.store.load_generator_envelope, request.generator_envelope_id
            )
            if envelope_payload is None:
                raise HTTPException(404, "generator envelope not found")
            envelope = GeneratorEnvelopeCertificate.model_validate(envelope_payload)
        engine = FastGateEngine(
            self.store,
            self.runtime_build_identity,
            envelope=envelope,
        )
        return await asyncio.to_thread(engine.run, request)

    async def autonomous_research_run_result(self, run_id: str):
        payload = await asyncio.to_thread(self.store.load_autonomous_research_event, run_id)
        if payload is None:
            raise HTTPException(404, "autonomous research run not found")
        if "disposition" not in payload:
            raise HTTPException(409, "autonomous research decision is not committed")
        return ResearchRunResult.model_validate(payload)

    async def permanent_trial_ledger(self, family_id: str | None = None, limit: int = 100):
        trials = await asyncio.to_thread(
            partial(
                self.store.list_research_trials,
                family_id=family_id,
                limit=min(max(limit, 1), 1_000),
            )
        )
        # Never expose return statistics or sealed blind attribution through the
        # public/Agent-facing ledger DTO.
        return [
            {
                "id": trial["id"],
                "research_event_id": trial["research_event_id"],
                "hypothesis_id": trial["hypothesis_id"],
                "family_id": trial["family_id"],
                "candidate_id": trial["candidate_id"],
                "trial_type": trial["trial_type"],
                "result": trial.get("result", "pending"),
                "reason_codes": trial.get("reason_codes", []),
                "created_at": trial["created_at"],
            }
            for trial in trials
        ]

    async def research_certificate_status(self, certificate_id: str):
        payload = await asyncio.to_thread(
            self.store.load_research_certificate_status, certificate_id
        )
        if payload is None:
            raise HTTPException(404, "research certificate not found")
        return payload

    async def research_datasets(self, limit: int = 50):
        bounded_limit = min(max(limit, 1), 200)
        payloads = await asyncio.to_thread(self.store.list_research_datasets, bounded_limit)
        return [ResearchDataset.model_validate(payload) for payload in payloads]

    def ai_market_summary(
        self,
        market,
        history,
        symbol: str,
        timeframe: str,
        *,
        maximum_bars: int = 2000,
    ) -> dict:
        candles = closed_candles(history, timeframe, market.timestamp)[-maximum_bars:]
        if len(candles) < 50:
            raise ValueError("至少需要 50 根已收盘 K 线才能生成 AI 策略候选")
        closes = [candle.close for candle in candles]
        returns = [
            (current / previous - Decimal("1"))
            for previous, current in zip(closes, closes[1:], strict=False)
            if previous
        ]
        average_return = sum(returns, Decimal("0")) / max(len(returns), 1)
        average_abs_deviation = sum((value - average_return).copy_abs() for value in returns) / max(
            len(returns), 1
        )
        recent_volume = sum((item.volume for item in candles[-20:]), Decimal("0")) / 20
        baseline_volume = sum((item.volume for item in candles[-100:]), Decimal("0")) / min(
            len(candles), 100
        )
        selected_kind = select_strategy_kind(candles)
        current_atr = atr(candles, 14)
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "bars": len(candles),
            "data_start": candles[0].timestamp.isoformat(),
            "data_end": candles[-1].timestamp.isoformat(),
            "market_timestamp": market.timestamp.isoformat(),
            "last_price": str(market.last),
            "spread_bps": str(market.spread_bps),
            "atr_14": str(current_atr),
            "atr_percent": str(current_atr / market.last * 100 if market.last else Decimal("0")),
            "funding_rate": str(market.funding_rate) if market.funding_rate is not None else None,
            "return_20_bars": str(closes[-1] / closes[-20] - Decimal("1")),
            "return_100_bars": str(closes[-1] / closes[-100] - Decimal("1")),
            "mean_absolute_return": str(average_abs_deviation),
            "recent_high": str(max(item.high for item in candles[-50:])),
            "recent_low": str(min(item.low for item in candles[-50:])),
            "volume_ratio_20_to_100": str(
                recent_volume / baseline_volume if baseline_volume else Decimal("0")
            ),
            "recent_average_volume": str(recent_volume),
            "baseline_average_volume": str(baseline_volume),
            "deterministic_regime": str(selected_kind),
            "closed_bars_only": True,
            "coverage_ratio": str(
                (Decimal(len(candles)) / Decimal(maximum_bars)).quantize(Decimal("0.0001"))
            ),
        }

    def strategy_required_features(self, raw_spec: dict) -> list[str]:
        parameters = (
            raw_spec.get("parameters") if isinstance(raw_spec.get("parameters"), dict) else {}
        )
        entry_model = str(parameters.get("entry_model") or "")
        required = {"ohlcv"}
        if (
            parameters.get("funding_mode") not in {None, "ignore"}
            or entry_model == "funding_contrarian"
        ):
            required.add("funding_rate")
        if parameters.get("open_interest_mode") not in {None, "ignore"}:
            required.add("open_interest")
        if entry_model == "positioning_contrarian":
            required.add("positioning")
        if (
            parameters.get("basis_mode") not in {None, "ignore"}
            or entry_model == "basis_contrarian"
        ):
            required.add("basis")
        if parameters.get("order_flow_mode") not in {None, "ignore"}:
            required.add("order_flow")
        if parameters.get("liquidation_flow_mode") not in {None, "ignore"}:
            required.add("liquidation_flow")
        return sorted(required)

    def review_policy_fingerprint(self, configuration: UserConfiguration) -> str:
        return stable_fingerprint(
            {
                "validation": configuration.strategy.validation.model_dump(mode="json"),
                "fee_bps": str(configuration.fee_bps),
                "slippage_bps": str(configuration.slippage_bps),
                "risk_per_trade": str(configuration.risk.risk_per_trade),
                "max_gross_exposure": str(configuration.risk.max_gross_exposure),
                "max_leverage": str(configuration.risk.max_leverage),
                "contract_leverage": str(configuration.contract_leverage),
            }
        )

    def recent_candidate_failure_digest(
        self,
        *,
        symbol: str,
        timeframe: str,
        family: StrategyKind | None,
        limit: int = 10,
    ) -> tuple[list[dict], list[str]]:
        digest: list[dict] = []
        rejected_fingerprints: list[str] = []
        for payload in self.store.list_strategy_candidates(500):
            context = payload.get("research_context")
            if not isinstance(context, dict):
                continue
            if context.get("symbol") != symbol or context.get("timeframe") != timeframe:
                continue
            spec = payload.get("spec") if isinstance(payload.get("spec"), dict) else {}
            if family is not None and spec.get("family") != family.value:
                continue
            if str(payload.get("status") or "") != str(CandidateStatus.REJECTED):
                continue
            fingerprint = str(payload.get("spec_fingerprint") or "")
            backtest = payload.get("backtest") if isinstance(payload.get("backtest"), dict) else {}
            reasons = list(
                backtest.get("validation_reasons") or payload.get("decision_reasons") or []
            )
            digest.append(
                {
                    "fingerprint": fingerprint,
                    "family": spec.get("family"),
                    "reasons": reasons[-5:],
                    "cost_r": (backtest.get("cost_model") or {}).get("round_trip_cost_r"),
                }
            )
            if fingerprint:
                rejected_fingerprints.append(fingerprint)
            if len(digest) >= limit:
                break
        return digest, rejected_fingerprints

    def candidate_trial_count(self, *, trial_group: str) -> int:
        return self.current_candidate_trial_count(trial_group=trial_group) + 1

    def current_candidate_trial_count(self, *, trial_group: str) -> int:
        count = 0
        for payload in self.store.list_strategy_candidates(500):
            context = payload.get("research_context")
            if isinstance(context, dict) and context.get("trial_group") == trial_group:
                count += 1
        return count

    def build_candidate_research_context(
        self,
        spec,
        *,
        symbol: str,
        timeframe: str,
        market_as_of: datetime,
        summary_material: dict,
        entry_profile: str = DEFAULT_ENTRY_PROFILE_ID,
    ) -> CandidateResearchContext:
        configuration = self.configuration_service.current_configuration()
        spec_payload = spec.model_dump(mode="json", exclude={"fingerprint"})
        trial_group = stable_fingerprint(
            {
                "exchange": configuration.exchange_id,
                "market_type": str(configuration.market_type),
                "symbol": symbol,
                "timeframe": timeframe,
                "family": spec_payload["family"],
                "contract": "strategy_generation_v2",
            }
        )
        return CandidateResearchContext(
            exchange=configuration.exchange_id,
            market_type=str(configuration.market_type),
            symbol=symbol,
            timeframe=timeframe,
            entry_profile=entry_profile,
            market_as_of=market_as_of,
            summary_fingerprint=stable_fingerprint(summary_material),
            fee_bps=configuration.fee_bps,
            slippage_bps=configuration.slippage_bps,
            risk_fraction=configuration.risk.risk_per_trade,
            max_gross_exposure=configuration.risk.max_gross_exposure,
            max_leverage=configuration.risk.max_leverage,
            required_features=self.strategy_required_features(spec_payload),
            trial_group=trial_group,
            trial_count=self.candidate_trial_count(trial_group=trial_group),
        )

    def build_candidate_preflight(
        self,
        spec,
        *,
        origin_summary: dict,
        configuration: UserConfiguration,
    ) -> dict[str, str | int | float | bool | list[str]]:
        parameters = spec.parameters
        last_price = Decimal(str(origin_summary["last_price"]))
        current_atr = Decimal(str(origin_summary["atr_14"]))
        stop_atr = Decimal(str(getattr(parameters, "stop_atr", Decimal("1.5"))))
        stop_distance = current_atr * stop_atr
        risk_amount = configuration.paper_equity * configuration.risk.risk_per_trade
        intended_quantity = risk_amount / stop_distance if stop_distance > 0 else Decimal("0")
        intended_notional = intended_quantity * last_price
        gross_exposure_limit = configuration.paper_equity * configuration.risk.max_gross_exposure
        leverage_limit = configuration.paper_equity * min(
            configuration.risk.max_leverage,
            configuration.contract_leverage,
        )
        recent_volume = Decimal(str(origin_summary.get("recent_average_volume") or "0"))
        volume_limit = (
            recent_volume * Decimal("0.02") * last_price if recent_volume > 0 else intended_notional
        )
        capped_notional = min(
            intended_notional,
            gross_exposure_limit,
            leverage_limit,
            volume_limit,
        )
        capped_quantity = capped_notional / last_price if last_price > 0 else Decimal("0")
        estimated_participation = (
            min(capped_quantity / recent_volume, Decimal("0.02"))
            if recent_volume > 0
            else Decimal("0.02")
        )
        estimated_impact_bps = Decimal(str(math.sqrt(float(estimated_participation)))) * Decimal(
            "5"
        )
        estimated_round_trip_bps = (
            configuration.fee_bps + configuration.slippage_bps + estimated_impact_bps
        ) * 2
        estimated_cost = capped_notional * estimated_round_trip_bps / Decimal("10000")
        capped_stop_risk = capped_quantity * stop_distance
        cost_r = estimated_cost / capped_stop_risk if capped_stop_risk > 0 else Decimal("999")
        maximum_cost_r = configuration.strategy.validation.maximum_estimated_round_trip_cost_r
        reasons: list[str] = []
        if cost_r > maximum_cost_r:
            reasons.append(f"预计往返成本 {cost_r:.4f}R 超过设计上限 {maximum_cost_r:.4f}R")
        position_limits: list[str] = []
        if capped_notional < intended_notional:
            if capped_notional == gross_exposure_limit:
                position_limits.append("gross_exposure")
            if capped_notional == leverage_limit:
                position_limits.append("leverage")
            if capped_notional == volume_limit:
                position_limits.append("volume_participation")
            reasons.append(f"风险仓位已受 {'+'.join(position_limits)} 上限约束")
        return {
            "eligible": cost_r <= maximum_cost_r,
            "estimated_round_trip_cost_r": str(cost_r.quantize(Decimal("0.0001"))),
            "maximum_round_trip_cost_r": str(maximum_cost_r),
            "intended_notional": str(intended_notional.quantize(Decimal("0.01"))),
            "capped_notional": str(capped_notional.quantize(Decimal("0.01"))),
            "intended_quantity": str(intended_quantity.quantize(Decimal("0.00000001"))),
            "actual_quantity": str(capped_quantity.quantize(Decimal("0.00000001"))),
            "position_limit_reason": "+".join(position_limits) or "stop_risk",
            "estimated_market_impact_bps_per_side": str(
                estimated_impact_bps.quantize(Decimal("0.0001"))
            ),
            "required_features": self.strategy_required_features(
                spec.model_dump(mode="json", exclude={"fingerprint"})
            ),
            "reasons": reasons,
        }

    async def build_ai_generation_summary(
        self,
        *,
        symbol: str,
        timeframe: str,
        origin_market,
        origin_history,
    ) -> dict:
        profile = get_entry_profile(DEFAULT_ENTRY_PROFILE_ID)
        requested = list(
            dict.fromkeys(
                (
                    timeframe,
                    profile.local_context_timeframe,
                    profile.direction_context_timeframe,
                    profile.macro_context_timeframe,
                )
            )
        )
        summaries: dict[str, dict] = {}
        for item in requested:
            try:
                if item == timeframe:
                    market, history = origin_market, origin_history
                else:
                    market, history = await self.runtime.market_context(
                        limit=2000,
                        symbol=symbol,
                        timeframe=item,
                    )
                summaries[item] = self.ai_market_summary(market, history, symbol, item)
            except (LookupError, ValueError) as exc:
                summaries[item] = {"status": "unavailable", "reason": str(exc)[:300]}
        return {"origin_timeframe": timeframe, "timeframes": summaries}

    def unified_research_strategy_library(self, family: str | None = None) -> dict:
        """Return public strategy priors as offline Research inputs only."""

        normalized_family = family.strip().lower() if family else None
        single_leg_families = {item.value for item in StrategyKind}
        research_families = {"relative_value", "relative_momentum", "carry"}
        if normalized_family not in {None, *single_leg_families, *research_families}:
            raise ValueError(
                "strategy family must be trend, mean_reversion, breakout, "
                "relative_value, relative_momentum, or carry"
            )
        selected_single_family = (
            StrategyKind(normalized_family) if normalized_family in single_leg_families else None
        )
        single_entries = (
            list_strategy_library(selected_single_family)
            if normalized_family is None or selected_single_family is not None
            else []
        )
        pair_entries = []
        if normalized_family in {None, "relative_value", "relative_momentum"}:
            for spec in PAIR_STRATEGY_LIBRARY:
                pair_family = (
                    "relative_momentum"
                    if spec.model == "relative_momentum_equal_dollar"
                    else "relative_value"
                )
                if normalized_family is not None and normalized_family != pair_family:
                    continue
                pair_entries.append(
                    {
                        "id": pair_strategy_id(spec),
                        "scope": "pair_research_only",
                        "family": pair_family,
                        "archetype": spec.model,
                        "version": spec.version,
                        "source_urls": [spec.source_url],
                        "hypothesis": spec.hypothesis,
                        "strategy_spec": spec.model_dump(mode="json"),
                        "execution_eligible": False,
                    }
                )
        carry_entries = []
        if normalized_family in {None, "carry"}:
            carry_entries = [
                {
                    "id": carry_strategy_id(spec),
                    "scope": "spot_perpetual_research_only",
                    "family": "carry",
                    "archetype": spec.model,
                    "version": spec.version,
                    "source_urls": [spec.source_url],
                    "hypothesis": spec.hypothesis,
                    "strategy_spec": spec.model_dump(mode="json"),
                    "execution_eligible": False,
                }
                for spec in CARRY_STRATEGY_LIBRARY
            ]
        single_payloads = [
            {
                **entry.model_dump(mode="json"),
                "scope": "single_leg_research_only",
                "execution_eligible": False,
            }
            for entry in single_entries
        ]
        all_entries = [*single_payloads, *pair_entries, *carry_entries]
        family_counts: dict[str, int] = {}
        for entry in all_entries:
            entry_family = str(entry["family"])
            family_counts[entry_family] = family_counts.get(entry_family, 0) + 1
        archetypes = sorted({str(entry["archetype"]) for entry in all_entries})
        return {
            "version": 4,
            "non_empty": bool(all_entries),
            "entry_count": len(all_entries),
            "family_counts": family_counts,
            "archetype_count": len(archetypes),
            "archetypes": archetypes,
            "counts": {
                "single_leg": len(single_payloads),
                "pair_research": len(pair_entries),
                "carry_research": len(carry_entries),
            },
            "entries": all_entries,
            "indicator_catalog": INDICATOR_CATALOG,
            "profitability_claim": False,
            "execution_eligible": False,
        }

    async def builtin_strategy_library(self, family: str | None = None):
        try:
            return self.unified_research_strategy_library(family)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    def normalize_personal_strategy_spec(self, raw_spec: dict):
        payload = dict(raw_spec)
        payload.pop("fingerprint", None)
        payload["source"] = "operator"
        return validate_strategy_spec(payload)

    def personal_strategy_view(self, record: PersonalStrategyRecord) -> StrategyLibraryItem:
        referenced = any(
            plan.causal_policy_selected_library_entry_id == record.id
            for plan in self.plans.values()
        )
        return StrategyLibraryItem(
            id=record.id,
            name=record.name,
            description=record.description,
            origin="personal",
            family=record.family,
            status=record.status,
            version=record.version,
            strategy_spec=record.strategy_spec,
            spec_fingerprint=record.spec_fingerprint,
            executable=False,
            can_edit=record.status == "draft",
            can_archive=record.status != "archived",
            can_restore=record.status == "archived",
            can_delete=record.status == "draft" and not referenced,
            evidence_summary=(
                "已被交易计划引用，仅允许归档" if referenced else "个人草稿，执行前必须完成回测认证"
            ),
            updated_at=record.updated_at,
        )

    def unified_operator_strategy_library(self) -> list[StrategyLibraryItem]:
        hidden = self.store.hidden_strategy_library_items()
        items: list[StrategyLibraryItem] = []
        for entry in list_strategy_library():
            validated = entry.validated_spec
            items.append(
                StrategyLibraryItem(
                    id=entry.id,
                    name=entry.name,
                    description=entry.summary,
                    origin="builtin",
                    family=str(entry.family),
                    status="available",
                    version=entry.version,
                    strategy_spec=validated.model_dump(mode="json", exclude={"fingerprint"}),
                    spec_fingerprint=validated.fingerprint,
                    executable=False,
                    hidden=entry.id in hidden,
                    can_archive=entry.id not in hidden,
                    can_restore=entry.id in hidden,
                    can_delete=False,
                    evidence_summary="公开先验；使用前必须通过不可变数据集与稳健性验证",
                )
            )
        for payload in self.store.list_personal_strategy_library_items(1000):
            items.append(
                self.personal_strategy_view(PersonalStrategyRecord.model_validate(payload))
            )
        for payload in self.store.list_strategy_candidates(500):
            try:
                candidate = StrategyCandidate.model_validate(payload)
            except ValidationError:
                continue
            if candidate.source == "system":
                continue
            items.append(
                StrategyLibraryItem(
                    id=candidate.id,
                    name=f"{str(candidate.spec.get('family', 'strategy'))} · {candidate.id[-8:]}",
                    description=str(candidate.spec.get("hypothesis") or "Agent 研究候选"),
                    origin="agent",
                    family=str(candidate.spec.get("family") or "unknown"),
                    status=str(candidate.status),
                    version=1,
                    strategy_spec=candidate.spec,
                    spec_fingerprint=candidate.spec_fingerprint,
                    executable=bool(
                        candidate.status == CandidateStatus.CHAMPION
                        and candidate.execution_artifact is not None
                    ),
                    can_archive=candidate.status
                    not in {CandidateStatus.RETIRED, CandidateStatus.CHAMPION},
                    can_restore=candidate.status == CandidateStatus.RETIRED,
                    can_delete=False,
                    evidence_summary=(
                        f"PF {candidate.backtest.profit_factor:f} · "
                        f"收益 {candidate.backtest.return_percent:f}%"
                        if candidate.backtest
                        else "尚未绑定不可变数据集回测"
                    ),
                    updated_at=candidate.updated_at,
                )
            )
        origin_order = {"personal": 0, "agent": 1, "builtin": 2}
        return sorted(
            items,
            key=lambda item: (
                origin_order[item.origin],
                -(item.updated_at.timestamp() if item.updated_at else 0),
                item.name,
            ),
        )

    async def operator_strategy_library(
        self,
        origin: Literal["builtin", "personal", "agent"] | None = None,
        status: str | None = None,
        family: str | None = None,
        include_hidden: bool = False,
        search: str | None = None,
    ):
        items = await asyncio.to_thread(self.unified_operator_strategy_library)
        if origin:
            items = [item for item in items if item.origin == origin]
        if status:
            items = [item for item in items if item.status == status]
        if family:
            items = [item for item in items if item.family == family]
        if not include_hidden:
            items = [
                item
                for item in items
                if not item.hidden and item.status not in {"archived", "retired"}
            ]
        if search:
            needle = search.casefold()
            items = [
                item
                for item in items
                if needle in item.name.casefold()
                or needle in item.description.casefold()
                or needle in item.id.casefold()
            ]
        return StrategyLibraryPage(items=items, total=len(items))

    async def create_personal_strategy(self, request: PersonalStrategyCreate):
        try:
            spec = self.normalize_personal_strategy_spec(request.strategy_spec)
        except ValueError as exc:
            raise HTTPException(422, f"StrategySpec 无效：{exc}") from exc
        now = datetime.now(UTC)
        record = PersonalStrategyRecord(
            name=request.name.strip(),
            description=request.description.strip(),
            family=str(spec.family),
            strategy_spec=spec.model_dump(mode="json", exclude={"fingerprint"}),
            spec_fingerprint=spec.fingerprint,
            origin_ref=request.origin_ref,
            created_at=now,
            updated_at=now,
        )
        await asyncio.to_thread(self.store.save_personal_strategy_library_item, record)
        self.add_audit(
            "strategy",
            "个人策略已加入策略库",
            item_id=record.id,
            family=record.family,
            origin_ref=record.origin_ref,
        )
        return self.personal_strategy_view(record)

    async def update_personal_strategy(self, item_id: str, request: PersonalStrategyUpdate):
        payload = await asyncio.to_thread(self.store.load_personal_strategy_library_item, item_id)
        if not payload:
            raise HTTPException(404, "personal strategy not found")
        current = PersonalStrategyRecord.model_validate(payload)
        if current.status != "draft":
            raise HTTPException(409, "只有个人草稿可以直接编辑")
        spec = self.normalize_personal_strategy_spec(request.strategy_spec or current.strategy_spec)
        updated = current.model_copy(
            deep=True,
            update={
                "name": request.name.strip() if request.name is not None else current.name,
                "description": (
                    request.description.strip()
                    if request.description is not None
                    else current.description
                ),
                "family": str(spec.family),
                "strategy_spec": spec.model_dump(mode="json", exclude={"fingerprint"}),
                "spec_fingerprint": spec.fingerprint,
                "updated_at": datetime.now(UTC),
            },
        )
        await asyncio.to_thread(self.store.save_personal_strategy_library_item, updated)
        self.add_audit("strategy", "个人策略草稿已更新", item_id=updated.id)
        return self.personal_strategy_view(updated)

    async def archive_strategy_library_item(self, item_id: str):
        personal_payload = await asyncio.to_thread(
            self.store.load_personal_strategy_library_item,
            item_id,
        )
        if personal_payload:
            record = PersonalStrategyRecord.model_validate(personal_payload)
            record.status = "archived"
            record.updated_at = datetime.now(UTC)
            await asyncio.to_thread(self.store.save_personal_strategy_library_item, record)
            self.add_audit("strategy", "个人策略已归档", severity="warning", item_id=item_id)
            return self.personal_strategy_view(record)
        candidate_payload = await asyncio.to_thread(self.store.load_strategy_candidate, item_id)
        if candidate_payload:
            candidate = StrategyCandidate.model_validate(candidate_payload)
            if candidate.source == "system":
                raise HTTPException(409, "内置候选不可归档，请隐藏对应公开模板")
            if candidate.status == CandidateStatus.CHAMPION:
                raise HTTPException(409, "当前 Champion 必须先由新的 Champion 替代")
            candidate.status = CandidateStatus.RETIRED
            candidate.execution_artifact = None
            candidate.updated_at = datetime.now(UTC)
            candidate.decision_reasons.append("操作员从策略库归档该候选")
            await asyncio.to_thread(self.store.save_strategy_candidate, candidate)
            self.add_audit("strategy", "Agent 策略候选已归档", severity="warning", item_id=item_id)
            return next(
                item
                for item in await asyncio.to_thread(self.unified_operator_strategy_library)
                if item.id == item_id
            )
        try:
            library_entry(item_id)
        except LookupError as exc:
            raise HTTPException(404, "strategy library item not found") from exc
        await asyncio.to_thread(self.store.set_strategy_library_hidden, item_id, True)
        return next(
            item
            for item in await asyncio.to_thread(self.unified_operator_strategy_library)
            if item.id == item_id
        )

    async def restore_strategy_library_item(self, item_id: str):
        personal_payload = await asyncio.to_thread(
            self.store.load_personal_strategy_library_item,
            item_id,
        )
        if personal_payload:
            record = PersonalStrategyRecord.model_validate(personal_payload)
            if record.status != "archived":
                raise HTTPException(409, "strategy is not archived")
            record.status = "draft"
            record.updated_at = datetime.now(UTC)
            await asyncio.to_thread(self.store.save_personal_strategy_library_item, record)
            self.add_audit("strategy", "个人策略已恢复", item_id=item_id)
            return self.personal_strategy_view(record)
        candidate_payload = await asyncio.to_thread(self.store.load_strategy_candidate, item_id)
        if candidate_payload:
            candidate = StrategyCandidate.model_validate(candidate_payload)
            if candidate.status != CandidateStatus.RETIRED or candidate.source == "system":
                raise HTTPException(409, "strategy candidate cannot be restored")
            candidate.status = CandidateStatus.PROPOSED
            candidate.updated_at = datetime.now(UTC)
            candidate.decision_reasons.append("操作员恢复候选；必须重新完成评估")
            await asyncio.to_thread(self.store.save_strategy_candidate, candidate)
            return next(
                item
                for item in await asyncio.to_thread(self.unified_operator_strategy_library)
                if item.id == item_id
            )
        try:
            library_entry(item_id)
        except LookupError as exc:
            raise HTTPException(404, "strategy library item not found") from exc
        await asyncio.to_thread(self.store.set_strategy_library_hidden, item_id, False)
        return next(
            item
            for item in await asyncio.to_thread(self.unified_operator_strategy_library)
            if item.id == item_id
        )

    async def set_strategy_library_preference(
        self,
        item_id: str,
        request: StrategyLibraryPreference,
    ):
        try:
            library_entry(item_id)
        except LookupError as exc:
            raise HTTPException(404, "built-in strategy not found") from exc
        await asyncio.to_thread(self.store.set_strategy_library_hidden, item_id, request.hidden)
        return next(
            item
            for item in await asyncio.to_thread(self.unified_operator_strategy_library)
            if item.id == item_id
        )

    async def delete_personal_strategy(self, item_id: str):
        payload = await asyncio.to_thread(self.store.load_personal_strategy_library_item, item_id)
        if not payload:
            raise HTTPException(404, "personal strategy not found")
        record = PersonalStrategyRecord.model_validate(payload)
        view = self.personal_strategy_view(record)
        if not view.can_delete:
            raise HTTPException(409, "该策略已有证据引用，只能归档")
        await asyncio.to_thread(self.store.delete_personal_strategy_library_item, item_id)
        self.add_audit(
            "strategy",
            "未引用的个人策略草稿已永久删除",
            severity="warning",
            item_id=item_id,
        )
        return {"deleted": True, "item_id": item_id}
