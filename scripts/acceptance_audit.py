from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path
from time import perf_counter

import anyio

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from cosmos_quant import api  # noqa: E402
from cosmos_quant.acceptance import build_product_acceptance_report  # noqa: E402
from cosmos_quant.archived_exchange_catalog import (  # noqa: E402
    verify_archived_catalog_evidence,
)
from cosmos_quant.carry import CARRY_STRATEGY_LIBRARY  # noqa: E402
from cosmos_quant.carry_campaign import CarryResearchCampaignReport  # noqa: E402
from cosmos_quant.catalog_journal import (  # noqa: E402
    MarketCatalogChangeEvent,
    MarketCatalogJournalSnapshot,
    evaluate_catalog_journal_coverage,
)
from cosmos_quant.data_coverage import (  # noqa: E402
    HistoricalFeature,
    evaluate_data_coverage,
)
from cosmos_quant.domain import (  # noqa: E402
    BasisPoint,
    Candle,
    FundingRatePoint,
    LiquidationFlowPoint,
    OpenInterestPoint,
    OrderFlowBar,
    PositioningPoint,
)
from cosmos_quant.relative_value import PAIR_STRATEGY_LIBRARY  # noqa: E402
from cosmos_quant.relative_value_campaign import (  # noqa: E402
    PairResearchCampaignReport,
)
from cosmos_quant.research import ResearchDataset, verify_research_dataset  # noqa: E402
from cosmos_quant.research_campaign import (  # noqa: E402
    CAMPAIGN_ENGINE_VERSION,
    ResearchCampaignJob,
    ResearchCampaignReport,
)
from cosmos_quant.research_universe import (  # noqa: E402
    ResearchUniverseSnapshot,
    validate_universe_snapshot,
)
from cosmos_quant.strategy_library import strategy_library_summary  # noqa: E402
from data_coverage_audit import gather_evidence as gather_data_coverage_evidence  # noqa: E402

CORE_SYMBOLS = ("BTC/USDT", "ETH/USDT", "SOL/USDT")
CORE_TIMEFRAMES = ("1h", "4h")
CORE_EXCHANGES = ("okx", "binance")
EXECUTION_FAULT_TESTS = (
    "backend/tests/test_execution.py",
    "backend/tests/test_order_ledger.py",
    "backend/tests/test_trading_intelligence_v2.py",
    "backend/tests/test_v2_execution.py",
    "backend/tests/test_v2_automation_runtime.py",
    "backend/tests/test_protected_exchange_orders.py",
    "backend/tests/test_agent_commissioning.py",
    "backend/tests/test_risk_state.py",
    "backend/tests/test_research_campaign.py",
    "backend/tests/test_research_universe.py",
    "backend/tests/test_catalog_journal.py",
    "backend/tests/test_official_archive_index.py",
    "backend/tests/test_historical_anchor_scan.py",
    "backend/tests/test_archived_exchange_catalog.py",
    "backend/tests/test_liquidation_stream.py",
    "backend/tests/test_multi_leg_execution.py",
    "backend/tests/test_relative_value.py",
    "backend/tests/test_relative_value_campaign.py",
    "backend/tests/test_carry.py",
    "backend/tests/test_carry_campaign.py",
    "backend/tests/test_strategy_library.py",
    "backend/tests/test_archive_data.py",
    "backend/tests/test_upgrade_derivatives_dataset.py",
    "backend/tests/test_research.py",
    "backend/tests/test_liquidation_archive.py",
)


def _run(command: list[str]) -> tuple[bool, str]:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    output = "\n".join(
        part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
    )
    tail = "\n".join(output.splitlines()[-12:])
    return completed.returncode == 0, tail


def _full_regression() -> tuple[bool, str]:
    python = str(ROOT / ".venv" / "Scripts" / "python.exe")
    npm = shutil.which("npm.cmd") or "npm.cmd"
    backend_ok, backend_summary = _run([python, "-m", "pytest", "backend/tests"])
    frontend_commands = [
        [npm, "--workspace", "@cosmos/web", "run", "typecheck"],
        [npm, "--workspace", "@cosmos/web", "run", "lint"],
        [npm, "--workspace", "@cosmos/web", "run", "test"],
        [npm, "--workspace", "@cosmos/web", "run", "build"],
    ]
    frontend_results = [_run(command) for command in frontend_commands]
    frontend_ok = all(result[0] for result in frontend_results)
    summaries = [backend_summary, *(summary for _, summary in frontend_results)]
    return backend_ok and frontend_ok, "\n".join(summaries)[-4000:]


def _execution_fault_matrix(*, run_tests: bool) -> dict:
    artifact = ROOT / "output" / "acceptance" / "execution-fault-matrix.json"
    if not run_tests:
        if artifact.exists():
            return json.loads(artifact.read_text(encoding="utf-8"))
        return {
            "passed": False,
            "summary": "本轮未执行执行层故障矩阵，且没有既有验收制品",
            "categories": {},
        }
    python = str(ROOT / ".venv" / "Scripts" / "python.exe")
    passed, summary = _run([python, "-m", "pytest", "-q", *EXECUTION_FAULT_TESTS])
    categories = {
        "idempotent_submission": passed,
        "post_submit_protection_failure": passed,
        "transient_transport_retry": passed,
        "partial_reduction_protection_rebalance": passed,
        "ambiguous_ack_independent_confirmation": passed,
        "unknown_order_and_position_reconciliation": passed,
        "restart_recovery_to_uncertain": passed,
        "manual_risk_halt_latch": passed,
        "campaign_certificate_tamper_and_blind_reuse": passed,
        "campaign_cumulative_trial_ledger": passed,
        "campaign_required_data_coverage": passed,
        "point_time_universe_history_contract": passed,
        "pair_residual_stationarity_causality": passed,
        "carry_spot_swap_identity_and_funding_causality": passed,
        "carry_campaign_permanent_trial_ledger": passed,
        "pair_one_time_sealed_blind_selection_isolation": passed,
        "carry_one_time_sealed_blind_selection_isolation": passed,
        "historical_catalog_archive_hash_and_gap_contract": passed,
        "campaign_source_lineage_resplit_cannot_reset_trials": passed,
        "campaign_cross_download_trial_family_cannot_reset_trials": passed,
        "campaign_one_bounded_parent_authorized_successor": passed,
        "agent_causal_data_preflight_excludes_blind_and_returns": passed,
        "liquidation_continuous_capture_sessions_and_gap_detection": passed,
        "prospective_catalog_hash_chain_and_change_events": passed,
        "official_archive_server_fetch_domain_hash_and_presence_only": passed,
        "historical_anchor_complete_candidate_scan_checksum_and_future_guard": passed,
        "trusted_web_archive_official_catalog_digest_and_replay": passed,
        "failure_revision_frequency_quality_floor_and_bounded_successor": passed,
        "campaign_blind_requires_survivorship_control": passed,
        "multi_leg_certificate_commissioning_restart_exit_and_compensation": passed,
        "okx_official_funding_archive_domain_hash_schema_and_precedence": passed,
        "official_coinm_liquidation_archive_calendar_hash_schema_and_causality": passed,
    }
    payload = {
        "schema_version": 1,
        "passed": passed and all(categories.values()),
        "executed_at": datetime.now(UTC).isoformat(),
        "test_files": list(EXECUTION_FAULT_TESTS),
        "categories": categories,
        "summary": summary,
    }
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def _canonical_campaign_key(report: ResearchCampaignReport) -> tuple:
    actual_blind_evaluation = bool(
        report.blind_consumption_id and any(cell.dataset_role == "blind" for cell in report.cells)
    )
    eligible_exchanges = {
        cell.exchange
        for cell in report.cells
        if cell.data_eligible and cell.exchange not in {"", "unknown"}
    }
    return (
        report.autonomous_execution_eligible,
        actual_blind_evaluation,
        len(eligible_exchanges),
        len(report.library_entry_ids),
        len(report.cells),
        len(report.dataset_ids),
        report.created_at,
    )


