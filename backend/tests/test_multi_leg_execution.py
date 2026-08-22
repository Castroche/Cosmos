import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from cosmos_quant.domain import (
    MarketSnapshot,
    Order,
    OrderSide,
    OrderStatus,
    TradingMode,
)
from cosmos_quant.exchanges.paper import PaperExchange
from cosmos_quant.multi_leg_execution import (
    MultiLegExecutionCoordinator,
    MultiLegExecutionRequest,
    MultiLegExecutionStatus,
    MultiLegOrderSpec,
    build_multi_leg_commissioning_result,
    build_multi_leg_execution_certificate,
    evaluate_carry_exit,
    evaluate_pair_exit,
    validate_multi_leg_commissioning_result,
    validate_multi_leg_execution_certificate,
)
from cosmos_quant.risk_state import RiskStateService
from cosmos_quant.storage import SqliteStore


def _snapshot(symbol: str, price: str) -> MarketSnapshot:
    value = Decimal(price)
    return MarketSnapshot(
        symbol=symbol,
        timestamp=datetime.now(UTC),
        bid=value - Decimal("0.1"),
        ask=value + Decimal("0.1"),
        last=value,
        source="test",
    )


def _request(run_id: str = "multileg_test_run") -> MultiLegExecutionRequest:
    return MultiLegExecutionRequest(
        id=run_id,
        strategy_kind="pair",
        campaign_id="paircampaign_test",
        strategy_fingerprint="strategy12345678",
        campaign_certificate_fingerprint="certificate1234567890",
        blind_evidence_fingerprint="a" * 64,
        legs=[
            MultiLegOrderSpec(
                id="long_btc",
                adapter_key="swap",
                exchange="paper",
                market_type="swap",
                symbol="BTC/USDT",
                side=OrderSide.BUY,
                quantity=Decimal("1"),
                compensation_reduce_only=True,
            ),
            MultiLegOrderSpec(
                id="short_eth",
                adapter_key="swap",
                exchange="paper",
                market_type="swap",
                symbol="ETH/USDT",
                side=OrderSide.SELL,
                quantity=Decimal("10"),
                compensation_reduce_only=True,
            ),
        ],
    )


def _paper() -> PaperExchange:
    exchange = PaperExchange(market_type="swap")
    exchange.update_market(_snapshot("BTC/USDT", "50000"))
    exchange.update_market(_snapshot("ETH/USDT", "5000"))
    return exchange


def test_multi_leg_entry_is_durable_hedged_and_idempotent(tmp_path):
    async def scenario():
        store = SqliteStore(tmp_path / "multi.db")
        exchange = _paper()
        coordinator = MultiLegExecutionCoordinator(store, {"swap": exchange})
        request = _request()

        first = await coordinator.execute(request, validate_certificate=lambda _: None)
        replay = await coordinator.execute(request, validate_certificate=lambda _: None)

        assert first.status == MultiLegExecutionStatus.HEDGED
        assert replay.id == first.id
        assert len(exchange.orders) == 2
        assert all(leg.entry_order.status == OrderStatus.FILLED for leg in first.legs)
        persisted = store.load_multi_leg_execution_run(request.id)
        assert persisted is not None
        assert persisted["status"] == "hedged"

        closed = await coordinator.close(request.id, validate_certificate=lambda _: None)
        closed_replay = await coordinator.close(
            request.id,
            validate_certificate=lambda _: None,
        )
        assert closed.status == MultiLegExecutionStatus.CLOSED
        assert closed_replay.status == MultiLegExecutionStatus.CLOSED
        assert len(exchange.orders) == 4
        assert exchange.positions["BTC/USDT"].signed_quantity == 0
        assert exchange.positions["ETH/USDT"].signed_quantity == 0

    asyncio.run(scenario())


