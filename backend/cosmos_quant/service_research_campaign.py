from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from hashlib import sha256
from uuid import uuid4

from fastapi import HTTPException

from .api_contracts import CausalPolicyCampaignEntry, ResearchCampaignRequest, ResearchDatasetBundle
from .configuration import StrategyKind
from .domain import (
    BasisPoint,
    Candle,
    FundingRatePoint,
    LiquidationFlowPoint,
    OpenInterestPoint,
    OrderFlowBar,
    PositioningPoint,
)
from .research import ResearchDataset
from .research_campaign import (
    CAMPAIGN_CELL_CONCURRENCY,
    CAMPAIGN_ENGINE_VERSION,
    ResearchCampaignCell,
    ResearchCampaignReport,
    aggregate_campaign,
    build_campaign_execution_certificate,
    campaign_cell,
    campaign_cell_cache_key,
    causal_policy_definitions_fingerprint,
    finalize_campaign,
    historical_feature_eligibility,
    ineligible_campaign_cell,
    research_experiment_fingerprint,
    research_lineage_id,
    research_trial_family_id,
)
from .research_integrity import OnlineFDRController, PermanentTrialRecord, TrialResult, TrialType
from .research_lifecycle import backtest_artifact_fingerprint, create_candidate
from .research_universe import ResearchUniverseSnapshot, validate_universe_snapshot
from .strategy_library import (
    StrategyLibraryEntry,
    infer_required_data,
    library_entry,
    list_strategy_library,
)

logger = __import__("logging").getLogger(__name__)


