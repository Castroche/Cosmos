from __future__ import annotations

import asyncio
from decimal import Decimal

from fastapi import HTTPException
from pydantic import ValidationError

from .agent.performance import build_forward_performance
from .agent.schemas import AgentAutonomyMode, TradeEpisodeStatus
from .alpha_factory import (
    ALPHA_MARKETS,
    AlphaModelArtifactV1,
    DevelopmentAuditCertificateV1,
    DevelopmentAuditEvidenceV1,
    FullStrategyCertificateV2,
    SandboxAdmissionCertificateV1,
    SandboxExecutionRoundEvidenceV1,
    SandboxMaturityEvidenceV1,
    alpha_transaction_cost_fingerprint,
    assess_crypto_5m_contract,
    dependency_versions,
)
from .api_contracts import DevelopmentAuditIssueRequest, SandboxAdmissionIssueRequest
from .domain import TradingMode
from .operator_workspace import stable_fingerprint
from .research_campaign import ResearchCampaignCell, ResearchCampaignReport
from .research_certificates import CertificateRevocationEvent

logger = __import__("logging").getLogger(__name__)


class ResearchAdmissionsService:
    def sandbox_maturity_snapshot(self, certificate: SandboxAdmissionCertificateV1) -> dict:
        episodes = [
            item
            for item in self.agent_store.list_episodes(10_000)
            if item.strategy_certificate_id == certificate.id
        ]
        performance = build_forward_performance(
            episodes,
            self.configuration_service.current_configuration().agent_forward_validation,
            scope_mode=AgentAutonomyMode.SANDBOX,
        )
        execution_payloads = self.store.list_sandbox_execution_evidence(certificate.id)
        execution = [
            SandboxExecutionRoundEvidenceV1.model_validate(item) for item in execution_payloads
        ]
        attributable_ids = {
            item.id
            for item in episodes
            if item.status == TradeEpisodeStatus.CLOSED
            and item.realized_pnl is not None
            and item.return_on_risk is not None
        }
        aligned_ids = {item.episode_id for item in execution if item.aligned}
        aligned = bool(attributable_ids) and attributable_ids.issubset(aligned_ids)
        profit_factor = performance.profit_factor
        if profit_factor is None and performance.wins > 0 and performance.losses == 0:
            profit_factor = Decimal("999.99")
        maturity = SandboxMaturityEvidenceV1(
            completed_episodes=performance.attributable_episodes,
            independent_runs=performance.independent_runs,
            recent_episodes=performance.recent_episodes,
            profit_factor=profit_factor or Decimal("0"),
            expected_r=performance.expectancy_r,
            maximum_drawdown_r=performance.max_drawdown_r,
            maximum_consecutive_losses=performance.max_consecutive_losses,
            execution_cost_aligned=aligned,
            slippage_aligned=aligned,
            native_protection_aligned=aligned,
        )
        return {
            "maturity": maturity,
            "performance": performance,
            "execution_evidence_count": len(execution),
            "aligned_episode_count": len(attributable_ids & aligned_ids),
        }

    def maybe_issue_full_strategy_certificate_v2(
        self,
        certificate_id: str,
    ) -> FullStrategyCertificateV2 | None:
        status = self.store.load_research_certificate_status(certificate_id)
        if not status or status["certificate_type"] != "sandbox_admission_v1" or status["revoked"]:
            return None
        admission = SandboxAdmissionCertificateV1.model_validate(status["certificate"])
        try:
            self._development_audit_for_admission(admission)
        except RuntimeError:
            return None
        existing = [
            item
            for item in self.store.list_full_strategy_certificates_v2()
            if item["certificate"].get("sandbox_admission_certificate_id") == admission.id
            and not item["revoked"]
        ]
        if existing:
            return FullStrategyCertificateV2.model_validate(existing[0]["certificate"])
        blind = self.store.find_completed_blind_consumption(admission.strategy_ir_hash)
        if blind is None:
            blind = self.store.find_completed_blind_consumption(admission.artifact_fingerprint)
        if blind and not bool(blind.get("passed")):
            self.store.revoke_research_certificate(
                CertificateRevocationEvent(
                    certificate_id=admission.id,
                    reason_code="sealed_blind_test_failed",
                    evidence_reference=str(blind.get("evaluation_id") or "sealed_blind"),
                )
            )
            return None
        if not blind:
            return None
        snapshot = self.sandbox_maturity_snapshot(admission)
        maturity = snapshot["maturity"]
        if not maturity.passed:
            return None
        configuration = self.configuration_service.current_configuration()
        certificate = FullStrategyCertificateV2(
            sandbox_admission_certificate_id=admission.id,
            strategy_ir_hash=admission.strategy_ir_hash,
            artifact_fingerprint=admission.artifact_fingerprint,
            sealed_blind_evidence_reference=str(blind["evaluation_id"]),
            sandbox_maturity=maturity,
            configuration_fingerprint=self.strategy_certificate_configuration_fingerprint(
                configuration
            ),
        )
        self.store.save_research_certificate(certificate, "full_strategy_v2")
        return certificate

    async def issue_development_audit(self, request: DevelopmentAuditIssueRequest):
        """Derive admission metrics from immutable server-side campaign cells."""

        datasets = await asyncio.to_thread(self.store.list_research_datasets, 2_000)
        contract = assess_crypto_5m_contract(datasets)
        if contract.status != "READY":
            raise HTTPException(409, "crypto_5m_v1 is not READY")
        artifacts: list[AlphaModelArtifactV1] = []
        for model_id in request.model_artifact_ids:
            payload = await asyncio.to_thread(self.store.load_alpha_model_artifact, model_id)
            if not payload:
                raise HTTPException(409, f"model artifact not found: {model_id}")
            artifacts.append(AlphaModelArtifactV1.model_validate(payload))
        ensemble = next(
            (
                item
                for item in artifacts
                if item.id == request.ensemble_model_id and item.model_family == "ensemble_50_50"
            ),
            None,
        )
        families = {item.model_family for item in artifacts}
        if ensemble is None or not (
            families & {"elastic_net_regression", "logistic_classification"}
            and families & {"lightgbm_regression", "lightgbm_classification"}
        ):
            raise HTTPException(409, "a bound linear, LightGBM, and ensemble trio is required")
        if any(
            item.ir_fingerprint != request.strategy_ir_hash or not item.walk_forward_passed
            for item in artifacts
        ):
            raise HTTPException(409, "model trio IR or walk-forward evidence is invalid")
        report_payload = await asyncio.to_thread(
            self.store.load_research_campaign, request.campaign_id
        )
        if report_payload is None:
            raise HTTPException(409, "persisted development campaign not found")
        report = ResearchCampaignReport.model_validate(report_payload)
        candidate = next(
            (
                item
                for item in report.candidates
                if item.strategy_fingerprint == request.strategy_ir_hash
            ),
            None,
        )
        if candidate is None:
            raise HTTPException(409, "campaign does not contain the frozen strategy IR")
        cells = [
            item
            for item in report.cells
            if item.dataset_role == "research"
            and item.strategy_fingerprint == request.strategy_ir_hash
            and item.timeframe == "5m"
        ]
        by_market: dict[str, ResearchCampaignCell] = {}
        for cell in cells:
            market = f"{cell.exchange.lower()}:{cell.symbol.upper()}"
            if market in by_market:
                raise HTTPException(409, f"duplicate development evaluation for {market}")
            by_market[market] = cell
        if set(by_market) != set(ALPHA_MARKETS):
            raise HTTPException(409, "campaign must evaluate the exact six crypto_5m_v1 markets")
        selected_dataset_by_market = {
            item.market: item.dataset_id for item in contract.markets if item.dataset_id
        }
        if any(
            by_market[market].dataset_id != selected_dataset_by_market.get(market)
            for market in ALPHA_MARKETS
        ):
            raise HTTPException(409, "campaign cells do not bind the current frozen data contract")
        passing = tuple(
            market
            for market in ALPHA_MARKETS
            if (
                by_market[market].data_eligible
                and by_market[market].robustness_passed
                and by_market[market].statistical_uncertainty_passed
                and by_market[market].temporal_stability_passed
                and by_market[market].expected_r_passed
                and by_market[market].profit_factor >= Decimal("1.15")
                and (by_market[market].stress_return_percent or Decimal("0")) > 0
                and by_market[market].max_drawdown_percent <= Decimal("20")
            )
        )
        passing_cells = [by_market[market] for market in passing]
        evidence = DevelopmentAuditEvidenceV1(
            expected_r_bootstrap_lower=(
                min(item.expected_r_confidence_lower_bound for item in passing_cells)
                if passing_cells
                else Decimal("0")
            ),
            profit_factor=(
                min(item.profit_factor for item in passing_cells) if passing_cells else Decimal("0")
            ),
            stress_net_return=(
                min(item.stress_return_percent or Decimal("0") for item in passing_cells)
                if passing_cells
                else Decimal("0")
            ),
            maximum_drawdown_fraction=(
                max(item.max_drawdown_percent for item in by_market.values()) / Decimal("100")
            ),
            pbo_95_upper=report.pbo.upper_confidence_bound,
            online_fdr_passed=candidate.online_fdr_passed,
            passing_markets=passing,
        )
        if not candidate.cross_market_passed or not report.pbo.passed or not evidence.passed:
            raise HTTPException(409, "persisted campaign does not pass development admission")
        certificate = DevelopmentAuditCertificateV1(
            strategy_ir_hash=request.strategy_ir_hash,
            artifact_fingerprint=ensemble.fingerprint,
            data_contract_fingerprint=contract.fingerprint,
            model_artifact_ids=tuple(item.id for item in artifacts),
            campaign_id=report.id,
            campaign_fingerprint=stable_fingerprint(report.model_dump(mode="json")),
            market_evaluation_fingerprints={
                market: by_market[market].backtest_fingerprint for market in ALPHA_MARKETS
            },
            evidence=evidence,
        )
        await asyncio.to_thread(
            self.store.save_research_certificate,
            certificate,
            "development_audit_v1",
        )
        return certificate.model_dump(mode="json")

    async def list_development_audits(self):
        return await asyncio.to_thread(
            self.store.list_research_certificates_by_type,
            "development_audit_v1",
        )

    async def issue_sandbox_admission(self, request: SandboxAdmissionIssueRequest):
        datasets = await asyncio.to_thread(self.store.list_research_datasets, 2_000)
        contract = assess_crypto_5m_contract(datasets)
        if contract.status != "READY":
            raise HTTPException(
                409,
                {
                    "code": "crypto_5m_v1_not_ready",
                    "status": contract.status,
                    "markets": [item.model_dump(mode="json") for item in contract.markets],
                },
            )
        artifacts: list[AlphaModelArtifactV1] = []
        for model_id in request.model_artifact_ids:
            payload = await asyncio.to_thread(self.store.load_alpha_model_artifact, model_id)
            if not payload:
                raise HTTPException(409, f"model artifact not found: {model_id}")
            artifacts.append(AlphaModelArtifactV1.model_validate(payload))
        families = {item.model_family for item in artifacts}
        required_family_groups = (
            {"elastic_net_regression", "logistic_classification"},
            {"lightgbm_regression", "lightgbm_classification"},
            {"ensemble_50_50"},
        )
        if not all(families & group for group in required_family_groups):
            raise HTTPException(409, "linear, LightGBM, and 50/50 ensemble artifacts are required")
        if not all(item.walk_forward_passed for item in artifacts):
            raise HTTPException(
                409, "linear and LightGBM models must independently pass walk-forward"
            )
        model_tasks = {item.task for item in artifacts if item.task != "ensemble"}
        if len(model_tasks) != 1:
            raise HTTPException(409, "linear and LightGBM model tasks differ")
        artifact_lineages = {
            (
                item.profile,
                item.horizon_bars,
                item.feature_names,
                item.feature_cube_fingerprint,
                item.label_fingerprint,
                item.training_window,
                item.validation_windows,
                item.purge_bars,
                item.embargo_bars,
                item.random_seed,
                item.ir_fingerprint,
                item.cost_model_fingerprint,
            )
            for item in artifacts
        }
        if len(artifact_lineages) != 1:
            raise HTTPException(409, "model trio does not share one frozen training lineage")
        installed_dependencies = dependency_versions()
        if any(
            any(
                installed_dependencies.get(name) != value
                for name, value in item.dependency_versions.items()
            )
            for item in artifacts
        ):
            raise HTTPException(409, "model artifact dependency version drift detected")
        ensemble = next(
            (
                item
                for item in artifacts
                if item.id == request.ensemble_model_id and item.model_family == "ensemble_50_50"
            ),
            None,
        )
        if ensemble is None:
            raise HTTPException(409, "ensemble_model_id is not the supplied ensemble artifact")
        if any(item.ir_fingerprint != request.strategy_ir_hash for item in artifacts):
            raise HTTPException(409, "model artifact and strategy IR fingerprints differ")
        if any(item.canonical_ir_payload is None for item in artifacts):
            raise HTTPException(409, "model artifacts must embed the frozen Canonical IR")
        configuration = self.configuration_service.current_configuration()
        expected_cost_fingerprint = alpha_transaction_cost_fingerprint(
            fee_bps=configuration.fee_bps,
            slippage_bps=configuration.slippage_bps,
        )
        if {item.cost_model_fingerprint for item in artifacts} != {expected_cost_fingerprint}:
            raise HTTPException(409, "model artifacts do not match the current frozen cost model")
        required_data_features = tuple(
            sorted({feature for item in artifacts for feature in item.required_data_features})
        )
        contract = assess_crypto_5m_contract(
            datasets,
            required_features=required_data_features,
        )
        if contract.status != "READY":
            raise HTTPException(
                409,
                {
                    "code": "model_required_features_untestable",
                    "required_features": required_data_features,
                    "status": contract.status,
                    "markets": [item.model_dump(mode="json") for item in contract.markets],
                },
            )
        audit_status = await asyncio.to_thread(
            self.store.load_research_certificate_status,
            request.development_audit_certificate_id,
        )
        if (
            audit_status is None
            or audit_status["certificate_type"] != "development_audit_v1"
            or audit_status["revoked"]
        ):
            raise HTTPException(409, "valid server-issued development audit is required")
        development_audit = DevelopmentAuditCertificateV1.model_validate(
            audit_status["certificate"]
        )
        if (
            development_audit.strategy_ir_hash != request.strategy_ir_hash
            or development_audit.artifact_fingerprint != ensemble.fingerprint
            or development_audit.data_contract_fingerprint != contract.fingerprint
            or set(development_audit.model_artifact_ids) != {item.id for item in artifacts}
        ):
            raise HTTPException(409, "development audit does not bind this frozen model lineage")
        try:
            certificate = SandboxAdmissionCertificateV1(
                strategy_ir_hash=request.strategy_ir_hash,
                artifact_fingerprint=ensemble.fingerprint,
                data_contract_fingerprint=contract.fingerprint,
                model_artifact_ids=tuple(item.id for item in artifacts),
                development_audit_certificate_id=development_audit.id,
                audit_evidence=development_audit.evidence,
                strategy_frozen=True,
            )
        except ValidationError as exc:
            raise HTTPException(409, str(exc)) from exc
        await asyncio.to_thread(
            self.store.save_research_certificate,
            certificate,
            "sandbox_admission_v1",
        )
        handoff_status = (
            "admitted_waiting_current_signal"
            if configuration.exchange_id == "okx"
            and configuration.trading_mode == TradingMode.SANDBOX
            else "queued_until_okx_sandbox"
        )
        handoff = await asyncio.to_thread(
            self.store.save_sandbox_admission_handoff,
            certificate,
            status=handoff_status,
        )
        if handoff_status == "admitted_waiting_current_signal":
            await self.process_sandbox_alpha_handoffs()
            handoff = (
                await asyncio.to_thread(
                    self.store.load_sandbox_admission_handoff,
                    certificate.id,
                )
                or handoff
            )
        self.add_audit(
            "research",
            "crypto_5m_v1 开发审核通过并自动进入 OKX 模拟盘准入队列",
            severity="warning",
            certificate_id=certificate.id,
            handoff_status=handoff_status,
        )
        return {"certificate": certificate.model_dump(mode="json"), "handoff": handoff}

    async def list_sandbox_admissions(self):
        records = await asyncio.to_thread(
            self.store.list_research_certificates_by_type,
            "sandbox_admission_v1",
        )
        result = []
        for record in records:
            certificate = SandboxAdmissionCertificateV1.model_validate(record["certificate"])
            audit_valid = True
            try:
                await asyncio.to_thread(self._development_audit_for_admission, certificate)
            except RuntimeError:
                audit_valid = False
            result.append(
                {
                    **record,
                    "revoked": bool(record["revoked"] or not audit_valid),
                    "handoff": await asyncio.to_thread(
                        self.store.load_sandbox_admission_handoff,
                        certificate.id,
                    ),
                    "progress": await asyncio.to_thread(
                        self.sandbox_maturity_snapshot,
                        certificate,
                    ),
                }
            )
        return result

    async def finalize_full_strategy_certificate(self, certificate_id: str):
        certificate = await asyncio.to_thread(
            self.maybe_issue_full_strategy_certificate_v2,
            certificate_id,
        )
        if certificate is None:
            raise HTTPException(
                409,
                "sealed blind PASS and all 30-episode OKX sandbox maturity gates are required",
            )
        return certificate.model_dump(mode="json")

    async def get_crypto_5m_data_contract(self):
        datasets = await asyncio.to_thread(self.store.list_research_datasets, 2_000)
        contract = assess_crypto_5m_contract(datasets)
        return {**contract.model_dump(mode="json"), "fingerprint": contract.fingerprint}

    async def get_alpha_model_artifact(self, model_id: str):
        payload = await asyncio.to_thread(self.store.load_alpha_model_artifact, model_id)
        if not payload:
            raise HTTPException(404, "alpha model artifact not found")
        return payload