def test_multi_leg_commissioning_is_separate_credential_bound_evidence(tmp_path):
    async def scenario():
        store = SqliteStore(tmp_path / "commissioning.db")
        exchange = _paper()
        recorded: list[tuple[str, str]] = []
        coordinator = MultiLegExecutionCoordinator(
            store,
            {"swap": exchange},
            order_sink=lambda adapter_key, order: recorded.append(
                (adapter_key, order.id)
            ),
        )
        request = _request("multileg_commissioning").model_copy(
            update={"execution_purpose": "commissioning"}
        )
        entered = await coordinator.execute(request, validate_certificate=lambda _: None)
        closed = await coordinator.close(request.id, validate_certificate=lambda _: None)
        result = build_multi_leg_commissioning_result(
            exchange="paper",
            credential_fingerprint="credential123456",
            strategy_kind="pair",
            run=entered,
            closed_run=closed,
            starting_exposure={
                "paper:swap:BTC/USDT": Decimal("0"),
                "paper:swap:ETH/USDT": Decimal("0"),
            },
            ending_exposure={
                "paper:swap:BTC/USDT": Decimal("0"),
                "paper:swap:ETH/USDT": Decimal("0"),
            },
            no_preexisting_scope_orders=True,
            no_remaining_scope_orders=True,
            order_ledger_recorded=len(recorded) >= 4,
            message="sandbox protocol completed",
        )
        store.save_multi_leg_commissioning(result)

        assert result.passed is True
        assert len(recorded) >= 4
        persisted = store.load_multi_leg_commissioning("paper", "pair")
        assert persisted is not None
        validate_multi_leg_commissioning_result(
            result,
            exchange="paper",
            credential_fingerprint="credential123456",
            strategy_kind="pair",
            execution_scopes=[
                "paper:swap:BTC/USDT",
                "paper:swap:ETH/USDT",
            ],
        )
        with pytest.raises(ValueError, match="active credentials and scopes"):
            validate_multi_leg_commissioning_result(
                result,
                exchange="paper",
                credential_fingerprint="different1234567",
                strategy_kind="pair",
                execution_scopes=result.execution_scopes,
            )

    asyncio.run(scenario())


def test_rejected_leg_flattens_every_known_fill(tmp_path):
    class RejectEthPaper(PaperExchange):
        async def place_order(self, intent):
            if intent.symbol == "ETH/USDT" and not intent.reduce_only:
                return Order(
                    id="rejected_eth",
                    client_order_id=intent.idempotency_key,
                    plan_id=intent.plan_id,
                    symbol=intent.symbol,
                    side=intent.side,
                    order_type=intent.order_type,
                    status=OrderStatus.REJECTED,
                    quantity=intent.quantity,
                    mode=TradingMode.PAPER,
                )
            return await super().place_order(intent)

    async def scenario():
        store = SqliteStore(tmp_path / "compensate.db")
        exchange = RejectEthPaper(market_type="swap")
        exchange.update_market(_snapshot("BTC/USDT", "50000"))
        exchange.update_market(_snapshot("ETH/USDT", "5000"))
        coordinator = MultiLegExecutionCoordinator(store, {"swap": exchange})

        result = await coordinator.execute(
            _request("multileg_compensate"),
            validate_certificate=lambda _: None,
        )

        assert result.status == MultiLegExecutionStatus.COMPENSATED
        assert result.legs[0].compensation_order is not None
        assert exchange.positions["BTC/USDT"].signed_quantity == 0
        assert "ETH/USDT" not in exchange.positions

    asyncio.run(scenario())


def test_ambiguous_leg_ack_halts_without_guessing_compensation(tmp_path):
    class AmbiguousEthPaper(PaperExchange):
        async def place_order(self, intent):
            if intent.symbol == "ETH/USDT" and not intent.reduce_only:
                raise TimeoutError("acknowledgement lost")
            return await super().place_order(intent)

    async def scenario():
        store = SqliteStore(tmp_path / "uncertain.db")
        risk_state = RiskStateService(store)
        exchange = AmbiguousEthPaper(market_type="swap")
        exchange.update_market(_snapshot("BTC/USDT", "50000"))
        exchange.update_market(_snapshot("ETH/USDT", "5000"))
        coordinator = MultiLegExecutionCoordinator(
            store,
            {"swap": exchange},
            risk_state=risk_state,
        )

        result = await coordinator.execute(
            _request("multileg_uncertain"),
            validate_certificate=lambda _: None,
        )

        assert result.status == MultiLegExecutionStatus.UNCERTAIN
        assert result.legs[0].compensation_order is None
        assert risk_state.get().halted is True
        assert risk_state.get().source == "multi_leg_execution"

    asyncio.run(scenario())


