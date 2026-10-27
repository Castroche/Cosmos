from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, Literal

from .domain import MarketSnapshot
from .trading_intelligence import (
    AutomationRuntimeStatus,
    AutomationRuntimeV2,
    MarketStateV2,
    PlanState,
    PositionStateV2,
    ThesisState,
    TradingModeV2,
    WaitPlan,
    WaitRuntimeStatus,
    WaitRuntimeV2,
    WakeAction,
    WakeCondition,
    WakeKind,
    evaluate_wake_condition,
)
from .trading_intelligence_engine import EventDrivenTradingEngine
from .trading_intelligence_store import TradingIntelligenceStore

DecisionTrigger = Literal[
    "NO_ACTIVE_THESIS",
    "WAKE_TRIGGERED",
    "PLAN_EXPIRED",
    "PLAN_INVALIDATED",
    "TRADE_COMPLETED",
    "THESIS_INVALIDATED",
    "MAJOR_MARKET_EVENT",
    "BOUNDED_RETRY",
    "ENTRY_ORDER_TERMINAL",
]
DecisionInvoker = Callable[
    [str, DecisionTrigger, str | None, TradingModeV2], Awaitable[dict[str, Any]]
]
MAX_ACTIVE_THESIS_AGE = timedelta(hours=4)
ExecutionInvoker = Callable[..., Awaitable[dict[str, Any] | Any]]


class DecisionContractError(RuntimeError):
    """The LLM request completed but its decision failed the execution contract."""


def is_expected_automated_entry_rejection(status_code: int, reason: str) -> bool:
    """Return whether an entry refusal is a normal fail-closed automation outcome."""

    expected_rejections = {
        "an attributable position already exists for this symbol; "
        "a second plan cannot share the venue's aggregate position",
        "current MarketState v2 is unavailable",
        "MarketState v2 is stale",
        "plan is not waiting for an entry trigger",
        "same-direction add requires exact position_id and position_version",
        "target position is no longer open",
        "exact current plan_version is required",
        "entry invalidated by a risk stop",
    }
    return status_code == 409 and (
        reason in expected_rejections
        or reason.startswith("global risk halt is active:")
        or reason.startswith("OKX position mode must be long_short_mode")
    )


