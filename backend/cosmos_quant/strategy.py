from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from uuid import uuid4

from .domain import (
    Candle,
    EntryRule,
    ExitPlan,
    MarketSnapshot,
    PlanStatus,
    PositionSizing,
    PriceZone,
    ProfitTarget,
    Side,
    StrategyPlan,
)
from .indicators import atr, ema
from .strategy_spec import TrendParameters
from .timeframes import signal_expiry


def quantize_strategy_price(value: Decimal) -> Decimal:
    """Preserve meaningful stop/target precision across crypto price scales."""

    magnitude = abs(value)
    if magnitude >= Decimal("100"):
        quantum = Decimal("0.01")
    elif magnitude >= Decimal("1"):
        quantum = Decimal("0.0001")
    elif magnitude >= Decimal("0.01"):
        quantum = Decimal("0.000001")
    else:
        quantum = Decimal("0.00000001")
    return value.quantize(quantum)


def target_reward_profile(
    parameters,
    minimum_reward_risk: Decimal,
    *,
    default_first_fraction: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    """Return first/second target R and fraction while preserving weighted R."""

    first_fraction = parameters.first_target_fraction or default_first_fraction
    first_reward_risk = parameters.first_target_reward_risk or (
        minimum_reward_risk + parameters.reward_buffer
    )
    second_reward_risk = second_target_reward_risk(
        parameters,
        minimum_reward_risk,
        first_reward_risk=first_reward_risk,
        first_fraction=first_fraction,
    )
    return first_reward_risk, second_reward_risk, first_fraction


def second_target_reward_risk(
    parameters,
    minimum_reward_risk: Decimal,
    *,
    first_reward_risk: Decimal,
    first_fraction: Decimal,
) -> Decimal:
    """Preserve the weighted reward floor after a structural first target."""

    weighted_floor = minimum_reward_risk + parameters.reward_buffer
    remaining_fraction = Decimal("1") - first_fraction
    required_second = (
        (weighted_floor - first_fraction * first_reward_risk) / remaining_fraction
        if remaining_fraction > 0
        else first_reward_risk
    )
    return max(
        first_reward_risk * parameters.second_target_extension,
        required_second,
    )


def _price(value: Decimal) -> Decimal:
    return quantize_strategy_price(value)


def _quantity(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)


class ExplainableTrendStrategy:
    """EMA regime + ATR bracket strategy that always emits explicit price levels."""

    name = "EMA 趋势回踩 + ATR 风险边界"

    def generate(
        self,
        candles: list[Candle],
        snapshot: MarketSnapshot,
        equity: Decimal,
        risk_fraction: Decimal = Decimal("0.0075"),
        exchange: str = "paper",
        timeframe: str = "1h",
        minimum_reward_risk: Decimal = Decimal("1.5"),
        allow_short: bool = True,
        side_override: Side | None = None,
        parameters: TrendParameters | None = None,
    ) -> StrategyPlan:
        if len(candles) < 55:
            raise ValueError("至少需要 55 根 K 线才能生成策略")
        parameters = parameters or TrendParameters()
        closes = [candle.close for candle in candles]
        fast = ema(closes, parameters.ema_fast)[-1]
        slow = ema(closes, parameters.ema_slow)[-1]
        volatility = atr(candles, 14)
        side = side_override or (Side.LONG if fast >= slow or not allow_short else Side.SHORT)
        reference = snapshot.last
        zone_half_width = volatility * parameters.entry_zone_atr
        zone = PriceZone(
            lower=_price(reference - zone_half_width),
            upper=_price(reference + zone_half_width),
        )

        if side == Side.LONG:
            stop = _price(reference - volatility * parameters.stop_atr)
            first_reward_risk, second_reward_risk, first_fraction = target_reward_profile(
                parameters,
                minimum_reward_risk,
                default_first_fraction=Decimal("0.6"),
            )
            first_target = _price(
                reference + volatility * parameters.stop_atr * first_reward_risk
            )
            second_target = _price(
                reference
                + volatility
                * parameters.stop_atr
                * second_reward_risk
            )
            trigger = (
                f"价格进入计划区间，且 EMA{parameters.ema_fast} 保持在 "
                f"EMA{parameters.ema_slow} 上方后挂限价买单"
                if fast >= slow
                else f"现货模式不做空；等待 EMA{parameters.ema_fast} 重新站上 "
                f"EMA{parameters.ema_slow} 且价格进入计划区间后买入"
            )
            invalidation = (
                f"{timeframe} 收盘价跌破 {stop:f}，或 EMA{parameters.ema_fast} "
                f"下穿 EMA{parameters.ema_slow}"
            )
            fast_display = fast.quantize(Decimal("0.01"))
            slow_display = slow.quantize(Decimal("0.01"))
            reason = (
                f"EMA{parameters.ema_fast} {fast_display} 高于 "
                f"EMA{parameters.ema_slow} {slow_display}"
                if fast >= slow
                else (
                    f"现货账户禁止做空；EMA{parameters.ema_fast} {fast_display} 低于 "
                    f"EMA{parameters.ema_slow} {slow_display}，只等待多头反转确认"
                )
            )
        else:
            stop = _price(reference + volatility * parameters.stop_atr)
            first_reward_risk, second_reward_risk, first_fraction = target_reward_profile(
                parameters,
                minimum_reward_risk,
                default_first_fraction=Decimal("0.6"),
            )
            first_target = _price(
                reference - volatility * parameters.stop_atr * first_reward_risk
            )
            second_target = _price(
                reference
                - volatility
                * parameters.stop_atr
                * second_reward_risk
            )
            trigger = (
                f"价格进入计划区间，且 EMA{parameters.ema_fast} 保持在 "
                f"EMA{parameters.ema_slow} 下方后挂限价卖单"
            )
            invalidation = (
                f"{timeframe} 收盘价升破 {stop:f}，或 EMA{parameters.ema_fast} "
                f"上穿 EMA{parameters.ema_slow}"
            )
            reason = (
                f"EMA{parameters.ema_fast} {fast.quantize(Decimal('0.01'))} 低于 "
                f"EMA{parameters.ema_slow} {slow.quantize(Decimal('0.01'))}"
            )

        risk_amount = (equity * risk_fraction).quantize(Decimal("0.01"))
        stop_distance = abs(reference - stop)
        quantity = _quantity(risk_amount / stop_distance) if stop_distance else Decimal("0")
        notional = (quantity * reference).quantize(Decimal("0.01"))

        return StrategyPlan(
            id=f"stg_{uuid4().hex[:12]}",
            name=self.name,
            exchange=exchange,
            symbol=snapshot.symbol,
            timeframe=timeframe,
            side=side,
            status=PlanStatus.DRAFT,
            signal_candle_at=candles[-1].timestamp,
            data_as_of=snapshot.timestamp,
            entry=EntryRule(
                zone=zone,
                trigger=trigger,
                expires_at=signal_expiry(candles[-1], timeframe),
                max_slippage_bps=Decimal("8"),
            ),
            exit=ExitPlan(
                stop_loss=stop,
                take_profits=[
                    ProfitTarget(
                        price=first_target,
                        close_fraction=first_fraction,
                        rationale="达到约 1.5R 后先锁定大部分利润",
                    ),
                    ProfitTarget(
                        price=second_target,
                        close_fraction=Decimal("1") - first_fraction,
                        rationale="保留剩余仓位捕捉趋势延伸",
                    ),
                ],
                trailing_stop_atr=parameters.trailing_stop_atr,
                trailing_activation=parameters.trailing_activation,
                break_even_after_first_target=parameters.break_even_after_first_target,
                maximum_holding_bars=parameters.maximum_holding_bars,
                invalidation=invalidation,
            ),
            sizing=PositionSizing(
                risk_fraction=risk_fraction,
                risk_amount=risk_amount,
                suggested_quantity=quantity,
                notional=notional,
            ),
            minimum_reward_risk=minimum_reward_risk,
            thesis="顺应中期 EMA 趋势，在当前波动率范围内等待回踩入场，并用 ATR 统一风险尺度。",
            reasons=[
                reason,
                f"14 周期 ATR 为 {volatility.quantize(Decimal('0.01'))}，"
                f"止损距离采用 {parameters.stop_atr:f} ATR",
                f"账户权益 {equity:f} USDT，单笔风险预算为 {risk_fraction * 100:f}%",
            ],
            pause_conditions=[
                "行情数据延迟超过 15 秒",
                "买卖价差超过 20 bps",
                "当日组合回撤达到 3%",
            ],
            snapshot=snapshot,
        )
