from __future__ import annotations

import asyncio
import json
from hashlib import sha256
from itertools import combinations
from uuid import uuid4

from fastapi import HTTPException

from .api_contracts import RelativeValueCampaignRequest, RelativeValueResearchRequest
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
from .relative_value import (
    PAIR_BACKTEST_ENGINE_VERSION,
    PAIR_STRATEGY_LIBRARY,
    PairBacktestReport,
    PairStrategySpec,
    run_pair_backtest,
)
from .relative_value_campaign import (
    PAIR_CAMPAIGN_ENGINE_VERSION,
    PairMarketData,
    PairResearchCampaignReport,
    pair_research_lineage_id,
    pair_strategy_id,
)
from .research import ResearchDataset, verify_research_dataset
from .research_campaign import research_experiment_fingerprint

logger = __import__("logging").getLogger(__name__)


class RelativeValueService:
    async def relative_value_strategy_library(self):
        return {
            "execution_enabled": False,
            "sources": sorted({spec.source_url for spec in PAIR_STRATEGY_LIBRARY}),
            "profitability_claim": False,
            "entries": [
                {
                    "id": pair_strategy_id(spec),
                    "spec": spec.model_dump(mode="json"),
                    "strategy_fingerprint": spec.fingerprint,
                }
                for spec in PAIR_STRATEGY_LIBRARY
            ],
        }

    async def run_relative_value_backtests(
        self,
        request: RelativeValueResearchRequest,
    ):
        if request.dataset_a_id == request.dataset_b_id:
            raise HTTPException(422, "relative-value datasets must be different")

        async def load_dataset(dataset_id: str):
            payload = await asyncio.to_thread(
                self.store.load_research_dataset,
                dataset_id,
            )
            if not payload:
                raise HTTPException(404, f"research dataset not found: {dataset_id}")
            (
                candle_payloads,
                funding_payloads,
                open_interest_payloads,
                positioning_payloads,
                basis_payloads,
                order_flow_payloads,
                liquidation_flow_payloads,
            ) = await asyncio.gather(
                asyncio.to_thread(
                    self.store.load_research_dataset_candles,
                    dataset_id,
                ),
                asyncio.to_thread(
                    self.store.load_research_dataset_funding,
                    dataset_id,
                ),
                asyncio.to_thread(
                    self.store.load_research_dataset_open_interest,
                    dataset_id,
                ),
                asyncio.to_thread(
                    self.store.load_research_dataset_positioning,
                    dataset_id,
                ),
                asyncio.to_thread(
                    self.store.load_research_dataset_basis,
                    dataset_id,
                ),
                asyncio.to_thread(
                    self.store.load_research_dataset_order_flow,
                    dataset_id,
                ),
                asyncio.to_thread(
                    self.store.load_research_dataset_liquidation_flow,
                    dataset_id,
                ),
            )
            dataset = ResearchDataset.model_validate(payload)
            candles = [Candle.model_validate(item) for item in candle_payloads]
            funding = [FundingRatePoint.model_validate(item) for item in funding_payloads]
            open_interest = [
                OpenInterestPoint.model_validate(item) for item in open_interest_payloads
            ]
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
                raise HTTPException(
                    409,
                    f"research dataset integrity failed: {dataset_id}",
                )
            return dataset, candles, funding

        first, second = await asyncio.gather(
            load_dataset(request.dataset_a_id),
            load_dataset(request.dataset_b_id),
        )
        specs = (
            [PairStrategySpec.model_validate(item) for item in request.strategy_specs]
            if request.strategy_specs
            else list(PAIR_STRATEGY_LIBRARY)
        )
        configuration = self.configuration_service.current_configuration()
        reports: list[PairBacktestReport] = []
        for spec in specs:
            report = await asyncio.to_thread(
                run_pair_backtest,
                dataset_a=first[0],
                candles_a=first[1],
                funding_a=first[2],
                dataset_b=second[0],
                candles_b=second[1],
                funding_b=second[2],
                strategy_spec=spec,
                initial_equity=configuration.paper_equity,
                fee_bps=configuration.fee_bps,
                slippage_bps=configuration.slippage_bps,
                research_trial_count=len(specs),
            )
            await asyncio.to_thread(
                self.store.save_relative_value_report,
                report,
            )
            reports.append(report)
        self.add_audit(
            "research",
            "多腿相对价值研究完成；执行保持禁用",
            dataset_a_id=request.dataset_a_id,
            dataset_b_id=request.dataset_b_id,
            report_ids=",".join(report.id for report in reports),
            execution_enabled=False,
        )
        return reports

    async def list_relative_value_backtests(self, limit: int = 50):
        payloads = await asyncio.to_thread(
            self.store.list_relative_value_reports,
            min(max(limit, 1), 200),
        )
        return [PairBacktestReport.model_validate(payload) for payload in payloads]

    async def run_relative_value_campaign(self, request: RelativeValueCampaignRequest):
        dataset_ids = list(dict.fromkeys(request.dataset_ids))
        if len(dataset_ids) != len(request.dataset_ids):
            raise HTTPException(422, "relative-value campaign datasets must be unique")
        blind_dataset_ids = list(dict.fromkeys(request.blind_dataset_ids))
        if len(blind_dataset_ids) != len(request.blind_dataset_ids):
            raise HTTPException(422, "relative-value blind datasets must be unique")
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

        async def load_dataset(dataset_id: str, *, blind: bool = False):
            payload = await asyncio.to_thread(self.store.load_research_dataset, dataset_id)
            if not payload:
                raise HTTPException(404, f"research dataset not found: {dataset_id}")
            dataset = ResearchDataset.model_validate(payload)
            if not blind and (dataset.research_role == "blind" or dataset.blind_sealed):
                raise HTTPException(
                    422,
                    "sealed blind datasets cannot be used for relative-value selection",
                )
            if blind and (dataset.research_role != "blind" or not dataset.blind_sealed):
                raise HTTPException(422, "relative-value blind dataset is not sealed")
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
            open_interest = [
                OpenInterestPoint.model_validate(item) for item in open_interest_payloads
            ]
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
            return dataset, candles, funding

        def build_pair_markets(loaded_items: list[tuple], *, scope: str) -> list[PairMarketData]:
            grouped: dict[tuple[str, str], list[tuple]] = {}
            for item in loaded_items:
                grouped.setdefault((item[0].exchange, item[0].timeframe), []).append(item)
            if len({key[0] for key in grouped}) < 2:
                raise HTTPException(
                    422,
                    f"relative-value {scope} requires at least two exchanges",
                )
            result: list[PairMarketData] = []
            for (exchange, timeframe), items in sorted(grouped.items()):
                by_symbol = {item[0].symbol: item for item in items}
                if len(by_symbol) != len(items):
                    raise HTTPException(
                        422,
                        f"duplicate {exchange} {timeframe} symbols in relative-value {scope}",
                    )
                if len(by_symbol) < 3:
                    raise HTTPException(
                        422,
                        f"{exchange} {timeframe} requires at least three synchronized symbols",
                    )
                for first_symbol, second_symbol in combinations(sorted(by_symbol), 2):
                    first = by_symbol[first_symbol]
                    second = by_symbol[second_symbol]
                    result.append(
                        PairMarketData(
                            dataset_a=first[0],
                            candles_a=first[1],
                            funding_a=first[2],
                            dataset_b=second[0],
                            candles_b=second[1],
                            funding_b=second[2],
                        )
                    )
            return result

        loaded = list(
            await asyncio.gather(*(load_dataset(dataset_id) for dataset_id in dataset_ids))
        )
        markets = build_pair_markets(loaded, scope="campaign")
        specs = (
            [PairStrategySpec.model_validate(item) for item in request.strategy_specs]
            if request.strategy_specs
            else list(PAIR_STRATEGY_LIBRARY)
        )
        lineage_id = pair_research_lineage_id(markets)
        historical_payloads = await asyncio.to_thread(
            self.store.list_relative_value_campaigns,
            10000,
        )
        pair_ids = {market.id for market in markets}
        lineage_history = [
            payload
            for payload in historical_payloads
            if payload.get("research_lineage_id") == lineage_id
            or set(payload.get("research_pair_ids") or []) == pair_ids
        ]
        parent_report = None
        if request.parent_campaign_id:
            parent_payload = await asyncio.to_thread(
                self.store.load_relative_value_campaign,
                request.parent_campaign_id,
            )
            if parent_payload is None:
                raise HTTPException(404, "parent relative-value campaign not found")
            parent_report = PairResearchCampaignReport.model_validate(parent_payload)
            if not parent_report.strategy_fingerprints or not parent_report.research_lineage_id:
                raise HTTPException(409, "legacy parent campaign lacks immutable lineage evidence")
            if parent_report.parent_campaign_id is not None:
                raise HTTPException(
                    409,
                    "bounded relative-value research permits only one successor on this lineage",
                )
            if any(
                payload.get("parent_campaign_id") == parent_report.id for payload in lineage_history
            ):
                raise HTTPException(
                    409,
                    "bounded relative-value research permits only one successor per research phase",
                )
            if pair_ids != set(parent_report.research_pair_ids):
                raise HTTPException(
                    422,
                    "relative-value successor must use the exact parent research lineage",
                )
            current_fingerprints = {spec.fingerprint for spec in specs}
            parent_fingerprints = set(parent_report.strategy_fingerprints)
            if not parent_fingerprints.issubset(current_fingerprints):
                raise HTTPException(422, "successor must retain every frozen parent hypothesis")
            new_fingerprints = current_fingerprints - parent_fingerprints
            allowed_children = {
                item.strategy_spec.fingerprint for item in parent_report.recommended_revisions
            }
            if not new_fingerprints or len(new_fingerprints) > 3:
                raise HTTPException(422, "successor requires one to three bounded child hypotheses")
            if not new_fingerprints.issubset(allowed_children):
                raise HTTPException(422, "successor contains a child not frozen by its parent")
        configuration = self.configuration_service.current_configuration()
        research_configuration = {
            "fee_bps": str(configuration.fee_bps),
            "slippage_bps": str(configuration.slippage_bps),
            "fold_count": request.fold_count,
            "formation_fraction": str(request.formation_fraction),
            "pair_campaign_engine_version": PAIR_CAMPAIGN_ENGINE_VERSION,
            "pair_backtest_engine_version": PAIR_BACKTEST_ENGINE_VERSION,
            "blind_set_id": blind_set_id,
        }
        research_configuration_fingerprint = sha256(
            json.dumps(
                research_configuration,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:20]

        # Older pair campaigns predate the permanent SQL lineage ledger. Backfill
        # them before registering the current experiment so repeated use of the
        # same development data can never reset multiplicity or sequence penalties.
        registered_experiments = {
            str(item["experiment_id"])
            for item in await asyncio.to_thread(
                self.store.list_research_campaign_experiments,
                lineage_id,
            )
        }
        prior_fingerprints: set[str] = set()
        for historical in reversed(lineage_history):
            historical_fingerprints = sorted(
                {str(item) for item in historical.get("strategy_fingerprints") or []}
            )
            prior_fingerprints.update(historical_fingerprints)
            if not historical_fingerprints:
                continue
            historical_configuration_fingerprint = str(
                historical.get("research_configuration_fingerprint") or "legacy_pair_unknown"
            )
            historical_experiment_id = str(
                historical.get("research_experiment_id")
                or research_experiment_fingerprint(
                    lineage_id=lineage_id,
                    engine_version=int(historical.get("engine_version") or 0),
                    research_configuration_fingerprint=(historical_configuration_fingerprint),
                    strategy_fingerprints=historical_fingerprints,
                )
            )
            if historical_experiment_id in registered_experiments:
                continue
            await asyncio.to_thread(
                self.store.register_research_campaign_experiment,
                lineage_id=lineage_id,
                experiment_id=historical_experiment_id,
                campaign_id=str(historical.get("id") or historical_experiment_id),
                engine_version=int(historical.get("engine_version") or 0),
                research_configuration_fingerprint=(historical_configuration_fingerprint),
                strategy_fingerprints=historical_fingerprints,
                blind_set_id=(
                    str(historical.get("blind_set_id"))
                    if historical.get("blind_set_id")
                    else blind_set_id
                ),
            )
            registered_experiments.add(historical_experiment_id)

        strategy_fingerprints = [spec.fingerprint for spec in specs]
        experiment_id = research_experiment_fingerprint(
            lineage_id=lineage_id,
            engine_version=PAIR_CAMPAIGN_ENGINE_VERSION,
            research_configuration_fingerprint=research_configuration_fingerprint,
            strategy_fingerprints=strategy_fingerprints,
        )
        campaign_id = f"paircampaign_{uuid4().hex[:16]}"
        try:
            ledger_summary = await asyncio.to_thread(
                self.store.register_research_campaign_experiment,
                lineage_id=lineage_id,
                experiment_id=experiment_id,
                campaign_id=campaign_id,
                engine_version=PAIR_CAMPAIGN_ENGINE_VERSION,
                research_configuration_fingerprint=research_configuration_fingerprint,
                strategy_fingerprints=strategy_fingerprints,
                blind_set_id=blind_set_id,
            )
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        if ledger_summary.get("replayed"):
            existing_payload = await asyncio.to_thread(
                self.store.load_relative_value_campaign,
                str(ledger_summary["campaign_id"]),
            )
            if existing_payload is not None:
                return PairResearchCampaignReport.model_validate(existing_payload)
            campaign_id = str(ledger_summary["campaign_id"])

        if parent_report is not None:
            experiment_kind = "bounded_revision"
            predecessor_campaign_id = parent_report.id
        elif lineage_history:
            experiment_kind = "structural_extension"
            predecessor_campaign_id = str(lineage_history[0].get("id") or "") or None
        else:
            experiment_kind = "initial"
            predecessor_campaign_id = None
        report = await asyncio.to_thread(
            self.run_pair_research_campaign,
            markets,
            strategy_specs=specs,
            initial_equity=configuration.paper_equity,
            fee_bps=configuration.fee_bps,
            slippage_bps=configuration.slippage_bps,
            fold_count=request.fold_count,
            formation_fraction=request.formation_fraction,
            report_id=campaign_id,
            report_sink=self.store.save_relative_value_report,
            parent_campaign_id=(parent_report.id if parent_report is not None else None),
            predecessor_campaign_id=predecessor_campaign_id,
            experiment_kind=experiment_kind,
            research_experiment_id=experiment_id,
            research_configuration_fingerprint=research_configuration_fingerprint,
            prior_hypothesis_fingerprints=sorted(prior_fingerprints),
            cumulative_hypothesis_count=int(ledger_summary["cumulative_hypothesis_count"]),
            sequential_experiment_count=int(ledger_summary["sequential_experiment_count"]),
            effective_trial_count=int(ledger_summary["effective_trial_count"]),
            new_trial_variant_count=int(ledger_summary["new_trial_variant_count"]),
            generate_failure_revisions=(
                request.generate_failure_revisions if parent_report is None else False
            ),
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
                if pair_strategy_id(spec) == report.provisional_selected_strategy_id
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
                    *(load_dataset(dataset_id, blind=True) for dataset_id in blind_dataset_ids)
                )
            )
            blind_markets = build_pair_markets(blind_loaded, scope="sealed blind evaluation")
            blind_report = await asyncio.to_thread(
                self.run_pair_research_campaign,
                blind_markets,
                strategy_specs=[selected_spec],
                initial_equity=configuration.paper_equity,
                fee_bps=configuration.fee_bps,
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
                            "the frozen development candidate passed the one-time "
                            "sealed blind evaluation"
                            if blind_passed
                            else "the frozen development candidate failed the one-time "
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
                if pair_strategy_id(spec) == report.provisional_selected_strategy_id
            )
            development_candidate = next(
                candidate
                for candidate in report.candidates
                if candidate.library_entry_id == report.provisional_selected_strategy_id
            )
            certificate = build_multi_leg_execution_certificate(
                strategy_kind="pair",
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
                    "research_pair_ids": report.research_pair_ids,
                    "effective_trial_count": report.effective_trial_count,
                    "research_configuration_fingerprint": (
                        report.research_configuration_fingerprint
                    ),
                },
                strategy_spec_payload=selected_spec.model_dump(mode="json"),
                allowed_execution_groups=[
                    [
                        f"{market.dataset_a.exchange}:"
                        f"{market.dataset_a.market_type}:{market.dataset_a.symbol}",
                        f"{market.dataset_b.exchange}:"
                        f"{market.dataset_b.market_type}:{market.dataset_b.symbol}",
                    ]
                    for market in markets
                ],
                allowed_timeframes=sorted({market.dataset_a.timeframe for market in markets}),
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
                        if "双腿原子下单" not in reason
                    ]
                    + [
                        "sealed-blind PASS is bound to a fail-closed multi-leg "
                        "execution certificate; venue commissioning remains mandatory"
                    ],
                }
            )
        await asyncio.to_thread(self.store.save_relative_value_campaign, report)
        if report.blind_consumption_id:
            await asyncio.to_thread(
                self.store.complete_blind_set_consumption,
                evaluation_id=report.blind_consumption_id,
                campaign_id=report.id,
                passed=report.blind_selected_candidate_passed,
            )
        self.add_audit(
            "research",
            "相对价值跨配对 Campaign 完成",
            campaign_id=report.id,
            pair_markets=len(markets),
            strategies=len(specs),
            pbo_passed=report.pbo.passed,
            selected=report.provisional_selected_strategy_id or "none",
            execution_enabled=report.execution_eligible,
        )
        return report

    async def list_relative_value_campaigns(self, limit: int = 50):
        payloads = await asyncio.to_thread(
            self.store.list_relative_value_campaigns,
            min(max(limit, 1), 200),
        )
        return [PairResearchCampaignReport.model_validate(payload) for payload in payloads]

    async def get_relative_value_campaign(self, campaign_id: str):
        payload = await asyncio.to_thread(self.store.load_relative_value_campaign, campaign_id)
        if payload is None:
            raise HTTPException(404, "relative-value campaign not found")
        return PairResearchCampaignReport.model_validate(payload)