class TradingIntelligenceAutomation:
    """Durable event dispatcher for autonomous Shadow and OKX Demo operation.

    LIVE_AUTHORIZED is intentionally absent from this class. Live remains a separate
    human-granted API path even if live credentials are later configured.
    """

    def __init__(
        self,
        store: TradingIntelligenceStore,
        engine: EventDrivenTradingEngine,
        *,
        decide: DecisionInvoker,
        execute: ExecutionInvoker,
        reconcile: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        self.store = store
        self.engine = engine
        self.decide = decide
        self.execute = execute
        self.reconcile = reconcile
        self._locks = {symbol: asyncio.Lock() for symbol in AutomationRuntimeV2().symbols}
        self.engine.on_wake = self.on_wake

    def state_with_latest_price(self, state: MarketStateV2) -> MarketStateV2:
        artifact = self.store.get_artifact(f"latest-price:{state.symbol}")
        if not artifact:
            return state
        tick = MarketSnapshot.model_validate(artifact["payload"])
        if tick.timestamp < state.data_as_of or datetime.now(UTC) - tick.timestamp > timedelta(
            seconds=15
        ):
            return state
        return state.model_copy(
            update={
                "market": state.market.model_copy(
                    update={
                        "values": {
                            **state.market.values,
                            "last": str(tick.last),
                            "bid": str(tick.bid),
                            "ask": str(tick.ask),
                        },
                    }
                )
            }
        )

    async def on_price_tick(self, tick: MarketSnapshot) -> list[str]:
        """Evaluate executable prices without refreshing feature timestamps or calling the LLM."""
        return await asyncio.to_thread(self._process_price_tick, tick)

    def _process_price_tick(self, tick: MarketSnapshot) -> list[str]:
        symbol = tick.symbol if tick.symbol.endswith(":USDT") else f"{tick.symbol}:USDT"
        if symbol not in self._locks or tick.last <= 0:
            return []
        if abs((datetime.now(UTC) - tick.timestamp).total_seconds()) > 15:
            return []
        previous_artifact = self.store.get_artifact(f"latest-price:{symbol}")
        previous_tick = (
            MarketSnapshot.model_validate(previous_artifact["payload"])
            if previous_artifact
            else None
        )
        if previous_tick and tick.timestamp <= previous_tick.timestamp:
            return []
        self.store.save_artifact(f"latest-price:{symbol}", "MarketSnapshot", tick)
        # Invalidation must continue while stopped and while a symbol's LLM is busy.
        for entry in self.store.list_entry_orders(symbol=symbol, active_only=True):
            zone = entry.valid_price_range
            invalid = bool(zone and not zone.lower <= tick.last <= zone.upper)
            if invalid and not entry.cancel_requested:
                self.store.update_entry_order(
                    entry.entry_id,
                    expected_version=entry.version,
                    updates={
                        "cancel_requested": True,
                        "reason": "latest price left valid_price_range",
                    },
                )
        runtime = self.runtime()
        state = self.store.latest_market_state(symbol)
        if not state or not runtime.enabled or symbol not in runtime.symbols:
            return []
        if not state.healthy_for_new_risk or datetime.now(UTC) - state.data_as_of > timedelta(
            minutes=10
        ):
            return []
        payload = self.engine._wake_payload(self.state_with_latest_price(state))
        previous_payload = (
            {**payload, "price": str(previous_tick.last)}
            if previous_tick
            else self.engine._wake_payload(state)
        )

        def uses_tick(condition: WakeCondition) -> bool:
            if condition.children:
                # A tick can change the truth of a composite as soon as any
                # nested price leaf changes; metric/structure leaves are read
                # from the last persisted state in the same evaluation.
                return any(uses_tick(child) for child in condition.children)
            return condition.kind in {
                WakeKind.PRICE_IN_ZONE,
                WakeKind.PRICE_CROSSES_ABOVE,
                WakeKind.PRICE_CROSSES_BELOW,
            }

        fired = []
        for wait in self.store.list_wait_runtimes(symbol=symbol, active_only=True):
            if tick.timestamp >= wait.wait_plan.expires_at:
                continue
            for condition in wait.wait_plan.wake_conditions:
                if not uses_tick(condition) or not evaluate_wake_condition(
                    condition,
                    payload,
                    previous_payload,
                    now=tick.timestamp,
                ):
                    continue
                event_id, created = self.store.fire_wake_once(
                    condition_id=condition.condition_id,
                    event_key=f"tick:{tick.timestamp.isoformat()}:{tick.last}",
                    aggregate_id=wait.wait_runtime_id,
                    aggregate_version=wait.version,
                    payload={
                        "condition": condition.model_dump(mode="json"),
                        "market_state_hash": state.state_hash,
                        "price": str(tick.last),
                        "price_as_of": tick.timestamp.isoformat(),
                        "episode_id": wait.episode_id,
                    },
                )
                if created:
                    self.store.update_wait_runtime(
                        wait.wait_runtime_id,
                        expected_version=wait.version,
                        status=WaitRuntimeStatus.FIRED,
                    )
                    self.enqueue_decision(symbol, "WAKE_TRIGGERED", event_id)
                    fired.append(event_id)
                break
        return fired

    def enqueue_decision(self, symbol: str, trigger: DecisionTrigger, event_id: str) -> None:
        self.store.append_event(
            dedupe_key=f"automation-deferred:{symbol}:{event_id}",
            event_type="AUTOMATION_DECISION_QUEUED",
            aggregate_type="symbol",
            aggregate_id=symbol,
            aggregate_version=1,
            payload={"trigger": trigger, "trigger_event_id": event_id},
        )

    async def on_entry_terminal(self, symbol: str, event_id: str) -> None:
        self.enqueue_decision(symbol, "ENTRY_ORDER_TERMINAL", event_id)

    async def drain_decisions(self, symbol: str) -> None:
        runtime = self.runtime()
        if not runtime.enabled or symbol not in runtime.symbols:
            return
        retry = runtime.symbol_next_retry_at.get(symbol)
        if retry and datetime.now(UTC) < retry:
            return
        async with self._locks[symbol]:
            item = await asyncio.to_thread(self.store.next_queued_decision, symbol)
            if item:
                state = self.store.latest_market_state(symbol)
                if not state or not self.runtime().enabled:
                    return
                await self._invoke_decision(
                    state,
                    trigger=item["payload"]["trigger"],
                    trigger_event_id=item["payload"]["trigger_event_id"],
                )
                self.store.append_event(
                    dedupe_key=f"automation-dequeued:{item['event_id']}",
                    event_type="AUTOMATION_DECISION_DEQUEUED",
                    aggregate_type="symbol",
                    aggregate_id=symbol,
                    aggregate_version=1,
                    payload={"queued_event_id": item["event_id"]},
                )
                await asyncio.to_thread(
                    self.store.advance_decision_cursor,
                    symbol,
                    item["sequence"],
                )
                return

    def runtime(self) -> AutomationRuntimeV2:
        return self.store.automation_runtime()

    def configure(
        self,
        *,
        enabled: bool,
        requested_mode: TradingModeV2,
        symbols: list[str] | None = None,
    ) -> AutomationRuntimeV2:
        if requested_mode == TradingModeV2.LIVE_AUTHORIZED:
            raise ValueError("autonomous runtime cannot select LIVE_AUTHORIZED")
        current = self.runtime()
        updated = current.model_copy(
            update={
                "enabled": enabled,
                "requested_mode": requested_mode,
                "symbols": symbols or current.symbols,
                "status": (
                    AutomationRuntimeStatus.STARTING if enabled else AutomationRuntimeStatus.STOPPED
                ),
                "consecutive_failures": 0 if enabled else current.consecutive_failures,
                "last_error_code": None if enabled else current.last_error_code,
                "last_error_hash": None if enabled else current.last_error_hash,
                "next_retry_at": None if enabled else current.next_retry_at,
                "symbol_failures": {} if enabled else current.symbol_failures,
                "symbol_next_retry_at": {} if enabled else current.symbol_next_retry_at,
                "symbol_error_codes": {} if enabled else current.symbol_error_codes,
                "updated_at": datetime.now(UTC),
            }
        )
        self.store.save_automation_runtime(updated)
        return updated

    async def on_market_state(self, state: MarketStateV2) -> list[str]:
        runtime = self.runtime()
        if not runtime.enabled or state.symbol not in runtime.symbols:
            return await self.engine.ingest_market_state(state)
        async with self._locks[state.symbol]:
            runtime = self.runtime()
            if not runtime.enabled or state.symbol not in runtime.symbols:
                return await self.engine.ingest_market_state(state)
            symbol_retry = runtime.symbol_next_retry_at.get(state.symbol)
            if symbol_retry and datetime.now(UTC) < symbol_retry:
                return await self.engine.ingest_market_state(state)
            fired = await self.engine.ingest_market_state(state)
            await self._expire_lifecycle(state)
            active_theses = self.store.list_active_theses(state.symbol)
            active_plans = self.store.list_open_plans(state.symbol)
            active_waits = self.store.list_wait_runtimes(
                symbol=state.symbol,
                active_only=True,
            )
            positions = self.store.list_positions(symbol=state.symbol)
            # A failed wake may have consumed its last wait while a filled plan
            # still occupies this symbol. Restore a durable, bounded recovery
            # wake instead of waiting forever for NO_ACTIVE_THESIS.
            runtime = self.runtime()
            if (
                (runtime.symbol_failures.get(state.symbol) or any(
                    position.state in {PositionStateV2.OPEN, PositionStateV2.PARTIALLY_FILLED}
                    for position in positions
                ))
                and not active_waits
                and self.store.next_queued_decision(state.symbol) is None
            ):
                self._schedule_fail_closed_wait(
                    state=state,
                    result={},
                    trigger_event_id=(
                        f"recovery:{state.symbol}:"
                        f"{runtime.symbol_next_retry_at.get(state.symbol)}"
                    ),
                    delay=(
                        timedelta(seconds=30) if runtime.symbol_failures.get(state.symbol)
                        else timedelta(minutes=30)
                    ),
                    reason=(
                        "[FAIL_CLOSED_COOLDOWN] restore stranded decision recovery"
                        if runtime.symbol_failures.get(state.symbol)
                        else "[POSITION_REVIEW] restore missing open-position review"
                    ),
                )
                self._mark_cycle_success()
                return fired
            if not active_theses and not active_plans and not active_waits and not positions:
                result = await self._invoke_decision(
                    state,
                    trigger="NO_ACTIVE_THESIS",
                    trigger_event_id=None,
                )
                if result and (result.get("decision_error") or result.get("rejected")):
                    self._schedule_fail_closed_wait(
                        state=state,
                        result=result,
                        trigger_event_id=state.state_hash,
                        delay=timedelta(minutes=5),
                        reason="[BOUNDED_RETRY] one repair after a rejected no-thesis decision",
                    )
            else:
                covered_thesis_ids = {item.thesis_id for item in active_plans}
                covered_thesis_ids.update(
                    item.wait_plan.thesis_id for item in active_waits if item.wait_plan.thesis_id
                )
                covered_thesis_ids.update(
                    plan.thesis_id
                    for item in positions
                    if (plan := self.store.get_plan(item.plan_id)) is not None
                )
                for thesis in active_theses:
                    if thesis.thesis_id in covered_thesis_ids:
                        continue
                    if any(item.wait_plan.thesis_id is None for item in active_waits):
                        continue
                    event_id, created = self.store.append_event(
                        dedupe_key=f"thesis-orphaned:{thesis.thesis_id}:{thesis.version}",
                        event_type="THESIS_ORPHANED",
                        aggregate_type="thesis",
                        aggregate_id=thesis.thesis_id,
                        aggregate_version=thesis.version,
                        payload={
                            "market_state_hash": state.state_hash,
                            "reason": "active thesis has no plan, wait runtime, or position",
                        },
                    )
                    if created:
                        result = await self._invoke_decision(
                            state,
                            trigger="BOUNDED_RETRY",
                            trigger_event_id=event_id,
                        )
                        if result and (result.get("decision_error") or result.get("rejected")):
                            self._cancel_active_thesis(thesis.thesis_id, event_id)
                    break
            self._mark_cycle_success()
            return fired

    async def on_wake(
        self,
        aggregate_id: str,
        condition: WakeCondition,
        state: MarketStateV2,
        event_id: str,
    ) -> None:
        runtime = self.runtime()
        if not runtime.enabled or state.symbol not in runtime.symbols:
            return
        if condition.action == WakeAction.RECALL_LLM:
            source_runtime = None
            artifact = self.store.get_artifact(aggregate_id)
            if artifact and artifact["artifact_type"] == "WaitRuntimeV2":
                source_runtime = WaitRuntimeV2.model_validate(artifact["payload"])
            result = await self._invoke_decision(
                state,
                trigger="WAKE_TRIGGERED",
                trigger_event_id=event_id,
            )
            if (
                result
                and (result.get("decision_error") or result.get("rejected"))
                and source_runtime
            ):
                if source_runtime.wait_plan.reason.startswith("[BOUNDED_RETRY]"):
                    owning_positions = self.store.list_positions(symbol=state.symbol)
                    owning_plans = self.store.list_open_plans(state.symbol)
                    thesis_owned = any(
                        plan.thesis_id == source_runtime.wait_plan.thesis_id
                        and (plan.state == PlanState.TRIGGERED or any(
                            position.plan_id == plan.plan_id
                            or plan.plan_id in position.plan_ids
                            for position in owning_positions
                        ))
                        for plan in owning_plans
                    )
                    if source_runtime.wait_plan.thesis_id and not thesis_owned:
                        self._cancel_active_thesis(
                            source_runtime.wait_plan.thesis_id,
                            event_id,
                        )
                    self._schedule_fail_closed_wait(
                        state=state,
                        result=result,
                        trigger_event_id=event_id,
                        delay=timedelta(minutes=30),
                        reason=(
                            "[FAIL_CLOSED_COOLDOWN] decision rejected after its one bounded retry"
                        ),
                    )
                elif source_runtime.wait_plan.reason.startswith("[FAIL_CLOSED_COOLDOWN]"):
                    self._schedule_fail_closed_wait(
                        state=state,
                        result=result,
                        trigger_event_id=event_id,
                        delay=timedelta(minutes=30),
                        reason=(
                            "[FAIL_CLOSED_COOLDOWN] schema remains invalid; retry only at "
                            "the next bounded cooldown"
                        ),
                    )
                else:
                    self._schedule_bounded_retry(source_runtime, state, event_id)
            return
        if condition.action == WakeAction.EXECUTE_ENTRY:
            plan = self.store.get_plan(aggregate_id)
            if not plan:
                raise RuntimeError("wake references an unknown trade plan")
            episode = next(
                (
                    item
                    for item in self.store.list_episodes(symbol=state.symbol, limit=500)
                    if item.plan_id == plan.plan_id
                ),
                None,
            )
            if not episode:
                raise RuntimeError("trade plan has no owning DecisionEpisode")
            try:
                result = await self.execute(
                    plan.plan_id,
                    episode.episode_id,
                    event_id,
                    runtime.requested_mode,
                )
            except Exception as exc:
                self._record_failure(
                    state.symbol,
                    operation="execution",
                    claim_id=event_id,
                    error=exc,
                )
                return
            current = self.runtime()
            symbol_failures = dict(current.symbol_failures)
            symbol_retries = dict(current.symbol_next_retry_at)
            symbol_errors = dict(current.symbol_error_codes)
            symbol_failures.pop(state.symbol, None)
            symbol_retries.pop(state.symbol, None)
            symbol_errors.pop(state.symbol, None)
            self._save_runtime(
                current.model_copy(
                    update={
                        "status": (
                            AutomationRuntimeStatus.DEGRADED
                            if symbol_failures
                            else AutomationRuntimeStatus.RUNNING
                        ),
                        "consecutive_failures": max(
                            symbol_failures.values(),
                            default=0,
                        ),
                        "last_execution_at": datetime.now(UTC),
                        "last_error_code": next(
                            iter(symbol_errors.values()),
                            None,
                        ),
                        "last_error_hash": (current.last_error_hash if symbol_errors else None),
                        "next_retry_at": min(symbol_retries.values(), default=None),
                        "symbol_failures": symbol_failures,
                        "symbol_next_retry_at": symbol_retries,
                        "symbol_error_codes": symbol_errors,
                    }
                )
            )
            del result
            return
        if condition.action == WakeAction.CANCEL_PLAN:
            plan = self.store.get_plan(aggregate_id)
            if plan and plan.state not in {
                PlanState.COMPLETED,
                PlanState.EXPIRED,
                PlanState.INVALIDATED,
                PlanState.CANCELLED,
            }:
                self.store.transition_plan(
                    plan.plan_id,
                    expected_version=plan.version,
                    target=PlanState.CANCELLED,
                )

    async def on_trade_completed(self, symbol: str, event_id: str) -> None:
        state = self.store.latest_market_state(symbol)
        if state and self.runtime().enabled:
            async with self._locks[symbol]:
                await self._invoke_decision(
                    state,
                    trigger="TRADE_COMPLETED",
                    trigger_event_id=event_id,
                )

    async def on_major_event(self, event_id: str) -> None:
        runtime = self.runtime()
        if not runtime.enabled:
            return
        for symbol in runtime.symbols:
            state = self.store.latest_market_state(symbol)
            if state:
                async with self._locks[symbol]:
                    await self._invoke_decision(
                        state,
                        trigger="MAJOR_MARKET_EVENT",
                        trigger_event_id=event_id,
                    )

    async def resume(self, *, evaluate_market_states: bool = True) -> None:
        if self.reconcile is not None:
            await self.reconcile()
        runtime = self._recover_fail_closed_plan_rejections(self.runtime())
        if not runtime.enabled:
            return
        self._recover_failed_entry_plans(runtime)
        self._void_legacy_entry_plans(runtime)
        blocked_symbols = await self._recover_immediate_entry_plans(runtime)
        # API/lifespan callers must finish account recovery without waiting for
        # per-symbol LLM locks. Durable market/queue workers evaluate new states.
        if not evaluate_market_states:
            return
        for symbol in runtime.symbols:
            if symbol in blocked_symbols:
                continue
            state = self.store.latest_market_state(symbol)
            if state:
                await self.on_market_state(state)

    def _recover_failed_entry_plans(self, runtime: AutomationRuntimeV2) -> None:
        """Cancel claimed plans that failed before creating orders or a position."""

        for symbol in runtime.symbols:
            for plan in self.store.list_open_plans(symbol):
                if (
                    plan.order_type is not None
                    or self.store.entry_for_plan(plan.plan_id) is not None
                ):
                    continue
                if plan.state != PlanState.TRIGGERED:
                    continue
                if self.store.position_for_plan(plan.plan_id) is not None:
                    continue
                events = self.store.list_events(aggregate_id=plan.plan_id, limit=100)
                failed = next(
                    (item for item in events if item["event_type"] == "ENTRY_EXECUTION_FAILED"),
                    None,
                )
                if failed is None:
                    continue
                recovered = self.store.transition_plan(
                    plan.plan_id,
                    expected_version=plan.version,
                    target=PlanState.CANCELLED,
                )
                self.store.append_event(
                    dedupe_key=f"failed-entry-plan-recovered:{plan.plan_id}:{recovered.version}",
                    event_type="FAILED_ENTRY_PLAN_RECOVERED",
                    aggregate_type="plan",
                    aggregate_id=plan.plan_id,
                    aggregate_version=recovered.version,
                    payload={
                        "failed_event_id": failed["event_id"],
                        "reason": "failed entry created neither orders nor a position",
                    },
                )

    async def _recover_immediate_entry_plans(self, runtime: AutomationRuntimeV2) -> set[str]:
        """Resume durable immediate plans after a process restart.

        A persisted MARKET/LIMIT plan in ACTIVE state is already an execution
        decision, not a reason to ask the LLM again.  Reuse its owning episode
        and exact plan version.  A structural refusal is surfaced as a terminal
        rejection and temporarily blocks restart-time re-entry for that symbol.
        """

        blocked_symbols: set[str] = set()
        for symbol in runtime.symbols:
            for plan in self.store.list_open_plans(symbol):
                if plan.order_type is None or plan.state != PlanState.ACTIVE:
                    continue
                if self.store.entry_for_plan(plan.plan_id) is not None:
                    continue
                position = self.store.position_for_plan(plan.plan_id)
                if position is not None and position.state not in {
                    PositionStateV2.FLAT,
                    PositionStateV2.CLOSED,
                }:
                    continue
                episode = self.store.episode_for_plan(plan.plan_id)
                if episode is None:
                    retired = self.store.transition_plan(
                        plan.plan_id,
                        expected_version=plan.version,
                        target=PlanState.CANCELLED,
                    )
                    self.store.append_event(
                        dedupe_key=f"orphaned-immediate-entry-plan:{plan.plan_id}:{retired.version}",
                        event_type="IMMEDIATE_ENTRY_PLAN_ORPHANED_ON_RESUME",
                        aggregate_type="plan",
                        aggregate_id=plan.plan_id,
                        aggregate_version=retired.version,
                        payload={"reason": "owning DecisionEpisode is missing"},
                    )
                    blocked_symbols.add(symbol)
                    continue
                try:
                    result = await self.execute(
                        plan.plan_id,
                        episode.episode_id,
                        None,
                        runtime.requested_mode,
                        plan_version=plan.version,
                    )
                except Exception as exc:
                    self._record_failure(
                        symbol,
                        operation="execution",
                        claim_id=episode.episode_id,
                        error=exc,
                    )
                    blocked_symbols.add(symbol)
                    continue
                if isinstance(result, dict) and result.get("status") == "REJECTED":
                    blocked_symbols.add(symbol)
        return blocked_symbols

    def _void_legacy_entry_plans(self, runtime: AutomationRuntimeV2) -> None:
        """Void pre-immediate-entry plans that have no durable execution state.

        Before the MARKET/LIMIT contract, a plan could remain open while its
        entry was only represented by a conditional wake rule.  Such a plan
        must not survive a restart as if it were an executable order.  Existing
        positions and legacy episodes with an order reference are retained for
        reconciliation and management; only orderless, positionless plans are
        retired with an audit event so the symbol can be sensed again.
        """

        retained_states = {
            PlanState.ACTIVE,
            PlanState.WAITING_TRIGGER,
            PlanState.ARMED,
            PlanState.TRIGGERED,
        }
        for symbol in runtime.symbols:
            for plan in self.store.list_open_plans(symbol):
                if plan.order_type is not None or plan.state not in retained_states:
                    continue
                if self.store.entry_for_plan(plan.plan_id) is not None:
                    continue
                position = self.store.position_for_plan(plan.plan_id)
                if position is not None and position.state not in {
                    PositionStateV2.FLAT,
                    PositionStateV2.CLOSED,
                }:
                    continue
                episodes = self.store.list_episodes(symbol=symbol, limit=5000)
                if any(
                    episode.plan_id == plan.plan_id and episode.order_ids for episode in episodes
                ):
                    continue

                retired = self.store.transition_plan(
                    plan.plan_id,
                    expected_version=plan.version,
                    target=PlanState.CANCELLED,
                )
                self.store.append_event(
                    dedupe_key=f"legacy-entry-plan-voided-on-resume:{plan.plan_id}:{retired.version}",
                    event_type="LEGACY_ENTRY_PLAN_VOIDED_ON_RESUME",
                    aggregate_type="plan",
                    aggregate_id=plan.plan_id,
                    aggregate_version=retired.version,
                    payload={
                        "symbol": symbol,
                        "previous_state": str(plan.state),
                        "reason": (
                            "legacy conditional entry plan had no durable entry "
                            "order or open position; voided on runtime resume"
                        ),
                    },
                )

    def _recover_fail_closed_plan_rejections(
        self,
        runtime: AutomationRuntimeV2,
    ) -> AutomationRuntimeV2:
        """Migrate v2 runtimes that once misclassified PLAN_REJECTED as HTTP failure."""

        symbol_failures = dict(runtime.symbol_failures)
        symbol_retries = dict(runtime.symbol_next_retry_at)
        symbol_errors = dict(runtime.symbol_error_codes)
        recovered_at: list[datetime] = []
        for symbol, error_code in tuple(symbol_errors.items()):
            if error_code != "decision:HTTPException":
                continue
            episodes = self.store.list_episodes(symbol=symbol, limit=1)
            if not episodes or episodes[0].status != "PLAN_REJECTED":
                continue
            rejected = episodes[0]
            if runtime.last_decision_at and rejected.created_at <= runtime.last_decision_at:
                continue
            symbol_failures.pop(symbol, None)
            symbol_retries.pop(symbol, None)
            symbol_errors.pop(symbol, None)
            recovered_at.append(rejected.created_at)
            self.store.append_event(
                dedupe_key=f"automation-plan-rejection-recovered:{rejected.episode_id}",
                event_type="AUTOMATION_PLAN_REJECTION_RECOVERED",
                aggregate_type="symbol",
                aggregate_id=symbol,
                aggregate_version=1,
                payload={
                    "episode_id": rejected.episode_id,
                    "reason": "PLAN_REJECTED is a fail-closed decision outcome",
                },
            )
        if not recovered_at:
            return runtime
        updated = runtime.model_copy(
            update={
                "status": (
                    runtime.status
                    if runtime.status == AutomationRuntimeStatus.HALTED
                    else (
                        AutomationRuntimeStatus.DEGRADED
                        if symbol_failures
                        else AutomationRuntimeStatus.RUNNING
                    )
                ),
                "consecutive_failures": max(symbol_failures.values(), default=0),
                "last_decision_at": max(
                    [value for value in [runtime.last_decision_at, *recovered_at] if value]
                ),
                "last_error_code": next(iter(symbol_errors.values()), None),
                "last_error_hash": None if not symbol_errors else runtime.last_error_hash,
                "next_retry_at": min(symbol_retries.values(), default=None),
                "symbol_failures": symbol_failures,
                "symbol_next_retry_at": symbol_retries,
                "symbol_error_codes": symbol_errors,
            }
        )
        self._save_runtime(updated)
        return self.runtime()

    async def _invoke_decision(
        self,
        state: MarketStateV2,
        *,
        trigger: DecisionTrigger,
        trigger_event_id: str | None,
    ) -> dict[str, Any] | None:
        runtime = self.runtime()
        if not runtime.enabled or state.symbol not in runtime.symbols:
            return None
        trigger_key = trigger_event_id or state.state_hash
        claim_id, claimed = self.store.append_event(
            dedupe_key=f"automation-decision:{state.symbol}:{trigger}:{trigger_key}",
            event_type="AUTOMATION_DECISION_CLAIMED",
            aggregate_type="symbol",
            aggregate_id=state.symbol,
            aggregate_version=1,
            payload={
                "trigger": trigger,
                "trigger_event_id": trigger_event_id,
                "market_state_hash": state.state_hash,
                "mode": str(runtime.requested_mode),
            },
        )
        if not claimed:
            return None
        try:
            result = await self.decide(
                state.symbol,
                trigger,
                trigger_event_id,
                runtime.requested_mode,
            )
        except Exception as exc:
            self._record_failure(
                state.symbol,
                operation="decision",
                claim_id=claim_id,
                error=exc,
            )
            return None
        if result.get("decision_error"):
            if self.runtime().enabled:
                self._record_failure(
                    state.symbol,
                    operation="decision",
                    claim_id=claim_id,
                    error=DecisionContractError("decision failed contract or state validation"),
                )
            return result
        current = self.runtime()
        symbol_failures = dict(current.symbol_failures)
        symbol_retries = dict(current.symbol_next_retry_at)
        symbol_errors = dict(current.symbol_error_codes)
        symbol_failures.pop(state.symbol, None)
        symbol_retries.pop(state.symbol, None)
        symbol_errors.pop(state.symbol, None)
        self._save_runtime(
            current.model_copy(
                update={
                    "status": (
                        AutomationRuntimeStatus.DEGRADED
                        if symbol_failures
                        else AutomationRuntimeStatus.RUNNING
                    ),
                    "consecutive_failures": max(symbol_failures.values(), default=0),
                    "last_decision_at": datetime.now(UTC),
                    "last_error_code": next(iter(symbol_errors.values()), None),
                    "last_error_hash": None if not symbol_errors else current.last_error_hash,
                    "next_retry_at": min(symbol_retries.values(), default=None),
                    "symbol_failures": symbol_failures,
                    "symbol_next_retry_at": symbol_retries,
                    "symbol_error_codes": symbol_errors,
                }
            )
        )
        if (
            not result.get("rejected")
            and self.store.list_positions(symbol=state.symbol)
            and not self.store.list_wait_runtimes(symbol=state.symbol, active_only=True)
        ):
            self._schedule_fail_closed_wait(
                state=state,
                result=result,
                trigger_event_id=claim_id,
                delay=timedelta(minutes=30),
                reason="[POSITION_REVIEW] re-evaluate an open position without a future wake",
            )
        return result

    def _schedule_bounded_retry(
        self,
        source: WaitRuntimeV2,
        state: MarketStateV2,
        trigger_event_id: str,
    ) -> None:
        retry_at = max(datetime.now(UTC), state.data_as_of) + timedelta(minutes=5)
        runtime = WaitRuntimeV2(
            episode_id=source.episode_id,
            symbol=source.symbol,
            wait_plan=WaitPlan(
                thesis_id=source.wait_plan.thesis_id,
                reason="[BOUNDED_RETRY] one schema/logic retry after a rejected wake decision",
                wake_conditions=[
                    WakeCondition(
                        condition_id=f"bounded-retry-{trigger_event_id}",
                        kind=WakeKind.EXPIRES_AT,
                        action=WakeAction.RECALL_LLM,
                        value=retry_at,
                    )
                ],
                expires_at=retry_at,
            ),
        )
        self.store.save_wait_runtime(runtime)
        self.store.append_event(
            dedupe_key=f"bounded-retry-scheduled:{trigger_event_id}",
            event_type="BOUNDED_RETRY_SCHEDULED",
            aggregate_type="wait_runtime",
            aggregate_id=runtime.wait_runtime_id,
            aggregate_version=runtime.version,
            payload={
                "source_wait_runtime_id": source.wait_runtime_id,
                "retry_at": retry_at.isoformat(),
                "thesis_id": source.wait_plan.thesis_id,
            },
        )

    def _schedule_fail_closed_wait(
        self,
        *,
        state: MarketStateV2,
        result: dict[str, Any],
        trigger_event_id: str,
        delay: timedelta,
        reason: str,
    ) -> None:
        episode = result.get("episode")
        episode_id = (
            getattr(episode, "episode_id", None)
            or (episode.get("episode_id") if isinstance(episode, dict) else None)
            or f"rejected-{trigger_event_id}"
        )
        retry_at = max(datetime.now(UTC), state.data_as_of) + delay
        runtime = WaitRuntimeV2(
            episode_id=episode_id,
            symbol=state.symbol,
            wait_plan=WaitPlan(
                reason=reason,
                wake_conditions=[
                    WakeCondition(
                        condition_id=f"fail-closed-retry-{trigger_event_id}",
                        kind=WakeKind.EXPIRES_AT,
                        action=WakeAction.RECALL_LLM,
                        value=retry_at,
                    )
                ],
                expires_at=retry_at,
            ),
        )
        self.store.save_wait_runtime(runtime)
        self.store.append_event(
            dedupe_key=f"fail-closed-wait-scheduled:{trigger_event_id}:{int(delay.total_seconds())}",
            event_type="FAIL_CLOSED_WAIT_SCHEDULED",
            aggregate_type="wait_runtime",
            aggregate_id=runtime.wait_runtime_id,
            aggregate_version=runtime.version,
            payload={
                "retry_at": retry_at.isoformat(),
                "delay_seconds": int(delay.total_seconds()),
                "reason": reason,
            },
        )

    def _cancel_active_thesis(self, thesis_id: str, trigger_event_id: str) -> None:
        thesis = self.store.get_thesis(thesis_id)
        if not thesis or thesis.state != ThesisState.ACTIVE:
            return
        cancelled = self.store.transition_thesis(
            thesis_id,
            expected_version=thesis.version,
            target=ThesisState.CANCELLED,
        )
        no_trade_supersession = trigger_event_id == "active-no-trade-wait"
        self.store.append_event(
            dedupe_key=(
                f"thesis-cancelled-no-trade:{thesis_id}:{cancelled.version}"
                if no_trade_supersession
                else f"thesis-cancelled-after-retry:{thesis_id}:{cancelled.version}"
            ),
            event_type=(
                "THESIS_CANCELLED_BY_NO_TRADE"
                if no_trade_supersession
                else "THESIS_CANCELLED_FAIL_CLOSED"
            ),
            aggregate_type="thesis",
            aggregate_id=thesis_id,
            aggregate_version=cancelled.version,
            payload={
                "trigger_event_id": (None if no_trade_supersession else trigger_event_id),
                "reason": (
                    "active NO_TRADE wait supersedes the old thesis"
                    if no_trade_supersession
                    else "bounded decision retry was rejected"
                ),
            },
        )

    async def _expire_lifecycle(self, state: MarketStateV2) -> None:
        for plan in self.store.list_open_plans(state.symbol):
            if state.data_as_of <= plan.expires_at or plan.state == PlanState.TRIGGERED:
                continue
            updated_plan = self.store.transition_plan(
                plan.plan_id,
                expected_version=plan.version,
                target=PlanState.EXPIRED,
            )
            event_id, created = self.store.append_event(
                dedupe_key=f"plan-expired:{updated_plan.plan_id}:{updated_plan.version}",
                event_type="PLAN_EXPIRED",
                aggregate_type="plan",
                aggregate_id=updated_plan.plan_id,
                aggregate_version=updated_plan.version,
                payload={"market_state_hash": state.state_hash},
            )
            if created and not self.store.list_positions(symbol=state.symbol):
                await self._invoke_decision(
                    state,
                    trigger="PLAN_EXPIRED",
                    trigger_event_id=event_id,
                )
        for thesis in self.store.list_active_theses(state.symbol):
            effective_expiry = min(
                thesis.expires_at,
                thesis.created_at + MAX_ACTIVE_THESIS_AGE,
            )
            if state.data_as_of <= effective_expiry:
                continue
            updated = self.store.transition_thesis(
                thesis.thesis_id,
                expected_version=thesis.version,
                target=ThesisState.EXPIRED,
            )
            self.store.cancel_active_waits(state.symbol)
            cancelled_plans = self.store.cancel_open_plans(state.symbol)
            event_id, created = self.store.append_event(
                dedupe_key=f"thesis-expired:{updated.thesis_id}:{updated.version}",
                event_type="THESIS_EXPIRED",
                aggregate_type="thesis",
                aggregate_id=updated.thesis_id,
                aggregate_version=updated.version,
                payload={
                    "market_state_hash": state.state_hash,
                    "declared_expires_at": thesis.expires_at.isoformat(),
                    "effective_expires_at": effective_expiry.isoformat(),
                    "maximum_active_age_hours": 4,
                    "cancelled_plan_ids": [item.plan_id for item in cancelled_plans],
                },
            )
            if created:
                await self._invoke_decision(
                    state,
                    trigger="THESIS_INVALIDATED",
                    trigger_event_id=event_id,
                )

    def _record_failure(
        self,
        symbol: str,
        *,
        operation: str,
        claim_id: str,
        error: Exception,
    ) -> None:
        current = self.runtime()
        symbol_failures = dict(current.symbol_failures)
        failures = symbol_failures.get(symbol, 0) + 1
        symbol_failures[symbol] = failures
        delay_seconds = min(30 * 2 ** min(failures - 1, 5), 900)
        error_hash = sha256(str(error).encode()).hexdigest()
        self.store.append_event(
            dedupe_key=f"automation-failure:{claim_id}",
            event_type="AUTOMATION_OPERATION_FAILED",
            aggregate_type="symbol",
            aggregate_id=symbol,
            aggregate_version=1,
            payload={
                "operation": operation,
                "error_type": type(error).__name__,
                "error_hash": error_hash,
                "retry_seconds": delay_seconds,
            },
        )
        self._save_runtime(
            current.model_copy(
                update={
                    "status": (AutomationRuntimeStatus.DEGRADED),
                    "consecutive_failures": max(symbol_failures.values()),
                    "last_error_code": f"{operation}:{type(error).__name__}",
                    "last_error_hash": error_hash,
                    "next_retry_at": datetime.now(UTC) + timedelta(seconds=delay_seconds),
                    "symbol_failures": symbol_failures,
                    "symbol_next_retry_at": {
                        **current.symbol_next_retry_at,
                        symbol: datetime.now(UTC) + timedelta(seconds=delay_seconds),
                    },
                    "symbol_error_codes": {
                        **current.symbol_error_codes,
                        symbol: f"{operation}:{type(error).__name__}",
                    },
                }
            )
        )

    def _mark_cycle_success(self) -> None:
        current = self.runtime()
        self._save_runtime(
            current.model_copy(
                update={
                    "status": (
                        AutomationRuntimeStatus.RUNNING
                        if current.enabled
                        and current.consecutive_failures == 0
                        and current.status != AutomationRuntimeStatus.HALTED
                        else current.status
                    ),
                    "last_cycle_at": datetime.now(UTC),
                }
            )
        )

    def _save_runtime(self, runtime: AutomationRuntimeV2) -> None:
        current = self.runtime()
        if current.status == AutomationRuntimeStatus.HALTED:
            runtime = runtime.model_copy(update={"status": AutomationRuntimeStatus.HALTED})
        self.store.save_automation_runtime(
            runtime.model_copy(update={"updated_at": datetime.now(UTC)}),
            expected_updated_at=runtime.updated_at,
        )
