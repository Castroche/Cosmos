from __future__ import annotations

from .api_contracts import DevelopmentAuditIssueRequest, SandboxAdmissionIssueRequest
from .dependencies import ServiceDependency


def register(app):

    @app.post("/api/research/development-audits")
    async def issue_development_audit(
        request: DevelopmentAuditIssueRequest, *, services: ServiceDependency
    ):
        """Derive admission metrics from immutable server-side campaign cells."""
        return await services.issue_development_audit(request)

    @app.get("/api/research/development-audits")
    async def list_development_audits(*, services: ServiceDependency):
        return await services.list_development_audits()

    @app.post("/api/research/sandbox-admissions")
    async def issue_sandbox_admission(
        request: SandboxAdmissionIssueRequest, *, services: ServiceDependency
    ):
        return await services.issue_sandbox_admission(request)

    @app.get("/api/research/sandbox-admissions")
    async def list_sandbox_admissions(*, services: ServiceDependency):
        return await services.list_sandbox_admissions()

    @app.post("/api/research/sandbox-admissions/{certificate_id}/finalize")
    async def finalize_full_strategy_certificate(
        certificate_id: str, *, services: ServiceDependency
    ):
        return await services.finalize_full_strategy_certificate(certificate_id)

    @app.get("/api/research/data-contracts/crypto-5m-v1")
    async def get_crypto_5m_data_contract(*, services: ServiceDependency):
        return await services.get_crypto_5m_data_contract()

    @app.get("/api/research/models/{model_id}")
    async def get_alpha_model_artifact(model_id: str, *, services: ServiceDependency):
        return await services.get_alpha_model_artifact(model_id)