class ResearchCampaignService:
    async def execute_research_campaign(
        self,
        request: ResearchCampaignRequest,
        *,
        progress_callback=None,
    ) -> ResearchCampaignReport:
        """Run a complete matrix with durable, versioned cell reuse."""

        research_dataset_ids = list(dict.fromkeys(request.dataset_ids))
        blind_dataset_ids = list(dict.fromkeys(request.blind_dataset_ids))
        overlap = set(research_dataset_ids) & set(blind_dataset_ids)
        if overlap:
            raise HTTPException(
                422,
                "research and blind dataset IDs must be disjoint",
            )
        dataset_ids = [*research_dataset_ids, *blind_dataset_ids]
        if len(dataset_ids) > 60:
            raise HTTPException(422, "campaign supports at most 60 total datasets")
        dataset_metadata: dict[str, ResearchDataset] = {}
        for dataset_id in dataset_ids:
            payload = await asyncio.to_thread(self.store.load_research_dataset, dataset_id)
            if not payload:
                raise HTTPException(404, f"research dataset not found: {dataset_id}")
            dataset_metadata[dataset_id] = ResearchDataset.model_validate(payload)
        if any(
            dataset_metadata[dataset_id].research_role == "blind"
            for dataset_id in research_dataset_ids
        ):
            raise HTTPException(422, "sealed blind datasets cannot be used for strategy selection")
        invalid_blind_ids = [
            dataset_id
            for dataset_id in blind_dataset_ids
            if dataset_metadata[dataset_id].research_role != "blind"
            or not dataset_metadata[dataset_id].blind_sealed
            or not dataset_metadata[dataset_id].blind_set_id
        ]
        if invalid_blind_ids:
            raise HTTPException(
                422,
                "blind datasets must be role-bound, fingerprinted and sealed: "
                + ", ".join(invalid_blind_ids),
            )
        blind_set_ids = {
            dataset_metadata[dataset_id].blind_set_id for dataset_id in blind_dataset_ids
        }
        if len(blind_set_ids) > 1:
            raise HTTPException(422, "blind datasets must belong to one sealed blind set")
        blind_set_id: str | None = None
        if blind_dataset_ids:
            blind_set_id = next(iter(blind_set_ids))
            invalid_development_ids = [
                dataset_id
                for dataset_id in research_dataset_ids
                if dataset_metadata[dataset_id].research_role != "development"
                or dataset_metadata[dataset_id].blind_set_id != blind_set_id
            ]
            if invalid_development_ids:
                raise HTTPException(
                    422,
                    "blind Campaign research datasets must be development splits "
                    "from the same sealed set: " + ", ".join(invalid_development_ids),
                )
            development_sources = {
                dataset_metadata[dataset_id].source_dataset_id
                for dataset_id in research_dataset_ids
            }
            blind_sources = {
                dataset_metadata[dataset_id].source_dataset_id for dataset_id in blind_dataset_ids
            }
            if development_sources != blind_sources:
                raise HTTPException(
                    422,
                    "development and blind datasets must contain matching source markets",
                )
        universe_snapshot = None
        point_in_time_selection_passed = False
        historical_survivorship_complete = False
        survivorship_bias_control_passed = False
        if request.universe_snapshot_id:
            universe_payload = await asyncio.to_thread(
                self.store.load_research_universe_snapshot,
                request.universe_snapshot_id,
            )
            if universe_payload is None:
                raise HTTPException(404, "research universe snapshot not found")
            universe_snapshot = ResearchUniverseSnapshot.model_validate(universe_payload)
            try:
                validate_universe_snapshot(universe_snapshot)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            expected_market_keys = {
                (
                    item.entry.exchange,
                    item.entry.symbol,
                    timeframe,
                    item.entry.market_type,
                )
                for item in universe_snapshot.instruments
                if item.selected
                for timeframe in universe_snapshot.policy.timeframes
            }
            actual_research_keys = {
                (
                    dataset_metadata[dataset_id].exchange,
                    dataset_metadata[dataset_id].symbol,
                    dataset_metadata[dataset_id].timeframe,
                    dataset_metadata[dataset_id].market_type,
                )
                for dataset_id in research_dataset_ids
            }
            if actual_research_keys != expected_market_keys or len(research_dataset_ids) != len(
                expected_market_keys
            ):
                raise HTTPException(
                    422,
                    "campaign datasets must cover the exact frozen point-in-time universe",
                )
            if blind_dataset_ids:
                actual_blind_keys = {
                    (
                        dataset_metadata[dataset_id].exchange,
                        dataset_metadata[dataset_id].symbol,
                        dataset_metadata[dataset_id].timeframe,
                        dataset_metadata[dataset_id].market_type,
                    )
                    for dataset_id in blind_dataset_ids
                }
                if actual_blind_keys != expected_market_keys or len(blind_dataset_ids) != len(
                    expected_market_keys
                ):
                    raise HTTPException(
                        422,
                        "blind datasets must cover the exact frozen point-in-time universe",
                    )
            insufficient_history: list[str] = []
            for market_key in sorted(expected_market_keys):
                timeframe = market_key[2]
                required_bars = universe_snapshot.policy.required_bars_by_timeframe[timeframe]
                matching_research = [
                    dataset_metadata[dataset_id]
                    for dataset_id in research_dataset_ids
                    if (
                        dataset_metadata[dataset_id].exchange,
                        dataset_metadata[dataset_id].symbol,
                        dataset_metadata[dataset_id].timeframe,
                        dataset_metadata[dataset_id].market_type,
                    )
                    == market_key
                ]
                matching_blind = [
                    dataset_metadata[dataset_id]
                    for dataset_id in blind_dataset_ids
                    if (
                        dataset_metadata[dataset_id].exchange,
                        dataset_metadata[dataset_id].symbol,
                        dataset_metadata[dataset_id].timeframe,
                        dataset_metadata[dataset_id].market_type,
                    )
                    == market_key
                ]
                coverage_datasets = [*matching_research, *matching_blind]
                covered_bars = sum(item.candle_count for item in coverage_datasets)
                coverage_ratio = Decimal(covered_bars) / Decimal(required_bars)
                quality_passed = all(
                    item.gap_count == 0 and item.invalid_count == 0 for item in coverage_datasets
                )
                if (
                    coverage_ratio < universe_snapshot.policy.minimum_history_coverage_ratio
                    or not quality_passed
                ):
                    exchange, symbol, _, market_type = market_key
                    insufficient_history.append(
                        f"{exchange}:{symbol}:{timeframe}:{market_type}={covered_bars}/{required_bars}"
                    )
            if insufficient_history:
                raise HTTPException(
                    422,
                    "campaign datasets do not satisfy the frozen history coverage "
                    "contract: " + ", ".join(insufficient_history),
                )
            pre_snapshot_datasets = [
                dataset_id
                for dataset_id in dataset_ids
                if dataset_metadata[dataset_id].data_start < universe_snapshot.as_of
            ]
            if pre_snapshot_datasets:
                raise HTTPException(
                    422,
                    "campaign datasets must start at or after the frozen point-in-time "
                    "universe snapshot: " + ", ".join(pre_snapshot_datasets),
                )
            point_in_time_selection_passed = bool(
                universe_snapshot.point_in_time_selection_passed
                and universe_snapshot.catalog_complete
            )
            prospective_coverage = True
            historical_survivorship_complete = bool(
                universe_snapshot.historical_survivorship_complete or prospective_coverage
            )
            survivorship_bias_control_passed = bool(
                point_in_time_selection_passed and historical_survivorship_complete
            )
        entry_ids = list(dict.fromkeys(request.library_entry_ids))
        candidate_ids = list(dict.fromkeys(request.candidate_ids))
        causal_policy_mode = bool(request.causal_policy_specs)
        causal_policy_definitions: list[dict] = []
        if causal_policy_mode:
            try:
                entries = [
                    self.build_causal_policy_campaign_entry(policy)
                    for policy in request.causal_policy_specs
                ]
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            policy_fingerprints = [entry.policy_fingerprint for entry in entries]
            if len(policy_fingerprints) != len(set(policy_fingerprints)):
                raise HTTPException(
                    422,
                    "causal policy Campaign hypotheses must be structurally unique",
                )
            causal_policy_definitions = [entry.frozen_definition for entry in entries]
        else:
            try:
                entries = (
                    [library_entry(entry_id) for entry_id in entry_ids]
                    if entry_ids
                    else list_strategy_library()
                    if not candidate_ids
                    else []
                )
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            for candidate_id in candidate_ids:
                payload = await asyncio.to_thread(
                    self.store.load_strategy_candidate,
                    candidate_id,
                )
                if not payload:
                    raise HTTPException(
                        404,
                        f"strategy candidate not found: {candidate_id}",
                    )
                candidate = self.parse_strategy_candidate_payload(
                    payload,
                    audit_context="research_campaign",
                )
                if candidate is None:
                    raise HTTPException(
                        409,
                        f"strategy candidate is stale: {candidate_id}",
                    )
                family = StrategyKind(candidate.spec["family"])
                entries.append(
                    StrategyLibraryEntry(
                        id=f"candidate:{candidate.id}",
                        name=f"Candidate {candidate.id}",
                        family=family,
                        archetype="agent_created_candidate",
                        origin=f"{candidate.source} generated strategy candidate",
                        source_url="https://local.invalid/research-candidate",
                        summary="Agent 创建或修改的可执行策略假设。",
                        indicators=[],
                        preferred_regimes=[],
                        required_data=infer_required_data(candidate.spec),
                        spec=candidate.spec,
                    )
                )
        research_matrix_size = len(research_dataset_ids) * len(entries)
        maximum_cell_count = research_matrix_size + len(blind_dataset_ids)
        if research_matrix_size == 0:
            raise HTTPException(422, "campaign matrix is empty")
        if maximum_cell_count > request.max_backtests:
            raise HTTPException(
                422,
                f"campaign requires at most {maximum_cell_count} cells; "
                "increase max_backtests or reduce scope",
            )
        datasets: list[ResearchDatasetBundle] = list(
            await asyncio.gather(
                *(
                    self.load_verified_research_dataset(dataset_id)
                    for dataset_id in research_dataset_ids
                )
            )
        )
        configuration = self.configuration_service.current_configuration()
        research_configuration = {
            "fee_bps": str(configuration.fee_bps),
            "slippage_bps": str(configuration.slippage_bps),
            "risk_fraction": str(configuration.risk.risk_per_trade),
            "minimum_reward_risk": str(configuration.risk.minimum_reward_risk),
            "validation": configuration.strategy.validation.model_dump(mode="json"),
            "campaign_engine_version": CAMPAIGN_ENGINE_VERSION,
            "campaign_hypothesis_mode": (
                "causal_agent_policy" if causal_policy_mode else "static_strategy"
            ),
            "causal_policy_definitions_fingerprint": (
                causal_policy_definitions_fingerprint(causal_policy_definitions)
            ),
            "universe_snapshot_fingerprint": (
                universe_snapshot.fingerprint if universe_snapshot is not None else None
            ),
            "survivorship_bias_control_passed": survivorship_bias_control_passed,
        }
        research_configuration_fingerprint = sha256(
            json.dumps(
                research_configuration,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:20]
        campaign_id = f"campaign_{uuid4().hex[:16]}"
        lineage_id = research_lineage_id(
            [dataset_metadata[dataset_id] for dataset_id in research_dataset_ids],
            blind_set_id=blind_set_id,
        )
        trial_family_id = research_trial_family_id(
            [dataset_metadata[dataset_id] for dataset_id in research_dataset_ids]
        )

        # Backfill the permanent ledger from durable reports before charging the
        # current experiment. Historical reports did not persist the configuration
        # fingerprint, so they share a conservative legacy configuration identity.
        historical_reports = await asyncio.to_thread(self.store.list_research_campaigns, 1000)
        current_research_ids = set(research_dataset_ids)
        ledger_dataset_payloads = await asyncio.to_thread(self.store.list_research_datasets, 5000)
        ledger_dataset_metadata = {
            str(payload["id"]): ResearchDataset.model_validate(payload)
            for payload in ledger_dataset_payloads
        }

        def source_lineage_keys(dataset_id_values: set[str]) -> set[tuple[str, ...]]:
            keys: set[tuple[str, ...]] = set()
            for dataset_id_value in dataset_id_values:
                dataset = ledger_dataset_metadata.get(dataset_id_value)
                if dataset is None:
                    continue
                keys.add(
                    (
                        dataset.source_dataset_id or dataset.id,
                        dataset.exchange,
                        dataset.market_type,
                        dataset.symbol,
                        dataset.timeframe,
                    )
                )
            return keys

        current_source_keys = source_lineage_keys(current_research_ids)
        lineage_history: list[dict] = []
        for historical in reversed(historical_reports):
            if historical.get("status") != "completed":
                continue
            historical_research_ids = set(
                historical.get("research_dataset_ids")
                or [
                    dataset_id
                    for dataset_id in historical.get("dataset_ids", [])
                    if dataset_id not in set(historical.get("blind_dataset_ids", []))
                ]
            )
            historical_datasets = [
                ledger_dataset_metadata[dataset_id]
                for dataset_id in historical_research_ids
                if dataset_id in ledger_dataset_metadata
            ]
            historical_trial_family_id = (
                research_trial_family_id(historical_datasets) if historical_datasets else ""
            )
            same_lineage = historical.get("research_lineage_id") == lineage_id
            historical_source_keys = source_lineage_keys(historical_research_ids)
            same_lineage = same_lineage or bool(
                current_source_keys
                and historical_source_keys == current_source_keys
                and len(historical_source_keys) == len(historical_research_ids)
            )
            if blind_set_id:
                same_lineage = same_lineage or historical.get("blind_set_id") == blind_set_id
            else:
                same_lineage = same_lineage or historical_research_ids == current_research_ids
            if same_lineage:
                lineage_history.append(historical)
            historical_fingerprints = sorted(
                {
                    str(cell.get("strategy_fingerprint"))
                    for cell in historical.get("cells", [])
                    if cell.get("strategy_fingerprint")
                    and cell.get("dataset_id") in historical_research_ids
                    and cell.get("dataset_role", "research") != "blind"
                }
            )
            if not historical_fingerprints:
                historical_fingerprints = sorted(
                    {
                        str(candidate.get("strategy_fingerprint"))
                        for candidate in historical.get("candidates", [])
                        if candidate.get("strategy_fingerprint")
                    }
                )
            if not historical_fingerprints:
                continue
            historical_configuration_fingerprint = str(
                historical.get("research_configuration_fingerprint") or "legacy_unknown"
            )
            historical_experiment_id = str(
                historical.get("research_experiment_id")
                or research_experiment_fingerprint(
                    lineage_id=str(
                        historical.get("research_lineage_id") or "legacy_unknown_lineage"
                    ),
                    engine_version=int(historical.get("engine_version") or 0),
                    research_configuration_fingerprint=(historical_configuration_fingerprint),
                    strategy_fingerprints=historical_fingerprints,
                )
            )
            if historical_trial_family_id == trial_family_id:
                await asyncio.to_thread(
                    self.store.register_research_trial_family_experiment,
                    family_id=trial_family_id,
                    experiment_id=historical_experiment_id,
                    campaign_id=str(historical.get("id") or historical_experiment_id),
                    engine_version=int(historical.get("engine_version") or 0),
                    research_configuration_fingerprint=(historical_configuration_fingerprint),
                    strategy_fingerprints=historical_fingerprints,
                )
            if not same_lineage:
                continue
            await asyncio.to_thread(
                self.store.register_research_campaign_experiment,
                lineage_id=lineage_id,
                experiment_id=historical_experiment_id,
                campaign_id=str(historical.get("id") or historical_experiment_id),
                engine_version=int(historical.get("engine_version") or 0),
                research_configuration_fingerprint=(historical_configuration_fingerprint),
                strategy_fingerprints=historical_fingerprints,
                blind_set_id=blind_set_id,
            )
            historical_blind_consumption = historical.get("blind_consumption_id")
            if historical_blind_consumption:
                await asyncio.to_thread(
                    self.store.mark_research_lineage_blind_opened,
                    lineage_id=lineage_id,
                    blind_consumption_id=str(historical_blind_consumption),
                )

        strategy_fingerprints = [entry.validated_spec.fingerprint for entry in entries]
        experiment_id = research_experiment_fingerprint(
            lineage_id=lineage_id,
            engine_version=CAMPAIGN_ENGINE_VERSION,
            research_configuration_fingerprint=research_configuration_fingerprint,
            strategy_fingerprints=strategy_fingerprints,
        )
        parent_report: ResearchCampaignReport | None = None
        if request.parent_campaign_id:
            parent_payload = await asyncio.to_thread(
                self.store.load_research_campaign,
                request.parent_campaign_id,
            )
            if parent_payload is None:
                raise HTTPException(404, "parent research campaign not found")
            parent_report = ResearchCampaignReport.model_validate(parent_payload)
            if parent_report.parent_campaign_id is not None:
                raise HTTPException(
                    409,
                    "bounded research permits only one successor on this lineage",
                )
            if parent_report.blind_consumption_id:
                raise HTTPException(
                    409,
                    "a consumed blind Campaign is terminal evidence and cannot be revised",
                )
            existing_successors = [
                payload
                for payload in lineage_history
                if payload.get("parent_campaign_id") == parent_report.id
            ]
            if existing_successors and not any(
                payload.get("research_experiment_id") == experiment_id
                for payload in existing_successors
            ):
                raise HTTPException(
                    409,
                    "bounded research permits only one successor per research phase",
                )
            if parent_report.research_lineage_id != lineage_id:
                raise HTTPException(
                    422,
                    "successor must use the exact parent evidence lineage",
                )
            if (
                set(parent_report.research_dataset_ids) != set(research_dataset_ids)
                or set(parent_report.blind_dataset_ids) != set(blind_dataset_ids)
                or parent_report.universe_snapshot_id != request.universe_snapshot_id
            ):
                raise HTTPException(
                    422,
                    "successor must reuse the parent's exact development, blind, "
                    "and frozen-universe scope",
                )
            parent_fingerprints = {
                cell.strategy_fingerprint
                for cell in parent_report.cells
                if cell.dataset_role != "blind"
            }
            current_fingerprints = set(strategy_fingerprints)
            if not parent_fingerprints.issubset(current_fingerprints):
                raise HTTPException(422, "successor must retain every frozen parent hypothesis")
            new_fingerprints = current_fingerprints - parent_fingerprints
            if not new_fingerprints or len(new_fingerprints) > 3:
                raise HTTPException(422, "successor requires one to three bounded child hypotheses")
            if causal_policy_mode:
                if not parent_report.causal_policy_definitions:
                    raise HTTPException(422, "causal policy successor requires a policy parent")
                allowed_definitions = {
                    str(item.get("policy_fingerprint")): item
                    for item in parent_report.authorized_policy_revision_definitions
                }
                if not new_fingerprints.issubset(
                    set(parent_report.authorized_policy_revision_fingerprints)
                ):
                    raise HTTPException(
                        422,
                        "causal policy successor contains a child not frozen by its parent",
                    )
                for entry in entries:
                    if entry.policy_fingerprint not in new_fingerprints:
                        continue
                    if allowed_definitions.get(entry.policy_fingerprint) != entry.frozen_definition:
                        raise HTTPException(
                            422,
                            "causal policy successor definition differs from its authorization",
                        )
            else:
                allowed_fingerprints = set(parent_report.authorized_revision_fingerprints)
                if not new_fingerprints.issubset(allowed_fingerprints):
                    raise HTTPException(422, "successor contains a child not frozen by its parent")
                new_candidate_ids = {
                    entry.id.removeprefix("candidate:")
                    for entry in entries
                    if entry.validated_spec.fingerprint in new_fingerprints
                    and entry.id.startswith("candidate:")
                }
                if len(new_candidate_ids) != len(
                    new_fingerprints
                ) or not new_candidate_ids.issubset(
                    set(parent_report.authorized_revision_candidate_ids)
                ):
                    raise HTTPException(
                        422,
                        (
                            "successor children must use the exact candidate IDs "
                            "authorized by its parent"
                        ),
                    )
        elif blind_set_id and lineage_history:
            exact_replay = any(
                payload.get("research_experiment_id") == experiment_id
                for payload in lineage_history
            )
            same_frozen_hypotheses = any(
                payload.get("parent_campaign_id") is None
                and {
                    str(cell.get("strategy_fingerprint"))
                    for cell in payload.get("cells", [])
                    if cell.get("strategy_fingerprint")
                    and cell.get("dataset_role", "research") != "blind"
                }
                == set(strategy_fingerprints)
                for payload in lineage_history
            )
            if not exact_replay and not same_frozen_hypotheses:
                raise HTTPException(
                    409,
                    "a new hypothesis set on a sealed evidence lineage requires "
                    "parent_campaign_id; only one bounded successor is permitted",
                )
        try:
            ledger_summary = await asyncio.to_thread(
                self.store.register_research_campaign_experiment,
                lineage_id=lineage_id,
                experiment_id=experiment_id,
                campaign_id=campaign_id,
                engine_version=CAMPAIGN_ENGINE_VERSION,
                research_configuration_fingerprint=research_configuration_fingerprint,
                strategy_fingerprints=strategy_fingerprints,
                blind_set_id=blind_set_id,
                trial_family_id=trial_family_id,
            )
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        replayed_report: ResearchCampaignReport | None = None
        if ledger_summary.get("replayed"):
            campaign_id = str(ledger_summary["campaign_id"])
            replayed_payload = await asyncio.to_thread(
                self.store.load_research_campaign,
                campaign_id,
            )
            if replayed_payload is not None:
                replayed_report = ResearchCampaignReport.model_validate(replayed_payload)
        effective_trial_count = int(ledger_summary["effective_trial_count"])
        if parent_report is not None:
            experiment_kind = (
                "bounded_policy_revision" if causal_policy_mode else "bounded_revision"
            )
            predecessor_campaign_id = parent_report.id
        elif lineage_history:
            experiment_kind = "structural_extension"
            predecessor_campaign_id = str(lineage_history[-1].get("id") or "") or None
        else:
            experiment_kind = "initial"
            predecessor_campaign_id = None

        # Complete all non-return data gates before reserving empirical trials.
        # Every eligible hypothesis is then charged exactly once before a cache or
        # backtest result can expose return-bearing evidence.
        feature_eligibility: dict[tuple[str, str], list[str]] = {}
        research_dataset_id_set = set(research_dataset_ids)
        for entry in entries:
            for (
                dataset,
                _candles,
                funding,
                open_interest,
                positioning,
                basis,
                order_flow,
                liquidation_flow,
            ) in datasets:
                if dataset.id not in research_dataset_id_set:
                    continue
                feature_eligibility[(entry.id, dataset.id)] = historical_feature_eligibility(
                    dataset=dataset,
                    required_data=entry.required_data,
                    funding=funding,
                    open_interest=open_interest,
                    positioning=positioning,
                    basis=basis,
                    order_flow=order_flow,
                    liquidation_flow=liquidation_flow,
                )

        fdr_controller = OnlineFDRController(self.store)
        permanent_trials: dict[str, PermanentTrialRecord] = {}
        fdr_allocations: dict[str, tuple[object, object]] = {}
        for entry in entries:
            eligible_dataset_fingerprints = sorted(
                dataset.data_fingerprint
                for dataset, *_rest in datasets
                if dataset.id in research_dataset_id_set
                and not feature_eligibility[(entry.id, dataset.id)]
            )
            if not eligible_dataset_fingerprints:
                continue
            dataset_scope_fingerprint = sha256(
                json.dumps(
                    eligible_dataset_fingerprints,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            artifact_fingerprint = sha256(
                json.dumps(
                    {
                        "engine_version": CAMPAIGN_ENGINE_VERSION,
                        "strategy_fingerprint": entry.validated_spec.fingerprint,
                        "dataset_scope_fingerprint": dataset_scope_fingerprint,
                        "validation_fingerprint": research_configuration_fingerprint,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            trial = PermanentTrialRecord.empirical(
                research_event_id=experiment_id,
                campaign_id=campaign_id,
                hypothesis_id=f"legacy_hypothesis:{entry.validated_spec.fingerprint}",
                family_id=f"{trial_family_id}:{entry.family.value}",
                candidate_id=entry.id,
                dataset_fingerprint=dataset_scope_fingerprint,
                validation_fingerprint=research_configuration_fingerprint,
                artifact_fingerprint=artifact_fingerprint,
            )
            persisted_trial = PermanentTrialRecord.model_validate(
                await asyncio.to_thread(self.store.reserve_research_trial, trial)
            )
            global_allocation = await asyncio.to_thread(
                fdr_controller.allocate_alpha,
                "research:global",
                scope="global",
                idempotency_key=f"{persisted_trial.idempotency_key}:global",
            )
            family_allocation = await asyncio.to_thread(
                fdr_controller.allocate_alpha,
                f"research:family:{persisted_trial.family_id}",
                scope=persisted_trial.family_id,
                idempotency_key=f"{persisted_trial.idempotency_key}:family",
            )
            permanent_trials[entry.id] = persisted_trial
            fdr_allocations[entry.id] = (global_allocation, family_allocation)

        semaphore = asyncio.Semaphore(CAMPAIGN_CELL_CONCURRENCY)
        completed_cells = 0
        cache_hits = 0
        computed_cells = 0

        async def report_progress() -> None:
            if progress_callback is not None:
                await progress_callback(
                    completed_cells,
                    maximum_cell_count,
                    cache_hits,
                    computed_cells,
                )

        await report_progress()

        async def evaluate_cell(
            entry,
            dataset: ResearchDataset,
            candles: list[Candle],
            funding: list[FundingRatePoint],
            open_interest: list[OpenInterestPoint],
            positioning: list[PositioningPoint],
            basis: list[BasisPoint],
            order_flow: list[OrderFlowBar],
            liquidation_flow: list[LiquidationFlowPoint],
            dataset_role: str,
            statistical_trial_count: int,
        ):
            nonlocal completed_cells, cache_hits, computed_cells
            async with semaphore:
                cache_key = campaign_cell_cache_key(
                    strategy_fingerprint=entry.validated_spec.fingerprint,
                    dataset_fingerprint=dataset.data_fingerprint,
                    dataset_id=dataset.id,
                    trial_count=statistical_trial_count,
                    research_configuration_fingerprint=(research_configuration_fingerprint),
                )
                data_ineligibility_reasons = feature_eligibility.get((entry.id, dataset.id), [])
                if data_ineligibility_reasons:
                    cell = ineligible_campaign_cell(
                        library_entry=entry,
                        dataset=dataset,
                        reasons=data_ineligibility_reasons,
                        dataset_role=dataset_role,
                    )
                    await asyncio.to_thread(
                        self.store.save_research_campaign_cell,
                        cache_key=cache_key,
                        strategy_fingerprint=entry.validated_spec.fingerprint,
                        dataset_fingerprint=dataset.data_fingerprint,
                        engine_version=CAMPAIGN_ENGINE_VERSION,
                        cell=cell,
                    )
                    computed_cells += 1
                    completed_cells += 1
                    await report_progress()
                    return cell
                cached_payload = await asyncio.to_thread(
                    self.store.load_research_campaign_cell,
                    cache_key,
                )
                if cached_payload is not None:
                    cell = ResearchCampaignCell.model_validate(cached_payload).model_copy(
                        update={"dataset_role": dataset_role}
                    )
                    cache_hits += 1
                    completed_cells += 1
                    await report_progress()
                    return cell
                if isinstance(entry, CausalPolicyCampaignEntry):
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
                        research_trial_count=statistical_trial_count,
                    )
                else:
                    candidate = create_candidate(entry.validated_spec)
                    result = await self.backtest_candidate_on_dataset(
                        candidate,
                        dataset,
                        candles,
                        funding,
                        open_interest,
                        positioning=positioning,
                        basis=basis,
                        order_flow=order_flow,
                        liquidation_flow=liquidation_flow,
                        research_trial_count=statistical_trial_count,
                    )
                cell = campaign_cell(
                    library_entry=entry,
                    dataset=dataset,
                    result=result,
                    backtest_fingerprint=backtest_artifact_fingerprint(result),
                    dataset_role=dataset_role,
                )
                await asyncio.to_thread(
                    self.store.save_research_campaign_cell,
                    cache_key=cache_key,
                    strategy_fingerprint=entry.validated_spec.fingerprint,
                    dataset_fingerprint=dataset.data_fingerprint,
                    engine_version=CAMPAIGN_ENGINE_VERSION,
                    cell=cell,
                )
                computed_cells += 1
                completed_cells += 1
                await report_progress()
                return cell

        research_tasks = [
            asyncio.create_task(
                evaluate_cell(
                    entry,
                    dataset,
                    candles,
                    funding,
                    open_interest,
                    positioning,
                    basis,
                    order_flow,
                    liquidation_flow,
                    "research",
                    effective_trial_count,
                )
            )
            for entry in entries
            for (
                dataset,
                candles,
                funding,
                open_interest,
                positioning,
                basis,
                order_flow,
                liquidation_flow,
            ) in datasets
            if dataset.id in set(research_dataset_ids)
        ]
        try:
            research_cells = await asyncio.gather(*research_tasks)
        except BaseException:
            for task in research_tasks:
                task.cancel()
            await asyncio.gather(*research_tasks, return_exceptions=True)
            raise
        research_candidates, research_pbo = aggregate_campaign(
            research_cells,
            cumulative_hypothesis_count=effective_trial_count,
        )
        online_fdr_results: dict[str, dict] = {}
        for candidate in research_candidates:
            allocations = fdr_allocations.get(candidate.library_entry_id)
            trial = permanent_trials.get(candidate.library_entry_id)
            if allocations is None or trial is None:
                continue
            global_allocation, family_allocation = allocations
            await asyncio.to_thread(
                fdr_controller.record_result,
                global_allocation,
                p_value=candidate.strategy_p_value,
            )
            await asyncio.to_thread(
                fdr_controller.record_result,
                family_allocation,
                p_value=candidate.strategy_p_value,
            )
            online_alpha = min(global_allocation.alpha, family_allocation.alpha)
            online_passed = candidate.strategy_p_value <= online_alpha
            online_fdr_results[candidate.library_entry_id] = {
                "alpha": str(online_alpha),
                "passed": online_passed,
            }
            if candidate.expected_r_testable_market_count == 0:
                trial_result = TrialResult.UNTESTABLE
            elif candidate.cross_market_passed and online_passed and research_pbo.passed:
                trial_result = TrialResult.PASS
            else:
                trial_result = TrialResult.FAIL
            await asyncio.to_thread(
                self.store.append_research_trial_result,
                trial.id,
                result=str(trial_result),
                p_value=candidate.strategy_p_value,
                adjusted_p_value=candidate.multiple_testing_adjusted_p_value,
                dsr=candidate.median_deflated_sharpe_probability,
                pbo=research_pbo.upper_confidence_bound,
                global_alpha=global_allocation.alpha,
                family_alpha=family_allocation.alpha,
                reason_codes=([] if online_passed else ["online_fdr_budget_not_met"]),
            )
        research_candidates = [
            candidate.model_copy(
                update={
                    "online_fdr_alpha": Decimal(
                        str(
                            online_fdr_results.get(candidate.library_entry_id, {}).get("alpha", "0")
                        )
                    ),
                    "online_fdr_passed": bool(
                        online_fdr_results.get(candidate.library_entry_id, {}).get("passed", False)
                    ),
                    "cross_market_passed": candidate.cross_market_passed
                    and bool(
                        online_fdr_results.get(candidate.library_entry_id, {}).get("passed", False)
                    ),
                }
            )
            for candidate in research_candidates
        ]
        provisional_selected = next(
            (candidate for candidate in research_candidates if candidate.cross_market_passed),
            None,
        )
        blind_cells: list[ResearchCampaignCell] = []
        blind_consumption_id: str | None = None
        blind_trial: PermanentTrialRecord | None = None
        if self.campaign_blind_opening_allowed(
            provisional_selected=provisional_selected,
            research_pbo=research_pbo,
            blind_dataset_ids=blind_dataset_ids,
            survivorship_bias_control_passed=survivorship_bias_control_passed,
        ):
            selected_entry = next(
                entry for entry in entries if entry.id == provisional_selected.library_entry_id
            )
            blind_consumption_id = f"blind_eval_{uuid4().hex[:16]}"
            reserved = await asyncio.to_thread(
                self.store.reserve_blind_set,
                blind_set_id=blind_set_id,
                strategy_fingerprint=selected_entry.validated_spec.fingerprint,
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
            blind_scope_fingerprint = sha256(
                json.dumps(
                    sorted(dataset_metadata[item].data_fingerprint for item in blind_dataset_ids),
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            blind_trial_candidate = PermanentTrialRecord.empirical(
                research_event_id=experiment_id,
                campaign_id=campaign_id,
                hypothesis_id=(f"legacy_hypothesis:{selected_entry.validated_spec.fingerprint}"),
                family_id=f"{trial_family_id}:{selected_entry.family.value}",
                candidate_id=selected_entry.id,
                dataset_fingerprint=blind_scope_fingerprint,
                validation_fingerprint=research_configuration_fingerprint,
                artifact_fingerprint=sha256(
                    f"blind:{experiment_id}:{selected_entry.validated_spec.fingerprint}:"
                    f"{blind_scope_fingerprint}".encode()
                ).hexdigest(),
            ).model_copy(
                update={
                    "trial_type": TrialType.BLIND,
                    "touched_blind": True,
                }
            )
            blind_trial = PermanentTrialRecord.model_validate(
                await asyncio.to_thread(self.store.reserve_research_trial, blind_trial_candidate)
            )
            blind_datasets = await asyncio.gather(
                *(
                    self.load_verified_research_dataset(dataset_id)
                    for dataset_id in blind_dataset_ids
                )
            )
            blind_cells = list(
                await asyncio.gather(
                    *(
                        evaluate_cell(
                            selected_entry,
                            dataset,
                            candles,
                            funding,
                            open_interest,
                            positioning,
                            basis,
                            order_flow,
                            liquidation_flow,
                            "blind",
                            1,
                        )
                        for (
                            dataset,
                            candles,
                            funding,
                            open_interest,
                            positioning,
                            basis,
                            order_flow,
                            liquidation_flow,
                        ) in blind_datasets
                    )
                )
            )
        elif progress_callback is not None:
            # The blind seal remains unopened when development evidence cannot nominate
            # one candidate. Report the actual terminal cell count to durable jobs.
            maximum_cell_count = len(research_cells)
            await report_progress()
        cells = [*research_cells, *blind_cells]
        report = finalize_campaign(
            dataset_ids=dataset_ids,
            blind_dataset_ids=blind_dataset_ids,
            blind_set_id=blind_set_id,
            blind_consumption_id=blind_consumption_id,
            library_entry_ids=[entry.id for entry in entries],
            causal_policy_definitions=causal_policy_definitions,
            cells=cells,
            campaign_id=campaign_id,
            research_lineage_id_value=lineage_id,
            research_trial_family_id_value=trial_family_id,
            research_experiment_id=experiment_id,
            research_configuration_fingerprint=research_configuration_fingerprint,
            cumulative_hypothesis_count=int(ledger_summary["cumulative_hypothesis_count"]),
            sequential_experiment_count=int(ledger_summary["sequential_experiment_count"]),
            effective_trial_count=effective_trial_count,
            permanent_trial_ids=[trial.id for trial in permanent_trials.values()],
            online_fdr_results=online_fdr_results,
            parent_campaign_id=(parent_report.id if parent_report is not None else None),
            predecessor_campaign_id=predecessor_campaign_id,
            experiment_kind=experiment_kind,
            universe_snapshot_id=(universe_snapshot.id if universe_snapshot is not None else None),
            universe_snapshot_fingerprint=(
                universe_snapshot.fingerprint if universe_snapshot is not None else None
            ),
            point_in_time_selection_passed=point_in_time_selection_passed,
            historical_survivorship_complete=historical_survivorship_complete,
            survivorship_bias_control_passed=survivorship_bias_control_passed,
        )
        if replayed_report is not None:
            report = report.model_copy(
                update={
                    "parent_campaign_id": replayed_report.parent_campaign_id,
                    "predecessor_campaign_id": replayed_report.predecessor_campaign_id,
                    "experiment_kind": replayed_report.experiment_kind,
                    "authorized_revision_candidate_ids": (
                        replayed_report.authorized_revision_candidate_ids
                    ),
                    "authorized_revision_fingerprints": (
                        replayed_report.authorized_revision_fingerprints
                    ),
                    "authorized_policy_revision_definitions": (
                        replayed_report.authorized_policy_revision_definitions
                    ),
                    "authorized_policy_revision_fingerprints": (
                        replayed_report.authorized_policy_revision_fingerprints
                    ),
                    "revision_source_library_entry_id": (
                        replayed_report.revision_source_library_entry_id
                    ),
                    "created_at": replayed_report.created_at,
                }
            )
        if report.autonomous_execution_eligible:
            report.execution_certificate = build_campaign_execution_certificate(
                report,
                dataset_fingerprints={
                    dataset_id: dataset_metadata[dataset_id].data_fingerprint
                    for dataset_id in dataset_ids
                },
            )
        await asyncio.to_thread(self.store.save_research_campaign, report)
        if blind_consumption_id is not None:
            await asyncio.to_thread(
                self.store.complete_blind_set_consumption,
                evaluation_id=blind_consumption_id,
                campaign_id=report.id,
                passed=report.blind_selected_candidate_passed,
            )
        if blind_trial is not None:
            await asyncio.to_thread(
                self.store.append_research_trial_result,
                blind_trial.id,
                result=str(
                    TrialResult.PASS if report.blind_selected_candidate_passed else TrialResult.FAIL
                ),
                reason_codes=[
                    "blind_passed"
                    if report.blind_selected_candidate_passed
                    else "blind_not_admitted"
                ],
            )
        self.add_audit(
            "research",
            "策略研究 Campaign 已完成",
            campaign_id=report.id,
            trial_count=report.trial_count,
            effective_trial_count=report.effective_trial_count,
            cumulative_hypothesis_count=report.cumulative_hypothesis_count,
            selected_library_entry_id=report.selected_library_entry_id,
            autonomous_execution_eligible=(report.autonomous_execution_eligible),
        )
        return report