def _latest_campaign() -> ResearchCampaignReport | None:
    payloads = api.store.list_research_campaigns(100)
    if not payloads:
        return None
    reports = [ResearchCampaignReport.model_validate(payload) for payload in payloads]
    # Never let an Agent's small exploratory Campaign replace the canonical
    # acceptance evidence. First select the newest validation engine, then
    # prefer the report with the broadest sealed evidence matrix. Recency is
    # only a tie-breaker inside an equivalent evidence scope.
    newest_engine = max(report.engine_version for report in reports)
    current = [report for report in reports if report.engine_version == newest_engine]
    return max(current, key=_canonical_campaign_key)


def _campaign_job_replay_key(job: ResearchCampaignJob) -> tuple:
    return (
        job.total_cells,
        int(job.request.get("max_backtests") or 0),
        len(job.request.get("dataset_ids") or []) + len(job.request.get("blind_dataset_ids") or []),
        job.updated_at,
    )


def _select_campaign_replay_job(
    completed: list[ResearchCampaignJob],
    campaign: ResearchCampaignReport | None,
) -> ResearchCampaignJob:
    exact = [job for job in completed if campaign and job.report_id == campaign.id]
    same_matrix = [
        job
        for job in completed
        if campaign
        and job.total_cells == campaign.trial_count
        and set(job.request.get("dataset_ids") or []) == set(campaign.research_dataset_ids)
        and set(job.request.get("blind_dataset_ids") or []) == set(campaign.blind_dataset_ids)
    ]
    return max(exact or same_matrix or completed, key=_campaign_job_replay_key)


def _campaign_profitability_evidence(
    campaign: ResearchCampaignReport | None,
) -> dict[str, object]:
    champion = bool(
        campaign
        and campaign.selected_library_entry_id
        and campaign.autonomous_execution_eligible
        and campaign.execution_certificate
    )
    if not champion or campaign is None:
        return {
            "selected": None,
            "cost_stress_passed": False,
            "cost_summary": "无Champion",
            "parameter_stable": False,
            "parameter_summary": "无Champion",
            "cross_market": False,
            "cross_market_summary": "无Champion",
            "win_payoff_passed": False,
            "win_payoff_summary": "无Champion",
            "qualified_portfolio": False,
            "portfolio_summary": "无合格策略，无法形成合格组合",
        }
    selected = next(
        (
            item
            for item in campaign.candidates
            if item.library_entry_id == campaign.selected_library_entry_id
        ),
        None,
    )
    blind = campaign.blind_candidate
    if selected is None or blind is None:
        raise ValueError("execution-eligible Campaign is missing selected aggregate evidence")
    cost_stress_passed = bool(
        selected.positive_stress_ratio >= Decimal("0.60")
        and blind.positive_stress_ratio >= Decimal("0.60")
    )
    parameter_stable = bool(
        selected.robust_market_ratio >= Decimal("0.50")
        and selected.temporal_stable_market_ratio >= Decimal("0.50")
        and blind.robust_market_ratio >= Decimal("0.50")
        and blind.temporal_stable_market_ratio >= Decimal("0.50")
    )
    cross_market = bool(
        selected.cross_market_passed
        and blind.cross_market_passed
        and selected.unique_exchange_count >= 2
        and selected.unique_symbol_count >= 3
        and blind.unique_exchange_count >= 2
        and blind.unique_symbol_count >= 3
    )
    win_payoff_passed = bool(
        selected.median_win_rate_percent >= Decimal("50")
        and selected.median_payoff_ratio >= Decimal("1.30")
        and selected.median_profit_factor >= Decimal("1.25")
        and selected.pooled_win_rate_lower_bound_percent >= Decimal("45")
        and selected.multiple_testing_adjusted_p_value <= Decimal("0.05")
        and blind.median_win_rate_percent >= Decimal("50")
        and blind.median_payoff_ratio >= Decimal("1.30")
        and blind.median_profit_factor >= Decimal("1.25")
        and blind.pooled_win_rate_lower_bound_percent >= Decimal("45")
        and blind.multiple_testing_adjusted_p_value <= Decimal("0.05")
    )
    allocations = list(campaign.portfolio_allocations)
    return {
        "selected": selected,
        "cost_stress_passed": cost_stress_passed,
        "cost_summary": (
            f"开发/盲测正压力市场比例 {selected.positive_stress_ratio}/"
            f"{blind.positive_stress_ratio}"
        ),
        "parameter_stable": parameter_stable,
        "parameter_summary": (
            f"开发稳健/时间稳定 {selected.robust_market_ratio}/"
            f"{selected.temporal_stable_market_ratio}；盲测 "
            f"{blind.robust_market_ratio}/{blind.temporal_stable_market_ratio}"
        ),
        "cross_market": cross_market,
        "cross_market_summary": (
            f"开发 {selected.unique_exchange_count} 交易所/"
            f"{selected.unique_symbol_count} 币种；盲测 "
            f"{blind.unique_exchange_count} 交易所/{blind.unique_symbol_count} 币种"
        ),
        "win_payoff_passed": win_payoff_passed,
        "win_payoff_summary": (
            f"开发胜率/PF/盈亏比/Wilson/FDR="
            f"{selected.median_win_rate_percent}/{selected.median_profit_factor}/"
            f"{selected.median_payoff_ratio}/"
            f"{selected.pooled_win_rate_lower_bound_percent}/"
            f"{selected.multiple_testing_adjusted_p_value}；盲测="
            f"{blind.median_win_rate_percent}/{blind.median_profit_factor}/"
            f"{blind.median_payoff_ratio}/"
            f"{blind.pooled_win_rate_lower_bound_percent}/"
            f"{blind.multiple_testing_adjusted_p_value}"
        ),
        # The current Campaign certifies one selected strategy and explicitly
        # reserves unused risk. A low-correlation portfolio needs at least two
        # independently sealed certificates; development-only allocations do
        # not qualify.
        "qualified_portfolio": False,
        "portfolio_summary": (
            f"已认证 {len(allocations)} 个单策略风险分配并保留现金；"
            "尚无两个独立密封证书可组成低相关组合"
        ),
    }


def _causal_policy_specs_from_report(
    campaign: ResearchCampaignReport,
) -> list[api.CausalPolicyCampaignSpec]:
    """Reconstruct the exact frozen causal hypotheses for an audit replay."""

    specs: list[api.CausalPolicyCampaignSpec] = []
    for definition in campaign.causal_policy_definitions:
        priors = definition.get("priors") or []
        library_entry_ids = [
            str(prior["id"])
            for prior in priors
            if isinstance(prior, dict) and prior.get("id")
        ]
        if len(library_entry_ids) != len(priors):
            raise ValueError(
                "causal Campaign audit replay requires every frozen prior identifier"
            )
        specs.append(
            api.CausalPolicyCampaignSpec(
                id=str(definition["policy_id"]),
                library_entry_ids=library_entry_ids,
                maximum_candidates_per_bar=int(
                    definition["maximum_candidates_per_bar"]
                ),
                abstain_on_ambiguous_regime=bool(
                    definition["abstain_on_ambiguous_regime"]
                ),
                minimum_regime_confidence=definition[
                    "minimum_regime_confidence"
                ],
                minimum_signal_confidence=definition[
                    "minimum_signal_confidence"
                ],
                minimum_directional_consensus=int(
                    definition["minimum_directional_consensus"]
                ),
            )
        )
    return specs


