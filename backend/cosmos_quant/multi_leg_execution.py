from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .domain import Order, OrderIntent, OrderSide, OrderStatus, OrderType
from .exchanges.base import ExchangeAdapter
from .risk_state import RiskStateService
from .storage import SqliteStore


class MultiLegExecutionStatus(StrEnum):
    RESERVED = "reserved"
    SUBMITTING = "submitting"
    RECONCILING = "reconciling"
    HEDGED = "hedged"
    COMPENSATING = "compensating"
    COMPENSATED = "compensated"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


TERMINAL_MULTI_LEG_STATUSES = {
    MultiLegExecutionStatus.HEDGED,
    MultiLegExecutionStatus.COMPENSATED,
    MultiLegExecutionStatus.CLOSED,
    MultiLegExecutionStatus.FAILED,
    MultiLegExecutionStatus.UNCERTAIN,
}


class MultiLegOrderSpec(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    adapter_key: str = Field(min_length=1, max_length=64)
    exchange: str = Field(min_length=1, max_length=32)
    market_type: Literal["spot", "swap", "future"]
    symbol: str = Field(min_length=3, max_length=64)
    side: OrderSide
    quantity: Decimal = Field(gt=0)
    order_type: OrderType = OrderType.MARKET
    limit_price: Decimal | None = Field(default=None, gt=0)
    compensation_reduce_only: bool
    role: Literal["pair_a", "pair_b", "spot", "perpetual"] | None = None

    @model_validator(mode="after")
    def limit_order_has_price(self):
        if self.order_type == OrderType.LIMIT and self.limit_price is None:
            raise ValueError("multi-leg limit order requires a limit price")
        return self


class MultiLegExecutionRequest(BaseModel):
    id: str = Field(min_length=8, max_length=96)
    strategy_kind: Literal["pair", "carry"]
    campaign_id: str = Field(min_length=8, max_length=96)
    strategy_fingerprint: str = Field(min_length=8, max_length=128)
    campaign_certificate_fingerprint: str = Field(min_length=16, max_length=128)
    blind_evidence_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_purpose: Literal["strategy", "commissioning"] = "strategy"
    trading_mode: Literal["paper", "sandbox", "live"] = "paper"
    timeframe: str = Field(default="1h", pattern=r"^[1-9][0-9]*[mhdw]$")
    legs: list[MultiLegOrderSpec] = Field(min_length=2, max_length=4)
    minimum_fill_fraction: Decimal = Field(default=Decimal("0.999"), gt=0, le=1)
    maximum_hedge_error_fraction: Decimal = Field(
        default=Decimal("0.001"),
        ge=0,
        le=Decimal("0.10"),
    )

    @model_validator(mode="after")
    def legs_are_unambiguous(self):
        if len({item.id for item in self.legs}) != len(self.legs):
            raise ValueError("multi-leg request contains duplicate leg ids")
        if len({(item.adapter_key, item.symbol, item.side) for item in self.legs}) != len(
            self.legs
        ):
            raise ValueError("multi-leg request contains duplicate execution legs")
        key_scopes: dict[str, tuple[str, str]] = {}
        scope_keys: dict[tuple[str, str], str] = {}
        for item in self.legs:
            scope = (item.exchange, item.market_type)
            if item.adapter_key in key_scopes and key_scopes[item.adapter_key] != scope:
                raise ValueError("one multi-leg adapter key cannot span venue scopes")
            if scope in scope_keys and scope_keys[scope] != item.adapter_key:
                raise ValueError("one venue scope cannot be split across adapter keys")
            key_scopes[item.adapter_key] = scope
            scope_keys[scope] = item.adapter_key
        return self

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(encoded).hexdigest()


class MultiLegState(BaseModel):
    spec: MultiLegOrderSpec
    entry_intent_key: str
    compensation_intent_key: str
    entry_order: Order | None = None
    compensation_order: Order | None = None
    entry_error: str = ""
    compensation_error: str = ""

    @property
    def fill_fraction(self) -> Decimal:
        if self.entry_order is None or self.spec.quantity <= 0:
            return Decimal("0")
        return min(self.entry_order.filled_quantity / self.spec.quantity, Decimal("1"))


class MultiLegExecutionRun(BaseModel):
    id: str
    request: MultiLegExecutionRequest
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: MultiLegExecutionStatus
    legs: list[MultiLegState]
    hedge_error_fraction: Decimal | None = Field(default=None, ge=0)
    reason: str = ""
    exit_monitor: MultiLegExitMonitorState | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MultiLegExitMonitorState(BaseModel):
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    market_data_at: datetime | None = None
    held_bars: int = Field(default=0, ge=0)
    signal_name: str = ""
    signal_value: Decimal | None = None
    hedge_ratio: Decimal | None = None
    should_close: bool = False
    reason: str = ""
    consecutive_errors: int = Field(default=0, ge=0)


def evaluate_pair_exit(
    *,
    spec_payload: dict,
    pair_a_side: OrderSide,
    zscore: Decimal,
    held_bars: int,
) -> tuple[bool, str]:
    exit_zscore = Decimal(str(spec_payload["exit_zscore"]))
    stop_zscore = Decimal(str(spec_payload["stop_zscore"]))
    maximum_holding_bars = int(spec_payload["maximum_holding_bars"])
    if str(spec_payload["model"]) == "relative_momentum_equal_dollar":
        direction_a = Decimal("1") if pair_a_side == OrderSide.BUY else Decimal("-1")
        directional_score = zscore * direction_a
        if directional_score <= -stop_zscore:
            return True, "relative_momentum_stop"
        if directional_score <= exit_zscore:
            return True, "relative_momentum_decay"
    else:
        if abs(zscore) >= stop_zscore:
            return True, "spread_stop"
        if abs(zscore) <= exit_zscore:
            return True, "mean_reversion"
    if held_bars >= maximum_holding_bars:
        return True, "maximum_holding"
    return False, "hold"


def evaluate_carry_exit(
    *,
    spec_payload: dict,
    basis_rate: Decimal,
    funding_rate: Decimal | None,
    held_bars: int,
) -> tuple[bool, str]:
    exit_basis_rate = Decimal(str(spec_payload["exit_basis_rate"]))
    stop_basis_rate = Decimal(str(spec_payload["stop_basis_rate"]))
    funding_exit_rate = Decimal(str(spec_payload["funding_exit_rate"]))
    minimum_holding_bars = int(spec_payload["minimum_holding_bars"])
    maximum_holding_bars = int(spec_payload["maximum_holding_bars"])
    if basis_rate >= stop_basis_rate:
        return True, "basis_stop"
    if held_bars >= maximum_holding_bars:
        return True, "maximum_holding"
    if held_bars >= minimum_holding_bars:
        if basis_rate <= exit_basis_rate:
            return True, "basis_convergence"
        if funding_rate is not None and funding_rate <= funding_exit_rate:
            return True, "funding_reversal"
    return False, "hold"


class MultiLegExecutionCertificate(BaseModel):
    version: int = 2
    strategy_kind: Literal["pair", "carry"]
    campaign_id: str
    campaign_engine_version: int = Field(ge=1)
    strategy_id: str
    strategy_fingerprint: str
    research_lineage_id: str
    blind_set_id: str
    blind_consumption_id: str
    blind_evidence_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    development_evidence_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    strategy_spec_payload: dict = Field(default_factory=dict)
    allowed_execution_groups: list[list[str]] = Field(min_length=1)
    allowed_timeframes: list[str] = Field(default_factory=lambda: ["1h"], min_length=1)
    issued_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class MultiLegCommissioningResult(BaseModel):
    """Credential-bound proof that one venue can complete the whole protocol.

    Commissioning is deliberately separate from research certification.  It
    proves execution mechanics in an exchange sandbox; it never makes a
    strategy profitable or eligible for live trading by itself.
    """

    version: int = 1
    exchange: str = Field(min_length=1, max_length=32)
    trading_mode: Literal["sandbox"] = "sandbox"
    credential_fingerprint: str = Field(min_length=16, max_length=128)
    strategy_kind: Literal["pair", "carry"]
    execution_scopes: list[str] = Field(min_length=2, max_length=4)
    run_id: str = Field(min_length=8, max_length=96)
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    entry_status: MultiLegExecutionStatus
    close_status: MultiLegExecutionStatus
    starting_exposure: dict[str, Decimal]
    ending_exposure: dict[str, Decimal]
    no_preexisting_scope_orders: bool
    no_remaining_scope_orders: bool
    order_ledger_recorded: bool
    passed: bool
    message: str
    tested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


def _certificate_fingerprint(payload: dict) -> str:
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def build_multi_leg_execution_certificate(
    *,
    strategy_kind: Literal["pair", "carry"],
    campaign_id: str,
    campaign_engine_version: int,
    strategy_id: str,
    strategy_fingerprint: str,
    research_lineage_id: str,
    blind_set_id: str | None,
    blind_consumption_id: str | None,
    blind_evidence_fingerprint: str | None,
    development_evidence: dict,
    allowed_execution_groups: list[list[str]],
    strategy_spec_payload: dict | None = None,
    allowed_timeframes: list[str] | None = None,
    development_passed: bool,
    pbo_passed: bool,
    independent_blind_test_completed: bool,
    blind_selected_candidate_passed: bool,
) -> MultiLegExecutionCertificate:
    if not all(
        (
            development_passed,
            pbo_passed,
            independent_blind_test_completed,
            blind_selected_candidate_passed,
        )
    ):
        raise ValueError("multi-leg certificate requires development, PBO, and blind PASS")
    if not blind_set_id or not blind_consumption_id or not blind_evidence_fingerprint:
        raise ValueError("multi-leg certificate requires complete sealed-blind provenance")
    normalized_groups = sorted(
        {
            tuple(sorted(group))
            for group in allowed_execution_groups
            if len(group) == 2 and len(set(group)) == 2
        }
    )
    if not normalized_groups:
        raise ValueError("multi-leg certificate requires at least one exact two-leg scope")
    development_fingerprint = _certificate_fingerprint(development_evidence)
    issued_at = datetime.now(UTC)
    normalized_timeframes = sorted(set(allowed_timeframes or ["1h"]))
    payload = {
        "version": 2,
        "strategy_kind": strategy_kind,
        "campaign_id": campaign_id,
        "campaign_engine_version": campaign_engine_version,
        "strategy_id": strategy_id,
        "strategy_fingerprint": strategy_fingerprint,
        "research_lineage_id": research_lineage_id,
        "blind_set_id": blind_set_id,
        "blind_consumption_id": blind_consumption_id,
        "blind_evidence_fingerprint": blind_evidence_fingerprint,
        "development_evidence_fingerprint": development_fingerprint,
        "strategy_spec_payload": strategy_spec_payload or {},
        "allowed_execution_groups": [list(group) for group in normalized_groups],
        "allowed_timeframes": normalized_timeframes,
        "issued_at": issued_at.isoformat().replace("+00:00", "Z"),
    }
    return MultiLegExecutionCertificate(
        **payload,
        fingerprint=_certificate_fingerprint(payload),
    )


def validate_multi_leg_execution_certificate(
    certificate: MultiLegExecutionCertificate,
    *,
    request: MultiLegExecutionRequest | None = None,
) -> None:
    excluded = {"fingerprint"}
    if certificate.version == 1:
        excluded.update({"strategy_spec_payload", "allowed_timeframes"})
    payload = certificate.model_dump(mode="json", exclude=excluded)
    if (
        certificate.version not in {1, 2}
        or certificate.fingerprint != _certificate_fingerprint(payload)
    ):
        raise ValueError("multi-leg execution certificate fingerprint mismatch")
    if request is None:
        return
    if request.execution_purpose != "strategy":
        raise ValueError("research certificate cannot authorize commissioning orders")
    if (
        request.strategy_kind != certificate.strategy_kind
        or request.campaign_id != certificate.campaign_id
        or request.strategy_fingerprint != certificate.strategy_fingerprint
        or request.campaign_certificate_fingerprint != certificate.fingerprint
        or request.blind_evidence_fingerprint
        != certificate.blind_evidence_fingerprint
    ):
        raise ValueError("multi-leg request is outside its certificate identity")
    request_scope = sorted(
        f"{leg.exchange}:{leg.market_type}:{leg.symbol}" for leg in request.legs
    )
    if request_scope not in certificate.allowed_execution_groups:
        raise ValueError("multi-leg request is outside the certificate market scope")
    if certificate.version >= 2 and request.timeframe not in certificate.allowed_timeframes:
        raise ValueError("multi-leg request timeframe is outside the certificate scope")


def build_multi_leg_commissioning_result(
    *,
    exchange: str,
    credential_fingerprint: str,
    strategy_kind: Literal["pair", "carry"],
    run: MultiLegExecutionRun,
    closed_run: MultiLegExecutionRun,
    starting_exposure: dict[str, Decimal],
    ending_exposure: dict[str, Decimal],
    no_preexisting_scope_orders: bool,
    no_remaining_scope_orders: bool,
    order_ledger_recorded: bool,
    message: str,
) -> MultiLegCommissioningResult:
    if run.request.execution_purpose != "commissioning":
        raise ValueError("commissioning evidence requires a commissioning execution request")
    scopes = sorted(
        f"{leg.exchange}:{leg.market_type}:{leg.symbol}" for leg in run.request.legs
    )
    exposure_flat = starting_exposure == ending_exposure
    passed = bool(
        run.status == MultiLegExecutionStatus.HEDGED
        and closed_run.status == MultiLegExecutionStatus.CLOSED
        and no_preexisting_scope_orders
        and no_remaining_scope_orders
        and order_ledger_recorded
        and exposure_flat
    )
    tested_at = datetime.now(UTC)
    payload = {
        "version": 1,
        "exchange": exchange,
        "trading_mode": "sandbox",
        "credential_fingerprint": credential_fingerprint,
        "strategy_kind": strategy_kind,
        "execution_scopes": scopes,
        "run_id": run.id,
        "request_fingerprint": run.request_fingerprint,
        "entry_status": str(run.status),
        "close_status": str(closed_run.status),
        "starting_exposure": {
            key: str(starting_exposure[key]) for key in sorted(starting_exposure)
        },
        "ending_exposure": {
            key: str(ending_exposure[key]) for key in sorted(ending_exposure)
        },
        "no_preexisting_scope_orders": no_preexisting_scope_orders,
        "no_remaining_scope_orders": no_remaining_scope_orders,
        "order_ledger_recorded": order_ledger_recorded,
        "passed": passed,
        "message": message,
        "tested_at": tested_at.isoformat().replace("+00:00", "Z"),
    }
    return MultiLegCommissioningResult(
        **payload,
        fingerprint=_certificate_fingerprint(payload),
    )


def validate_multi_leg_commissioning_result(
    result: MultiLegCommissioningResult,
    *,
    exchange: str,
    credential_fingerprint: str,
    strategy_kind: Literal["pair", "carry"],
    execution_scopes: list[str],
) -> None:
    payload = result.model_dump(mode="json", exclude={"fingerprint"})
    expected_scopes = sorted(execution_scopes)
    if result.version != 1 or result.fingerprint != _certificate_fingerprint(payload):
        raise ValueError("multi-leg commissioning fingerprint mismatch")
    if not result.passed:
        raise ValueError("multi-leg sandbox commissioning has not passed")
    if (
        result.exchange != exchange
        or result.trading_mode != "sandbox"
        or result.credential_fingerprint != credential_fingerprint
        or result.strategy_kind != strategy_kind
        or result.execution_scopes != expected_scopes
    ):
        raise ValueError("multi-leg commissioning does not match active credentials and scopes")


class MultiLegExecutionCoordinator:
    """Durable fail-closed coordinator for pair and carry entry legs.

    The exchange cannot provide a truly atomic transaction across two orders.
    This protocol therefore submits deterministic idempotent intents together,
    reconciles every acknowledged order, and flattens all known fills whenever
    the requested hedge was not completed. Ambiguous acknowledgements halt risk
    instead of guessing whether a leg landed.
    """

    def __init__(
        self,
        store: SqliteStore,
        adapters: dict[str, ExchangeAdapter],
        *,
        risk_state: RiskStateService | None = None,
        reconciliation_attempts: int = 3,
        reconciliation_delay_seconds: float = 0.05,
        order_sink: Callable[[str, Order], None] | None = None,
    ):
        self.store = store
        self.adapters = adapters
        self.risk_state = risk_state
        self.reconciliation_attempts = max(reconciliation_attempts, 1)
        self.reconciliation_delay_seconds = max(reconciliation_delay_seconds, 0)
        self.order_sink = order_sink

    async def execute(
        self,
        request: MultiLegExecutionRequest,
        *,
        validate_certificate: Callable[[MultiLegExecutionRequest], None],
    ) -> MultiLegExecutionRun:
        validate_certificate(request)
        payload = self.store.load_multi_leg_execution_run(request.id)
        if payload:
            run = MultiLegExecutionRun.model_validate(payload)
            if run.request_fingerprint != request.fingerprint:
                raise RuntimeError("multi-leg execution id is bound to another request")
            if run.status in TERMINAL_MULTI_LEG_STATUSES:
                return run
            if run.status == MultiLegExecutionStatus.CLOSING:
                raise RuntimeError("multi-leg hedge is closing; resume the close operation")
            if run.status != MultiLegExecutionStatus.RESERVED and any(
                leg.entry_order is None for leg in run.legs
            ):
                await self._recover_missing_entry_acknowledgements(run)
                if any(leg.entry_order is None for leg in run.legs):
                    return self._mark_uncertain(
                        run,
                        "process resumed with an entry intent whose exchange acknowledgement "
                        "cannot be resolved by deterministic client order id",
                    )
        else:
            run = MultiLegExecutionRun(
                id=request.id,
                request=request,
                request_fingerprint=request.fingerprint,
                status=MultiLegExecutionStatus.RESERVED,
                legs=[
                    MultiLegState(
                        spec=leg,
                        entry_intent_key=self._intent_key(request, leg, "entry"),
                        compensation_intent_key=self._intent_key(
                            request,
                            leg,
                            "compensate",
                        ),
                    )
                    for leg in request.legs
                ],
            )
            self._save(run)
        if run.status == MultiLegExecutionStatus.RESERVED:
            run.status = MultiLegExecutionStatus.SUBMITTING
            self._save(run)
            await self._submit_entries(run)
            if any(leg.entry_order is None for leg in run.legs):
                return self._mark_uncertain(
                    run,
                    "at least one entry acknowledgement is ambiguous; manual venue "
                    "reconciliation is required before any compensation",
                )
        run.status = MultiLegExecutionStatus.RECONCILING
        self._save(run)
        await self._reconcile_and_cancel_working_entries(run)
        if run.status == MultiLegExecutionStatus.UNCERTAIN:
            return run
        fractions = [leg.fill_fraction for leg in run.legs]
        run.hedge_error_fraction = max(fractions) - min(fractions)
        fully_hedged = bool(
            all(
                leg.entry_order is not None
                and leg.entry_order.status == OrderStatus.FILLED
                and leg.fill_fraction >= request.minimum_fill_fraction
                for leg in run.legs
            )
            and run.hedge_error_fraction <= request.maximum_hedge_error_fraction
        )
        if fully_hedged:
            run.status = MultiLegExecutionStatus.HEDGED
            run.reason = "all entry legs filled within the declared hedge tolerance"
            self._save(run)
            return run
        if not any(fraction > 0 for fraction in fractions):
            run.status = MultiLegExecutionStatus.FAILED
            run.reason = "no entry leg filled; no compensating exposure exists"
            self._save(run)
            return run
        return await self._flatten_known_fills(
            run,
            active_status=MultiLegExecutionStatus.COMPENSATING,
            success_status=MultiLegExecutionStatus.COMPENSATED,
            active_reason="entry hedge incomplete; flattening every known filled leg",
            success_reason=(
                "incomplete hedge was flattened with confirmed compensating fills"
            ),
        )

    async def close(
        self,
        run_id: str,
        *,
        validate_certificate: Callable[[MultiLegExecutionRequest], None],
    ) -> MultiLegExecutionRun:
        payload = self.store.load_multi_leg_execution_run(run_id)
        if payload is None:
            raise ValueError("multi-leg execution run does not exist")
        run = MultiLegExecutionRun.model_validate(payload)
        validate_certificate(run.request)
        if run.status == MultiLegExecutionStatus.CLOSED:
            return run
        if run.status not in {
            MultiLegExecutionStatus.HEDGED,
            MultiLegExecutionStatus.CLOSING,
        }:
            raise RuntimeError("only a confirmed hedged run can enter coordinated close")
        return await self._flatten_known_fills(
            run,
            active_status=MultiLegExecutionStatus.CLOSING,
            success_status=MultiLegExecutionStatus.CLOSED,
            active_reason="coordinated strategy exit is flattening every hedged leg",
            success_reason="all hedged legs were closed with confirmed offsetting fills",
        )

    async def _submit_entries(self, run: MultiLegExecutionRun) -> None:
        async def submit(leg: MultiLegState):
            adapter = self._adapter(leg.spec.adapter_key)
            return await adapter.place_order(
                OrderIntent(
                    idempotency_key=leg.entry_intent_key,
                    plan_id=run.id,
                    plan_fingerprint=run.request_fingerprint,
                    symbol=leg.spec.symbol,
                    side=leg.spec.side,
                    order_type=leg.spec.order_type,
                    quantity=leg.spec.quantity,
                    limit_price=leg.spec.limit_price,
                )
            )

        results = await asyncio.gather(
            *(submit(leg) for leg in run.legs),
            return_exceptions=True,
        )
        for leg, result in zip(run.legs, results, strict=True):
            if isinstance(result, BaseException):
                leg.entry_error = str(result)[:500]
            else:
                leg.entry_order = result
                self._record_order(leg.spec.adapter_key, result)
        await self._recover_missing_entry_acknowledgements(run)
        self._save(run)

    async def _recover_missing_entry_acknowledgements(
        self,
        run: MultiLegExecutionRun,
    ) -> None:
        for leg in run.legs:
            if leg.entry_order is not None:
                continue
            try:
                leg.entry_order = await self._adapter(
                    leg.spec.adapter_key
                ).find_order_by_client_id(
                    leg.entry_intent_key,
                    leg.spec.symbol,
                )
                if leg.entry_order is not None:
                    self._record_order(leg.spec.adapter_key, leg.entry_order)
            except Exception as exc:
                leg.entry_error = str(exc)[:500]
        self._save(run)

    async def _reconcile_and_cancel_working_entries(
        self,
        run: MultiLegExecutionRun,
    ) -> None:
        working = {OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED}
        for attempt in range(self.reconciliation_attempts):
            for leg in run.legs:
                if leg.entry_order is None or leg.entry_order.status not in working:
                    continue
                try:
                    leg.entry_order = await self._adapter(leg.spec.adapter_key).fetch_order(
                        leg.entry_order.id,
                        leg.spec.symbol,
                    )
                    self._record_order(leg.spec.adapter_key, leg.entry_order)
                except Exception as exc:
                    leg.entry_error = str(exc)[:500]
            self._save(run)
            if all(
                leg.entry_order is not None and leg.entry_order.status not in working
                for leg in run.legs
            ):
                return
            if attempt + 1 < self.reconciliation_attempts:
                await asyncio.sleep(self.reconciliation_delay_seconds)
        for leg in run.legs:
            if leg.entry_order is None or leg.entry_order.status not in working:
                continue
            adapter = self._adapter(leg.spec.adapter_key)
            try:
                leg.entry_order = await adapter.cancel_order(
                    leg.entry_order.id,
                    leg.spec.symbol,
                )
                self._record_order(leg.spec.adapter_key, leg.entry_order)
                leg.entry_order = await adapter.fetch_order(
                    leg.entry_order.id,
                    leg.spec.symbol,
                )
                self._record_order(leg.spec.adapter_key, leg.entry_order)
            except Exception as exc:
                leg.entry_error = str(exc)[:500]
                self._save(run)
                self._mark_uncertain(
                    run,
                    "a working entry order could not be canceled and reconciled",
                )
                return
        self._save(run)

    async def _flatten_known_fills(
        self,
        run: MultiLegExecutionRun,
        *,
        active_status: MultiLegExecutionStatus,
        success_status: MultiLegExecutionStatus,
        active_reason: str,
        success_reason: str,
    ) -> MultiLegExecutionRun:
        run.status = active_status
        run.reason = active_reason
        self._save(run)

        async def compensate(leg: MultiLegState):
            assert leg.entry_order is not None
            return await self._adapter(leg.spec.adapter_key).place_order(
                OrderIntent(
                    idempotency_key=leg.compensation_intent_key,
                    plan_id=run.id,
                    plan_fingerprint=run.request_fingerprint,
                    symbol=leg.spec.symbol,
                    side=(
                        OrderSide.SELL
                        if leg.spec.side == OrderSide.BUY
                        else OrderSide.BUY
                    ),
                    order_type=OrderType.MARKET,
                    quantity=leg.entry_order.filled_quantity,
                    reduce_only=leg.spec.compensation_reduce_only,
                )
            )

        filled_legs = [
            leg
            for leg in run.legs
            if leg.entry_order is not None and leg.entry_order.filled_quantity > 0
        ]
        results = await asyncio.gather(
            *(compensate(leg) for leg in filled_legs),
            return_exceptions=True,
        )
        for leg, result in zip(filled_legs, results, strict=True):
            if isinstance(result, BaseException):
                leg.compensation_error = str(result)[:500]
            else:
                leg.compensation_order = result
                self._record_order(leg.spec.adapter_key, result)
        self._save(run)
        compensated = all(
            leg.compensation_order is not None
            and leg.compensation_order.status == OrderStatus.FILLED
            and leg.entry_order is not None
            and leg.compensation_order.filled_quantity >= leg.entry_order.filled_quantity
            for leg in filled_legs
        )
        if compensated:
            run.status = success_status
            run.reason = success_reason
            self._save(run)
            return run
        return self._mark_uncertain(
            run,
            "compensating orders did not all reach confirmed full fills",
        )

    def _adapter(self, key: str) -> ExchangeAdapter:
        adapter = self.adapters.get(key)
        if adapter is None:
            raise ValueError(f"multi-leg adapter is unavailable: {key}")
        return adapter

    def _record_order(self, adapter_key: str, order: Order) -> None:
        if self.order_sink is not None:
            self.order_sink(adapter_key, order)

    @staticmethod
    def _intent_key(
        request: MultiLegExecutionRequest,
        leg: MultiLegOrderSpec,
        phase: str,
    ) -> str:
        encoded = f"{request.fingerprint}:{leg.id}:{phase}".encode()
        return f"cx{sha256(encoded).hexdigest()[:24]}"

    def _save(self, run: MultiLegExecutionRun) -> None:
        run.updated_at = datetime.now(UTC)
        self.store.save_multi_leg_execution_run(run)

    def _mark_uncertain(
        self,
        run: MultiLegExecutionRun,
        reason: str,
    ) -> MultiLegExecutionRun:
        run.status = MultiLegExecutionStatus.UNCERTAIN
        run.reason = reason
        self._save(run)
        if self.risk_state is not None:
            self.risk_state.halt(reason, source="multi_leg_execution")
        return run
