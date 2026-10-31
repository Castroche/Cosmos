from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from hashlib import sha256

from cosmos_quant.exchanges.ccxt_adapter import CcxtExchangeAdapter

from .entry_runtime import EntryOrderService
from .trading_intelligence import AutomationRuntimeStatus, AutomationRuntimeV2, TradingModeV2
from .v2_position_runtime import PositionReconciliationIntegrityError, V2PositionReconciler

logger = __import__("logging").getLogger(__name__)


class PositionWorkersService:
    async def reconcile_v2_demo_state(self, exchange, *, cancel_all: bool = False) -> dict:
        # Hold the account lock across venue awaits and ledger commits. Startup
        # recovery, the position worker, and new entries must not manage one pool
        # from different position versions or partially reconciled entry fills.
        async with self.v2_entry_allocation_lock:
            configuration = self.configuration_service.current_configuration()
            automation = self.require_v2_automation()
            service = EntryOrderService(
                self.trading_intelligence_store,
                on_terminal=automation.on_entry_terminal,
                margin_mode=str(configuration.margin_mode),
                execution_leverage=configuration.contract_leverage,
            )
            counters = await service.reconcile(
                exchange,
                cancel_all=(
                    cancel_all
                    or not automation.runtime().enabled
                    or self.risk_state_service.get().halted
                ),
            )
            if counters.get("uncertain"):
                raise PositionReconciliationIntegrityError("pending entry order remains uncertain")
            result = await V2PositionReconciler(
                self.trading_intelligence_store,
                halt=self.risk_state_service.halt,
                on_trade_completed=self.enqueue_v2_trade_completed,
                margin_mode=str(configuration.margin_mode),
                commissioned_max_leverage=configuration.risk.max_leverage,
            ).reconcile(exchange)
            self.risk_state_service.recover_protection_reconciliation()
            return result

    async def reconcile_v2_before_resume(self) -> None:
        automation = self.require_v2_automation().runtime()
        entries = await asyncio.to_thread(
            self.trading_intelligence_store.list_entry_orders,
            needs_reconciliation=True,
            mode="OKX_DEMO",
        )
        needs_demo = any(item.mode == "OKX_DEMO" and not item.terminal_notified for item in entries)
        risk_state = self.risk_state_service.get()
        protection_recovery_pending = (
            risk_state.halted and risk_state.source == "protection_reconciliation"
        )
        if (
            not needs_demo
            and not self.trading_intelligence_store.list_positions()
            and not protection_recovery_pending
        ):
            return
        adapter = self.build_v2_demo_adapter()
        try:
            await self.reconcile_v2_demo_state(adapter, cancel_all=not automation.enabled)
        finally:
            await adapter.close()

    def build_v2_demo_adapter(self) -> CcxtExchangeAdapter:
        view = self.configuration_service.get()
        configuration = view.configuration
        return self.CcxtExchangeAdapter(
            "okx",
            self.secret_store.get_credentials("okx:sandbox"),
            sandbox=True,
            live_enabled=False,
            public_only=False,
            market_type="swap",
            margin_mode=configuration.margin_mode,
            contract_leverage=configuration.contract_leverage,
            store=self.store,
            account_id="okx:demo:swap:v2",
            proxy_url=configuration.network_proxy_url,
        )

    async def trading_intelligence_position_loop(self) -> None:
        adapter: CcxtExchangeAdapter | None = None
        adapter_generation = -1
        try:
            while True:
                service = self.trading_intelligence_automation
                automation = service.runtime() if service else AutomationRuntimeV2()
                pending = self.trading_intelligence_store.list_entry_orders(active_only=True)
                positions = self.trading_intelligence_store.list_positions()
                risk_state = self.risk_state_service.get()
                protection_recovery_pending = (
                    risk_state.halted and risk_state.source == "protection_reconciliation"
                )
                if (
                    not positions
                    and not any(item.mode == "OKX_DEMO" for item in pending)
                    and (
                        not automation.enabled
                        or automation.requested_mode != TradingModeV2.OKX_DEMO
                    )
                    and not protection_recovery_pending
                ):
                    if adapter is not None:
                        await adapter.close()
                        adapter = None
                    await asyncio.sleep(5)
                    continue
                if adapter is not None and adapter_generation != self.v2_configuration_generation:
                    await adapter.close()
                    adapter = None
                if adapter is None:
                    adapter = self.build_v2_demo_adapter()
                    adapter_generation = self.v2_configuration_generation
                try:
                    await self.reconcile_v2_demo_state(
                        adapter,
                        cancel_all=not automation.enabled or self.risk_state_service.get().halted,
                    )
                    current = service.runtime()
                    self.trading_intelligence_store.save_automation_runtime(
                        current.model_copy(
                            update={
                                "status": (
                                    AutomationRuntimeStatus.STOPPED
                                    if not current.enabled
                                    else AutomationRuntimeStatus.DEGRADED
                                    if current.symbol_failures
                                    else AutomationRuntimeStatus.RUNNING
                                ),
                                "last_reconciliation_at": datetime.now(UTC),
                                "last_error_code": next(
                                    iter(current.symbol_error_codes.values()),
                                    None,
                                ),
                                "last_error_hash": (
                                    current.last_error_hash if current.symbol_error_codes else None
                                ),
                                "updated_at": datetime.now(UTC),
                            }
                        ),
                        expected_updated_at=current.updated_at,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.metrics.increment("trading_intelligence_position_loop_failures_total")
                    managed_positions = self.trading_intelligence_store.list_positions()
                    if managed_positions or isinstance(
                        exc,
                        PositionReconciliationIntegrityError,
                    ):
                        self.risk_state_service.halt(
                            "Trading Intelligence v2 Demo position worker failed while "
                            f"managing an attributable position: {type(exc).__name__}",
                            source="protection_reconciliation",
                        )
                        current = service.runtime()
                        self.trading_intelligence_store.save_automation_runtime(
                            current.model_copy(
                                update={
                                    "status": AutomationRuntimeStatus.HALTED,
                                    "last_error_code": (f"position_integrity:{type(exc).__name__}"),
                                    "last_error_hash": sha256(str(exc).encode()).hexdigest(),
                                    "updated_at": datetime.now(UTC),
                                }
                            ),
                            expected_updated_at=current.updated_at,
                        )
                    else:
                        current = service.runtime()
                        self.trading_intelligence_store.save_automation_runtime(
                            current.model_copy(
                                update={
                                    "status": AutomationRuntimeStatus.DEGRADED,
                                    "last_error_code": (f"position_transport:{type(exc).__name__}"),
                                    "last_error_hash": sha256(str(exc).encode()).hexdigest(),
                                    "updated_at": datetime.now(UTC),
                                }
                            ),
                            expected_updated_at=current.updated_at,
                        )
                    if adapter is not None:
                        await adapter.close()
                        adapter = None
                await asyncio.sleep(1)
        finally:
            if adapter is not None:
                await adapter.close()
