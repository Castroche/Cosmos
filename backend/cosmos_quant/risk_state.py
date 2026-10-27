from __future__ import annotations

from datetime import UTC, date, datetime

from pydantic import BaseModel, Field

from .storage import SqliteStore


class RiskControlState(BaseModel):
    halted: bool = False
    reason: str = ""
    source: str = ""
    halted_at: datetime | None = None
    session_date: date = Field(default_factory=lambda: datetime.now(UTC).date())


class RiskStateService:
    """Durable fail-closed trading switch shared by API and execution workers."""

    _LEGACY_STRATEGY_GOVERNANCE_HALTS = {
        (
            "No cross-market robust Champion; block all new risk until strategy "
            "certification is enforced"
        ),
        "没有跨市场稳健 Champion；在策略认证完成前禁止所有新增风险",
    }

    _SOURCE_PRIORITY = {
        "": 0,
        "maintenance": 10,
        "reconciliation_transport": 20,
        "reconciliation": 20,
        "daily_drawdown": 30,
        "agent_daily_drawdown": 30,
        "protection_reconciliation": 50,
        "protection_rebalance": 60,
        "reconciliation_integrity": 70,
        "execution_uncertain": 80,
        "manual": 100,
    }

    def __init__(self, store: SqliteStore):
        self.store = store
        # In-flight commands cannot survive a stop/resume cycle in this process.
        self.entry_generation = 0

    def invalidate_entries(self) -> None:
        self.entry_generation += 1

    def get(self) -> RiskControlState:
        payload = self.store.load_risk_state()
        state = RiskControlState.model_validate_json(payload) if payload else RiskControlState()
        today = datetime.now(UTC).date()
        if (
            state.halted
            and state.source in {"daily_drawdown", "agent_daily_drawdown"}
            and state.session_date < today
        ):
            state = RiskControlState(session_date=today)
            self._save(state)
        return state

    def halt(self, reason: str, source: str = "manual") -> RiskControlState:
        self.invalidate_entries()
        current = self.get()
        current_priority = self._SOURCE_PRIORITY.get(current.source, 70)
        requested_priority = self._SOURCE_PRIORITY.get(source, 70)
        if current.halted and current_priority >= requested_priority:
            # A lower/equal-priority subsystem may add diagnostics elsewhere,
            # but it cannot downgrade a stronger latched stop into a condition
            # that its own automatic recovery path is allowed to clear.
            return current
        state = RiskControlState(
            halted=True,
            reason=reason.strip() or "未提供停机原因",
            source=source,
            halted_at=current.halted_at or datetime.now(UTC),
            session_date=datetime.now(UTC).date(),
        )
        self._save(state)
        return state

    def resume(self, confirmation: str) -> RiskControlState:
        if confirmation != "RESUME TRADING":
            raise ValueError("恢复交易确认短语必须为 RESUME TRADING")
        self.invalidate_entries()
        state = RiskControlState()
        self._save(state)
        return state

    def recover_reconciliation_transport(self) -> RiskControlState:
        """Clear only a recovered reconciliation transport halt.

        Integrity halts (unknown orders, uncertain states, or missing protection)
        remain latched and still require an operator confirmation.
        """

        current = self.get()
        legacy_transport_halt = current.source == "reconciliation" and current.reason.startswith(
            ("订单对账失败", "订单对账连续失败")
        )
        if not current.halted or not (
            current.source == "reconciliation_transport" or legacy_transport_halt
        ):
            return current
        state = RiskControlState()
        self._save(state)
        return state

    def recover_demo_reconciliation_integrity(self) -> RiskControlState:
        """Clear a Demo-only integrity latch after a fully clean venue report.

        The RuntimeManager calls this only in SANDBOX and only after uncertain,
        unknown-position, unknown-order and unprotected-order sets are all empty.
        Live integrity incidents remain operator-latched.
        """

        current = self.get()
        if not current.halted or current.source != "reconciliation_integrity":
            return current
        state = RiskControlState()
        self._save(state)
        return state

    def recover_protection_reconciliation(self) -> RiskControlState:
        """Clear only a protection-read halt after exact protection revalidation.

        The caller must first reconstruct attributable fills, match the current
        exchange position, and verify that native protection exactly covers the
        remaining quantity. Submission, reduction, manual, drawdown and generic
        integrity halts remain latched.
        """

        current = self.get()
        if not current.halted or current.source != "protection_reconciliation":
            return current
        state = RiskControlState()
        self._save(state)
        return state

    def recover_maintenance(self) -> RiskControlState:
        """Clear only a maintenance halt after storage maintenance succeeds."""

        current = self.get()
        if not current.halted or current.source != "maintenance":
            return current
        state = RiskControlState()
        self._save(state)
        return state

    def recover_legacy_strategy_governance_halt(self) -> RiskControlState:
        """Clear the obsolete global halt that represented missing strategy evidence.

        A missing Champion is a strategy-level admission failure: the candidate must
        remain WAIT and cannot reach the risk-increasing execution tool.  It is not an
        operational incident and must not latch the account-wide emergency stop.
        Only the exact historical manual reason is migrated; operator halts, drawdown,
        reconciliation, uncertain execution, and protection failures remain latched.
        """

        current = self.get()
        if not (
            current.halted
            and current.source == "manual"
            and current.reason in self._LEGACY_STRATEGY_GOVERNANCE_HALTS
        ):
            return current
        state = RiskControlState()
        self._save(state)
        return state

    def _save(self, state: RiskControlState) -> None:
        self.store.save_risk_state(state.model_dump_json())
