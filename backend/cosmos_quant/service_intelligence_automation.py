from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from fastapi import HTTPException
from pydantic import ValidationError

from .api_contracts import (
    HumanLiveGrantIssueRequest,
    HumanLiveGrantPreviewRequest,
    TradingIntelligenceAutomationRequest,
)
from .domain import PositionSide, Side
from .trading_intelligence import AutomationRuntimeStatus, HumanLiveGrant, TradingModeV2
from .trading_intelligence_automation import TradingIntelligenceAutomation

logger = __import__("logging").getLogger(__name__)


class IntelligenceAutomationService:
    async def v2_demo_commissioning(self, symbols: list[str]) -> dict:
        view = self.configuration_service.get()
        blockers: list[str] = []
        credentials_view = view.credential_scopes.get("okx:sandbox")
        if not credentials_view or not (
            credentials_view.api_key_configured
            and credentials_view.secret_configured
            and credentials_view.passphrase_configured
        ):
            blockers.append("okx_demo_credentials_incomplete")
            return {"eligible": False, "blockers": blockers}
        risk_state = self.risk_state_service.get()
        if risk_state.halted:
            blockers.append(f"global_risk_halt:{risk_state.source}")
        for attempt in range(3):
            adapter = self.build_v2_demo_adapter()
            try:
                health = await adapter.health()
                account = await adapter.validate_account_configuration()
                portfolio = await adapter.portfolio()
                open_orders = await adapter.open_orders()
                known_order_ids = {
                    order_id
                    for episode in self.trading_intelligence_store.list_episodes(limit=1000)
                    for order_id in episode.order_ids
                }
                unknown_orders = [item.id for item in open_orders if item.id not in known_order_ids]
                if unknown_orders:
                    blockers.append("unknown_demo_open_orders")
                known_position_scopes = {
                    (
                        item.symbol.removesuffix(":USDT"),
                        Side.LONG if item.side == "LONG" else Side.SHORT,
                    )
                    for item in self.trading_intelligence_store.list_positions()
                }
                unknown_positions = [
                    item
                    for item in portfolio.open_positions
                    if (item.symbol, item.side) not in known_position_scopes
                ]
                if unknown_positions:
                    blockers.append("unknown_demo_open_positions")
                protection = {}
                for symbol in symbols:
                    protection[symbol] = all(
                        [
                            await adapter.protection_available(
                                symbol.removesuffix(":USDT"), position_side
                            )
                            for position_side in (PositionSide.LONG, PositionSide.SHORT)
                        ]
                    )
                    if not protection[symbol]:
                        blockers.append(f"native_protection_unavailable:{symbol}")
                return {
                    "eligible": not blockers,
                    "blockers": blockers,
                    "health": health,
                    "account": account,
                    "equity": str(portfolio.equity),
                    "open_position_count": len(portfolio.open_positions),
                    "open_order_count": len(open_orders),
                    "unknown_order_count": len(unknown_orders),
                    "unknown_position_count": len(unknown_positions),
                    "protection": protection,
                }
            except Exception as exc:
                if attempt == 2:
                    return {
                        "eligible": False,
                        "blockers": [*blockers, "okx_demo_temporarily_unavailable"],
                        "error_code": type(exc).__name__,
                        "error_hash": sha256(str(exc).encode()).hexdigest(),
                    }
                await asyncio.sleep(0.5 * (attempt + 1))
            finally:
                await adapter.close()
        raise AssertionError("bounded Demo commissioning loop did not return")

    def require_v2_automation(self) -> TradingIntelligenceAutomation:
        pass
        if self.trading_intelligence_automation is None:
            self.trading_intelligence_automation = TradingIntelligenceAutomation(
                self.trading_intelligence_store,
                self.trading_intelligence_engine,
                decide=self.invoke_automated_trading_intelligence_decision,
                execute=self.execute_automated_trading_intelligence_plan,
                reconcile=self.reconcile_v2_before_resume,
            )
        return self.trading_intelligence_automation

    async def get_trading_intelligence_automation(self):
        return await asyncio.to_thread(self.require_v2_automation().runtime)

    async def commission_trading_intelligence_automation(
        self,
        request: TradingIntelligenceAutomationRequest,
    ):
        if request.mode == TradingModeV2.SHADOW:
            return {"eligible": True, "blockers": [], "mode": str(request.mode)}
        return await self.v2_demo_commissioning(request.symbols)

    async def start_trading_intelligence_automation(
        self,
        request: TradingIntelligenceAutomationRequest,
    ):
        generation = self.risk_state_service.entry_generation
        configuration_generation = self.v2_configuration_generation
        if request.mode == TradingModeV2.OKX_DEMO:
            commissioning = await self.v2_demo_commissioning(request.symbols)
            if not commissioning["eligible"]:
                raise HTTPException(409, commissioning)
        service = self.require_v2_automation()
        async with self.v2_submission_lock:
            if generation != self.risk_state_service.entry_generation:
                raise HTTPException(409, "自动化启动已被停止操作取消，请重新发起")
            if configuration_generation != self.v2_configuration_generation:
                raise HTTPException(409, "验收期间账户配置已改变，请重新验收")
            if self.risk_state_service.get().halted:
                raise HTTPException(409, "风险暂停尚未解除，不能启动自动化")
            # No await between the generation check and the durable state transition.
            service.configure(enabled=True, requested_mode=request.mode, symbols=request.symbols)
        try:
            await service.resume(evaluate_market_states=False)
        except Exception:
            if generation == self.risk_state_service.entry_generation:
                service.configure(
                    enabled=False,
                    requested_mode=request.mode,
                    symbols=request.symbols,
                )
            raise
        if (
            generation != self.risk_state_service.entry_generation
            or self.risk_state_service.get().halted
        ):
            return service.runtime()
        current = service.runtime()
        running = current.model_copy(
            update={
                "status": (
                    AutomationRuntimeStatus.DEGRADED
                    if current.symbol_failures
                    else AutomationRuntimeStatus.RUNNING
                ),
                "updated_at": datetime.now(UTC),
            }
        )
        await asyncio.to_thread(
            self.trading_intelligence_store.save_automation_runtime,
            running,
            expected_updated_at=current.updated_at,
        )
        self.learning_shadow.start()
        return service.runtime()

    async def stop_trading_intelligence_automation(self):
        self.risk_state_service.invalidate_entries()
        await self.learning_shadow.stop()
        service = self.require_v2_automation()
        stopped = await asyncio.to_thread(
            service.configure,
            enabled=False,
            requested_mode=service.runtime().requested_mode,
            symbols=service.runtime().symbols,
        )
        await self.reconcile_v2_before_resume()
        return stopped

    async def preview_trading_intelligence_live_grant(self, request: HumanLiveGrantPreviewRequest):
        return self.v2_live_grant_preview_payload(request)

    async def issue_trading_intelligence_live_grant(self, request: HumanLiveGrantIssueRequest):
        preview = self.v2_live_grant_preview_payload(request)
        if preview["blockers"]:
            raise HTTPException(409, {"blockers": preview["blockers"]})
        if request.configuration_fingerprint != preview["configuration_fingerprint"]:
            raise HTTPException(409, "configuration changed after grant preview")
        try:
            grant = HumanLiveGrant(
                symbols=request.symbols,
                max_risk_per_trade=request.max_risk_per_trade,
                max_portfolio_risk=request.max_portfolio_risk,
                max_leverage=request.max_leverage,
                max_daily_drawdown=request.max_daily_drawdown,
                max_open_positions=request.max_open_positions,
                configuration_fingerprint=request.configuration_fingerprint,
                expires_at=datetime.now(UTC) + timedelta(hours=request.valid_hours),
                risk_confirmation_one=request.risk_confirmation_one,
                risk_confirmation_two=request.risk_confirmation_two,
            )
        except ValidationError as exc:
            raise HTTPException(400, exc.errors(include_url=False)) from exc
        await asyncio.to_thread(self.trading_intelligence_store.issue_grant, grant)
        self.add_audit(
            "trading_intelligence",
            "HumanLiveGrant v2 issued",
            severity="critical",
            grant_id=grant.grant_id,
            expires_at=grant.expires_at.isoformat(),
        )
        return grant

    async def revoke_trading_intelligence_live_grant(self, grant_id: str):
        try:
            grant = await asyncio.to_thread(self.trading_intelligence_store.revoke_grant, grant_id)
        except KeyError as exc:
            raise HTTPException(404, "HumanLiveGrant not found") from exc
        self.add_audit(
            "trading_intelligence",
            "HumanLiveGrant v2 revoked",
            severity="critical",
            grant_id=grant_id,
        )
        return grant

    async def unregistered_api_route(self, unregistered_path: str):
        del unregistered_path
        raise HTTPException(404, "API route not found")