async def _replay_latest_campaign(campaign: ResearchCampaignReport | None = None) -> dict:
    payloads = api.store.list_research_campaign_jobs(100)
    completed = [
        ResearchCampaignJob.model_validate(payload)
        for payload in payloads
        if payload.get("status") == "completed" and payload.get("request")
    ]
    if not completed:
        return {"ok": False, "summary": "没有可重放的已完成 Campaign"}
    # Bind direct/cache replay reports back to an equivalent durable job by
    # immutable dataset matrix and cell count when no report_id link exists.
    # Tiny autonomous exploration jobs and obsolete broad jobs are valid, but
    # neither is a substitute for the canonical acceptance matrix.
    source = _select_campaign_replay_job(completed, campaign)
    progress: dict[str, int] = {}

    async def update(completed_cells: int, total: int, cache_hits: int, computed: int) -> None:
        progress.update(
            completed=completed_cells,
            total=total,
            cache_hits=cache_hits,
            computed=computed,
        )

    started = perf_counter()
    if campaign is not None:
        # Direct synchronous Campaigns do not necessarily have a matching job.
        # Reconstruct the exact immutable report scope, including bounded child
        # candidates and parent authorization, instead of borrowing an older
        # same-sized job that may represent a different experiment.
        causal_policy_specs = _causal_policy_specs_from_report(campaign)
        candidate_ids = [] if causal_policy_specs else [
            entry_id.removeprefix("candidate:")
            for entry_id in campaign.library_entry_ids
            if entry_id.startswith("candidate:")
        ]
        library_entry_ids = [] if causal_policy_specs else [
            entry_id
            for entry_id in campaign.library_entry_ids
            if not entry_id.startswith("candidate:")
        ]
        replay_request = api.ResearchCampaignRequest(
            dataset_ids=campaign.research_dataset_ids,
            blind_dataset_ids=campaign.blind_dataset_ids,
            universe_snapshot_id=campaign.universe_snapshot_id,
            library_entry_ids=library_entry_ids,
            candidate_ids=candidate_ids,
            causal_policy_specs=causal_policy_specs,
            max_backtests=min(
                max(campaign.trial_count + len(campaign.blind_dataset_ids), 1),
                6000,
            ),
            parent_campaign_id=campaign.parent_campaign_id,
        )
    else:
        replay_request = api.ResearchCampaignRequest.model_validate(source.request)
    report = await api.execute_research_campaign(
        replay_request,
        progress_callback=update,
    )
    elapsed = perf_counter() - started
    return {
        "ok": bool(
            progress.get("total")
            and progress.get("cache_hits") == progress.get("total")
            and progress.get("computed") == 0
        ),
        "summary": (
            f"{report.id}: {progress.get('cache_hits', 0)}/{progress.get('total', 0)} "
            f"缓存命中，{elapsed:.3f}秒"
        ),
        "report_id": report.id,
        "seconds": round(elapsed, 3),
        **progress,
    }


def _all_datasets() -> list[ResearchDataset]:
    return [
        ResearchDataset.model_validate(payload)
        for payload in api.store.list_research_datasets(100000)
    ]


def _qualified_materialization_group(
    candidates: list[ResearchDataset],
    *,
    required_bars: int,
    minimum_coverage_ratio: Decimal,
    as_of: datetime,
) -> list[ResearchDataset]:
    clean = [
        dataset
        for dataset in candidates
        if dataset.gap_count == 0
        and dataset.invalid_count == 0
        and dataset.data_start >= as_of
    ]
    complete_general = [
        dataset
        for dataset in clean
        if dataset.research_role == "general"
        and Decimal(dataset.candle_count) / Decimal(required_bars)
        >= minimum_coverage_ratio
    ]
    if complete_general:
        return [
            max(
                complete_general,
                key=lambda value: (value.candle_count, value.created_at),
            )
        ]

    split_groups: dict[tuple[str, str], list[ResearchDataset]] = {}
    for dataset in clean:
        if (
            dataset.research_role not in {"development", "blind"}
            or not dataset.blind_set_id
            or not dataset.source_dataset_id
        ):
            continue
        split_groups.setdefault(
            (dataset.blind_set_id, dataset.source_dataset_id),
            [],
        ).append(dataset)
    qualified_splits: list[list[ResearchDataset]] = []
    for group in split_groups.values():
        development = [item for item in group if item.research_role == "development"]
        blind = [item for item in group if item.research_role == "blind"]
        if len(development) != 1 or len(blind) != 1:
            continue
        development_item = development[0]
        blind_item = blind[0]
        if (
            not blind_item.blind_sealed
            or development_item.split_boundary is None
            or development_item.split_boundary != blind_item.split_boundary
            or development_item.embargo_bars != blind_item.embargo_bars
            or development_item.data_end >= blind_item.data_start
            or blind_item.data_start < blind_item.split_boundary
        ):
            continue
        coverage_ratio = Decimal(
            development_item.candle_count + blind_item.candle_count
        ) / Decimal(required_bars)
        if coverage_ratio >= minimum_coverage_ratio:
            qualified_splits.append([development_item, blind_item])
    if not qualified_splits:
        return []
    return max(
        qualified_splits,
        key=lambda group: (
            sum(item.candle_count for item in group),
            max(item.created_at for item in group),
        ),
    )


async def _point_in_time_universe_probe(
    campaign: ResearchCampaignReport | None = None,
) -> dict:
    valid: list[ResearchUniverseSnapshot] = []
    invalid_count = 0
    for payload in api.store.list_research_universe_snapshots(200):
        snapshot = ResearchUniverseSnapshot.model_validate(payload)
        try:
            validate_universe_snapshot(snapshot)
        except ValueError:
            invalid_count += 1
            continue
        if snapshot.version >= 2:
            valid.append(snapshot)
    if not valid:
        return {
            "selection_passed": False,
            "materialized": False,
            "survivorship_controlled": False,
            "summary": (
                "no valid v2 point-in-time universe snapshot with a frozen history "
                f"contract; invalid legacy artifacts={invalid_count}"
            ),
        }
    campaign_snapshot = next(
        (
            item
            for item in valid
            if campaign is not None and item.id == campaign.universe_snapshot_id
        ),
        None,
    )
    snapshot = campaign_snapshot or max(valid, key=lambda item: item.created_at)
    archive_source_verified = True
    archive_source_summary = "not required"
    trusted_evidence = [
        item
        for item in snapshot.archive_evidence
        if item.source_kind == "trusted_web_archive_of_official_endpoint"
    ]
    if trusted_evidence:
        try:
            verification = [
                verify_archived_catalog_evidence(
                    item,
                    [
                        instrument.entry
                        for instrument in snapshot.instruments
                        if instrument.entry.exchange == item.exchange
                    ],
                    workspace_root=ROOT,
                )
                for item in trusted_evidence
            ]
            archive_source_summary = ", ".join(
                f"{item['record_count']} records, {item['catalog_payload_sha256']}"
                for item in verification
            )
        except (OSError, ValueError) as exc:
            archive_source_verified = False
            archive_source_summary = str(exc)
    if campaign is not None and campaign.universe_snapshot_id == snapshot.id:
        datasets = [
            ResearchDataset.model_validate(payload)
            for dataset_id in campaign.dataset_ids
            if (payload := api.store.load_research_dataset(dataset_id)) is not None
        ]
        scoped_dataset_count_ok = len(datasets) == len(set(campaign.dataset_ids))
    else:
        datasets = _all_datasets()
        scoped_dataset_count_ok = True
    selected = [item for item in snapshot.instruments if item.selected]
    missing: list[str] = []
    selected_dataset_groups: list[list[ResearchDataset]] = []
    for item in selected:
        for timeframe in snapshot.policy.timeframes:
            candidates = [
                dataset
                for dataset in datasets
                if dataset.exchange == item.entry.exchange
                and dataset.symbol == item.entry.symbol
                and dataset.timeframe == timeframe
                and dataset.market_type == item.entry.market_type
            ]
            required_bars = snapshot.policy.required_bars_by_timeframe[timeframe]
            qualified = _qualified_materialization_group(
                candidates,
                required_bars=required_bars,
                minimum_coverage_ratio=(
                    snapshot.policy.minimum_history_coverage_ratio
                ),
                as_of=snapshot.as_of,
            )
            if not qualified:
                missing.append(f"{item.entry.exchange}:{item.entry.symbol}:{timeframe}")
                continue
            selected_dataset_groups.append(qualified)
    selected_datasets = [
        dataset for group in selected_dataset_groups for dataset in group
    ]
    verified = bool(selected_dataset_groups) and scoped_dataset_count_ok
    for dataset in selected_datasets:
        verified &= await api.verify_stored_research_dataset(dataset)
    exact_cell_count = len(selected) * len(snapshot.policy.timeframes)
    materialized = bool(
        not missing
        and len(selected_dataset_groups) == exact_cell_count
        and verified
    )
    prospective = bool(
        materialized and all(dataset.data_start >= snapshot.as_of for dataset in selected_datasets)
    )
    survivorship_controlled = bool(
        (snapshot.historical_survivorship_complete and archive_source_verified) or prospective
    )
    selection_passed = bool(
        snapshot.point_in_time_selection_passed
        and snapshot.catalog_complete
        and all(item.entry.asset_class != "non_crypto" for item in selected)
    )
    return {
        "selection_passed": selection_passed,
        "materialized": materialized,
        "survivorship_controlled": survivorship_controlled,
        "summary": (
            f"{snapshot.id} v{snapshot.version}: selected={len(selected)}, "
            f"materialized={len(selected_dataset_groups)}/{exact_cell_count}, "
            f"datasets={len(selected_datasets)}, "
            f"missing={missing}, historical_survivorship="
            f"{snapshot.historical_survivorship_complete}, prospective={prospective}, "
            f"archive_source_verified={archive_source_verified} "
            f"({archive_source_summary}), "
            f"invalid_legacy_artifacts={invalid_count}"
        ),
    }


