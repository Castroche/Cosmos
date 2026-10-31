from __future__ import annotations

import asyncio
import json
import math
from hashlib import sha256
from uuid import uuid4

from fastapi import HTTPException

from .api_contracts import CarryCampaignRequest, CarryResearchRequest
from .carry import (
    CARRY_BACKTEST_ENGINE_VERSION,
    CARRY_STRATEGY_LIBRARY,
    CarryBacktestReport,
    CarryStrategySpec,
    run_carry_backtest,
)
from .carry_campaign import (
    CARRY_CAMPAIGN_ENGINE_VERSION,
    CarryMarketData,
    CarryResearchCampaignReport,
    carry_research_lineage_id,
    carry_strategy_id,
)
from .domain import (
    BasisPoint,
    Candle,
    FundingRatePoint,
    LiquidationFlowPoint,
    OpenInterestPoint,
    OrderFlowBar,
    PositioningPoint,
)
from .multi_leg_execution import build_multi_leg_execution_certificate
from .research import ResearchDataset, verify_research_dataset
from .research_campaign import research_experiment_fingerprint

logger = __import__("logging").getLogger(__name__)


class CarryService:
    async def _load_verified_carry_dataset(self, dataset_id: str, *, blind: bool = False):
        payload = await asyncio.to_thread(self.store.load_research_dataset, dataset_id)
        if not payload:
            raise HTTPException(404, f"research dataset not found: {dataset_id}")
        dataset = ResearchDataset.model_validate(payload)
        if not blind and (dataset.research_role == "blind" or dataset.blind_sealed):
            raise HTTPException(422, "sealed blind datasets cannot be used for carry selection")
        if blind and (dataset.research_role != "blind" or not dataset.blind_sealed):
            raise HTTPException(422, "carry blind dataset is not sealed")
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
        verification = verify_research_dataset(
            dataset,
            candles,
            funding,
            [OpenInterestPoint.model_validate(item) for item in open_interest_payloads],
            [PositioningPoint.model_validate(item) for item in positioning_payloads],
            [BasisPoint.model_validate(item) for item in basis_payloads],
            [OrderFlowBar.model_validate(item) for item in order_flow_payloads],
            [LiquidationFlowPoint.model_validate(item) for item in liquidation_flow_payloads],
        )
        if not verification["verified"]:
            raise HTTPException(409, f"research dataset integrity failed: {dataset_id}")
        return dataset, candles, funding

    async def get_carry_strategy_library(self):
        """Expose immutable public-prior carry hypotheses; none are pre-approved."""
        return list(CARRY_STRATEGY_LIBRARY)

    async def run_carry_backtests(self, request: CarryResearchRequest):
        spot, perpetual = await asyncio.gather(
            self._load_verified_carry_dataset(request.spot_dataset_id),
            self._load_verified_carry_dataset(request.perpetual_dataset_id),
        )
        specs = (
            [CarryStrategySpec.model_validate(item) for item in request.strategy_specs]
            if request.strategy_specs
            else list(CARRY_STRATEGY_LIBRARY)
        )
        aligned_count = len(
            {item.timestamp for item in spot[1]} & {item.timestamp for item in perpetual[1]}
        )
        reports = []
        configuration = self.configuration_service.current_configuration()
        for spec in specs:
            report = await asyncio.to_thread(
                run_carry_backtest,
                spot_dataset=spot[0],
                spot_candles=spot[1],
                perpetual_dataset=perpetual[0],
                perpetual_candles=perpetual[1],
                funding_rates=perpetual[2],
                strategy_spec=spec,
                initial_equity=configuration.paper_equity,
                spot_fee_bps=configuration.fee_bps,
                perpetual_fee_bps=configuration.fee_bps,
                slippage_bps=configuration.slippage_bps,
                research_trial_count=len(specs),
                evaluation_start_index=max(
                    spec.confirmation_bars,
                    math.ceil(aligned_count * float(request.formation_fraction)),
                ),
            )
            await asyncio.to_thread(self.store.save_carry_report, report)
            reports.append(report)
        return reports

    async def list_carry_backtests(self, limit: int = 50):
        payloads = await asyncio.to_thread(
            self.store.list_carry_reports,
            min(max(limit, 1), 200),
        )
        return [CarryBacktestReport.model_validate(payload) for payload in payloads]

    async def run_carry_campaign(self, request: CarryCampaignRequest):
        dataset_ids = list(dict.fromkeys(request.dataset_ids))
        if len(dataset_ids) != len(request.dataset_ids):
            raise HTTPException(422, "carry campaign datasets must be unique")
        blind_dataset_ids = list(dict.fromkeys(request.blind_dataset_ids))
        if len(blind_dataset_ids) != len(request.blind_dataset_ids):
            raise HTTPException(422, "carry blind datasets must be unique")
        if set(dataset_ids) & set(blind_dataset_ids):
            raise HTTPException(422, "development and blind datasets must be disjoint")
        metadata_payloads = await asyncio.gather(
            *(
                asyncio.to_thread(self.store.load_research_dataset, dataset_id)
                for dataset_id in [*dataset_ids, *blind_dataset_ids]
            )
        )
        if any(payload is None for payload in metadata_payloads):
            missing = [
                dataset_id
                for dataset_id, payload in zip(
                    [*dataset_ids, *blind_dataset_ids], metadata_payloads, strict=True
                )
                if payload is None
            ]
            raise HTTPException(404, f"research dataset not found: {missing}")
        metadata = [ResearchDataset.model_validate(payload) for payload in metadata_payloads]
        try:
            blind_set_id = self._validate_sealed_blind_scope(
                metadata[: len(dataset_ids)],
                metadata[len(dataset_ids) :],
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        loaded = await asyncio.gather(
            *(self._load_verified_carry_dataset(dataset_id) for dataset_id in dataset_ids)
        )

        def build_carry_markets(loaded_items: list[tuple], *, scope: str) -> list[CarryMarketData]:
            grouped: dict[tuple[str, str, str], dict[str, tuple]] = {}
            for item in loaded_items:
                dataset = item[0]
                if dataset.market_type not in {"spot", "swap"}:
                    raise HTTPException(422, f"{scope} accepts only spot and swap datasets")
                key = (dataset.exchange, dataset.symbol, dataset.timeframe)
                market_types = grouped.setdefault(key, {})
                if dataset.market_type in market_types:
                    raise HTTPException(422, f"duplicate carry leg: {key} {dataset.market_type}")
                market_types[dataset.market_type] = item
            incomplete = [key for key, legs in grouped.items() if set(legs) != {"spot", "swap"}]
            if incomplete:
                raise HTTPException(
                    422, f"carry markets require exact spot+swap legs: {incomplete}"
                )
            scope_counts: dict[tuple[str, str], int] = {}
            for exchange, _symbol, timeframe in grouped:
                key = (exchange, timeframe)
                scope_counts[key] = scope_counts.get(key, 0) + 1
            if len({exchange for exchange, _ in scope_counts}) < 2:
                raise HTTPException(422, f"{scope} requires at least two exchanges")
            if any(count < 3 for count in scope_counts.values()):
                raise HTTPException(
                    422,
                    f"each {scope} exchange/timeframe scope requires at least three symbols",
                )
            return [
                CarryMarketData(
                    spot_dataset=legs["spot"][0],
                    spot_candles=legs["spot"][1],
                    perpetual_dataset=legs["swap"][0],
                    perpetual_candles=legs["swap"][1],
                    funding_rates=legs["swap"][2],
                )
                for _key, legs in sorted(grouped.items())
            ]

        markets = build_carry_markets(list(loaded), scope="carry campaign")
        specs = (
            [CarryStrategySpec.model_validate(item) for item in request.strategy_specs]
            if request.strategy_specs
            else list(CARRY_STRATEGY_LIBRARY)
        )
        lineage_id = carry_research_lineage_id(markets)
        history = [
            item
            for item in await asyncio.to_thread(self.store.list_carry_campaigns, 10000)
            if item.get("research_lineage_id") == lineage_id
        ]
        parent_report = None
        market_ids = {market.id for market in markets}
        if request.parent_campaign_id:
            parent_payload = await asyncio.to_thread(
                self.store.load_carry_campaign,
                request.parent_campaign_id,
            )
            if parent_payload is None:
                raise HTTPException(404, "parent carry campaign not found")
            parent_report = CarryResearchCampaignReport.model_validate(parent_payload)
            if parent_report.parent_campaign_id is not None:
                raise HTTPException(
                    409,
                    "bounded carry research permits only one successor on this lineage",
                )
            if any(item.get("parent_campaign_id") == parent_report.id for item in history):
                raise HTTPException(
                    409,
                    "bounded carry research permits only one successor per research phase",
                )
            if market_ids != set(parent_report.research_market_ids):
                raise HTTPException(422, "carry successor must use the exact parent lineage")
            current_fingerprints = {spec.fingerprint for spec in specs}
            parent_fingerprints = set(parent_report.strategy_fingerprints)
            if not parent_fingerprints.issubset(current_fingerprints):
                raise HTTPException(422, "carry successor must retain every parent hypothesis")
            new_fingerprints = current_fingerprints - parent_fingerprints
            allowed_children = {
                item.strategy_spec.fingerprint for item in parent_report.recommended_revisions
            }
            if not new_fingerprints or len(new_fingerprints) > 3:
                raise HTTPException(422, "carry successor requires one to three bounded children")
            if not new_fingerprints.issubset(allowed_children):
                raise HTTPException(
                    422, "carry successor contains a child not frozen by its parent"
                )
        prior_fingerprints = {
            str(fingerprint)
            for item in history
            for fingerprint in item.get("strategy_fingerprints") or []
        }
        configuration = self.configuration_service.current_configuration()
        research_configuration = {
            "spot_fee_bps": str(configuration.fee_bps),
            "perpetual_fee_bps": str(configuration.fee_bps),
            "slippage_bps": str(configuration.slippage_bps),
            "fold_count": request.fold_count,
            "formation_fraction": str(request.formation_fraction),
            "carry_campaign_engine_version": CARRY_CAMPAIGN_ENGINE_VERSION,
            "carry_backtest_engine_version": CARRY_BACKTEST_ENGINE_VERSION,
            "blind_set_id": blind_set_id,
        }
        configuration_fingerprint = sha256(
            json.dumps(
                research_configuration,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:20]
        strategy_fingerprints = [spec.fingerprint for spec in specs]
        experiment_id = research_experiment_fingerprint(
            lineage_id=lineage_id,
            engine_version=CARRY_CAMPAIGN_ENGINE_VERSION,
            research_configuration_fingerprint=configuration_fingerprint,
            strategy_fingerprints=strategy_fingerprints,
        )
        campaign_id = f"carrycampaign_{uuid4().hex[:16]}"
        try:
            ledger = await asyncio.to_thread(
                self.store.register_research_campaign_experiment,
                lineage_id=lineage_id,
                experiment_id=experiment_id,
                campaign_id=campaign_id,
                engine_version=CARRY_CAMPAIGN_ENGINE_VERSION,
                research_configuration_fingerprint=configuration_fingerprint,
                strategy_fingerprints=strategy_fingerprints,
                blind_set_id=blind_set_id,
            )
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        if ledger.get("replayed"):
            payload = await asyncio.to_thread(
                self.store.load_carry_campaign,
                str(ledger["campaign_id"]),
            )
            if payload is not None:
                return CarryResearchCampaignReport.model_validate(payload)
            campaign_id = str(ledger["campaign_id"])
        if parent_report is not None:
            experiment_kind = "bounded_revision"
            predecessor_campaign_id = parent_report.id
        elif history:
            experiment_kind = "structural_extension"
            predecessor_campaign_id = str(history[0].get("id") or "") or None
        else:
            experiment_kind = "initial"
            predecessor_campaign_id = None
        report = await asyncio.to_thread(
            self.run_carry_research_campaign,
            markets,
            strategy_specs=specs,
            initial_equity=configuration.paper_equity,
            spot_fee_bps=configuration.fee_bps,
            perpetual_fee_bps=configuration.fee_bps,
            slippage_bps=configuration.slippage_bps,
            fold_count=request.fold_count,
            formation_fraction=request.formation_fraction,
            cumulative_hypothesis_count=max(
                int(ledger["cumulative_hypothesis_count"]),
                len(prior_fingerprints | set(strategy_fingerprints)),
            ),
            sequential_experiment_count=int(ledger["sequential_experiment_count"]),
            effective_trial_count=int(ledger["effective_trial_count"]),
            new_trial_variant_count=int(ledger["new_trial_variant_count"]),
            research_experiment_id=experiment_id,
            research_configuration_fingerprint=configuration_fingerprint,
            report_id=campaign_id,
            report_sink=self.store.save_carry_report,
            parent_campaign_id=(parent_report.id if parent_report is not None else None),
            predecessor_campaign_id=predecessor_campaign_id,
            experiment_kind=experiment_kind,
            generate_failure_revisions=request.generate_failure_revisions,
            include_blind_pending_reason=not bool(blind_dataset_ids),
        )
        if blind_dataset_ids and report.provisional_selected_strategy_id is None:
            report = report.model_copy(
                update={
                    "blind_set_id": blind_set_id,
                    "selection_reasons": [
                        *report.selection_reasons,
                        "sealed blind set remained unopened because development "
                        "evidence did not nominate one candidate",
                    ],
                }
            )
        elif blind_dataset_ids:
            selected_spec = next(
                spec
                for spec in specs
                if carry_strategy_id(spec) == report.provisional_selected_strategy_id
            )
            blind_consumption_id = f"blind_eval_{uuid4().hex[:16]}"
            reserved = await asyncio.to_thread(
                self.store.reserve_blind_set,
                blind_set_id=blind_set_id,
                strategy_fingerprint=selected_spec.fingerprint,
                evaluation_id=blind_consumption_id,
            )
            if not reserved:
                existing = await asyncio.to_thread(
                    self.store.load_blind_set_consumption,
                    blind_set_id,
                )
                raise HTTPException(
                    409,
                    f"sealed blind set has already been opened and cannot be reused: {existing}",
                )
            await asyncio.to_thread(
                self.store.mark_research_lineage_blind_opened,
                lineage_id=lineage_id,
                blind_consumption_id=blind_consumption_id,
            )
            blind_loaded = list(
                await asyncio.gather(
                    *(
                        self._load_verified_carry_dataset(dataset_id, blind=True)
                        for dataset_id in blind_dataset_ids
                    )
                )
            )
            blind_markets = build_carry_markets(
                blind_loaded,
                scope="sealed blind carry evaluation",
            )
            blind_report = await asyncio.to_thread(
                self.run_carry_research_campaign,
                blind_markets,
                strategy_specs=[selected_spec],
                initial_equity=configuration.paper_equity,
                spot_fee_bps=configuration.fee_bps,
                perpetual_fee_bps=configuration.fee_bps,
                slippage_bps=configuration.slippage_bps,
                fold_count=request.fold_count,
                formation_fraction=request.formation_fraction,
                cumulative_hypothesis_count=report.cumulative_hypothesis_count,
                sequential_experiment_count=report.sequential_experiment_count,
                effective_trial_count=report.effective_trial_count,
                generate_failure_revisions=False,
                include_blind_pending_reason=False,
            )
            blind_candidate = blind_report.candidates[0]
            blind_passed = blind_candidate.cross_market_passed
            blind_metadata = metadata[len(dataset_ids) :]
            report = report.model_copy(
                update={
                    "blind_set_id": blind_set_id,
                    "blind_consumption_id": blind_consumption_id,
                    "independent_blind_test_completed": True,
                    "blind_selected_candidate_passed": blind_passed,
                    "blind_market_count": len(blind_markets),
                    "blind_evidence_fingerprint": self._sealed_blind_evidence_fingerprint(
                        strategy_fingerprint=selected_spec.fingerprint,
                        candidate=blind_candidate,
                        datasets=blind_metadata,
                    ),
                    "selection_reasons": [
                        *report.selection_reasons,
                        (
                            "the frozen carry candidate passed the one-time sealed blind evaluation"
                            if blind_passed
                            else "the frozen carry candidate failed the one-time "
                            "sealed blind evaluation"
                        ),
                        "blind performance metrics are intentionally withheld; only "
                        "pass/fail evidence is disclosed",
                    ],
                }
            )
        if report.blind_selected_candidate_passed:
            selected_spec = next(
                spec
                for spec in specs
                if carry_strategy_id(spec) == report.provisional_selected_strategy_id
            )
            development_candidate = next(
                candidate
                for candidate in report.candidates
                if candidate.library_entry_id == report.provisional_selected_strategy_id
            )
            certificate = build_multi_leg_execution_certificate(
                strategy_kind="carry",
                campaign_id=report.id,
                campaign_engine_version=report.engine_version,
                strategy_id=report.provisional_selected_strategy_id,
                strategy_fingerprint=selected_spec.fingerprint,
                research_lineage_id=report.research_lineage_id,
                blind_set_id=report.blind_set_id,
                blind_consumption_id=report.blind_consumption_id,
                blind_evidence_fingerprint=report.blind_evidence_fingerprint,
                development_evidence={
                    "candidate": development_candidate.model_dump(mode="json"),
                    "pbo": report.pbo.model_dump(mode="json"),
                    "research_market_ids": report.research_market_ids,
                    "effective_trial_count": report.effective_trial_count,
                    "research_configuration_fingerprint": (
                        report.research_configuration_fingerprint
                    ),
                },
                strategy_spec_payload=selected_spec.model_dump(mode="json"),
                allowed_execution_groups=[
                    [
                        f"{market.spot_dataset.exchange}:spot:{market.spot_dataset.symbol}",
                        f"{market.perpetual_dataset.exchange}:swap:{market.perpetual_dataset.symbol}",
                    ]
                    for market in markets
                ],
                allowed_timeframes=sorted({market.spot_dataset.timeframe for market in markets}),
                development_passed=development_candidate.cross_market_passed,
                pbo_passed=report.pbo.passed,
                independent_blind_test_completed=report.independent_blind_test_completed,
                blind_selected_candidate_passed=report.blind_selected_candidate_passed,
            )
            report = report.model_copy(
                update={
                    "execution_eligible": True,
                    "execution_certificate": certificate,
                    "selection_reasons": [
                        reason
                        for reason in report.selection_reasons
                        if "partial-fill compensation" not in reason
                        and "cannot issue an execution certificate" not in reason
                    ]
                    + [
                        "sealed-blind PASS is bound to a fail-closed multi-leg "
                        "execution certificate; venue commissioning remains mandatory"
                    ],
                }
            )
        await asyncio.to_thread(self.store.save_carry_campaign, report)
        if report.blind_consumption_id:
            await asyncio.to_thread(
                self.store.complete_blind_set_consumption,
                evaluation_id=report.blind_consumption_id,
                campaign_id=report.id,
                passed=report.blind_selected_candidate_passed,
            )
        self.add_audit(
            "research",
            "Spot-perpetual carry Campaign completed",
            campaign_id=report.id,
            markets=len(markets),
            strategies=len(specs),
            pbo_passed=report.pbo.passed,
            selected=report.provisional_selected_strategy_id or "none",
            execution_enabled=report.execution_eligible,
        )
        return report

    async def list_carry_campaigns(self, limit: int = 50):
        payloads = await asyncio.to_thread(
            self.store.list_carry_campaigns,
            min(max(limit, 1), 200),
        )
        return [CarryResearchCampaignReport.model_validate(payload) for payload in payloads]

    async def get_carry_campaign(self, campaign_id: str):
        payload = await asyncio.to_thread(self.store.load_carry_campaign, campaign_id)
        if payload is None:
            raise HTTPException(404, "carry campaign not found")
        return CarryResearchCampaignReport.model_validate(payload)