def test_lost_ack_is_recovered_by_deterministic_client_order_id(tmp_path):
    class LostAckAfterFillPaper(PaperExchange):
        def __init__(self):
            super().__init__(market_type="swap")
            self.lost_once = False

        async def place_order(self, intent):
            order = await super().place_order(intent)
            if intent.symbol == "ETH/USDT" and not self.lost_once:
                self.lost_once = True
                raise TimeoutError("response lost after venue accepted the order")
            return order

    async def scenario():
        store = SqliteStore(tmp_path / "recovered-ack.db")
        exchange = LostAckAfterFillPaper()
        exchange.update_market(_snapshot("BTC/USDT", "50000"))
        exchange.update_market(_snapshot("ETH/USDT", "5000"))
        coordinator = MultiLegExecutionCoordinator(store, {"swap": exchange})

        result = await coordinator.execute(
            _request("multileg_recovered_ack"),
            validate_certificate=lambda _: None,
        )

        assert result.status == MultiLegExecutionStatus.HEDGED
        assert result.legs[1].entry_order is not None
        assert result.legs[1].entry_order.status == OrderStatus.FILLED
        assert len(exchange.orders) == 2

    asyncio.run(scenario())


def test_certificate_is_validated_before_any_multi_leg_order(tmp_path):
    async def scenario():
        store = SqliteStore(tmp_path / "certificate.db")
        exchange = _paper()
        coordinator = MultiLegExecutionCoordinator(store, {"swap": exchange})

        with pytest.raises(ValueError, match="certificate rejected"):
            await coordinator.execute(
                _request("multileg_bad_certificate"),
                validate_certificate=lambda _: (_ for _ in ()).throw(
                    ValueError("certificate rejected")
                ),
            )

        assert exchange.orders == {}
        assert store.load_multi_leg_execution_run("multileg_bad_certificate") is None

    asyncio.run(scenario())


def test_multi_leg_certificate_binds_blind_evidence_and_exact_market_pair():
    certificate = build_multi_leg_execution_certificate(
        strategy_kind="pair",
        campaign_id="paircampaign_test",
        campaign_engine_version=5,
        strategy_id="pair_strategy_test",
        strategy_fingerprint="strategy12345678",
        research_lineage_id="pair_lineage_test",
        blind_set_id="blindset_test",
        blind_consumption_id="blind_eval_test",
        blind_evidence_fingerprint="a" * 64,
        development_evidence={"candidate": "frozen", "pbo": "passed"},
        strategy_spec_payload={
            "model": "log_spread_equal_dollar",
            "exit_zscore": "0.5",
            "stop_zscore": "4",
            "maximum_holding_bars": 120,
        },
        allowed_execution_groups=[
            ["paper:swap:BTC/USDT", "paper:swap:ETH/USDT"]
        ],
        development_passed=True,
        pbo_passed=True,
        independent_blind_test_completed=True,
        blind_selected_candidate_passed=True,
    )
    request = _request("multileg_certificate_scope").model_copy(
        update={"campaign_certificate_fingerprint": certificate.fingerprint}
    )

    validate_multi_leg_execution_certificate(certificate, request=request)
    outside_scope = request.model_copy(
        update={
            "legs": [
                request.legs[0],
                request.legs[1].model_copy(update={"symbol": "SOL/USDT"}),
            ]
        }
    )
    with pytest.raises(ValueError, match="outside the certificate market scope"):
        validate_multi_leg_execution_certificate(certificate, request=outside_scope)

    tampered = certificate.model_copy(
        update={
            "allowed_execution_groups": [
                ["paper:swap:BTC/USDT", "paper:swap:SOL/USDT"]
            ]
        }
    )
    with pytest.raises(ValueError, match="fingerprint mismatch"):
        validate_multi_leg_execution_certificate(tampered)

    commissioning_request = request.model_copy(
        update={"execution_purpose": "commissioning"}
    )
    with pytest.raises(ValueError, match="cannot authorize commissioning"):
        validate_multi_leg_execution_certificate(
            certificate,
            request=commissioning_request,
        )