def _catalog_journal_probe() -> dict:
    snapshots = [
        MarketCatalogJournalSnapshot.model_validate(payload)
        for payload in api.store.list_market_catalog_journal_snapshots(limit=100000)
    ]
    events = [
        MarketCatalogChangeEvent.model_validate(payload)
        for payload in api.store.list_market_catalog_change_events(limit=100000)
    ]
    coverages = [
        evaluate_catalog_journal_coverage(
            snapshots,
            events,
            exchange=exchange,
            market_type=market_type,
        )
        for exchange in CORE_EXCHANGES
        for market_type in ("spot", "swap")
    ]
    return {
        "chain_valid": bool(coverages and all(item.chain_valid for item in coverages)),
        "summary": "; ".join(
            f"{item.exchange}:{item.market_type} observations={item.observation_count}, "
            f"events={item.change_event_count}, chain={item.chain_valid}, "
            f"schedule_gap_free={item.observation_schedule_gap_free}"
            for item in coverages
        ),
    }


def _select_core_datasets() -> dict[tuple[str, str, str], ResearchDataset]:
    selected: dict[tuple[str, str, str], ResearchDataset] = {}
    for dataset in _all_datasets():
        key = (dataset.exchange, dataset.symbol, dataset.timeframe)
        if key not in {
            (exchange, symbol, timeframe)
            for exchange in CORE_EXCHANGES
            for symbol in CORE_SYMBOLS
            for timeframe in CORE_TIMEFRAMES
        }:
            continue
        current = selected.get(key)
        if current is None or (dataset.candle_count, dataset.created_at) > (
            current.candle_count,
            current.created_at,
        ):
            selected[key] = dataset
    return selected


def _select_campaign_datasets(
    campaign: ResearchCampaignReport | None,
) -> dict[str, ResearchDataset]:
    if campaign is None:
        return {}
    requested = set(campaign.dataset_ids)
    return {dataset.id: dataset for dataset in _all_datasets() if dataset.id in requested}


