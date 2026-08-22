from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from uuid import uuid4

from .configuration import StrategyKind
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
from .indicators import atr, rsi, sma, standard_deviation
from .signals import (
    NoTradeSignal,
    evaluate_signal,
    score_market_state,
    strategy_family_fit,
)
from .strategy import (
    ExplainableTrendStrategy,
    quantize_strategy_price,
    second_target_reward_risk,
    target_reward_profile,
)
from .strategy_spec import (
    BreakoutParameters,
    MeanReversionParameters,
    validate_strategy_spec,
)
from .timeframes import signal_expiry


def _price(value: Decimal) -> Decimal:
    return quantize_strategy_price(value)


def _quantity(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)


def _sizing(
    equity: Decimal,
    risk_fraction: Decimal,
    reference: Decimal,
    stop: Decimal,
) -> PositionSizing:
    risk_amount = (equity * risk_fraction).quantize(Decimal("0.01"))
    stop_distance = abs(reference - stop)
    quantity = _quantity(risk_amount / stop_distance) if stop_distance else Decimal("0")
    return PositionSizing(
        risk_fraction=risk_fraction,
        risk_amount=risk_amount,
        suggested_quantity=quantity,
        notional=(quantity * reference).quantize(Decimal("0.01")),
    )


class MeanReversionStrategy:
    name = "布林偏离 + RSI 均值回归"

    def generate(
        self,
        candles: list[Candle],
        snapshot: MarketSnapshot,
        equity: Decimal,
        risk_fraction: Decimal,
        exchange: str,
        timeframe: str,
        minimum_reward_risk: Decimal,
        allow_short: bool = True,
        side_override: Side | None = None,
        parameters: MeanReversionParameters | None = None,
    ) -> StrategyPlan:
        if len(candles) < 55:
            raise ValueError("至少需要 55 根 K 线才能生成策略")
        parameters = parameters or MeanReversionParameters()
        closes = [candle.close for candle in candles]
        mean = sma(closes, parameters.mean_window)
        deviation = standard_deviation(closes, parameters.mean_window)
        momentum = rsi(closes, parameters.rsi_period)
        volatility = atr(candles, 14)
        reference = snapshot.last
        side = side_override or (Side.LONG if reference <= mean or not allow_short else Side.SHORT)
        if not allow_short and reference > mean:
            reference = min(reference, mean - deviation)
        zone_half = volatility * parameters.entry_zone_atr
        zone = PriceZone(lower=_price(reference - zone_half), upper=_price(reference + zone_half))
        stop_multiple = parameters.stop_atr
        first_reward_risk, second_reward_risk, first_fraction = target_reward_profile(
            parameters,
            minimum_reward_risk,
            default_first_fraction=Decimal("0.7"),
        )
        first_target_multiple = stop_multiple * first_reward_risk
        second_target_multiple = stop_multiple * second_reward_risk
        first_rationale = "回归目标先锁定 70%"
        if side == Side.LONG:
            stop = _price(reference - volatility * stop_multiple)
            mean_target = _price(mean)
            if parameters.mean_completion_mode == "mean_touch" and mean_target > reference:
                first = mean_target
                actual_first_reward_risk = (first - reference) / max(
                    reference - stop,
                    Decimal("0.00000001"),
                )
                second_reward_risk = second_target_reward_risk(
                    parameters,
                    minimum_reward_risk,
                    first_reward_risk=actual_first_reward_risk,
                    first_fraction=first_fraction,
                )
                second_target_multiple = stop_multiple * second_reward_risk
                first_rationale = "触及入场时可见统计均值后先减仓"
            else:
                first = _price(max(mean, reference + volatility * first_target_multiple))
            second = _price(reference + volatility * second_target_multiple)
            trigger = "价格处于 20 周期均值下方，RSI 止跌回升并重新进入计划区间后买入"
            invalidation = f"收盘价跌破 {stop:f}，或波动率扩张超过近 20 根 K 线正常范围"
        else:
            stop = _price(reference + volatility * stop_multiple)
            mean_target = _price(mean)
            if parameters.mean_completion_mode == "mean_touch" and mean_target < reference:
                first = mean_target
                actual_first_reward_risk = (reference - first) / max(
                    stop - reference,
                    Decimal("0.00000001"),
                )
                second_reward_risk = second_target_reward_risk(
                    parameters,
                    minimum_reward_risk,
                    first_reward_risk=actual_first_reward_risk,
                    first_fraction=first_fraction,
                )
                second_target_multiple = stop_multiple * second_reward_risk
                first_rationale = "触及入场时可见统计均值后先减仓"
            else:
                first = _price(min(mean, reference - volatility * first_target_multiple))
            second = _price(reference - volatility * second_target_multiple)
            trigger = "价格处于 20 周期均值上方，RSI 见顶回落并重新进入计划区间后卖出"
            invalidation = f"收盘价升破 {stop:f}，或波动率扩张超过近 20 根 K 线正常范围"
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
            ),
            exit=ExitPlan(
                stop_loss=stop,
                take_profits=[
                    ProfitTarget(
                        price=first,
                        close_fraction=first_fraction,
                        rationale=first_rationale,
                    ),
                    ProfitTarget(
                        price=second,
                        close_fraction=Decimal("1") - first_fraction,
                        rationale="保留尾仓捕捉过度修复",
                    ),
                ],
                trailing_stop_atr=parameters.trailing_stop_atr,
                trailing_activation=parameters.trailing_activation,
                break_even_after_first_target=parameters.break_even_after_first_target,
                maximum_holding_bars=parameters.maximum_holding_bars,
                invalidation=invalidation,
            ),
            sizing=_sizing(equity, risk_fraction, reference, stop),
            minimum_reward_risk=minimum_reward_risk,
            thesis="捕捉价格对 20 周期统计均值的过度偏离，在动量转折后执行回归交易。",
            reasons=[
                f"{parameters.mean_window} 周期均值为 {_price(mean):f}，"
                f"标准差为 {_price(deviation):f}",
                f"{parameters.rsi_period} 周期 RSI 为 {momentum.quantize(Decimal('0.1'))}",
                f"14 周期 ATR 为 {_price(volatility):f}，止损采用 {parameters.stop_atr:f} ATR",
                f"退出模式为 {parameters.mean_completion_mode}",
            ],
            pause_conditions=["趋势强度持续扩大", "行情延迟超过 15 秒", "价差超过风控上限"],
            snapshot=snapshot,
        )


