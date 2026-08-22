from __future__ import annotations

from decimal import Decimal, InvalidOperation
from hashlib import sha256

from pydantic import BaseModel, Field, computed_field

from .schemas import AgentAutonomyMode, AgentToolCall, ToolRiskClass


class AgentPolicy(BaseModel):
    id: str
    autonomy_mode: AgentAutonomyMode = AgentAutonomyMode.OBSERVE
    allowed_symbols: list[str] = Field(default_factory=list)
    allowed_timeframes: list[str] = Field(default_factory=list)
    max_risk_per_trade: Decimal = Field(default=Decimal("0"), ge=0, le=Decimal("0.05"))
    max_daily_drawdown: Decimal = Field(default=Decimal("0"), ge=0, le=Decimal("0.20"))
    max_open_positions: int = Field(default=0, ge=0, le=100)
    max_trades_per_day: int = Field(default=0, ge=0, le=1000)
    max_leverage: Decimal = Field(default=Decimal("1"), ge=1, le=Decimal("100"))
    require_native_protection: bool = True
    allow_agent_entry: bool = False
    allow_agent_cancel_entry: bool = True
    allow_agent_reduce: bool = True
    allow_agent_exit: bool = True
    allow_agent_halt: bool = True
    allow_agent_resume: bool = False
    allow_agent_change_policy: bool = False

    @computed_field
    @property
    def fingerprint(self) -> str:
        payload = self.model_dump_json(exclude_computed_fields=True)
        return sha256(payload.encode()).hexdigest()[:16]


class PolicyDecision(BaseModel):
    allowed: bool
    reasons: list[str] = Field(default_factory=list)


class AgentPolicyEngine:
    """Non-LLM authorization boundary for every Agent tool invocation."""

    def evaluate(
        self,
        policy: AgentPolicy,
        call: AgentToolCall,
        risk_class: ToolRiskClass,
    ) -> PolicyDecision:
        reasons: list[str] = []
        symbol = str(call.arguments.get("symbol") or "").upper().replace("-", "/")
        timeframe = str(call.arguments.get("timeframe") or "").lower()
        if symbol and policy.allowed_symbols and symbol not in policy.allowed_symbols:
            reasons.append(f"symbol {symbol} is outside the Agent grant")
        if timeframe and policy.allowed_timeframes and timeframe not in policy.allowed_timeframes:
            reasons.append(f"timeframe {timeframe} is outside the Agent grant")

        if (
            risk_class == ToolRiskClass.RESEARCH
            and policy.autonomy_mode == AgentAutonomyMode.OBSERVE
        ):
            reasons.append("observe mode cannot create research artifacts")
        elif risk_class == ToolRiskClass.INCREASE_RISK:
            if call.name not in {
                "execute_validated_trade_plan",
            }:
                reasons.append(f"unknown risk-increasing tool {call.name} is denied by default")
            if policy.autonomy_mode not in {
                AgentAutonomyMode.PAPER,
                AgentAutonomyMode.SANDBOX,
                AgentAutonomyMode.LIVE_CAPPED,
            }:
                reasons.append("current autonomy mode cannot increase account risk")
            if not policy.allow_agent_entry:
                reasons.append("Agent entry permission is disabled")
            self._check_requested_risk(policy, call, reasons)
        elif risk_class == ToolRiskClass.DECREASE_RISK:
            self._check_decrease_permission(policy, call, reasons)
        elif risk_class == ToolRiskClass.CONTROL:
            permissions = {
                "halt_trading": policy.allow_agent_halt,
                "resume_trading": policy.allow_agent_resume,
                "change_agent_policy": policy.allow_agent_change_policy,
            }
            if call.name not in permissions:
                reasons.append(f"unknown control tool {call.name} is denied by default")
            elif not permissions[call.name]:
                reasons.append(f"Agent permission for {call.name} is disabled")
        return PolicyDecision(allowed=not reasons, reasons=reasons)

    @staticmethod
    def _decimal_argument(call: AgentToolCall, name: str) -> Decimal | None:
        if name not in call.arguments or call.arguments[name] is None:
            return None
        try:
            return Decimal(str(call.arguments[name]))
        except InvalidOperation:
            return Decimal("Infinity")

    def _check_requested_risk(
        self, policy: AgentPolicy, call: AgentToolCall, reasons: list[str]
    ) -> None:
        requested_risk = self._decimal_argument(call, "risk_fraction")
        if requested_risk is not None and requested_risk > policy.max_risk_per_trade:
            reasons.append(
                f"requested risk {requested_risk} exceeds cap {policy.max_risk_per_trade}"
            )
        leverage = self._decimal_argument(call, "leverage")
        if leverage is not None and leverage > policy.max_leverage:
            reasons.append(f"requested leverage {leverage} exceeds cap {policy.max_leverage}")
        if policy.require_native_protection and call.arguments.get("protected") is not True:
            reasons.append("risk-increasing order must declare native protection")

    @staticmethod
    def _check_decrease_permission(
        policy: AgentPolicy, call: AgentToolCall, reasons: list[str]
    ) -> None:
        permissions = {
            "cancel_entry_order": policy.allow_agent_cancel_entry,
            "reduce_position": policy.allow_agent_reduce,
            "exit_position": policy.allow_agent_exit,
        }
        if call.name not in permissions:
            reasons.append(f"unknown risk-decreasing tool {call.name} is denied by default")
        elif not permissions[call.name]:
            reasons.append(f"Agent permission for {call.name} is disabled")
