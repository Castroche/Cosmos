from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from time import perf_counter

from fastapi import HTTPException, Query

from .ai_configuration import AIConfigurationUpdate, AIProvider
from .ai_gateway import AIProviderError
from .alpha_factory import (
    AlphaModelArtifactV1,
    SandboxAdmissionCertificateV1,
    alpha_transaction_cost_fingerprint,
)
from .configuration import ConfigurationUpdate, ConnectionTestResult
from .domain import PlanStatus, TradingMode
from .research_certificates import CertificateRevocationEvent

logger = __import__("logging").getLogger(__name__)


class SettingsService:
    async def get_settings(self):
        return self.configuration_service.get()

    async def get_ai_settings(self):
        return self.ai_configuration_service.get()

    async def get_ai_usage(
        self,
        days: int = Query(default=30, ge=1, le=365),
        provider: AIProvider | None = None,
    ):
        return await asyncio.to_thread(
            self.ai_configuration_service.usage_report,
            days=days,
            provider=provider,
        )

    async def get_ai_health(self):
        return {
            "generated_at": datetime.now(UTC),
            "providers": await asyncio.to_thread(self.ai_gateway.health_snapshot),
            "research_fallback": "deterministic",
            "review_llm_calls": 0,
            "execution_llm_calls": 0,
            "pipeline_continues_when_ai_unavailable": True,
            "role_isolation": {
                "research": "optional_ai_generation_with_quality_gate",
                "review": "deterministic_only",
                "trading": "deterministic_only",
            },
        }

    async def update_ai_settings(self, update: AIConfigurationUpdate):
        try:
            view = await asyncio.to_thread(self.ai_configuration_service.save, update)
        except Exception as exc:
            raise HTTPException(400, str(exc)) from exc
        enabled = ",".join(
            str(item.provider) for item in view.configuration.providers if item.enabled
        )
        self.add_audit(
            "settings",
            "AI 服务配置已更新；凭证仅保存在本机系统凭证库",
            enabled_providers=enabled,
            routing_mode=view.configuration.routing_mode,
        )
        return view

    async def test_ai_provider(self, provider: AIProvider):
        self.ai_gateway.service = self.ai_configuration_service
        self.ai_gateway.proxy_url = (
            self.configuration_service.current_configuration().network_proxy_url
        )
        result = await self.ai_gateway.test_provider(provider)
        self.add_audit(
            "ai",
            "AI 厂商连接验收已完成",
            provider=provider,
            ok=result.ok,
            failure_code=result.failure_code,
            latency_ms=result.latency_ms,
        )
        return result

    async def discover_ai_models(self, provider: AIProvider):
        self.ai_gateway.service = self.ai_configuration_service
        self.ai_gateway.proxy_url = (
            self.configuration_service.current_configuration().network_proxy_url
        )
        try:
            return await self.ai_gateway.discover_models(provider)
        except AIProviderError as exc:
            raise HTTPException(502, {"code": exc.code, "message": exc.public_message}) from exc

    async def update_settings(self, update: ConfigurationUpdate):
        # Settings cannot change account identity while an entry is being allocated.
        async with self.v2_entry_allocation_lock:
            return await self._update_settings(update)

    async def _update_settings(self, update: ConfigurationUpdate):
        pass
        previous = self.configuration_service.current_configuration()
        previous_context = (
            previous.exchange_id,
            previous.trading_mode,
            previous.market_type,
        )
        next_context = (
            update.configuration.exchange_id,
            update.configuration.trading_mode,
            update.configuration.market_type,
        )
        credential_change = bool(
            update.credentials
            and (
                update.credentials.clear_existing
                or update.credentials.api_key
                or update.credentials.secret
                or update.credentials.passphrase
            )
        )
        previous_target_credentials = (
            self.configuration_service.credentials_for(update.configuration).copy()
            if credential_change
            else None
        )
        sensitive_change = previous_context != next_context or credential_change
        connection_change = sensitive_change or any(
            getattr(previous, field) != getattr(update.configuration, field)
            for field in ("network_proxy_url", "margin_mode", "contract_leverage")
        )
        if connection_change and (
            self.trading_intelligence_store.automation_runtime().enabled
            or self.trading_intelligence_store.list_positions()
            or self.trading_intelligence_store.list_entry_orders(active_only=True)
        ):
            raise HTTPException(
                409,
                "请先停止自动化并收口 v2 持仓及挂单，再修改账户、凭证或连接配置",
            )
        if sensitive_change and previous.trading_mode != TradingMode.PAPER:
            try:
                await self.runtime.initialize()
                assert self.runtime.execution is not None
                open_orders = await self.runtime.execution.open_orders()
                portfolio = await self.runtime.portfolio()
            except Exception as exc:
                raise HTTPException(
                    409,
                    f"无法确认旧执行环境没有风险敞口，拒绝切换设置：{exc}",
                ) from exc
            if open_orders:
                raise HTTPException(409, "旧执行环境仍有挂单，请先撤单并完成对账")
            if portfolio.open_positions:
                raise HTTPException(409, "旧执行环境仍有持仓，请先平仓后再切换")
        try:
            view = self.configuration_service.save(update)
            await self.runtime.initialize(force=True)
            self.v2_configuration_generation += 1
        except Exception as exc:
            self.store.save_configuration(previous)
            if previous_target_credentials is not None:
                with suppress(Exception):
                    self.configuration_service.restore_credentials(
                        update.configuration, previous_target_credentials
                    )
            with suppress(Exception):
                await self.runtime.initialize(force=True)
            raise HTTPException(400, str(exc)) from exc
        if previous_context != next_context:
            archived = 0
            for plan in self.plans.values():
                if plan.status in {PlanStatus.ACTIVE, PlanStatus.PAUSED}:
                    plan.status = PlanStatus.ARCHIVED
                    self.store.save_strategy(plan)
                    archived += 1
            if archived:
                self.add_audit(
                    "strategy",
                    "执行环境已切换，旧环境活动策略全部归档",
                    severity="warning",
                    archived=archived,
                )
        next_strategy_configuration_fingerprint = (
            self.strategy_certificate_configuration_fingerprint(view.configuration)
        )
        for item in await asyncio.to_thread(self.store.list_full_strategy_certificates_v2):
            certificate = item["certificate"]
            if (
                not item["revoked"]
                and certificate.get("configuration_fingerprint")
                != next_strategy_configuration_fingerprint
            ):
                event = CertificateRevocationEvent(
                    certificate_id=certificate["id"],
                    reason_code="configuration_or_cost_model_changed",
                    evidence_reference=next_strategy_configuration_fingerprint,
                )
                await asyncio.to_thread(self.store.revoke_research_certificate, event)
        next_cost_fingerprint = alpha_transaction_cost_fingerprint(
            fee_bps=view.configuration.fee_bps,
            slippage_bps=view.configuration.slippage_bps,
        )
        sandbox_certificates = await asyncio.to_thread(
            self.store.list_research_certificates_by_type,
            "sandbox_admission_v1",
        )
        for item in sandbox_certificates:
            if item["revoked"]:
                continue
            admission = SandboxAdmissionCertificateV1.model_validate(item["certificate"])
            cost_fingerprints = set()
            for model_id in admission.model_artifact_ids:
                payload = await asyncio.to_thread(self.store.load_alpha_model_artifact, model_id)
                if payload:
                    cost_fingerprints.add(
                        AlphaModelArtifactV1.model_validate(payload).cost_model_fingerprint
                    )
            if cost_fingerprints != {next_cost_fingerprint}:
                await asyncio.to_thread(
                    self.store.revoke_research_certificate,
                    CertificateRevocationEvent(
                        certificate_id=admission.id,
                        reason_code="configuration_or_cost_model_changed",
                        evidence_reference=next_cost_fingerprint,
                    ),
                )
        self.add_audit(
            "settings",
            "运行设置已更新",
            exchange=view.configuration.exchange_id,
            mode=str(view.configuration.trading_mode),
        )
        return view

    async def test_connection(self, update: ConfigurationUpdate):
        started = perf_counter()
        credentials = self.configuration_service.credentials_for(update.configuration)
        if update.credentials:
            incoming = update.credentials.model_dump(exclude={"clear_existing"})
            credentials.update({key: value for key, value in incoming.items() if value})
        try:
            return await self.runtime.test_connection(update.configuration, credentials)
        except Exception as exc:
            return ConnectionTestResult(
                ok=False,
                exchange=update.configuration.exchange_id,
                mode=update.configuration.trading_mode,
                public_market_ok=False,
                private_account_ok=False
                if update.configuration.trading_mode != TradingMode.PAPER
                else None,
                latency_ms=round((perf_counter() - started) * 1000),
                symbol=update.configuration.default_symbol,
                failure_code="public_market_unavailable",
                required_action="检查网络、代理、交易所可用性和交易品种后重新测试连接。",
                message=str(exc),
            )