class BreakoutStrategy:
    name = "Donchian 区间突破 + ATR 跟随"

    def generate(
        self,
        candles: list[Candle],
        snapshot: MarketSnapshot,
        equity: Decimal,
        risk_fraction: Decimal,
        exchange: str,
        timeframe: str,
        minimum_reward_risk: Decimal,
        allow_short: bool = True,
        side_override: Side | None = None,
        parameters: BreakoutParameters | None = None,
    ) -> StrategyPlan:
        if len(candles) < 55:
            raise ValueError("至少需要 55 根 K 线才能生成策略")
        parameters = parameters or BreakoutParameters()
        window = candles[-(parameters.lookback + 1) : -1]
        upper = max(candle.high for candle in window)
        lower = min(candle.low for candle in window)
        midpoint = (upper + lower) / Decimal("2")
        volatility = atr(candles, 14)
        side = side_override or (
            Side.LONG if snapshot.last >= midpoint or not allow_short else Side.SHORT
        )
        stop_multiple = parameters.stop_atr
        first_reward_risk, second_reward_risk, first_fraction = target_reward_profile(
            parameters,
            minimum_reward_risk,
            default_first_fraction=Decimal("0.6"),
        )
        first_target_multiple = stop_multiple * first_reward_risk
        second_target_multiple = stop_multiple * second_reward_risk
        if side == Side.LONG:
            zone = PriceZone(
                lower=_price(upper),
                upper=_price(upper + volatility * parameters.entry_zone_atr),
            )
            reference = (zone.lower + zone.upper) / Decimal("2")
            stop = _price(reference - volatility * stop_multiple)
            first = _price(reference + volatility * first_target_multiple)
            second = _price(reference + volatility * second_target_multiple)
            trigger = (
                f"价格放量突破过去 {parameters.lookback} 根 K 线高点 "
                f"{upper:f} 并保持一个收盘周期后买入"
            )
            invalidation = f"突破后收盘重新跌回 {upper:f} 下方，或触及硬止损 {stop:f}"
        else:
            zone = PriceZone(
                lower=_price(lower - volatility * parameters.entry_zone_atr),
                upper=_price(lower),
            )
            reference = (zone.lower + zone.upper) / Decimal("2")
            stop = _price(reference + volatility * stop_multiple)
            first = _price(reference - volatility * first_target_multiple)
            second = _price(reference - volatility * second_target_multiple)
            trigger = (
                f"价格放量跌破过去 {parameters.lookback} 根 K 线低点 "
                f"{lower:f} 并保持一个收盘周期后卖出"
            )
            invalidation = f"突破后收盘重新升回 {lower:f} 上方，或触及硬止损 {stop:f}"
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
            ),
            exit=ExitPlan(
                stop_loss=stop,
                take_profits=[
                    ProfitTarget(
                        price=first,
                        close_fraction=first_fraction,
                        rationale="达到最低盈亏比后减仓",
                    ),
                    ProfitTarget(
                        price=second,
                        close_fraction=Decimal("1") - first_fraction,
                        rationale="趋势尾仓使用 ATR 跟随",
                    ),
                ],
                trailing_stop_atr=parameters.trailing_stop_atr,
                trailing_activation=parameters.trailing_activation,
                break_even_after_first_target=parameters.break_even_after_first_target,
                maximum_holding_bars=parameters.maximum_holding_bars,
                invalidation=invalidation,
            ),
            sizing=_sizing(equity, risk_fraction, reference, stop),
            minimum_reward_risk=minimum_reward_risk,
            thesis="等待 20 周期价格区间被有效突破，以 ATR 定义初始风险并跟随趋势扩张。",
            reasons=[
                f"{parameters.lookback} 周期区间为 {lower:f}–{upper:f}",
                f"当前价格位于区间中轴 {midpoint:f} 的{'上方' if side == Side.LONG else '下方'}",
                f"14 周期 ATR 为 {_price(volatility):f}，止损采用 {parameters.stop_atr:f} ATR",
            ],
            pause_conditions=["突破成交量不足", "行情延迟超过 15 秒", "价差超过风控上限"],
            snapshot=snapshot,
        )