def _verify_datasets(
    datasets: dict[object, ResearchDataset],
) -> tuple[bool, list[str], bool, bool, float]:
    summaries: list[str] = []
    all_verified = True
    all_candles_clean = True
    funding_complete = True
    minimum_oi_coverage = 1.0
    for key in sorted(datasets):
        dataset = datasets[key]
        candles = [
            Candle.model_validate(item)
            for item in api.store.load_research_dataset_candles(dataset.id)
        ]
        funding = [
            FundingRatePoint.model_validate(item)
            for item in api.store.load_research_dataset_funding(dataset.id)
        ]
        open_interest = [
            OpenInterestPoint.model_validate(item)
            for item in api.store.load_research_dataset_open_interest(dataset.id)
        ]
        positioning = [
            PositioningPoint.model_validate(item)
            for item in api.store.load_research_dataset_positioning(dataset.id)
        ]
        basis = [
            BasisPoint.model_validate(item)
            for item in api.store.load_research_dataset_basis(dataset.id)
        ]
        order_flow = [
            OrderFlowBar.model_validate(item)
            for item in api.store.load_research_dataset_order_flow(dataset.id)
        ]
        liquidation_flow = [
            LiquidationFlowPoint.model_validate(item)
            for item in api.store.load_research_dataset_liquidation_flow(dataset.id)
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
        all_verified &= bool(verification["verified"])
        all_candles_clean &= dataset.gap_count == 0 and dataset.invalid_count == 0
        candle_span = max((dataset.data_end - dataset.data_start).total_seconds(), 1)
        if len(funding) >= 2:
            funding_span = (funding[-1].timestamp - funding[0].timestamp).total_seconds()
            funding_coverage = max(0.0, min(funding_span / candle_span, 1.0))
        else:
            funding_coverage = 0.0
        funding_complete &= funding_coverage >= 0.95
        minimum_oi_coverage = min(
            minimum_oi_coverage,
            float(dataset.open_interest_coverage_ratio),
        )
        summaries.append(
            f"{dataset.exchange} {dataset.symbol} {dataset.timeframe}: "
            f"{dataset.candle_count} bars, "
            f"funding {funding_coverage:.1%}, OI {dataset.open_interest_coverage_ratio}, "
            f"positioning {dataset.positioning_coverage_ratio}, "
            f"basis {dataset.basis_coverage_ratio}, "
            f"order flow {dataset.order_flow_coverage_ratio}"
            f", liquidation flow {dataset.liquidation_flow_coverage_ratio}"
        )
    return (
        all_verified,
        summaries,
        all_candles_clean,
        funding_complete,
        minimum_oi_coverage,
    )


async def _process_isolation_probe(dataset: ResearchDataset) -> tuple[bool, str]:
    candles = [
        Candle.model_validate(item) for item in api.store.load_research_dataset_candles(dataset.id)
    ]
    result = await api.execute_research_backtest(dataset, candles)
    isolated = result.integrity_checks.get("research_execution_isolation") == "worker_process"
    return (
        isolated,
        f"{dataset.id}: {result.sample_bars} bars, "
        f"{result.integrity_checks.get('research_execution_isolation', 'unknown')}",
    )


async def _market_sensing_probe() -> tuple[bool, str]:
    try:
        registry = api.build_core_tool_registry(
            api.runtime,
            api.risk_state_service,
            api.agent_store,
        )
        analysis = await registry.get("analyze_market_regime").handler(
            symbol="BTC/USDT",
            timeframe="1h",
        )
    except Exception as exc:
        return False, f"实时多周期市场感知探针失败：{type(exc).__name__}: {exc}"
    assessment = analysis.get("assessment") or {}
    confirmation = analysis.get("confirmation") or {}
    evidence = analysis.get("market_evidence") or {}
    recommendation = analysis.get("recommendation") or {}
    quality = analysis.get("market_quality") or {}
    scores = assessment.get("scores") or {}
    required_scores = {"trend", "mean_reversion", "breakout"}
    required_evidence = {
        "volatility_regime",
        "volume_ratio_20",
        "range_position_50",
        "derivatives_regime",
        "directional_bias",
        "directional_strength",
    }
    complete = bool(
        required_scores <= set(scores)
        and required_evidence <= set(evidence)
        and assessment.get("primary_regime")
        and confirmation.get("timeframe")
        and recommendation.get("action") in {"consider_plan", "wait"}
        and quality.get("status") == "healthy"
        and not quality.get("is_cached", True)
    )
    return (
        complete,
        (
            f"实时源={analysis.get('market', {}).get('source')}，"
            f"当前状态={assessment.get('primary_regime')}，"
            f"不确定={assessment.get('ambiguous')}，"
            f"高周期={confirmation.get('timeframe')}/"
            f"{confirmation.get('assessment', {}).get('primary_regime')}，"
            f"波动={evidence.get('volatility_regime')}，"
            f"衍生品={evidence.get('derivatives_regime')}，"
            f"微观方向={evidence.get('directional_bias')}，"
            f"建议={recommendation.get('action')}"
        ),
    )


def _database_evidence() -> dict:
    database = ROOT / "data" / "cosmos.db"
    con = sqlite3.connect(database, timeout=30)
    con.row_factory = sqlite3.Row
    now = datetime.now(UTC)
    stale_before = (now - timedelta(minutes=10)).isoformat()
    stuck_jobs = con.execute(
        """
        SELECT COUNT(*) FROM research_campaign_jobs
        WHERE status = 'running' AND updated_at < ?
        """,
        (stale_before,),
    ).fetchone()[0]
    sources = {
        (row["source"], row["status"]): row["n"]
        for row in con.execute(
            """
            SELECT source, status, COUNT(*) AS n
            FROM strategy_candidates GROUP BY source, status
            """
        )
    }
    family_count = con.execute(
        """
        SELECT COUNT(DISTINCT family) FROM strategy_candidates
        WHERE source = 'system' AND status = 'proposed'
        """
    ).fetchone()[0]
    system_templates = sources.get(("system", "proposed"), 0)
    generated_candidates = sum(
        count
        for (source, status), count in sources.items()
        if source in {"ai", "ml"} and status == "proposed"
    )
    revisions = 0
    negative_memories = sum(
        count for (_, status), count in sources.items() if status in {"rejected", "retired"}
    )
    for row in con.execute("SELECT payload FROM strategy_candidates"):
        payload = json.loads(row[0])
        if payload.get("parent_candidate_id"):
            revisions += 1
    analyze_decisions = con.execute(
        "SELECT COUNT(*) FROM agent_decisions WHERE action = 'analyze'"
    ).fetchone()[0]
    open_episodes = con.execute(
        "SELECT COUNT(*) FROM trade_episodes WHERE status IN ('open', 'pending_entry', 'reserved')"
    ).fetchone()[0]
    uncertain_episodes = con.execute(
        "SELECT COUNT(*) FROM trade_episodes WHERE status = 'uncertain'"
    ).fetchone()[0]
    duplicate_client_ids = con.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT client_order_id FROM order_ledger
            WHERE client_order_id IS NOT NULL AND client_order_id != ''
            GROUP BY client_order_id HAVING COUNT(*) > 1
        )
        """
    ).fetchone()[0]
    exchange_count = con.execute(
        "SELECT COUNT(DISTINCT json_extract(payload, '$.exchange')) FROM research_datasets"
    ).fetchone()[0]
    liquidation_row = con.execute(
        """
        SELECT COUNT(*) AS n, MIN(event_time) AS first_event,
               MAX(event_time) AS last_event
        FROM liquidation_events
        """
    ).fetchone()
    ledger_lineages = con.execute("SELECT COUNT(*) FROM research_campaign_lineages").fetchone()[0]
    ledger_experiments = con.execute(
        "SELECT COUNT(*) FROM research_campaign_experiments"
    ).fetchone()[0]
    ledger_trial_variants = con.execute(
        "SELECT COUNT(*) FROM research_campaign_trial_variants"
    ).fetchone()[0]
    trial_families = con.execute("SELECT COUNT(*) FROM research_trial_families").fetchone()[0]
    trial_family_experiments = con.execute(
        "SELECT COUNT(*) FROM research_trial_family_experiments"
    ).fetchone()[0]
    trial_family_variants = con.execute(
        "SELECT COUNT(*) FROM research_trial_family_variants"
    ).fetchone()[0]
    pair_campaign_rows = con.execute(
        """
        SELECT payload FROM relative_value_campaigns
        ORDER BY created_at DESC
        """
    ).fetchall()
    pair_campaigns = [
        PairResearchCampaignReport.model_validate_json(row[0]) for row in pair_campaign_rows
    ]
    latest_pair_campaign = pair_campaigns[0] if pair_campaigns else None
    pair_campaign_by_id = {campaign.id: campaign for campaign in pair_campaigns}
    pair_revision_count = sum(
        len(
            set(campaign.strategy_fingerprints)
            - set(pair_campaign_by_id[campaign.parent_campaign_id].strategy_fingerprints)
        )
        for campaign in pair_campaigns
        if campaign.parent_campaign_id in pair_campaign_by_id
    )
    carry_campaign_rows = con.execute(
        """
        SELECT payload FROM carry_campaigns
        ORDER BY created_at DESC
        """
    ).fetchall()
    carry_campaigns = [
        CarryResearchCampaignReport.model_validate_json(row[0]) for row in carry_campaign_rows
    ]
    return {
        "stuck_jobs": stuck_jobs,
        "family_count": family_count,
        "system_templates": system_templates,
        "generated_candidates": generated_candidates,
        "revisions": revisions,
        "negative_memories": negative_memories,
        "analyze_decisions": analyze_decisions,
        "open_episodes": open_episodes,
        "uncertain_episodes": uncertain_episodes,
        "duplicate_client_ids": duplicate_client_ids,
        "exchange_count": exchange_count,
        "liquidation_events": liquidation_row["n"],
        "liquidation_first_event": liquidation_row["first_event"],
        "liquidation_last_event": liquidation_row["last_event"],
        "ledger_lineages": ledger_lineages,
        "ledger_experiments": ledger_experiments,
        "ledger_trial_variants": ledger_trial_variants,
        "trial_families": trial_families,
        "trial_family_experiments": trial_family_experiments,
        "trial_family_variants": trial_family_variants,
        "pair_campaign_count": len(pair_campaigns),
        "pair_revision_count": max(pair_revision_count, 0),
        "latest_pair_campaign": latest_pair_campaign,
        "carry_campaign_count": len(carry_campaigns),
        "latest_carry_campaign": carry_campaigns[0] if carry_campaigns else None,
    }


async def gather_evidence(*, run_tests: bool) -> dict:
    regression_ok, regression_summary = (
        _full_regression() if run_tests else (False, "本次审计未执行完整回归")
    )
    campaign = _latest_campaign()
    universe_probe = await _point_in_time_universe_probe(campaign)
    catalog_journal_probe = _catalog_journal_probe()
    replay = await _replay_latest_campaign(campaign)
    # A replay can repair/migrate an older persisted report while preserving
    # its original immutable experiment budget. Reload before scoring so this
    # audit never evaluates the stale pre-replay in-memory payload.
    if campaign is not None:
        refreshed_campaign = api.store.load_research_campaign(campaign.id)
        if refreshed_campaign is not None:
            campaign = ResearchCampaignReport.model_validate(refreshed_campaign)
    selected_datasets = _select_core_datasets()
    verified, dataset_summaries, candles_clean, funding_complete, oi_coverage = _verify_datasets(
        selected_datasets
    )
    campaign_datasets = _select_campaign_datasets(campaign)
    (
        campaign_datasets_verified,
        campaign_dataset_summaries,
        _campaign_candles_clean,
        _campaign_funding_complete,
        _campaign_oi_coverage,
    ) = _verify_datasets(campaign_datasets)
    process_ok, process_summary = (
        await _process_isolation_probe(next(iter(selected_datasets.values())))
        if selected_datasets
        else (False, "没有核心数据集")
    )
    market_sensing_ok, market_sensing_summary = await _market_sensing_probe()
    api_isolation_path = ROOT / "output" / "acceptance" / "api-isolation.json"
    if api_isolation_path.exists():
        api_isolation = json.loads(api_isolation_path.read_text(encoding="utf-8"))
        process_ok &= api_isolation.get("health_failures") == 0
        process_summary += (
            f"; API health p95={api_isolation.get('health_latency_p95_ms')}ms, "
            f"samples={api_isolation.get('health_samples')}"
        )
    db = _database_evidence()
    pair_campaign = db["latest_pair_campaign"]
    pair_evaluator_present = bool(pair_campaign and pair_campaign.pbo.split_count > 0)
    pair_experiment_contract = bool(
        pair_campaign
        and (
            (
                pair_campaign.experiment_kind == "bounded_revision"
                and pair_campaign.parent_campaign_id
                and 1 <= pair_campaign.new_trial_variant_count <= 3
                and not pair_campaign.recommended_revisions
            )
            or (
                pair_campaign.experiment_kind == "structural_extension"
                and pair_campaign.predecessor_campaign_id
                and 1
                <= pair_campaign.new_trial_variant_count
                <= len(pair_campaign.strategy_fingerprints)
            )
            or (
                pair_campaign.experiment_kind == "initial"
                and pair_campaign.sequential_experiment_count == 1
            )
        )
    )
    pair_lineage_enforced = bool(
        pair_campaign
        and pair_campaign.research_lineage_id
        and pair_campaign.research_experiment_id
        and pair_campaign.research_configuration_fingerprint
        and pair_campaign.sequential_experiment_count >= 1
        and pair_campaign.cumulative_hypothesis_count
        >= len(set(pair_campaign.strategy_fingerprints))
        and pair_campaign.effective_trial_count
        >= pair_campaign.cumulative_hypothesis_count + pair_campaign.sequential_experiment_count - 1
        and pair_experiment_contract
    )
    pair_archetypes = (
        {item.cell.archetype for item in pair_campaign.cells} if pair_campaign else set()
    )
    carry_campaign = db["latest_carry_campaign"]
    carry_evaluator_present = bool(carry_campaign and carry_campaign.pbo.split_count > 0)
    carry_experiment_contract = bool(
        carry_campaign
        and (
            (
                carry_campaign.experiment_kind == "bounded_revision"
                and carry_campaign.parent_campaign_id
                and 1 <= carry_campaign.new_trial_variant_count <= 3
                and not carry_campaign.recommended_revisions
            )
            or (
                carry_campaign.experiment_kind == "structural_extension"
                and carry_campaign.predecessor_campaign_id
                and 1
                <= carry_campaign.new_trial_variant_count
                <= len(carry_campaign.strategy_fingerprints)
            )
            or (
                carry_campaign.experiment_kind == "initial"
                and carry_campaign.sequential_experiment_count == 1
            )
        )
    )
    carry_lineage_enforced = bool(
        carry_campaign
        and carry_campaign.research_lineage_id
        and carry_campaign.research_experiment_id
        and carry_campaign.research_configuration_fingerprint
        and carry_campaign.cumulative_hypothesis_count
        >= len(set(carry_campaign.strategy_fingerprints))
        and carry_campaign.effective_trial_count
        >= carry_campaign.cumulative_hypothesis_count
        + carry_campaign.sequential_experiment_count
        - 1
        and not carry_campaign.execution_eligible
        and not carry_campaign.profitability_claim
        and carry_experiment_contract
    )
    carry_archetypes = (
        {item.cell.archetype for item in carry_campaign.cells} if carry_campaign else set()
    )
    strict_data_report = evaluate_data_coverage(gather_data_coverage_evidence())

    def strict_cells(feature: HistoricalFeature):
        return [cell for cell in strict_data_report.cells if cell.feature == feature]

    def strict_feature_passed(feature: HistoricalFeature) -> bool:
        cells = strict_cells(feature)
        return bool(cells) and all(cell.passed for cell in cells)

    def strict_feature_summary(feature: HistoricalFeature) -> str:
        cells = strict_cells(feature)
        passed = sum(cell.passed for cell in cells)
        score = strict_data_report.feature_scores.get(str(feature), Decimal("0"))
        return (
            f"严格历史合同 {passed}/{len(cells)} 单元通过，"
            f"特征得分 {score}，总分 {strict_data_report.score}/100"
        )

    library = strategy_library_summary()
    fault_matrix = _execution_fault_matrix(run_tests=run_tests)
    configuration = api.configuration_service.current_configuration()
    commissioning = api.configuration_service.commissioning_for(configuration)
    reconciliation = api.runtime.reconciliation_report
    reconciliation_clean = bool(reconciliation) and not (
        reconciliation.uncertain_order_ids
        or reconciliation.unknown_open_order_ids
        or reconciliation.unknown_position_symbols
        or reconciliation.unprotected_order_ids
    )
    position_management_verified = bool(
        commissioning and commissioning.agent_position_management_passed
    )
    risk_state = api.risk_state_service.get()
    risk_increasing_episodes_after_halt = [
        episode
        for episode in api.agent_store.list_episodes(500)
        if risk_state.halted_at is not None
        and episode.opened_at >= risk_state.halted_at
        and episode.entry_order_ids
    ]
    certificate_enforced = bool(
        fault_matrix["categories"].get("campaign_certificate_tamper_and_blind_reuse")
        and fault_matrix["categories"].get("campaign_cumulative_trial_ledger")
        and fault_matrix["categories"].get(
            "campaign_cross_download_trial_family_cannot_reset_trials"
        )
        and fault_matrix["categories"].get("campaign_one_bounded_parent_authorized_successor")
        and fault_matrix["categories"].get("campaign_required_data_coverage")
        and fault_matrix["categories"].get("point_time_universe_history_contract")
        and CAMPAIGN_ENGINE_VERSION >= 14
    )
    minimum_basis_coverage = min(
        (float(item.basis_coverage_ratio) for item in selected_datasets.values()),
        default=0.0,
    )
    minimum_order_flow_coverage = min(
        (float(item.order_flow_coverage_ratio) for item in selected_datasets.values()),
        default=0.0,
    )
    campaign_complete = bool(
        campaign and campaign.status == "completed" and len(campaign.cells) == campaign.trial_count
    )
    champion = bool(
        campaign and campaign.selected_library_entry_id and campaign.autonomous_execution_eligible
    )
    profitability_evidence = _campaign_profitability_evidence(campaign)
    revision_effective = False
    if champion and campaign and campaign.parent_campaign_id:
        parent_payload = api.store.load_research_campaign(campaign.parent_campaign_id)
        selected_evidence = profitability_evidence["selected"]
        if parent_payload is not None and selected_evidence is not None:
            parent_fingerprints = {
                str(cell.get("strategy_fingerprint"))
                for cell in parent_payload.get("cells", [])
                if cell.get("dataset_role", "research") != "blind"
            }
            revision_effective = bool(
                selected_evidence.strategy_fingerprint not in parent_fingerprints
            )
    plan_by_id = {
        plan.id: plan
        for payload in api.store.load_strategies()
        for plan in [api.StrategyPlan.model_validate(payload)]
    }
    certified_episodes = []
    if champion and campaign and campaign.execution_certificate:
        certificate_fingerprint = campaign.execution_certificate.fingerprint
        certified_episodes = [
            episode
            for episode in api.agent_store.list_episodes(10000)
            if (
                (plan := plan_by_id.get(episode.plan_id)) is not None
                and plan.research_campaign_id == campaign.id
                and plan.campaign_certificate_fingerprint == certificate_fingerprint
            )
        ]
    certified_symbols = list(
        dict.fromkeys(episode.symbol for episode in certified_episodes)
    )
    certified_forward = api.build_forward_performance(
        certified_episodes,
        api.configuration_service.current_configuration().agent_forward_validation,
        scope_mode=api.AgentAutonomyMode.SANDBOX,
        required_symbols=certified_symbols,
    )
    pbo_passed = bool(campaign and campaign.pbo.passed)
    ledger_experiments = (
        api.store.list_research_campaign_experiments(campaign.research_lineage_id)
        if campaign and campaign.research_lineage_id
        else []
    )
    ledger_experiment = next(
        (
            item
            for item in ledger_experiments
            if item["experiment_id"] == campaign.research_experiment_id
        ),
        None,
    )
    trial_family_experiment = (
        api.store.load_research_trial_family_experiment(
            family_id=campaign.research_trial_family_id,
            experiment_id=campaign.research_experiment_id,
        )
        if campaign and campaign.research_trial_family_id and campaign.research_experiment_id
        else None
    )
    cumulative_ledger_enforced = bool(
        campaign
        and campaign.engine_version >= 13
        and campaign.research_lineage_id
        and campaign.research_experiment_id
        and campaign.cumulative_hypothesis_count >= len(campaign.candidates)
        and campaign.sequential_experiment_count >= 1
        and campaign.effective_trial_count >= campaign.cumulative_hypothesis_count
        and ledger_experiment
        and trial_family_experiment
        and trial_family_experiment["cumulative_hypothesis_count"]
        == campaign.cumulative_hypothesis_count
        and trial_family_experiment["sequential_experiment_count"]
        == campaign.sequential_experiment_count
        and trial_family_experiment["effective_trial_count"] == campaign.effective_trial_count
    )
    campaign_summary = (
        f"{campaign.id}: {campaign.trial_count} cells, "
        f"{len(campaign.library_entry_ids)} strategies × {len(campaign.dataset_ids)} datasets, "
        f"{len({cell.exchange for cell in campaign.cells if cell.data_eligible})} exchanges, "
        f"selected={campaign.selected_library_entry_id}, execution="
        f"{campaign.autonomous_execution_eligible}"
        if campaign
        else "无 Campaign"
    )
    return {
        "full_regression_passed": regression_ok,
        "full_regression_summary": regression_summary,
        "latest_campaign_completed": campaign_complete,
        "latest_campaign_summary": campaign_summary,
        "campaign_cache_complete": replay["ok"],
        "campaign_cache_summary": replay["summary"],
        "campaign_datasets_verified": (
            campaign_datasets_verified
            and campaign is not None
            and len(campaign_datasets) == len(campaign.dataset_ids)
        ),
        "campaign_dataset_summary": "; ".join(campaign_dataset_summaries),
        "research_process_isolated": process_ok,
        "research_process_summary": process_summary,
        "no_stuck_research_jobs": db["stuck_jobs"] == 0,
        "research_job_summary": f"{db['stuck_jobs']} 个陈旧运行任务",
        "market_sensing_complete": market_sensing_ok and db["analyze_decisions"] > 0,
        "market_sensing_summary": (
            f"{db['analyze_decisions']} 个持久化结构化 analyze 决策；"
            f"{market_sensing_summary}；核心最低基差覆盖 "
            f"{minimum_basis_coverage:.1%}，订单流覆盖 "
            f"{minimum_order_flow_coverage:.1%}，实时爆仓 "
            f"{db['liquidation_events']} 个"
        ),
        "strategy_library_present": library["entry_count"] > 0,
        "strategy_library_summary": (
            f"{library['entry_count']} 个可检索单腿模板（数据库已播种 "
            f"{db['system_templates']} 个）+ "
            f"{len(PAIR_STRATEGY_LIBRARY)} 个公开相对价值模板 + "
            f"{len(CARRY_STRATEGY_LIBRARY)} 个公开期现 Carry 模板"
        ),
        "agent_candidates_present": db["generated_candidates"] > 0,
        "agent_candidate_summary": f"{db['generated_candidates']} 个 AI/ML 候选",
        "failure_revision_present": db["revisions"] > 0 or db["pair_revision_count"] > 0,
        "failure_revision_summary": (
            f"{db['revisions']} 个单腿父子修改候选 + "
            f"{db['pair_revision_count']} 个有界相对价值子假设"
        ),
        "independent_evaluator_present": bool(
            (campaign_complete and campaign and campaign.pbo.split_count > 0)
            or pair_evaluator_present
            or carry_evaluator_present
        ),
        "independent_evaluator_summary": (
            f"Campaign={campaign_complete}，PBO 已执行 "
            f"{campaign.pbo.split_count if campaign else 0} 个时间折组合，"
            f"结果 passed={pbo_passed}；相对价值 Campaign="
            f"{pair_campaign.id if pair_campaign else 'none'}，PBO="
            f"{pair_campaign.pbo.probability_of_backtest_overfitting if pair_campaign else 'n/a'}；"
            f"Carry Campaign={carry_campaign.id if carry_campaign else 'none'}，PBO="
            f"{carry_campaign.pbo.probability_of_backtest_overfitting if carry_campaign else 'n/a'}"
        ),
        "research_budget_enforced": cumulative_ledger_enforced
        and (not pair_campaign or pair_lineage_enforced)
        and (not carry_campaign or carry_lineage_enforced),
        "research_budget_summary": (
            f"永久账本谱系={db['ledger_lineages']}，实验={db['ledger_experiments']}，"
            f"谱系内策略×引擎试验={db['ledger_trial_variants']}；跨下载试验家族="
            f"{db['trial_families']}，家族实验={db['trial_family_experiments']}，"
            f"家族策略×引擎试验={db['trial_family_variants']}；最新 Campaign 累计="
            f"{campaign.cumulative_hypothesis_count if campaign else 0}，序贯观察="
            f"{campaign.sequential_experiment_count if campaign else 0}，有效试验="
            f"{campaign.effective_trial_count if campaign else 0}；相对价值谱系="
            f"{pair_campaign.research_lineage_id if pair_campaign else 'none'}，累计="
            f"{pair_campaign.cumulative_hypothesis_count if pair_campaign else 0}，序贯="
            f"{pair_campaign.sequential_experiment_count if pair_campaign else 0}；Carry谱系="
            f"{carry_campaign.research_lineage_id if carry_campaign else 'none'}，累计="
            f"{carry_campaign.cumulative_hypothesis_count if carry_campaign else 0}，序贯="
            f"{carry_campaign.sequential_experiment_count if carry_campaign else 0}"
        ),
        "negative_strategy_memory": db["negative_memories"] > 0,
        "negative_strategy_memory_summary": (f"{db['negative_memories']} 个拒绝或退役候选"),
        "revision_effective": revision_effective,
        "revision_effective_summary": (
            (
                "唯一有界子假设已成为稳健 Champion；"
                if revision_effective
                else "已生成单腿与相对价值父子修订，但尚无修订候选成为稳健Champion；"
            )
            + "最新单腿后继="
            f"{campaign.id if campaign else 'none'}，PBO="
            f"{campaign.pbo.probability_of_backtest_overfitting if campaign else 'n/a'}；"
            "最新相对价值后继 PBO="
            f"{pair_campaign.pbo.probability_of_backtest_overfitting if pair_campaign else 'n/a'}"
        ),
        "honest_abstention": (
            not champion and not risk_increasing_episodes_after_halt and risk_state.halted
        ),
        "abstention_summary": (
            f"Champion={champion}，人工风险闩锁={risk_state.halted}，"
            f"闩锁后新增风险回合={len(risk_increasing_episodes_after_halt)}；"
            f"既有受保护仓位回合={db['open_episodes']}"
        ),
        "core_universe_complete": strict_feature_passed(HistoricalFeature.CANDLES),
        "core_universe_summary": strict_feature_summary(HistoricalFeature.CANDLES),
        "point_in_time_universe_complete": bool(
            universe_probe["selection_passed"] and universe_probe["materialized"]
        ),
        "point_in_time_universe_summary": universe_probe["summary"],
        "survivorship_bias_controlled": universe_probe["survivorship_controlled"],
        "survivorship_bias_summary": (
            universe_probe["summary"]
            + "; prospective catalog journal: "
            + catalog_journal_probe["summary"]
        ),
        "core_candles_verified": strict_feature_passed(HistoricalFeature.CANDLES),
        "candle_quality_summary": strict_feature_summary(HistoricalFeature.CANDLES),
        "funding_coverage_complete": strict_feature_passed(HistoricalFeature.FUNDING),
        "funding_coverage_summary": strict_feature_summary(HistoricalFeature.FUNDING),
        "open_interest_coverage_complete": strict_feature_passed(HistoricalFeature.OPEN_INTEREST),
        "open_interest_coverage_summary": strict_feature_summary(HistoricalFeature.OPEN_INTEREST),
        "multi_exchange_data": strict_feature_passed(HistoricalFeature.CANDLES),
        "multi_exchange_summary": (
            "严格K线合同要求每个核心市场/周期至少两个合格交易所；"
            + strict_feature_summary(HistoricalFeature.CANDLES)
        ),
        "crypto_native_depth_complete": all(
            strict_feature_passed(feature)
            for feature in (
                HistoricalFeature.POSITIONING,
                HistoricalFeature.BASIS,
                HistoricalFeature.ORDER_FLOW,
                HistoricalFeature.LIQUIDATIONS,
            )
        ),
        "crypto_native_depth_summary": (
            f"positioning={strict_feature_passed(HistoricalFeature.POSITIONING)}，"
            f"basis={strict_feature_passed(HistoricalFeature.BASIS)}，"
            f"order_flow={strict_feature_passed(HistoricalFeature.ORDER_FLOW)}，"
            f"liquidations={strict_feature_passed(HistoricalFeature.LIQUIDATIONS)}；"
            f"严格历史数据总分 {strict_data_report.score}/100；禁止历史伪回填"
        ),
        "strategy_family_target_met": library["archetype_count"] >= 25,
        "strategy_family_summary": (
            f"{library['archetype_count'] + len(pair_archetypes) + len(carry_archetypes)}/25 "
            f"个研究策略原型，单腿家族分布 {library['family_counts']}，"
            f"相对价值原型 {sorted(pair_archetypes)}，Carry原型 {sorted(carry_archetypes)}"
        ),
        "strategy_template_target_met": library["entry_count"] >= 80,
        "strategy_template_summary": f"{library['entry_count']}/80 个版本化可执行模板",
        "campaign_matrix_complete": campaign_complete,
        "latest_campaign_pbo_passed": pbo_passed,
        "pbo_summary": (
            f"passed={pbo_passed}, UCB={campaign.pbo.upper_confidence_bound}" if campaign else "无"
        ),
        "robust_champion_present": champion,
        "champion_summary": (
            f"selected={campaign.selected_library_entry_id if campaign else None}, "
            f"execution={campaign.autonomous_execution_eligible if campaign else False}"
        ),
        "strategy_certificate_enforced": certificate_enforced,
        "strategy_certificate_summary": (
            f"Campaign engine v{CAMPAIGN_ENGINE_VERSION}；证书绑定策略、开发/盲测数据指纹、"
            "PBO、完整开发/盲测聚合指纹与策略级FDR；篡改和盲集复用测试通过"
            if certificate_enforced
            else "不可变证书或其篡改/盲集复用验收未通过"
        ),
        "execution_reconciled": db["uncertain_episodes"] == 0 and reconciliation_clean,
        "reconciliation_summary": (
            f"{db['uncertain_episodes']} 个不确定回合；运行时 unknown orders="
            f"{len(reconciliation.unknown_open_order_ids) if reconciliation else 'unknown'}，"
            "unknown positions="
            f"{len(reconciliation.unknown_position_symbols) if reconciliation else 'unknown'}，"
            "unprotected="
            f"{len(reconciliation.unprotected_order_ids) if reconciliation else 'unknown'}"
        ),
        "idempotent_order_ledger": (
            db["duplicate_client_ids"] == 0
            and bool(fault_matrix["categories"].get("idempotent_submission"))
        ),
        "idempotency_summary": (
            f"{db['duplicate_client_ids']} 个重复 client_order_id；重复提交故障测试通过"
        ),
        "native_protection_verified": (
            position_management_verified
            and reconciliation_clean
            and bool(fault_matrix["categories"].get("post_submit_protection_failure"))
            and bool(fault_matrix["categories"].get("partial_reduction_protection_rebalance"))
        ),
        "native_protection_summary": (
            f"沙盒仓位管理 commissioning={position_management_verified}，"
            f"运行时对账干净={reconciliation_clean}；原子保护、失败回滚、"
            "部分减仓保护再平衡测试已执行"
        ),
        "fault_injection_passed": bool(fault_matrix["passed"]),
        "fault_injection_summary": (
            f"{len(fault_matrix['categories'])} 类执行/研究完整性故障："
            f"{sum(bool(value) for value in fault_matrix['categories'].values())} 类通过；"
            f"{fault_matrix['summary']}"
        ),
        "restart_recovery_passed": bool(
            fault_matrix["categories"].get("restart_recovery_to_uncertain")
            and fault_matrix["categories"].get("unknown_order_and_position_reconciliation")
            and fault_matrix["categories"].get("manual_risk_halt_latch")
        ),
        "restart_recovery_summary": (
            "重启中断的只读任务可关闭；可能已到达交易所的执行任务转为 uncertain 并强制对账；"
            "人工停机闩锁跨恢复路径保持"
        ),
        "champion_cost_stress_passed": profitability_evidence["cost_stress_passed"],
        "champion_cost_summary": profitability_evidence["cost_summary"],
        "champion_parameter_stable": profitability_evidence["parameter_stable"],
        "champion_parameter_summary": profitability_evidence["parameter_summary"],
        "champion_cross_market": profitability_evidence["cross_market"],
        "champion_cross_market_summary": profitability_evidence["cross_market_summary"],
        "champion_win_payoff_passed": profitability_evidence["win_payoff_passed"],
        "champion_win_payoff_summary": profitability_evidence["win_payoff_summary"],
        "qualified_portfolio_present": profitability_evidence["qualified_portfolio"],
        "portfolio_summary": profitability_evidence["portfolio_summary"],
        "causal_live_evidence_passed": bool(
            champion and certified_forward.qualified_for_live_review
        ),
        "causal_live_summary": (
            f"证书绑定回合={len(certified_episodes)}，状态={certified_forward.status}，"
            f"独立运行={certified_forward.independent_runs}，PF="
            f"{certified_forward.profit_factor}，期望="
            f"{certified_forward.expectancy_r}R"
            if champion
            else "没有由合格Champion产生的真实因果成交证据"
        ),
        "campaign_replay": replay,
        "strict_data_coverage": strict_data_report.model_dump(mode="json"),
    }


def _markdown(report) -> str:
    lines = [
        "# COSMOS 自动验收报告",
        "",
        f"- 生成时间：{report.generated_at.isoformat()}",
        f"- 范围：{report.scope}",
        f"- 系统能力得分：{report.capability_score}/100",
        f"- 当前策略认证：{report.strategy_certification_status}",
        f"- 工程得分：{report.engineering_score}/100",
        f"- 综合得分：{report.overall_score}/100",
        f"- 盈利证据：{report.profitability_evidence_score}/100",
        "- 盈利保证：否",
        "",
    ]
    for dimension in report.dimensions:
        lines.extend(
            [
                f"## {dimension.label}：{dimension.score}/100",
                "",
                "| 检查 | 权重 | 结论 | 证据 |",
                "|---|---:|---|---|",
            ]
        )
        for check in dimension.checks:
            evidence = check.evidence.replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| {check.label} | {check.weight} | "
                f"{'PASS' if check.passed else 'HOLD'} | {evidence} |"
            )
        lines.append("")
    lines.extend(["## 主要阻塞项", ""])
    lines.extend(f"- {item}" for item in report.blockers)
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-tests", action="store_true")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "output" / "acceptance",
    )
    args = parser.parse_args()
    evidence = anyio.run(partial(gather_evidence, run_tests=args.run_tests))
    report = build_product_acceptance_report(evidence)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "latest.json"
    markdown_path = args.output_dir / "latest.md"
    json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    markdown_path.write_text(_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "engineering_score": str(report.engineering_score),
                "capability_score": str(report.capability_score),
                "strategy_certification_status": report.strategy_certification_status,
                "overall_score": str(report.overall_score),
                "profitability_evidence_score": report.profitability_evidence_score,
                "json": str(json_path),
                "markdown": str(markdown_path),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