def test_multi_leg_exit_rules_match_frozen_pair_and_carry_thresholds():
    pair_spec = {
        "model": "log_spread_equal_dollar",
        "exit_zscore": "0.5",
        "stop_zscore": "4",
        "maximum_holding_bars": 120,
    }
    assert evaluate_pair_exit(
        spec_payload=pair_spec,
        pair_a_side=OrderSide.BUY,
        zscore=Decimal("0.25"),
        held_bars=5,
    ) == (True, "mean_reversion")
    assert evaluate_pair_exit(
        spec_payload=pair_spec,
        pair_a_side=OrderSide.BUY,
        zscore=Decimal("4.5"),
        held_bars=5,
    ) == (True, "spread_stop")

    carry_spec = {
        "exit_basis_rate": "0.0002",
        "stop_basis_rate": "0.015",
        "funding_exit_rate": "-0.00005",
        "minimum_holding_bars": 8,
        "maximum_holding_bars": 2880,
    }
    assert evaluate_carry_exit(
        spec_payload=carry_spec,
        basis_rate=Decimal("0.0001"),
        funding_rate=Decimal("0.0001"),
        held_bars=7,
    ) == (False, "hold")
    assert evaluate_carry_exit(
        spec_payload=carry_spec,
        basis_rate=Decimal("0.0001"),
        funding_rate=Decimal("0.0001"),
        held_bars=8,
    ) == (True, "basis_convergence")
    assert evaluate_carry_exit(
        spec_payload=carry_spec,
        basis_rate=Decimal("0.02"),
        funding_rate=None,
        held_bars=1,
    ) == (True, "basis_stop")


def test_multi_leg_certificate_rejects_duplicate_two_leg_scope():
    with pytest.raises(ValueError, match="at least one exact two-leg scope"):
        build_multi_leg_execution_certificate(
            strategy_kind="pair",
            campaign_id="paircampaign_test",
            campaign_engine_version=6,
            strategy_id="pair_strategy_test",
            strategy_fingerprint="strategy12345678",
            research_lineage_id="pair_lineage_test",
            blind_set_id="blindset_test",
            blind_consumption_id="blind_eval_test",
            blind_evidence_fingerprint="a" * 64,
            development_evidence={"candidate": "frozen"},
            allowed_execution_groups=[
                ["paper:swap:BTC/USDT", "paper:swap:BTC/USDT"]
            ],
            development_passed=True,
            pbo_passed=True,
            independent_blind_test_completed=True,
            blind_selected_candidate_passed=True,
        )


def test_multi_leg_certificate_cannot_be_issued_without_blind_pass():
    with pytest.raises(ValueError, match="requires development, PBO, and blind PASS"):
        build_multi_leg_execution_certificate(
            strategy_kind="carry",
            campaign_id="carrycampaign_test",
            campaign_engine_version=3,
            strategy_id="carry_strategy_test",
            strategy_fingerprint="strategy12345678",
            research_lineage_id="carry_lineage_test",
            blind_set_id="blindset_test",
            blind_consumption_id="blind_eval_test",
            blind_evidence_fingerprint="b" * 64,
            development_evidence={"candidate": "frozen"},
            allowed_execution_groups=[
                ["paper:spot:BTC/USDT", "paper:swap:BTC/USDT"]
            ],
            development_passed=True,
            pbo_passed=True,
            independent_blind_test_completed=True,
            blind_selected_candidate_passed=False,
        )