class StrategyFactory:
    def __init__(self):
        self._strategies = {
            StrategyKind.TREND: ExplainableTrendStrategy(),
            StrategyKind.MEAN_REVERSION: MeanReversionStrategy(),
            StrategyKind.BREAKOUT: BreakoutStrategy(),
        }

    def evaluate(self, kind: StrategyKind, **kwargs):
        spec = validate_strategy_spec(kwargs.get("strategy_spec"), kind)
        return evaluate_signal(
            kind,
            candles=kwargs["candles"],
            snapshot=kwargs["snapshot"],
            timeframe=kwargs["timeframe"],
            allow_short=kwargs.get("allow_short", True),
            confirmation_candles=kwargs.get("confirmation_candles"),
            require_confirmation=kwargs.get("require_confirmation", False),
            strategy_spec=spec,
        )

    def evaluate_ensemble(self, **kwargs):
        assessment = score_market_state(kwargs["candles"])
        candidates = {kind: self.evaluate(kind, **kwargs) for kind in StrategyKind}
        actionable = [
            (kind, signal)
            for kind, signal in candidates.items()
            if signal.actionable and strategy_family_fit(assessment, kind)[0]
        ]
        if actionable:
            winner_kind, winner = max(
                actionable,
                key=lambda item: (
                    item[1].confidence * Decimal("0.7")
                    + assessment.scores[item[0].value] * Decimal("0.3")
                ),
            )
        else:
            winner_kind = assessment.selected_kind
            winner = candidates[winner_kind]
        winner = winner.model_copy(
            update={
                "reasons": [*assessment.reasons, *winner.reasons],
                "features": {
                    **winner.features,
                    "market_state_confidence": assessment.confidence,
                    "market_state_ambiguous": assessment.ambiguous,
                    **{
                        f"market_state_score_{key}": value
                        for key, value in assessment.scores.items()
                    },
                },
            }
        )
        return winner_kind, winner, assessment

    def generate_automatic(self, **kwargs) -> StrategyPlan:
        winner_kind, signal, assessment = self.evaluate_ensemble(**kwargs)
        if not signal.actionable:
            raise NoTradeSignal(signal)
        plan = self.generate(winner_kind, **kwargs)
        return plan.model_copy(
            update={
                "confidence": signal.confidence,
                "reasons": [*assessment.reasons, *plan.reasons],
            }
        )

    def generate_unchecked(self, kind: StrategyKind, **kwargs) -> StrategyPlan:
        """Build protocol fixtures that are explicitly not market signals.

        This is reserved for exchange commissioning where a far-away order is
        constructed only to validate the protected-order transport.
        """

        kwargs.pop("confirmation_candles", None)
        kwargs.pop("require_confirmation", None)
        raw_spec = kwargs.pop("strategy_spec", None)
        if raw_spec is not None:
            spec = validate_strategy_spec(raw_spec, kind)
            kwargs["parameters"] = spec.parameters
        return self._strategies[kind].generate(**kwargs)

    def generate(self, kind: StrategyKind, **kwargs) -> StrategyPlan:
        spec = validate_strategy_spec(kwargs.get("strategy_spec"), kind)
        signal = self.evaluate(kind, **kwargs)
        if not signal.actionable:
            raise NoTradeSignal(signal)
        generation_kwargs = dict(kwargs)
        generation_kwargs.pop("confirmation_candles", None)
        generation_kwargs.pop("require_confirmation", None)
        generation_kwargs.pop("strategy_spec", None)
        plan = self._strategies[kind].generate(
            **generation_kwargs,
            side_override=Side(signal.direction),
            parameters=spec.parameters,
        )
        return StrategyPlan.model_validate(
            {
                **plan.model_dump(exclude_computed_fields=True),
                "algorithm": kind.value,
                "regime": signal.regime,
                "confidence": signal.confidence,
                "signal_candle_at": signal.signal_candle_at,
                "data_as_of": signal.data_as_of,
                "reasons": [*signal.reasons, *plan.reasons],
                "strategy_spec": spec.model_dump(mode="json", exclude={"fingerprint"}),
                "strategy_spec_fingerprint": spec.fingerprint,
            }
        )
