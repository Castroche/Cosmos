from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .configuration import StrategyKind
from .research_lifecycle import StrategyCandidate, create_candidate
from .strategy_spec import StrategySpec, validate_strategy_spec


class StrategyLibraryEntry(BaseModel):
    """A versioned and executable public strategy hypothesis.

    An entry is a research prior, never a profitability claim. Every activation
    still requires a fresh immutable dataset and out-of-sample validation.
    """

    id: str
    version: int = 2
    name: str
    family: StrategyKind
    archetype: str
    origin: str
    source_url: str
    summary: str
    indicators: list[str] = Field(default_factory=list)
    preferred_regimes: list[str] = Field(default_factory=list)
    avoid_regimes: list[str] = Field(default_factory=list)
    required_data: list[str] = Field(default_factory=lambda: ["ohlcv"])
    expected_profile: Literal[
        "balanced",
        "high_payoff_lower_win_rate",
        "selective_high_quality",
    ] = "balanced"
    spec: dict

    @property
    def validated_spec(self) -> StrategySpec:
        return validate_strategy_spec(self.spec, self.family)


def _entry(
    *,
    id: str,
    name: str,
    family: StrategyKind,
    archetype: str,
    origin: str,
    source_url: str,
    summary: str,
    indicators: list[str],
    preferred_regimes: list[str],
    parameters: dict,
    avoid_regimes: list[str] | None = None,
    required_data: list[str] | None = None,
    expected_profile: Literal[
        "balanced",
        "high_payoff_lower_win_rate",
        "selective_high_quality",
    ] = "balanced",
) -> StrategyLibraryEntry:
    spec = validate_strategy_spec(
        {
            "family": family,
            "source": "system",
            "hypothesis": (
                f"公开策略先验：{name}。该模板仅用于生成可证伪的研究假设，"
                "不代表历史或未来盈利承诺。"
            ),
            "parameters": parameters,
        },
        family,
    )
    return StrategyLibraryEntry(
        id=id,
        name=name,
        family=family,
        archetype=archetype,
        origin=origin,
        source_url=source_url,
        summary=summary,
        indicators=indicators,
        preferred_regimes=preferred_regimes,
        avoid_regimes=avoid_regimes or [],
        required_data=required_data or ["ohlcv"],
        expected_profile=expected_profile,
        spec=spec.model_dump(mode="json", exclude={"fingerprint"}),
    )


SSRN_TSMOM = "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2089463"
FIDELITY_MACD = (
    "https://www.fidelity.com/learning-center/trading-investing/"
    "technical-analysis/technical-indicator-guide/macd"
)
FIDELITY_RSI = (
    "https://www.fidelity.com/learning-center/trading-investing/"
    "technical-analysis/technical-indicator-guide/RSI"
)
FIDELITY_BOLLINGER = (
    "https://www.fidelity.com/learning-center/trading-investing/"
    "technical-analysis/technical-indicator-guide/bollinger-bands"
)
FIDELITY_INDICATORS = (
    "https://www.fidelity.com/learning-center/trading-investing/"
    "technical-analysis/technical-indicator-guide/overview"
)
TURTLE_RULES = "https://www.turtletrader.com/rules/"
OKX_FUNDING = "https://www.okx.com/en-eu/learn/funding-rate-arbitrage-crypto-derivatives"
CME_OPEN_INTEREST = "https://www.cmegroup.com/education/lessons/open-interest"
BINANCE_FUTURES_DATA = (
    "https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/Introduction"
)
BINANCE_MARKET_STREAMS = (
    "https://developers.binance.com/en/docs/catalog/"
    "core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/market"
)
BINANCE_LIQUIDATION_STREAM = (
    "https://developers.binance.com/docs/derivatives/usds-margined-futures/"
    "websocket-market-streams/Liquidation-Order-Streams"
)


BUILTIN_STRATEGY_LIBRARY: tuple[StrategyLibraryEntry, ...] = (
    # Trend and time-series momentum
    _entry(
        id="builtin_ema_12_26_trend_v2",
        name="EMA 12/26 趋势",
        family=StrategyKind.TREND,
        archetype="moving_average_trend",
        origin="公开双均线趋势规则",
        source_url=FIDELITY_MACD,
        summary="短周期 EMA 位于长周期 EMA 同侧且斜率、效率满足门槛时入场。",
        indicators=["EMA", "ATR", "efficiency_ratio"],
        preferred_regimes=["trend", "normal_volatility"],
        avoid_regimes=["range", "high_noise"],
        parameters={"entry_model": "ema_crossover", "ema_fast": 12, "ema_slow": 26},
    ),
    _entry(
        id="builtin_ema_20_50_trend_v2",
        name="EMA 20/50 趋势",
        family=StrategyKind.TREND,
        archetype="moving_average_trend",
        origin="公开双均线趋势规则",
        source_url=SSRN_TSMOM,
        summary="中速 EMA 趋势与方向效率共同确认，ATR 定义风险。",
        indicators=["EMA", "ATR", "efficiency_ratio"],
        preferred_regimes=["trend", "directional_normal_volatility"],
        avoid_regimes=["range"],
        parameters={"entry_model": "ema_crossover", "ema_fast": 20, "ema_slow": 50},
    ),
    _entry(
        id="builtin_ema_32_96_pullback_v2",
        name="EMA 32/96 回踩趋势",
        family=StrategyKind.TREND,
        archetype="trend_pullback",
        origin="慢速时间序列动量的成本敏感实现",
        source_url=SSRN_TSMOM,
        summary="慢速趋势成立后等待价格重新收复快线，降低追涨杀跌。",
        indicators=["EMA", "ATR", "efficiency_ratio"],
        preferred_regimes=["persistent_trend", "costly_execution"],
        avoid_regimes=["fast_reversal"],
        expected_profile="selective_high_quality",
        parameters={
            "entry_model": "ema_crossover",
            "ema_fast": 32,
            "ema_slow": 96,
            "minimum_efficiency_ratio": "0.30",
            "pullback_mode": "close_reclaim",
            "maximum_entry_extension_atr": "0.5",
        },
    ),
    _entry(
        id="builtin_sma_20_100_trend_v2",
        name="SMA 20/100 趋势",
        family=StrategyKind.TREND,
        archetype="moving_average_trend",
        origin="公开简单移动平均趋势规则",
        source_url=FIDELITY_INDICATORS,
        summary="使用简单移动平均降低 EMA 对短期噪声的敏感度。",
        indicators=["SMA", "ATR"],
        preferred_regimes=["persistent_trend"],
        avoid_regimes=["range"],
        parameters={"entry_model": "sma_crossover", "ema_fast": 20, "ema_slow": 100},
    ),
    _entry(
        id="builtin_sma_50_200_trend_v2",
        name="SMA 50/200 长趋势",
        family=StrategyKind.TREND,
        archetype="long_horizon_trend",
        origin="公开长期均线交叉规则",
        source_url=FIDELITY_INDICATORS,
        summary="低换手长期方向策略，优先用于较长周期数据。",
        indicators=["SMA", "ATR"],
        preferred_regimes=["long_persistent_trend", "high_cost"],
        avoid_regimes=["short_cycle_range"],
        expected_profile="selective_high_quality",
        parameters={"entry_model": "sma_crossover", "ema_fast": 50, "ema_slow": 200},
    ),
    _entry(
        id="builtin_macd_12_26_9_v2",
        name="MACD 12/26/9 趋势动量",
        family=StrategyKind.TREND,
        archetype="macd_momentum",
        origin="Gerald Appel MACD 公开规则",
        source_url=FIDELITY_MACD,
        summary="MACD 与信号线交叉并位于零轴同侧时跟随方向。",
        indicators=["MACD", "ATR"],
        preferred_regimes=["trend", "momentum_expansion"],
        avoid_regimes=["range"],
        parameters={
            "entry_model": "macd_crossover",
            "ema_fast": 12,
            "ema_slow": 26,
            "macd_signal": 9,
        },
    ),
    _entry(
        id="builtin_macd_8_21_5_v2",
        name="MACD 8/21/5 快速动量",
        family=StrategyKind.TREND,
        archetype="macd_momentum",
        origin="MACD 快速参数公开变体",
        source_url=FIDELITY_MACD,
        summary="更快捕捉动量切换，要求更高方向效率以抑制噪声。",
        indicators=["MACD", "ATR", "efficiency_ratio"],
        preferred_regimes=["fast_trend", "volatility_expansion"],
        avoid_regimes=["high_noise"],
        parameters={
            "entry_model": "macd_crossover",
            "ema_fast": 8,
            "ema_slow": 21,
            "macd_signal": 5,
            "minimum_efficiency_ratio": "0.25",
        },
    ),
    _entry(
        id="builtin_roc_20_momentum_v2",
        name="ROC 20 时间序列动量",
        family=StrategyKind.TREND,
        archetype="time_series_momentum",
        origin="公开时间序列动量研究",
        source_url=SSRN_TSMOM,
        summary="20 周期收益方向与慢速均线方向一致时入场。",
        indicators=["ROC", "EMA", "ATR"],
        preferred_regimes=["directional_momentum"],
        avoid_regimes=["mean_reverting"],
        parameters={
            "entry_model": "roc_momentum",
            "roc_period": 20,
            "roc_threshold_percent": "1",
        },
    ),
    _entry(
        id="builtin_roc_60_momentum_v2",
        name="ROC 60 慢速动量",
        family=StrategyKind.TREND,
        archetype="time_series_momentum",
        origin="公开时间序列动量研究",
        source_url=SSRN_TSMOM,
        summary="更长观察期过滤短期反复，适合持续性趋势。",
        indicators=["ROC", "EMA", "ATR"],
        preferred_regimes=["persistent_trend", "high_cost"],
        avoid_regimes=["fast_reversal"],
        parameters={
            "entry_model": "roc_momentum",
            "roc_period": 60,
            "roc_threshold_percent": "3",
            "ema_slow": 100,
        },
    ),
    _entry(
        id="builtin_price_ema_100_v2",
        name="价格/EMA100 长趋势",
        family=StrategyKind.TREND,
        archetype="price_filter_trend",
        origin="公开价格与长期均线方向过滤规则",
        source_url=FIDELITY_INDICATORS,
        summary="价格位于 EMA100 同侧且均线斜率确认后跟随。",
        indicators=["EMA", "ATR"],
        preferred_regimes=["broad_trend"],
        avoid_regimes=["flat_slope"],
        parameters={
            "entry_model": "price_ema_trend",
            "ema_fast": 20,
            "ema_slow": 100,
        },
    ),
    # Mean-reversion and oscillator reversal
    _entry(
        id="builtin_zscore_20_rsi_v2",
        name="Z-Score 20 + RSI 回归",
        family=StrategyKind.MEAN_REVERSION,
        archetype="statistical_mean_reversion",
        origin="公开标准分数回归规则",
        source_url=FIDELITY_RSI,
        summary="价格统计偏离与 RSI 极值同时出现时交易向均值的回归。",
        indicators=["z_score", "RSI", "ATR"],
        preferred_regimes=["range", "normal_volatility"],
        avoid_regimes=["strong_trend"],
        parameters={"entry_model": "zscore_rsi", "mean_window": 20},
    ),
    _entry(
        id="builtin_zscore_50_deep_v2",
        name="Z-Score 50 深度回归",
        family=StrategyKind.MEAN_REVERSION,
        archetype="statistical_mean_reversion",
        origin="公开标准分数回归规则",
        source_url=FIDELITY_RSI,
        summary="等待更深的长期统计偏离，减少低质量交易。",
        indicators=["z_score", "RSI", "ATR"],
        preferred_regimes=["stable_range", "high_cost"],
        avoid_regimes=["breakout"],
        expected_profile="selective_high_quality",
        parameters={
            "entry_model": "zscore_rsi",
            "mean_window": 50,
            "zscore_entry": "2",
            "rsi_oversold": "32",
            "rsi_overbought": "68",
        },
    ),
    _entry(
        id="builtin_bollinger_20_2_rsi_v2",
        name="Bollinger 20/2 + RSI",
        family=StrategyKind.MEAN_REVERSION,
        archetype="bollinger_reversion",
        origin="John Bollinger Bands 与 Wilder RSI 公开组合",
        source_url=FIDELITY_BOLLINGER,
        summary="价格越过波动率带且 RSI 极值确认时等待回归。",
        indicators=["Bollinger_Bands", "RSI", "ATR"],
        preferred_regimes=["range", "low_or_normal_volatility"],
        avoid_regimes=["band_walk"],
        parameters={"entry_model": "bollinger_rsi", "mean_window": 20},
    ),
    _entry(
        id="builtin_bollinger_50_25_rsi_v2",
        name="Bollinger 50/2.5 深度回归",
        family=StrategyKind.MEAN_REVERSION,
        archetype="bollinger_reversion",
        origin="Bollinger Bands 保守参数变体",
        source_url=FIDELITY_BOLLINGER,
        summary="较长均值与更宽波动率带形成低频深度回归信号。",
        indicators=["Bollinger_Bands", "RSI", "ATR"],
        preferred_regimes=["stable_range", "high_cost"],
        avoid_regimes=["trend_acceleration"],
        expected_profile="selective_high_quality",
        parameters={
            "entry_model": "bollinger_rsi",
            "mean_window": 50,
            "band_deviations": "2.5",
            "rsi_oversold": "30",
            "rsi_overbought": "70",
        },
    ),
    _entry(
        id="builtin_bollinger_20_reentry_v1",
        name="Bollinger 20/2 收盘再进入",
        family=StrategyKind.MEAN_REVERSION,
        archetype="bollinger_reentry_confirmation",
        origin="John Bollinger Bands 与 Wilder RSI 的公开再进入确认规则",
        source_url=FIDELITY_BOLLINGER,
        summary="先观察前一根收盘越过波动带且 RSI 极端，再等待当前收盘重新进入带内与 RSI 区间。",
        indicators=["Bollinger_Bands", "RSI", "ATR"],
        preferred_regimes=["stable_range", "failed_breakout"],
        avoid_regimes=["band_walk", "trend_acceleration"],
        expected_profile="selective_high_quality",
        parameters={
            "entry_model": "bollinger_reentry",
            "mean_window": 20,
            "band_deviations": "2",
            "rsi_oversold": "30",
            "rsi_overbought": "70",
        },
    ),
    _entry(
        id="builtin_bollinger_50_reentry_v1",
        name="Bollinger 50/2.5 深度收盘再进入",
        family=StrategyKind.MEAN_REVERSION,
        archetype="bollinger_reentry_confirmation",
        origin="Bollinger Bands 深度偏离后的公开再进入确认规则",
        source_url=FIDELITY_BOLLINGER,
        summary="较长均值与更宽带宽先定义深度极端，再要求下一收盘重新进入带内和 RSI 区间。",
        indicators=["Bollinger_Bands", "RSI", "ATR"],
        preferred_regimes=["stable_range", "deep_failed_breakout", "high_cost"],
        avoid_regimes=["persistent_trend"],
        expected_profile="selective_high_quality",
        parameters={
            "entry_model": "bollinger_reentry",
            "mean_window": 50,
            "band_deviations": "2.5",
            "rsi_oversold": "30",
            "rsi_overbought": "70",
        },
    ),
    _entry(
        id="builtin_rsi_14_30_70_v2",
        name="RSI 14 极值反转",
        family=StrategyKind.MEAN_REVERSION,
        archetype="oscillator_reversal",
        origin="Wilder RSI 传统阈值规则",
        source_url=FIDELITY_RSI,
        summary="RSI 进入传统超卖或超买区时生成反转候选。",
        indicators=["RSI", "ATR"],
        preferred_regimes=["range"],
        avoid_regimes=["strong_trend"],
        parameters={
            "entry_model": "rsi_reversal",
            "rsi_oversold": "30",
            "rsi_overbought": "70",
        },
    ),
    _entry(
        id="builtin_rsi_7_20_80_v2",
        name="RSI 7 深度短周期反转",
        family=StrategyKind.MEAN_REVERSION,
        archetype="oscillator_reversal",
        origin="RSI 短周期保守阈值变体",
        source_url=FIDELITY_RSI,
        summary="短 RSI 仅在更极端阈值触发，适用于快速震荡。",
        indicators=["RSI", "ATR"],
        preferred_regimes=["fast_range"],
        avoid_regimes=["persistent_trend"],
        parameters={
            "entry_model": "rsi_reversal",
            "rsi_period": 7,
            "rsi_oversold": "20",
            "rsi_overbought": "80",
        },
    ),
    _entry(
        id="builtin_stochastic_14_v2",
        name="Stochastic 14 极值反转",
        family=StrategyKind.MEAN_REVERSION,
        archetype="oscillator_reversal",
        origin="公开随机振荡器规则",
        source_url=FIDELITY_INDICATORS,
        summary="%K 进入 20/80 极值区时生成均值回归候选。",
        indicators=["stochastic", "ATR"],
        preferred_regimes=["range"],
        avoid_regimes=["trend"],
        parameters={"entry_model": "stochastic_reversal", "oscillator_period": 14},
    ),
    _entry(
        id="builtin_stochastic_21_v2",
        name="Stochastic 21 慢速反转",
        family=StrategyKind.MEAN_REVERSION,
        archetype="oscillator_reversal",
        origin="随机振荡器慢速参数变体",
        source_url=FIDELITY_INDICATORS,
        summary="更长随机振荡窗口用于较慢震荡状态。",
        indicators=["stochastic", "ATR"],
        preferred_regimes=["slow_range"],
        avoid_regimes=["fast_breakout"],
        parameters={"entry_model": "stochastic_reversal", "oscillator_period": 21},
    ),
    _entry(
        id="builtin_cci_20_v2",
        name="CCI 20 极值回归",
        family=StrategyKind.MEAN_REVERSION,
        archetype="commodity_channel_reversal",
        origin="Donald Lambert CCI 公开规则",
        source_url=FIDELITY_INDICATORS,
        summary="CCI 超过正负 100 时生成回归候选。",
        indicators=["CCI", "ATR"],
        preferred_regimes=["range", "cyclical"],
        avoid_regimes=["strong_trend"],
        parameters={"entry_model": "cci_reversal", "oscillator_period": 20},
    ),
    _entry(
        id="builtin_williams_14_v2",
        name="Williams %R 14 反转",
        family=StrategyKind.MEAN_REVERSION,
        archetype="range_position_reversal",
        origin="Larry Williams %R 公开规则",
        source_url=FIDELITY_INDICATORS,
        summary="收盘位于观察区间极端位置时生成反向候选。",
        indicators=["Williams_%R", "ATR"],
        preferred_regimes=["range"],
        avoid_regimes=["breakout"],
        parameters={"entry_model": "williams_reversal", "oscillator_period": 14},
    ),
    _entry(
        id="builtin_vwap_20_reversion_v2",
        name="VWAP 20 偏离回归",
        family=StrategyKind.MEAN_REVERSION,
        archetype="volume_weighted_reversion",
        origin="公开成交量加权均价回归规则",
        source_url=FIDELITY_INDICATORS,
        summary="价格偏离滚动 VWAP 超过一个 ATR 时等待回归。",
        indicators=["VWAP", "ATR"],
        preferred_regimes=["liquid_range"],
        avoid_regimes=["volume_breakout"],
        parameters={"entry_model": "vwap_reversion", "mean_window": 20},
    ),
    # Price and volatility breakouts
    _entry(
        id="builtin_turtle_20_v2",
        name="Turtle / Donchian 20 突破",
        family=StrategyKind.BREAKOUT,
        archetype="price_channel_breakout",
        origin="公开 Turtle System 1 规则",
        source_url=TURTLE_RULES,
        summary="突破过去 20 根已收盘 K 线区间并由成交量确认。",
        indicators=["Donchian_channel", "ATR", "volume_ratio"],
        preferred_regimes=["breakout", "volatility_expansion"],
        avoid_regimes=["range"],
        expected_profile="high_payoff_lower_win_rate",
        parameters={"entry_model": "donchian_volume", "lookback": 20},
    ),
    _entry(
        id="builtin_turtle_55_v2",
        name="Turtle / Donchian 55 突破",
        family=StrategyKind.BREAKOUT,
        archetype="price_channel_breakout",
        origin="公开 Turtle System 2 规则",
        source_url=TURTLE_RULES,
        summary="使用 55 周期区间过滤假突破并降低换手。",
        indicators=["Donchian_channel", "ATR", "volume_ratio"],
        preferred_regimes=["persistent_breakout", "high_noise"],
        avoid_regimes=["tight_range"],
        expected_profile="selective_high_quality",
        parameters={
            "entry_model": "donchian_volume",
            "lookback": 55,
            "minimum_volume_ratio": "1.2",
        },
    ),
    _entry(
        id="builtin_donchian_40_retest_v1",
        name="Donchian 40 Breakout Retest",
        family=StrategyKind.BREAKOUT,
        archetype="breakout_retest_continuation",
        origin="Public Donchian breakout followed by causal level-retest confirmation",
        source_url=TURTLE_RULES,
        summary=(
            "Require a volume-confirmed 40-bar breakout on a prior closed candle, then enter "
            "only when a later closed candle retests and reclaims the breakout level."
        ),
        indicators=["Donchian_channel", "ATR", "volume_ratio", "price_retest"],
        preferred_regimes=["breakout_continuation", "persistent_trend"],
        avoid_regimes=["one_bar_false_breakout", "range"],
        expected_profile="selective_high_quality",
        parameters={
            "entry_model": "donchian_retest",
            "lookback": 40,
            "minimum_volume_ratio": "1.15",
            "retest_window": 6,
            "retest_tolerance_atr": "0.25",
            "stop_atr": "1.25",
            "first_target_reward_risk": "1.5",
            "first_target_fraction": "0.5",
            "second_target_extension": "2.2",
            "maximum_holding_bars": 80,
        },
    ),
    _entry(
        id="builtin_donchian_80_retest_v1",
        name="Donchian 80 Selective Breakout Retest",
        family=StrategyKind.BREAKOUT,
        archetype="breakout_retest_continuation",
        origin="Long-horizon Donchian breakout followed by causal level-retest confirmation",
        source_url=TURTLE_RULES,
        summary=(
            "Use an 80-bar channel and stricter breakout volume, then wait up to ten closed "
            "bars for a narrow ATR-scaled reclaim before entering."
        ),
        indicators=["Donchian_channel", "ATR", "volume_ratio", "price_retest"],
        preferred_regimes=["structural_breakout", "long_persistent_trend"],
        avoid_regimes=["thin_liquidity", "fast_reversal"],
        expected_profile="selective_high_quality",
        parameters={
            "entry_model": "donchian_retest",
            "lookback": 80,
            "minimum_volume_ratio": "1.25",
            "retest_window": 10,
            "retest_tolerance_atr": "0.18",
            "stop_atr": "1.35",
            "first_target_reward_risk": "1.6",
            "first_target_fraction": "0.45",
            "second_target_extension": "2.4",
            "maximum_holding_bars": 120,
        },
    ),
    _entry(
        id="builtin_donchian_100_v2",
        name="Donchian 100 长周期突破",
        family=StrategyKind.BREAKOUT,
        archetype="long_horizon_breakout",
        origin="Donchian 长周期公开变体",
        source_url=TURTLE_RULES,
        summary="极低换手长区间突破，重点捕捉大级别行情。",
        indicators=["Donchian_channel", "ATR", "volume_ratio"],
        preferred_regimes=["structural_breakout", "high_cost"],
        avoid_regimes=["short_range"],
        expected_profile="high_payoff_lower_win_rate",
        parameters={
            "entry_model": "donchian_volume",
            "lookback": 100,
            "minimum_volume_ratio": "1.1",
        },
    ),
    _entry(
        id="builtin_high_low_20_v2",
        name="20 周期纯价格突破",
        family=StrategyKind.BREAKOUT,
        archetype="price_action_breakout",
        origin="公开区间高低点突破规则",
        source_url=FIDELITY_INDICATORS,
        summary="不要求成交量确认，用于交易量数据不稳定但价格可靠的市场。",
        indicators=["rolling_high_low", "ATR"],
        preferred_regimes=["price_discovery"],
        avoid_regimes=["false_breakout"],
        expected_profile="high_payoff_lower_win_rate",
        parameters={"entry_model": "high_low_breakout", "lookback": 20},
    ),
    _entry(
        id="builtin_high_low_60_v2",
        name="60 周期纯价格突破",
        family=StrategyKind.BREAKOUT,
        archetype="price_action_breakout",
        origin="公开区间高低点突破规则",
        source_url=FIDELITY_INDICATORS,
        summary="更长区间降低噪声，保留价格发现行情。",
        indicators=["rolling_high_low", "ATR"],
        preferred_regimes=["persistent_price_discovery"],
        avoid_regimes=["range"],
        parameters={"entry_model": "high_low_breakout", "lookback": 60},
    ),
    _entry(
        id="builtin_bollinger_breakout_20_v2",
        name="Bollinger 20/2 波动率突破",
        family=StrategyKind.BREAKOUT,
        archetype="volatility_band_breakout",
        origin="Bollinger Bands 波动率扩张规则",
        source_url=FIDELITY_BOLLINGER,
        summary="价格越过前一周期波动率带并获得成交量确认。",
        indicators=["Bollinger_Bands", "volume_ratio", "ATR"],
        preferred_regimes=["volatility_expansion"],
        avoid_regimes=["low_volume_range"],
        expected_profile="high_payoff_lower_win_rate",
        parameters={"entry_model": "bollinger_expansion", "lookback": 20},
    ),
    _entry(
        id="builtin_bollinger_breakout_50_v2",
        name="Bollinger 50/2.5 结构突破",
        family=StrategyKind.BREAKOUT,
        archetype="volatility_band_breakout",
        origin="Bollinger Bands 长周期参数变体",
        source_url=FIDELITY_BOLLINGER,
        summary="突破较长且更宽的波动率带，目标是降低假信号。",
        indicators=["Bollinger_Bands", "volume_ratio", "ATR"],
        preferred_regimes=["structural_volatility_expansion"],
        avoid_regimes=["short_noise"],
        expected_profile="selective_high_quality",
        parameters={
            "entry_model": "bollinger_expansion",
            "lookback": 50,
            "band_deviations": "2.5",
        },
    ),
    _entry(
        id="builtin_atr_range_15_v2",
        name="ATR 1.5 倍实体扩张",
        family=StrategyKind.BREAKOUT,
        archetype="range_expansion",
        origin="Wilder ATR 波动率扩张规则",
        source_url=FIDELITY_INDICATORS,
        summary="最新 K 线方向明确且全幅超过历史 ATR 的 1.5 倍时跟随。",
        indicators=["ATR", "volume_ratio"],
        preferred_regimes=["shock_expansion", "momentum_start"],
        avoid_regimes=["illiquid_spike"],
        expected_profile="high_payoff_lower_win_rate",
        parameters={
            "entry_model": "atr_expansion",
            "minimum_range_atr": "1.5",
        },
    ),
    _entry(
        id="builtin_atr_range_20_v2",
        name="ATR 2.0 倍选择性扩张",
        family=StrategyKind.BREAKOUT,
        archetype="range_expansion",
        origin="Wilder ATR 波动率扩张保守变体",
        source_url=FIDELITY_INDICATORS,
        summary="仅跟随超过两倍 ATR 且成交量确认的方向扩张。",
        indicators=["ATR", "volume_ratio"],
        preferred_regimes=["large_shock_expansion"],
        avoid_regimes=["thin_liquidity"],
        expected_profile="selective_high_quality",
        parameters={
            "entry_model": "atr_expansion",
            "minimum_range_atr": "2",
            "minimum_volume_ratio": "1.25",
        },
    ),
    _entry(
        id="builtin_bollinger_squeeze_release_v2",
        name="Bollinger 带宽释放",
        family=StrategyKind.BREAKOUT,
        archetype="volatility_squeeze_release",
        origin="Bollinger Bands 收缩后扩张规则",
        source_url=FIDELITY_BOLLINGER,
        summary="价格越过波动率带且带宽达到最低水平时交易扩张方向。",
        indicators=["Bollinger_bandwidth", "volume_ratio", "ATR"],
        preferred_regimes=["squeeze_release"],
        avoid_regimes=["already_extended"],
        expected_profile="high_payoff_lower_win_rate",
        parameters={
            "entry_model": "bollinger_expansion",
            "lookback": 20,
            "minimum_bandwidth": "0.03",
            "minimum_volume_ratio": "1.2",
        },
    ),
    # Crypto-native, directional funding hypotheses. These are deliberately
    # not labelled market-neutral arbitrage.
    _entry(
        id="builtin_funding_contrarian_10bp_v3",
        name="资金费拥挤反转 1bp",
        family=StrategyKind.MEAN_REVERSION,
        archetype="funding_crowding_reversal",
        origin="永续合约资金费多空拥挤的公开机制假设",
        source_url=OKX_FUNDING,
        summary="资金费绝对值达到 1bp 时逆拥挤方向交易，并使用 RSI 避免极端追单。",
        indicators=["funding_rate", "RSI", "ATR"],
        preferred_regimes=["crowded_perpetual", "stable_range"],
        avoid_regimes=["basis_dislocation", "strong_trend"],
        required_data=["ohlcv", "funding_rate"],
        expected_profile="selective_high_quality",
        parameters={
            "entry_model": "funding_contrarian",
            "funding_rate_threshold": "0.0001",
        },
    ),
    _entry(
        id="builtin_funding_contrarian_30bp_v3",
        name="资金费拥挤反转 3bp",
        family=StrategyKind.MEAN_REVERSION,
        archetype="funding_crowding_reversal",
        origin="永续合约极端资金费拥挤的保守变体",
        source_url=OKX_FUNDING,
        summary="仅在资金费达到 3bp 的极端拥挤时产生方向性反转假设。",
        indicators=["funding_rate", "RSI", "ATR"],
        preferred_regimes=["extreme_crowding"],
        avoid_regimes=["structural_trend"],
        required_data=["ohlcv", "funding_rate"],
        expected_profile="selective_high_quality",
        parameters={
            "entry_model": "funding_contrarian",
            "funding_rate_threshold": "0.0003",
        },
    ),
    _entry(
        id="builtin_funding_aware_roc_24_v3",
        name="资金费成本约束 ROC 动量",
        family=StrategyKind.TREND,
        archetype="funding_aware_momentum",
        origin="时间序列动量叠加永续资金成本约束",
        source_url=OKX_FUNDING,
        summary="保留价格动量方向，但拒绝支付超过 2bp 的单期资金费。",
        indicators=["ROC", "EMA", "funding_rate", "ATR"],
        preferred_regimes=["persistent_trend", "balanced_funding"],
        avoid_regimes=["crowded_direction"],
        required_data=["ohlcv", "funding_rate"],
        parameters={
            "entry_model": "roc_momentum",
            "roc_period": 24,
            "roc_threshold_percent": "1",
            "funding_mode": "avoid_paying_extreme",
            "maximum_paying_funding_rate": "0.0002",
        },
    ),
    _entry(
        id="builtin_oi_confirmed_roc_24_v3",
        name="持仓量扩张确认 ROC 动量",
        family=StrategyKind.TREND,
        archetype="open_interest_confirmed_momentum",
        origin="公开期货持仓量趋势确认规则",
        source_url=CME_OPEN_INTEREST,
        summary="价格 ROC 形成方向后，仅在过去 24 小时持仓量同步扩张时允许交易。",
        indicators=["ROC", "EMA", "open_interest_change_24h", "ATR"],
        preferred_regimes=["persistent_trend", "participation_expansion"],
        avoid_regimes=["position_unwinding", "thin_market"],
        required_data=["ohlcv", "open_interest"],
        expected_profile="selective_high_quality",
        parameters={
            "entry_model": "roc_momentum",
            "roc_period": 24,
            "roc_threshold_percent": "1",
            "open_interest_mode": "confirm_expansion",
            "minimum_open_interest_change_24h": "0",
        },
    ),
    _entry(
        id="builtin_oi_confirmed_donchian_40_v3",
        name="持仓量扩张确认 Donchian 突破",
        family=StrategyKind.BREAKOUT,
        archetype="open_interest_confirmed_breakout",
        origin="公开期货持仓量趋势确认规则叠加 Donchian 突破",
        source_url=CME_OPEN_INTEREST,
        summary="40 周期价格突破必须同时具有成交量和至少 2% 的持仓量扩张。",
        indicators=[
            "Donchian",
            "volume_ratio",
            "open_interest_change_24h",
            "ATR",
        ],
        preferred_regimes=["breakout", "participation_expansion"],
        avoid_regimes=["position_unwinding", "false_breakout"],
        required_data=["ohlcv", "open_interest"],
        expected_profile="high_payoff_lower_win_rate",
        parameters={
            "entry_model": "donchian_volume",
            "lookback": 40,
            "minimum_volume_ratio": "1.2",
            "open_interest_mode": "confirm_expansion",
            "minimum_open_interest_change_24h": "0.02",
        },
    ),
    _entry(
        id="builtin_positioning_contrarian_15_v1",
        name="Global Long/Short 1.5 Crowding Fade",
        family=StrategyKind.MEAN_REVERSION,
        archetype="positioning_crowding_reversal",
        origin="Public global-account long/short ratio crowding hypothesis",
        source_url=BINANCE_FUTURES_DATA,
        summary=(
            "Fade only a causally observed global long/short ratio above 1.5 "
            "or below its reciprocal."
        ),
        indicators=["global_long_short_ratio", "ATR"],
        preferred_regimes=["crowded_range", "positioning_extreme"],
        avoid_regimes=["structural_trend"],
        required_data=["ohlcv", "positioning"],
        expected_profile="selective_high_quality",
        parameters={
            "entry_model": "positioning_contrarian",
            "positioning_ratio_threshold": "1.5",
        },
    ),
    _entry(
        id="builtin_positioning_contrarian_20_v1",
        name="Global Long/Short 2.0 Deep Crowding Fade",
        family=StrategyKind.MEAN_REVERSION,
        archetype="positioning_crowding_reversal",
        origin="Conservative public global-account long/short ratio hypothesis",
        source_url=BINANCE_FUTURES_DATA,
        summary="Trade only a deeper 2.0/0.5 positioning imbalance to reduce turnover.",
        indicators=["global_long_short_ratio", "ATR"],
        preferred_regimes=["extreme_crowding", "stable_range"],
        avoid_regimes=["price_discovery"],
        required_data=["ohlcv", "positioning"],
        expected_profile="selective_high_quality",
        parameters={
            "entry_model": "positioning_contrarian",
            "positioning_ratio_threshold": "2.0",
        },
    ),
    _entry(
        id="builtin_basis_contrarian_15bp_v1",
        name="Perpetual Basis 15bp Crowding Fade",
        family=StrategyKind.MEAN_REVERSION,
        archetype="basis_crowding_reversal",
        origin="Public perpetual mark/index basis crowding hypothesis",
        source_url=BINANCE_FUTURES_DATA,
        summary="Fade a causal mark/index basis dislocation beyond positive or negative 15bp.",
        indicators=["mark_index_basis", "ATR"],
        preferred_regimes=["basis_dislocation", "liquid_range"],
        avoid_regimes=["structural_repricing"],
        required_data=["ohlcv", "basis"],
        expected_profile="selective_high_quality",
        parameters={
            "entry_model": "basis_contrarian",
            "basis_rate_threshold": "0.0015",
        },
    ),
    _entry(
        id="builtin_basis_contrarian_30bp_v1",
        name="Perpetual Basis 30bp Deep Crowding Fade",
        family=StrategyKind.MEAN_REVERSION,
        archetype="basis_crowding_reversal",
        origin="Conservative public perpetual basis dislocation hypothesis",
        source_url=BINANCE_FUTURES_DATA,
        summary="Trade only mark/index basis dislocations beyond 30bp.",
        indicators=["mark_index_basis", "ATR"],
        preferred_regimes=["extreme_basis_dislocation"],
        avoid_regimes=["structural_repricing"],
        required_data=["ohlcv", "basis"],
        expected_profile="selective_high_quality",
        parameters={
            "entry_model": "basis_contrarian",
            "basis_rate_threshold": "0.003",
        },
    ),
    _entry(
        id="builtin_flow_confirmed_roc_24_v1",
        name="Closed-Bar Taker-Flow Confirmed ROC",
        family=StrategyKind.TREND,
        archetype="order_flow_confirmed_momentum",
        origin="Public aggregate-taker-flow directional confirmation hypothesis",
        source_url=BINANCE_MARKET_STREAMS,
        summary=(
            "Allow 24-bar price momentum only after the completed bar's taker "
            "imbalance confirms the same direction."
        ),
        indicators=["ROC", "EMA", "taker_flow_imbalance", "ATR"],
        preferred_regimes=["participation_expansion", "directional_momentum"],
        avoid_regimes=["flow_divergence"],
        required_data=["ohlcv", "order_flow"],
        expected_profile="selective_high_quality",
        parameters={
            "entry_model": "roc_momentum",
            "roc_period": 24,
            "roc_threshold_percent": "1",
            "order_flow_mode": "confirm_direction",
            "minimum_order_flow_imbalance": "0.05",
        },
    ),
    _entry(
        id="builtin_flow_confirmed_donchian_40_v1",
        name="Closed-Bar Taker-Flow Confirmed Donchian",
        family=StrategyKind.BREAKOUT,
        archetype="order_flow_confirmed_breakout",
        origin="Public aggregate-taker-flow breakout confirmation hypothesis",
        source_url=BINANCE_MARKET_STREAMS,
        summary=(
            "Require a 40-bar price breakout, volume expansion and same-direction "
            "closed-bar taker imbalance."
        ),
        indicators=["Donchian", "volume_ratio", "taker_flow_imbalance", "ATR"],
        preferred_regimes=["breakout", "aggressive_flow_confirmation"],
        avoid_regimes=["false_breakout", "flow_divergence"],
        required_data=["ohlcv", "order_flow"],
        expected_profile="high_payoff_lower_win_rate",
        parameters={
            "entry_model": "donchian_volume",
            "lookback": 40,
            "minimum_volume_ratio": "1.2",
            "order_flow_mode": "confirm_direction",
            "minimum_order_flow_imbalance": "0.08",
        },
    ),
    # Selective composite priors. Each combines one directional hypothesis with
    # a non-directional ADX state gate; ADX never votes on direction itself.
    _entry(
        id="builtin_adx_ema_20_50_pullback_v1",
        name="ADX 确认 EMA 20/50 回踩趋势",
        family=StrategyKind.TREND,
        archetype="adx_pullback_trend",
        origin="公开 EMA 回踩规则叠加 Wilder ADX 状态过滤",
        source_url=FIDELITY_INDICATORS,
        summary="EMA 趋势成立后等待快线回踩收复，并要求 ADX 至少为 25。",
        indicators=["EMA", "ADX", "ATR", "efficiency_ratio"],
        preferred_regimes=["persistent_trend"],
        avoid_regimes=["range", "transition"],
        expected_profile="selective_high_quality",
        parameters={
            "entry_model": "ema_crossover",
            "ema_fast": 20,
            "ema_slow": 50,
            "pullback_mode": "close_reclaim",
            "minimum_efficiency_ratio": "0.25",
            "trend_strength_mode": "require_trend",
            "adx_threshold": "25",
        },
    ),
    _entry(
        id="builtin_adx_macd_12_26_9_v1",
        name="ADX 确认 MACD 12/26/9",
        family=StrategyKind.TREND,
        archetype="adx_confirmed_momentum",
        origin="公开 MACD 动量规则叠加 Wilder ADX 状态过滤",
        source_url=FIDELITY_MACD,
        summary="MACD 位于零轴同侧且 ADX 至少为 25 时跟随趋势。",
        indicators=["MACD", "ADX", "ATR"],
        preferred_regimes=["persistent_trend", "momentum_expansion"],
        avoid_regimes=["range"],
        expected_profile="selective_high_quality",
        parameters={
            "entry_model": "macd_crossover",
            "ema_fast": 12,
            "ema_slow": 26,
            "macd_signal": 9,
            "trend_strength_mode": "require_trend",
            "adx_threshold": "25",
        },
    ),
    _entry(
        id="builtin_adx_roc_60_v1",
        name="ADX 30 确认 ROC 60 慢动量",
        family=StrategyKind.TREND,
        archetype="adx_confirmed_momentum",
        origin="公开时间序列动量叠加 Wilder ADX 强趋势过滤",
        source_url=SSRN_TSMOM,
        summary="仅在 ADX 至少为 30 的强趋势中交易 60 周期 ROC 方向。",
        indicators=["ROC", "EMA", "ADX", "ATR"],
        preferred_regimes=["strong_persistent_trend"],
        avoid_regimes=["range", "weak_trend"],
        expected_profile="selective_high_quality",
        parameters={
            "entry_model": "roc_momentum",
            "roc_period": 60,
            "roc_threshold_percent": "3",
            "ema_slow": 100,
            "trend_strength_mode": "require_trend",
            "adx_threshold": "30",
        },
    ),
    _entry(
        id="builtin_adx_range_zscore_50_v1",
        name="ADX 20 震荡过滤 Z-Score 50",
        family=StrategyKind.MEAN_REVERSION,
        archetype="adx_range_reversion",
        origin="公开 Z-Score 回归叠加 Wilder ADX 震荡过滤",
        source_url=FIDELITY_RSI,
        summary="只在 ADX 不高于 20 的震荡状态交易深度统计偏离。",
        indicators=["z_score", "RSI", "ADX", "ATR"],
        preferred_regimes=["stable_range"],
        avoid_regimes=["trend", "breakout"],
        expected_profile="selective_high_quality",
        parameters={
            "entry_model": "zscore_rsi",
            "mean_window": 50,
            "zscore_entry": "2",
            "rsi_oversold": "30",
            "rsi_overbought": "70",
            "trend_strength_mode": "require_range",
            "adx_threshold": "20",
        },
    ),
    _entry(
        id="builtin_adx_range_bollinger_20_v1",
        name="ADX 18 震荡过滤 Bollinger 回归",
        family=StrategyKind.MEAN_REVERSION,
        archetype="adx_range_reversion",
        origin="公开 Bollinger 回归叠加 Wilder ADX 震荡过滤",
        source_url=FIDELITY_BOLLINGER,
        summary="只在 ADX 不高于 18 时交易 Bollinger 带外与 RSI 极值的共同反转。",
        indicators=["Bollinger_Bands", "RSI", "ADX", "ATR"],
        preferred_regimes=["quiet_range"],
        avoid_regimes=["band_walk", "trend"],
        expected_profile="selective_high_quality",
        parameters={
            "entry_model": "bollinger_rsi",
            "mean_window": 20,
            "band_deviations": "2",
            "rsi_oversold": "30",
            "rsi_overbought": "70",
            "trend_strength_mode": "require_range",
            "adx_threshold": "18",
        },
    ),
    _entry(
        id="builtin_adx_range_vwap_30_v1",
        name="ADX 20 震荡过滤 VWAP 30 回归",
        family=StrategyKind.MEAN_REVERSION,
        archetype="adx_range_reversion",
        origin="公开滚动 VWAP 回归叠加 Wilder ADX 震荡过滤",
        source_url=FIDELITY_INDICATORS,
        summary="只在低 ADX 状态交易超过 1.25 ATR 的滚动 VWAP 偏离。",
        indicators=["VWAP", "ADX", "ATR"],
        preferred_regimes=["liquid_stable_range"],
        avoid_regimes=["volume_breakout", "trend"],
        expected_profile="selective_high_quality",
        parameters={
            "entry_model": "vwap_reversion",
            "mean_window": 30,
            "vwap_deviation_atr": "1.25",
            "trend_strength_mode": "require_range",
            "adx_threshold": "20",
        },
    ),
    _entry(
        id="builtin_adx_donchian_55_v1",
        name="ADX 25 确认 Donchian 55",
        family=StrategyKind.BREAKOUT,
        archetype="adx_confirmed_breakout",
        origin="公开 Donchian 突破叠加 Wilder ADX 状态过滤",
        source_url=TURTLE_RULES,
        summary="55 周期突破需要成交量至少为均值 1.3 倍且 ADX 至少为 25。",
        indicators=["Donchian_channel", "volume_ratio", "ADX", "ATR"],
        preferred_regimes=["persistent_breakout"],
        avoid_regimes=["range", "false_breakout"],
        expected_profile="selective_high_quality",
        parameters={
            "entry_model": "donchian_volume",
            "lookback": 55,
            "minimum_volume_ratio": "1.3",
            "trend_strength_mode": "require_trend",
            "adx_threshold": "25",
        },
    ),
    _entry(
        id="builtin_multifactor_roc_24_v1",
        name="多因子确认 ROC 24",
        family=StrategyKind.TREND,
        archetype="multi_factor_crypto_momentum",
        origin="公开时间序列动量、ADX、持仓量、资金费与闭合订单流的因果组合",
        source_url=BINANCE_FUTURES_DATA,
        summary=(
            "ROC 方向必须同时通过 ADX 强度、持仓量扩张、闭合 K 线 taker flow "
            "以及极端资金成本过滤；任一历史字段缺失即不交易。"
        ),
        indicators=[
            "ROC",
            "EMA",
            "ADX",
            "open_interest_change_24h",
            "funding_rate",
            "taker_flow_imbalance",
            "ATR",
        ],
        preferred_regimes=["persistent_trend", "participation_expansion"],
        avoid_regimes=["crowded_direction", "flow_divergence"],
        required_data=["ohlcv", "open_interest", "funding_rate", "order_flow"],
        expected_profile="selective_high_quality",
        parameters={
            "entry_model": "roc_momentum",
            "roc_period": 24,
            "roc_threshold_percent": "1.5",
            "trend_strength_mode": "require_trend",
            "adx_threshold": "25",
            "open_interest_mode": "confirm_expansion",
            "minimum_open_interest_change_24h": "0.02",
            "funding_mode": "avoid_paying_extreme",
            "maximum_paying_funding_rate": "0.0002",
            "order_flow_mode": "confirm_direction",
            "minimum_order_flow_imbalance": "0.08",
        },
    ),
    # Orthogonal crypto-native hypotheses. These are pre-declared causal
    # combinations, not post-backtest parameter sweeps. Each one is counted as
    # an independent strategy in Campaign-level FDR and PBO validation.
    _entry(
        id="builtin_flow_adx_macd_12_26_v1",
        name="Flow-Confirmed ADX MACD",
        family=StrategyKind.TREND,
        archetype="flow_confirmed_trend_acceleration",
        origin="MACD direction combined with closed-bar taker-flow and ADX state evidence",
        source_url=BINANCE_MARKET_STREAMS,
        summary="Trade MACD direction only when trend strength and completed-bar taker flow agree.",
        indicators=["MACD", "ADX", "taker_flow_imbalance", "ATR"],
        preferred_regimes=["trend_acceleration", "aggressive_flow_confirmation"],
        avoid_regimes=["range", "flow_divergence"],
        required_data=["ohlcv", "order_flow"],
        expected_profile="selective_high_quality",
        parameters={
            "entry_model": "macd_crossover",
            "ema_fast": 12,
            "ema_slow": 26,
            "macd_signal": 9,
            "trend_strength_mode": "require_trend",
            "adx_threshold": "22",
            "order_flow_mode": "confirm_direction",
            "minimum_order_flow_imbalance": "0.04",
        },
    ),
    _entry(
        id="builtin_oi_basis_roc_48_v1",
        name="OI-Confirmed Basis-Aware ROC 48",
        family=StrategyKind.TREND,
        archetype="participation_basis_aware_momentum",
        origin="Time-series momentum combined with causal participation and futures-basis state",
        source_url=BINANCE_FUTURES_DATA,
        summary=(
            "Require expanding open interest while rejecting momentum entries "
            "into an extreme directional basis."
        ),
        indicators=["ROC", "EMA", "open_interest_change_24h", "mark_index_basis", "ATR"],
        preferred_regimes=["participation_expansion", "balanced_basis"],
        avoid_regimes=["position_unwinding", "crowded_direction"],
        required_data=["ohlcv", "open_interest", "basis"],
        expected_profile="selective_high_quality",
        parameters={
            "entry_model": "roc_momentum",
            "roc_period": 48,
            "roc_threshold_percent": "1.2",
            "open_interest_mode": "confirm_expansion",
            "minimum_open_interest_change_24h": "0",
            "basis_mode": "avoid_crowded_direction",
            "maximum_directional_basis_rate": "0.002",
        },
    ),
    _entry(
        id="builtin_funding_flow_roc_36_v1",
        name="Funding-Cost Flow-Confirmed ROC 36",
        family=StrategyKind.TREND,
        archetype="cost_flow_confirmed_momentum",
        origin=(
            "Time-series momentum combined with perpetual funding cost and closed-bar taker flow"
        ),
        source_url=OKX_FUNDING,
        summary=(
            "Follow medium-horizon momentum only when flow agrees and the position "
            "is not paying extreme funding."
        ),
        indicators=["ROC", "EMA", "funding_rate", "taker_flow_imbalance", "ATR"],
        preferred_regimes=["directional_momentum", "balanced_funding"],
        avoid_regimes=["funding_crowding", "flow_divergence"],
        required_data=["ohlcv", "funding_rate", "order_flow"],
        expected_profile="selective_high_quality",
        parameters={
            "entry_model": "roc_momentum",
            "roc_period": 36,
            "roc_threshold_percent": "1",
            "funding_mode": "avoid_paying_extreme",
            "maximum_paying_funding_rate": "0.0003",
            "order_flow_mode": "confirm_direction",
            "minimum_order_flow_imbalance": "0.04",
        },
    ),
    _entry(
        id="builtin_oi_flow_donchian_30_v1",
        name="Participation and Flow Donchian 30",
        family=StrategyKind.BREAKOUT,
        archetype="participation_flow_breakout",
        origin=(
            "Donchian breakout confirmed by open-interest expansion and completed-bar taker flow"
        ),
        source_url=BINANCE_MARKET_STREAMS,
        summary=(
            "Accept a liquid 30-bar breakout only when both derivatives "
            "participation and aggressive flow confirm it."
        ),
        indicators=[
            "Donchian",
            "volume_ratio",
            "open_interest_change_24h",
            "taker_flow_imbalance",
            "ATR",
        ],
        preferred_regimes=["breakout", "participation_expansion"],
        avoid_regimes=["position_unwinding", "flow_divergence"],
        required_data=["ohlcv", "open_interest", "order_flow"],
        expected_profile="selective_high_quality",
        parameters={
            "entry_model": "donchian_volume",
            "lookback": 30,
            "minimum_volume_ratio": "1.05",
            "open_interest_mode": "confirm_expansion",
            "minimum_open_interest_change_24h": "0",
            "order_flow_mode": "confirm_direction",
            "minimum_order_flow_imbalance": "0.04",
        },
    ),
    _entry(
        id="builtin_derivatives_context_donchian_50_v1",
        name="Full Derivatives-Context Donchian 50",
        family=StrategyKind.BREAKOUT,
        archetype="derivatives_context_breakout",
        origin=(
            "Donchian breakout combined with ADX, funding, open interest, basis "
            "and taker-flow evidence"
        ),
        source_url=BINANCE_FUTURES_DATA,
        summary=(
            "A deliberately selective breakout that requires trend, participation "
            "and flow while avoiding crowded carry."
        ),
        indicators=[
            "Donchian",
            "ADX",
            "funding_rate",
            "open_interest_change_24h",
            "mark_index_basis",
            "taker_flow_imbalance",
            "ATR",
        ],
        preferred_regimes=["persistent_breakout", "participation_expansion"],
        avoid_regimes=["crowded_direction", "false_breakout"],
        required_data=["ohlcv", "funding_rate", "open_interest", "basis", "order_flow"],
        expected_profile="selective_high_quality",
        parameters={
            "entry_model": "donchian_volume",
            "lookback": 50,
            "minimum_volume_ratio": "1.1",
            "trend_strength_mode": "require_trend",
            "adx_threshold": "22",
            "funding_mode": "avoid_paying_extreme",
            "maximum_paying_funding_rate": "0.0003",
            "open_interest_mode": "confirm_expansion",
            "minimum_open_interest_change_24h": "0",
            "basis_mode": "avoid_crowded_direction",
            "maximum_directional_basis_rate": "0.0025",
            "order_flow_mode": "confirm_direction",
            "minimum_order_flow_imbalance": "0.04",
        },
    ),
    _entry(
        id="builtin_positioning_flow_reversal_15_v1",
        name="Positioning Crowding with Reversal Flow",
        family=StrategyKind.MEAN_REVERSION,
        archetype="positioning_flow_reversal",
        origin=(
            "Global-account positioning crowding combined with low-ADX and "
            "reversal-direction taker flow"
        ),
        source_url=BINANCE_FUTURES_DATA,
        summary=(
            "Fade a 1.5 positioning imbalance only in a range and after closed-bar "
            "flow confirms the reversal direction."
        ),
        indicators=["global_long_short_ratio", "ADX", "taker_flow_imbalance", "ATR"],
        preferred_regimes=["crowded_range", "flow_reversal"],
        avoid_regimes=["structural_trend", "flow_continuation"],
        required_data=["ohlcv", "positioning", "order_flow"],
        expected_profile="selective_high_quality",
        parameters={
            "entry_model": "positioning_contrarian",
            "positioning_ratio_threshold": "1.5",
            "trend_strength_mode": "require_range",
            "adx_threshold": "22",
            "order_flow_mode": "confirm_direction",
            "minimum_order_flow_imbalance": "0.04",
        },
    ),
    _entry(
        id="builtin_basis_flow_reversal_10bp_v1",
        name="Basis Dislocation with Reversal Flow",
        family=StrategyKind.MEAN_REVERSION,
        archetype="basis_flow_reversal",
        origin=(
            "Perpetual basis dislocation combined with low-ADX and reversal-direction taker flow"
        ),
        source_url=BINANCE_FUTURES_DATA,
        summary=(
            "Fade a causal 10bp mark-index dislocation only when the market is "
            "range-bound and flow turns against crowding."
        ),
        indicators=["mark_index_basis", "ADX", "taker_flow_imbalance", "ATR"],
        preferred_regimes=["basis_dislocation", "flow_reversal"],
        avoid_regimes=["structural_repricing", "trend"],
        required_data=["ohlcv", "basis", "order_flow"],
        expected_profile="selective_high_quality",
        parameters={
            "entry_model": "basis_contrarian",
            "basis_rate_threshold": "0.001",
            "trend_strength_mode": "require_range",
            "adx_threshold": "22",
            "order_flow_mode": "confirm_direction",
            "minimum_order_flow_imbalance": "0.04",
        },
    ),
    _entry(
        id="builtin_funding_basis_reversal_10bp_v1",
        name="Funding Crowding with Basis Sanity Gate",
        family=StrategyKind.MEAN_REVERSION,
        archetype="carry_crowding_reversal",
        origin=(
            "Perpetual funding crowding combined with low-ADX and directional basis sanity evidence"
        ),
        source_url=OKX_FUNDING,
        summary=(
            "Fade a 1bp funding extreme in a range while rejecting a basis state "
            "crowded against the intended reversal."
        ),
        indicators=["funding_rate", "mark_index_basis", "ADX", "ATR"],
        preferred_regimes=["carry_crowding", "stable_range"],
        avoid_regimes=["structural_repricing", "trend"],
        required_data=["ohlcv", "funding_rate", "basis"],
        expected_profile="selective_high_quality",
        parameters={
            "entry_model": "funding_contrarian",
            "funding_rate_threshold": "0.0001",
            "trend_strength_mode": "require_range",
            "adx_threshold": "22",
            "basis_mode": "avoid_crowded_direction",
            "maximum_directional_basis_rate": "0.002",
        },
    ),
    _entry(
        id="builtin_vwap_flow_range_20_v1",
        name="Range VWAP Reversion with Flow",
        family=StrategyKind.MEAN_REVERSION,
        archetype="flow_confirmed_vwap_reversion",
        origin="Rolling VWAP deviation combined with low-ADX and reversal-direction taker flow",
        source_url=BINANCE_MARKET_STREAMS,
        summary=(
            "Trade a moderate VWAP deviation only in a range and after completed-bar "
            "aggressive flow confirms reversion."
        ),
        indicators=["VWAP", "ADX", "taker_flow_imbalance", "ATR"],
        preferred_regimes=["liquid_range", "flow_reversal"],
        avoid_regimes=["trend", "volume_breakout"],
        required_data=["ohlcv", "order_flow"],
        expected_profile="selective_high_quality",
        parameters={
            "entry_model": "vwap_reversion",
            "mean_window": 20,
            "vwap_deviation_atr": "0.75",
            "trend_strength_mode": "require_range",
            "adx_threshold": "20",
            "order_flow_mode": "confirm_direction",
            "minimum_order_flow_imbalance": "0.03",
        },
    ),
    _entry(
        id="builtin_bollinger_carry_flow_reversal_20_v1",
        name="Bollinger Carry and Flow Reversal",
        family=StrategyKind.MEAN_REVERSION,
        archetype="carry_flow_band_reversal",
        origin=(
            "Bollinger-RSI reversal combined with low-ADX, carry sanity and "
            "reversal-direction taker flow"
        ),
        source_url=FIDELITY_BOLLINGER,
        summary=(
            "Use a wider band-and-RSI reversal only in a range, with flow "
            "confirmation and without paying crowded carry."
        ),
        indicators=[
            "Bollinger_Bands",
            "RSI",
            "ADX",
            "funding_rate",
            "mark_index_basis",
            "taker_flow_imbalance",
            "ATR",
        ],
        preferred_regimes=["crowded_range", "flow_reversal"],
        avoid_regimes=["band_walk", "structural_trend"],
        required_data=["ohlcv", "funding_rate", "basis", "order_flow"],
        expected_profile="selective_high_quality",
        parameters={
            "entry_model": "bollinger_rsi",
            "mean_window": 20,
            "band_deviations": "1.75",
            "rsi_oversold": "35",
            "rsi_overbought": "65",
            "trend_strength_mode": "require_range",
            "adx_threshold": "22",
            "funding_mode": "avoid_paying_extreme",
            "maximum_paying_funding_rate": "0.0003",
            "basis_mode": "avoid_crowded_direction",
            "maximum_directional_basis_rate": "0.0025",
            "order_flow_mode": "confirm_direction",
            "minimum_order_flow_imbalance": "0.03",
        },
    ),
)


def _standard_horizon_variants() -> tuple[StrategyLibraryEntry, ...]:
    """Return explicit public-rule horizon priors, not optimized parameters.

    The variants broaden short/medium/long horizon coverage. Campaign trial
    counts include every variant, so this library expansion cannot evade
    multiple-testing and PBO penalties.
    """

    entries: list[StrategyLibraryEntry] = []
    trend_variants = (
        (8, "0.45", 32),
        (12, "0.60", 40),
        (16, "0.80", 48),
        (28, "1.20", 64),
        (36, "1.50", 72),
        (48, "1.80", 96),
        (72, "2.50", 120),
        (96, "3.20", 160),
        (120, "4.00", 200),
        (168, "5.00", 200),
    )
    for period, threshold, slow_ema in trend_variants:
        entries.append(
            _entry(
                id=f"builtin_roc_{period}_standard_horizon_v1",
                name=f"ROC {period} 标准周期动量",
                family=StrategyKind.TREND,
                archetype="standard_horizon_momentum",
                origin="公开时间序列动量规则的预声明短、中、长周期变体",
                source_url=SSRN_TSMOM,
                summary=(
                    f"使用 {period} 根收盘 ROC 与 EMA {slow_ema} 方向过滤；"
                    "参数在回测前固定，作为独立试验计入 Campaign。"
                ),
                indicators=["ROC", "EMA", "ATR"],
                preferred_regimes=["persistent_trend"],
                avoid_regimes=["stable_range", "rapid_reversal"],
                expected_profile="selective_high_quality",
                parameters={
                    "entry_model": "roc_momentum",
                    "roc_period": period,
                    "roc_threshold_percent": threshold,
                    "ema_slow": slow_ema,
                    "maximum_entry_extension_atr": "0.65",
                },
            )
        )
    reversion_variants = (
        (10, "1.60", "25", "75"),
        (15, "1.70", "26", "74"),
        (25, "1.80", "27", "73"),
        (30, "1.90", "28", "72"),
        (40, "2.00", "29", "71"),
        (60, "2.10", "30", "70"),
        (70, "2.20", "31", "69"),
        (80, "2.30", "32", "68"),
        (90, "2.40", "33", "67"),
        (100, "2.50", "34", "66"),
    )
    for window, zscore, oversold, overbought in reversion_variants:
        entries.append(
            _entry(
                id=f"builtin_zscore_{window}_standard_horizon_v1",
                name=f"Z-Score {window} 标准周期回归",
                family=StrategyKind.MEAN_REVERSION,
                archetype="standard_horizon_reversion",
                origin="公开 Z-Score 均值回归规则的预声明短、中、长周期变体",
                source_url=FIDELITY_RSI,
                summary=(
                    f"使用 {window} 根均值与 {zscore} 倍标准差偏离，"
                    "并以 RSI 极值和低 ADX 排除趋势带走。"
                ),
                indicators=["z_score", "RSI", "ADX", "ATR"],
                preferred_regimes=["stable_range"],
                avoid_regimes=["persistent_trend", "volatility_breakout"],
                expected_profile="selective_high_quality",
                parameters={
                    "entry_model": "zscore_rsi",
                    "mean_window": window,
                    "zscore_entry": zscore,
                    "rsi_oversold": oversold,
                    "rsi_overbought": overbought,
                    "trend_strength_mode": "require_range",
                    "adx_threshold": "22",
                },
            )
        )
    breakout_variants = (
        (10, "1.80"),
        (15, "1.70"),
        (25, "1.60"),
        (30, "1.55"),
        (40, "1.50"),
        (60, "1.45"),
        (80, "1.40"),
        (100, "1.35"),
        (120, "1.30"),
        (168, "1.25"),
    )
    for lookback, volume_ratio in breakout_variants:
        entries.append(
            _entry(
                id=f"builtin_donchian_{lookback}_standard_horizon_v1",
                name=f"Donchian {lookback} 标准周期突破",
                family=StrategyKind.BREAKOUT,
                archetype="standard_horizon_breakout",
                origin="公开 Donchian/Turtle 突破规则的预声明短、中、长周期变体",
                source_url=TURTLE_RULES,
                summary=(
                    f"突破前 {lookback} 根价格通道且成交量达到均值 "
                    f"{volume_ratio} 倍；所有变体独立计入试验次数。"
                ),
                indicators=["Donchian_channel", "volume_ratio", "ATR"],
                preferred_regimes=["volatility_breakout", "persistent_breakout"],
                avoid_regimes=["quiet_range"],
                expected_profile="high_payoff_lower_win_rate",
                parameters={
                    "entry_model": "donchian_volume",
                    "lookback": lookback,
                    "minimum_volume_ratio": volume_ratio,
                    "maximum_entry_extension_atr": "0.70",
                },
            )
        )
    return tuple(entries)


def _mean_reversion_exit_variants() -> tuple[StrategyLibraryEntry, ...]:
    """Predeclare structurally different, causal mean-reversion exits.

    The original public priors mostly vary their entry signal while sharing the
    same minimum-R first target. These variants freeze an alternative before a
    Campaign: realize most of the position at the statistical mean visible on
    the signal candle, retain a small convex tail whose target preserves the
    weighted reward/risk floor, and abandon stale positions after a fixed number
    of bars. Every variant remains an independent multiple-testing hypothesis.
    """

    common = {
        "origin": (
            "公开均值回归规则的预声明均值触及、分批退出和固定信号半衰期变体"
        ),
        "expected_profile": "selective_high_quality",
        "required_data": ["ohlcv"],
    }
    return (
        _entry(
            id="builtin_zscore_20_mean_touch_exit_v1",
            name="Z-Score 20 均值触及分批退出",
            family=StrategyKind.MEAN_REVERSION,
            archetype="mean_touch_statistical_reversion",
            source_url=FIDELITY_RSI,
            summary=(
                "Z-Score 与 RSI 负责入场；80% 仓位在信号时可见均值退出，"
                "尾仓目标维持加权收益风险下限，40 根后强制退出。"
            ),
            indicators=["z_score", "RSI", "ATR", "time_stop"],
            preferred_regimes=["stable_range", "failed_breakout"],
            avoid_regimes=["persistent_trend"],
            parameters={
                "entry_model": "zscore_rsi",
                "mean_window": 20,
                "zscore_entry": "1.5",
                "rsi_oversold": "32",
                "rsi_overbought": "68",
                "mean_completion_mode": "mean_touch",
                "first_target_fraction": "0.80",
                "maximum_holding_bars": 40,
                "trailing_stop_atr": "1.0",
                "trailing_activation": "first_target",
            },
            **common,
        ),
        _entry(
            id="builtin_zscore_50_mean_touch_exit_v1",
            name="Z-Score 50 深度均值触及退出",
            family=StrategyKind.MEAN_REVERSION,
            archetype="mean_touch_statistical_reversion",
            source_url=FIDELITY_RSI,
            summary=(
                "只交易较深的50周期统计偏离；80%仓位回到冻结均值时退出，"
                "较慢信号最多持有100根。"
            ),
            indicators=["z_score", "RSI", "ATR", "time_stop"],
            preferred_regimes=["stable_range", "deep_failed_breakout"],
            avoid_regimes=["trend_acceleration"],
            parameters={
                "entry_model": "zscore_rsi",
                "mean_window": 50,
                "zscore_entry": "2.0",
                "rsi_oversold": "30",
                "rsi_overbought": "70",
                "mean_completion_mode": "mean_touch",
                "first_target_fraction": "0.80",
                "maximum_holding_bars": 100,
                "trailing_stop_atr": "1.2",
                "trailing_activation": "first_target",
            },
            **common,
        ),
        _entry(
            id="builtin_bollinger_20_mean_touch_exit_v1",
            name="Bollinger 20 均值触及分批退出",
            family=StrategyKind.MEAN_REVERSION,
            archetype="mean_touch_bollinger_reversion",
            source_url=FIDELITY_BOLLINGER,
            summary=(
                "Bollinger带外与RSI极值负责入场，触及20周期可见均值时退出"
                "80%仓位，40根后放弃未完成回归。"
            ),
            indicators=["Bollinger_Bands", "RSI", "ATR", "time_stop"],
            preferred_regimes=["range", "normal_volatility"],
            avoid_regimes=["band_walk", "persistent_trend"],
            parameters={
                "entry_model": "bollinger_rsi",
                "mean_window": 20,
                "band_deviations": "2.0",
                "rsi_oversold": "30",
                "rsi_overbought": "70",
                "mean_completion_mode": "mean_touch",
                "first_target_fraction": "0.80",
                "maximum_holding_bars": 40,
                "trailing_stop_atr": "1.0",
                "trailing_activation": "first_target",
            },
            **common,
        ),
        _entry(
            id="builtin_bollinger_50_mean_touch_exit_v1",
            name="Bollinger 50 深度均值触及退出",
            family=StrategyKind.MEAN_REVERSION,
            archetype="mean_touch_bollinger_reversion",
            source_url=FIDELITY_BOLLINGER,
            summary=(
                "50周期宽带定义深度偏离，均值触及后实现大部分仓位，"
                "尾仓保持凸性且最长持有100根。"
            ),
            indicators=["Bollinger_Bands", "RSI", "ATR", "time_stop"],
            preferred_regimes=["stable_range", "high_cost"],
            avoid_regimes=["trend_acceleration"],
            parameters={
                "entry_model": "bollinger_rsi",
                "mean_window": 50,
                "band_deviations": "2.5",
                "rsi_oversold": "30",
                "rsi_overbought": "70",
                "mean_completion_mode": "mean_touch",
                "first_target_fraction": "0.80",
                "maximum_holding_bars": 100,
                "trailing_stop_atr": "1.2",
                "trailing_activation": "first_target",
            },
            **common,
        ),
        _entry(
            id="builtin_bollinger_20_reentry_mean_touch_exit_v1",
            name="Bollinger 20 再进入均值触及退出",
            family=StrategyKind.MEAN_REVERSION,
            archetype="mean_touch_failed_breakout_reversion",
            source_url=FIDELITY_BOLLINGER,
            summary=(
                "先要求收盘重新进入Bollinger带，再以冻结均值作为80%仓位"
                "目标；把确认入场和快速兑现组合成独立假设。"
            ),
            indicators=["Bollinger_Bands", "RSI", "ATR", "time_stop"],
            preferred_regimes=["stable_range", "failed_breakout"],
            avoid_regimes=["band_walk"],
            parameters={
                "entry_model": "bollinger_reentry",
                "mean_window": 20,
                "band_deviations": "2.0",
                "rsi_oversold": "30",
                "rsi_overbought": "70",
                "mean_completion_mode": "mean_touch",
                "first_target_fraction": "0.80",
                "maximum_holding_bars": 40,
                "trailing_stop_atr": "1.0",
                "trailing_activation": "first_target",
            },
            **common,
        ),
        _entry(
            id="builtin_rsi_14_mean_touch_exit_v1",
            name="RSI 14 均值触及分批退出",
            family=StrategyKind.MEAN_REVERSION,
            archetype="mean_touch_oscillator_reversion",
            source_url=FIDELITY_RSI,
            summary=(
                "传统RSI极值入场后，75%仓位在20周期冻结均值退出，"
                "28根信号半衰期后不再等待。"
            ),
            indicators=["RSI", "SMA", "ATR", "time_stop"],
            preferred_regimes=["range", "cyclical"],
            avoid_regimes=["strong_trend"],
            parameters={
                "entry_model": "rsi_reversal",
                "mean_window": 20,
                "rsi_period": 14,
                "rsi_oversold": "30",
                "rsi_overbought": "70",
                "mean_completion_mode": "mean_touch",
                "first_target_fraction": "0.75",
                "maximum_holding_bars": 28,
                "trailing_stop_atr": "1.0",
                "trailing_activation": "first_target",
            },
            **common,
        ),
        _entry(
            id="builtin_stochastic_21_mean_touch_exit_v1",
            name="Stochastic 21 均值触及分批退出",
            family=StrategyKind.MEAN_REVERSION,
            archetype="mean_touch_oscillator_reversion",
            source_url=FIDELITY_INDICATORS,
            summary=(
                "慢速随机振荡极值入场后，以可见均值完成75%仓位并保留"
                "凸性尾仓，最长持有42根。"
            ),
            indicators=["stochastic", "SMA", "ATR", "time_stop"],
            preferred_regimes=["slow_range"],
            avoid_regimes=["fast_breakout"],
            parameters={
                "entry_model": "stochastic_reversal",
                "mean_window": 21,
                "oscillator_period": 21,
                "oscillator_oversold": "20",
                "oscillator_overbought": "80",
                "mean_completion_mode": "mean_touch",
                "first_target_fraction": "0.75",
                "maximum_holding_bars": 42,
                "trailing_stop_atr": "1.0",
                "trailing_activation": "first_target",
            },
            **common,
        ),
        _entry(
            id="builtin_adx_range_vwap_30_mean_touch_exit_v1",
            name="ADX震荡VWAP 30 均值触及退出",
            family=StrategyKind.MEAN_REVERSION,
            archetype="mean_touch_vwap_range_reversion",
            source_url=FIDELITY_INDICATORS,
            summary=(
                "低ADX下交易滚动VWAP偏离，触及信号时可见VWAP后完成80%"
                "仓位；60根后退出未完成回归。"
            ),
            indicators=["VWAP", "ADX", "ATR", "time_stop"],
            preferred_regimes=["liquid_stable_range"],
            avoid_regimes=["volume_breakout", "trend"],
            parameters={
                "entry_model": "vwap_reversion",
                "mean_window": 30,
                "vwap_deviation_atr": "1.25",
                "trend_strength_mode": "require_range",
                "adx_threshold": "20",
                "mean_completion_mode": "mean_touch",
                "first_target_fraction": "0.80",
                "maximum_holding_bars": 60,
                "trailing_stop_atr": "1.0",
                "trailing_activation": "first_target",
            },
            **common,
        ),
        _entry(
            id="builtin_adx_range_vwap_30_breakeven_mean_touch_exit_v1",
            name="ADX震荡VWAP 30 均值触及保本尾仓",
            family=StrategyKind.MEAN_REVERSION,
            archetype="breakeven_mean_touch_vwap_reversion",
            source_url=FIDELITY_INDICATORS,
            summary=(
                "低ADX的VWAP偏离入场，60%仓位在冻结均值兑现；首目标后"
                "剩余仓位止损不低于实际入场价，同时保留凸性尾仓。"
            ),
            indicators=["VWAP", "ADX", "ATR", "break_even_stop", "time_stop"],
            preferred_regimes=["liquid_stable_range"],
            avoid_regimes=["volume_breakout", "trend"],
            parameters={
                "entry_model": "vwap_reversion",
                "mean_window": 30,
                "vwap_deviation_atr": "1.25",
                "trend_strength_mode": "require_range",
                "adx_threshold": "20",
                "mean_completion_mode": "mean_touch",
                "first_target_fraction": "0.60",
                "break_even_after_first_target": True,
                "maximum_holding_bars": 60,
                "trailing_stop_atr": "1.0",
                "trailing_activation": "first_target",
            },
            **common,
        ),
        _entry(
            id="builtin_adx_range_vwap_37_breakeven_mean_touch_exit_v1",
            name="ADX震荡VWAP 37 慢速均值触及保本尾仓",
            family=StrategyKind.MEAN_REVERSION,
            archetype="breakeven_mean_touch_vwap_reversion",
            source_url=FIDELITY_INDICATORS,
            summary=(
                "使用较慢VWAP窗口降低换手，60%仓位在冻结均值兑现；"
                "首目标后尾仓保本，74根后放弃失效信号。"
            ),
            indicators=["VWAP", "ADX", "ATR", "break_even_stop", "time_stop"],
            preferred_regimes=["liquid_stable_range", "lower_turnover"],
            avoid_regimes=["volume_breakout", "trend"],
            parameters={
                "entry_model": "vwap_reversion",
                "mean_window": 37,
                "vwap_deviation_atr": "1.25",
                "trend_strength_mode": "require_range",
                "adx_threshold": "20",
                "mean_completion_mode": "mean_touch",
                "first_target_fraction": "0.60",
                "break_even_after_first_target": True,
                "maximum_holding_bars": 74,
                "trailing_stop_atr": "1.0",
                "trailing_activation": "first_target",
            },
            **common,
        ),
        _entry(
            id="builtin_bollinger_20_reentry_breakeven_mean_touch_exit_v1",
            name="Bollinger再进入均值触及保本尾仓",
            family=StrategyKind.MEAN_REVERSION,
            archetype="breakeven_failed_breakout_reversion",
            source_url=FIDELITY_BOLLINGER,
            summary=(
                "收盘重新进入Bollinger带后交易失败突破，60%仓位在冻结"
                "均值兑现，首目标后把尾仓最差退出提高到实际入场价。"
            ),
            indicators=["Bollinger_Bands", "RSI", "ATR", "break_even_stop"],
            preferred_regimes=["stable_range", "failed_breakout"],
            avoid_regimes=["band_walk"],
            parameters={
                "entry_model": "bollinger_reentry",
                "mean_window": 20,
                "band_deviations": "2.0",
                "rsi_oversold": "30",
                "rsi_overbought": "70",
                "mean_completion_mode": "mean_touch",
                "first_target_fraction": "0.60",
                "break_even_after_first_target": True,
                "maximum_holding_bars": 40,
                "trailing_stop_atr": "1.0",
                "trailing_activation": "first_target",
            },
            **common,
        ),
        _entry(
            id="builtin_zscore_50_breakeven_mean_touch_exit_v1",
            name="Z-Score 50 深度均值触及保本尾仓",
            family=StrategyKind.MEAN_REVERSION,
            archetype="breakeven_statistical_reversion",
            source_url=FIDELITY_RSI,
            summary=(
                "深度统计偏离与RSI共同入场，60%仓位在50周期冻结均值"
                "兑现；首目标后尾仓保本并保留高收益目标。"
            ),
            indicators=["z_score", "RSI", "ATR", "break_even_stop", "time_stop"],
            preferred_regimes=["stable_range", "deep_failed_breakout"],
            avoid_regimes=["trend_acceleration"],
            parameters={
                "entry_model": "zscore_rsi",
                "mean_window": 50,
                "zscore_entry": "2.0",
                "rsi_oversold": "30",
                "rsi_overbought": "70",
                "mean_completion_mode": "mean_touch",
                "first_target_fraction": "0.60",
                "break_even_after_first_target": True,
                "maximum_holding_bars": 100,
                "trailing_stop_atr": "1.2",
                "trailing_activation": "first_target",
            },
            **common,
        ),
    )


def _confirmed_statistical_reversal_variants() -> tuple[StrategyLibraryEntry, ...]:
    """Predeclare causal failed-extreme entries with minimum-R exits.

    These hypotheses do not buy or sell the first extreme observation.  The
    previous closed bar must be beyond both the Z-Score and RSI thresholds and
    the current closed bar must return through both thresholds while remaining
    on the displaced side of its visible mean.  This is a structural entry
    alternative, not a parameter-grid expansion.
    """

    common = {
        "family": StrategyKind.MEAN_REVERSION,
        "origin": (
            "公开 Z-Score/RSI 均值回归规则的预声明已收盘双重重返确认变体"
        ),
        "source_url": FIDELITY_RSI,
        "indicators": ["z_score", "RSI", "ADX", "ATR", "time_stop"],
        "avoid_regimes": ["persistent_trend", "trend_acceleration"],
        "required_data": ["ohlcv"],
        "expected_profile": "selective_high_quality",
    }
    variants = (
        (
            "builtin_zscore_20_reentry_minimum_r_v1",
            "Z-Score 20 双重重返确认",
            "failed_extreme_statistical_reversion",
            ["stable_range", "failed_breakout"],
            20,
            "1.75",
            "32",
            "68",
            "22",
            "1.2",
            "1.30",
            "0.70",
            40,
        ),
        (
            "builtin_zscore_30_reentry_minimum_r_v1",
            "Z-Score 30 双重重返确认",
            "failed_extreme_statistical_reversion",
            ["stable_range", "normal_volatility"],
            30,
            "1.85",
            "31",
            "69",
            "22",
            "1.3",
            "1.30",
            "0.70",
            60,
        ),
        (
            "builtin_zscore_50_reentry_minimum_r_v1",
            "Z-Score 50 深度重返确认",
            "deep_failed_extreme_reversion",
            ["stable_range", "deep_failed_breakout"],
            50,
            "2.0",
            "30",
            "70",
            "20",
            "1.4",
            "1.35",
            "0.65",
            100,
        ),
        (
            "builtin_zscore_70_reentry_minimum_r_v1",
            "Z-Score 70 慢速重返确认",
            "slow_failed_extreme_reversion",
            ["slow_range", "high_cost"],
            70,
            "2.1",
            "30",
            "70",
            "20",
            "1.5",
            "1.40",
            "0.65",
            140,
        ),
    )
    return tuple(
        _entry(
            id=entry_id,
            name=name,
            archetype=archetype,
            summary=(
                f"前一根已收盘 K 线达到 {window} 周期统计极端，当前收盘须同时"
                "重返 Z-Score 与 RSI 阈值；第一目标受最低收益风险约束，"
                "未在预声明信号半衰期内完成则退出。"
            ),
            preferred_regimes=preferred_regimes,
            parameters={
                "entry_model": "zscore_reentry",
                "mean_window": window,
                "zscore_entry": zscore_entry,
                "rsi_oversold": rsi_oversold,
                "rsi_overbought": rsi_overbought,
                "trend_strength_mode": "require_range",
                "adx_threshold": adx_threshold,
                "stop_atr": stop_atr,
                "first_target_reward_risk": first_target_reward_risk,
                "first_target_fraction": first_target_fraction,
                "maximum_holding_bars": maximum_holding_bars,
                "second_target_extension": "1.8",
                "trailing_stop_atr": "1.0",
                "trailing_activation": "first_target",
            },
            **common,
        )
        for (
            entry_id,
            name,
            archetype,
            preferred_regimes,
            window,
            zscore_entry,
            rsi_oversold,
            rsi_overbought,
            adx_threshold,
            stop_atr,
            first_target_reward_risk,
            first_target_fraction,
            maximum_holding_bars,
        ) in variants
    )


def _liquidation_context_variants() -> tuple[StrategyLibraryEntry, ...]:
    """Predeclare causal liquidation filters around familiar public rules.

    Liquidation flow is deliberately a confirmation/avoidance input only. It
    cannot manufacture an entry, and the COIN-M source remains explicitly
    labelled as cross-contract underlying context.
    """

    common = {
        "origin": (
            "公开趋势、突破与均值回归规则，叠加官方强平订单流的预声明因果过滤"
        ),
        "source_url": BINANCE_LIQUIDATION_STREAM,
        "required_data": ["ohlcv", "liquidation_flow"],
        "expected_profile": "selective_high_quality",
    }
    return (
        _entry(
            id="builtin_roc_24_liquidation_confirm_v1",
            name="ROC 24 强平方向确认",
            family=StrategyKind.TREND,
            archetype="liquidation_confirmed_momentum",
            summary=(
                "ROC 与 EMA 先产生方向，过去 24 小时已闭合强平流必须同向且"
                "不平衡度至少 12%；强平流本身不能触发开仓。"
            ),
            indicators=["ROC", "EMA", "ATR", "liquidation_flow_imbalance_24h"],
            preferred_regimes=["persistent_trend", "forced_unwind_continuation"],
            avoid_regimes=["stable_range", "two_sided_liquidation"],
            parameters={
                "entry_model": "roc_momentum",
                "roc_period": 24,
                "roc_threshold_percent": "1.0",
                "ema_slow": 64,
                "maximum_entry_extension_atr": "0.65",
                "liquidation_flow_mode": "confirm_direction",
                "minimum_liquidation_flow_imbalance": "0.12",
            },
            **common,
        ),
        _entry(
            id="builtin_donchian_20_liquidation_confirm_v1",
            name="Donchian 20 强平方向确认",
            family=StrategyKind.BREAKOUT,
            archetype="liquidation_confirmed_breakout",
            summary=(
                "价格与成交量先确认 Donchian 突破，再要求过去 24 小时强平流"
                "同向；拒绝把未来小时或未闭合事件用于确认。"
            ),
            indicators=[
                "Donchian_channel",
                "volume_ratio",
                "ATR",
                "liquidation_flow_imbalance_24h",
            ],
            preferred_regimes=["volatility_breakout", "forced_unwind_continuation"],
            avoid_regimes=["quiet_range", "failed_breakout"],
            parameters={
                "entry_model": "donchian_volume",
                "lookback": 20,
                "minimum_volume_ratio": "1.5",
                "maximum_entry_extension_atr": "0.70",
                "liquidation_flow_mode": "confirm_direction",
                "minimum_liquidation_flow_imbalance": "0.12",
            },
            **common,
        ),
        _entry(
            id="builtin_ema_20_50_liquidation_avoid_v1",
            name="EMA 20/50 反向强平规避",
            family=StrategyKind.TREND,
            archetype="liquidation_aware_trend",
            summary=(
                "EMA 趋势正常开仓，但当 24 小时强平流以至少 20% 的不平衡度"
                "显著反向时放弃该次信号，避免保护逻辑阻断所有普通入场。"
            ),
            indicators=["EMA", "ATR", "liquidation_flow_imbalance_24h"],
            preferred_regimes=["trend", "orderly_deleveraging"],
            avoid_regimes=["opposed_forced_unwind"],
            parameters={
                "entry_model": "ema_crossover",
                "ema_fast": 20,
                "ema_slow": 50,
                "liquidation_flow_mode": "avoid_opposed_extreme",
                "minimum_liquidation_flow_imbalance": "0.20",
            },
            **common,
        ),
        _entry(
            id="builtin_bollinger_20_liquidation_avoid_v1",
            name="Bollinger-RSI 反向强平规避",
            family=StrategyKind.MEAN_REVERSION,
            archetype="liquidation_aware_reversion",
            summary=(
                "Bollinger 与 RSI 先产生回归信号；若正在发生显著反向强平瀑布，"
                "则不接飞刀，但普通区间信号无需等待强平事件。"
            ),
            indicators=[
                "Bollinger_Bands",
                "RSI",
                "ADX",
                "ATR",
                "liquidation_flow_imbalance_24h",
            ],
            preferred_regimes=["stable_range", "post_liquidation_stabilization"],
            avoid_regimes=["opposed_forced_unwind", "band_walk"],
            parameters={
                "entry_model": "bollinger_rsi",
                "mean_window": 20,
                "band_deviations": "2.0",
                "rsi_oversold": "30",
                "rsi_overbought": "70",
                "trend_strength_mode": "require_range",
                "adx_threshold": "22",
                "liquidation_flow_mode": "avoid_opposed_extreme",
                "minimum_liquidation_flow_imbalance": "0.25",
            },
            **common,
        ),
    )


BUILTIN_STRATEGY_LIBRARY = (
    *BUILTIN_STRATEGY_LIBRARY,
    *_standard_horizon_variants(),
    *_mean_reversion_exit_variants(),
    *_confirmed_statistical_reversal_variants(),
    *_liquidation_context_variants(),
)


def list_strategy_library(
    family: StrategyKind | None = None,
) -> list[StrategyLibraryEntry]:
    entries = list(BUILTIN_STRATEGY_LIBRARY)
    if family is not None:
        entries = [entry for entry in entries if entry.family == family]
    return entries


def library_entry(entry_id: str) -> StrategyLibraryEntry:
    try:
        return next(entry for entry in BUILTIN_STRATEGY_LIBRARY if entry.id == entry_id)
    except StopIteration as exc:
        raise LookupError(f"unknown strategy library entry: {entry_id}") from exc


def infer_required_data(spec: dict) -> list[str]:
    """Infer causal feature dependencies for Agent-created strategy specs."""

    parameters = spec.get("parameters") or {}
    entry_model = str(parameters.get("entry_model") or "")
    required = {"ohlcv"}
    if entry_model == "funding_contrarian" or parameters.get("funding_mode", "ignore") != "ignore":
        required.add("funding_rate")
    if parameters.get("open_interest_mode", "ignore") != "ignore":
        required.add("open_interest")
    if entry_model == "positioning_contrarian":
        required.add("positioning")
    if entry_model == "basis_contrarian" or parameters.get("basis_mode", "ignore") != "ignore":
        required.add("basis")
    if parameters.get("order_flow_mode", "ignore") != "ignore":
        required.add("order_flow")
    if parameters.get("liquidation_flow_mode", "ignore") != "ignore":
        required.add("liquidation_flow")
    return sorted(required)


def rank_strategy_library(
    entries: list[StrategyLibraryEntry],
    assessment,
    available_data: set[str] | None = None,
) -> list[tuple[StrategyLibraryEntry, dict]]:
    """Rank public priors by current regime without using backtest outcomes."""

    primary_regime = str(getattr(assessment, "primary_regime", "uncertain"))
    volatility_regime = str(getattr(assessment, "volatility_regime", "normal"))
    preferred_archetypes = set(getattr(assessment, "preferred_archetypes", []) or [])
    avoided_archetypes = set(getattr(assessment, "avoided_archetypes", []) or [])
    ranked: list[tuple[StrategyLibraryEntry, dict]] = []
    for entry in entries:
        score = 0
        reasons: list[str] = []
        missing_data = (
            sorted(set(entry.required_data) - available_data) if available_data is not None else []
        )
        if missing_data:
            score -= 100
            reasons.append("missing current causal data: " + ", ".join(missing_data))
        if entry.archetype in preferred_archetypes:
            score += 5
            reasons.append("策略原型与当前市场状态的首选原型一致")
        if entry.archetype in avoided_archetypes:
            score -= 6
            reasons.append("策略原型属于当前市场状态的规避原型")
        regime_text = " ".join(entry.preferred_regimes)
        avoid_text = " ".join(entry.avoid_regimes)
        for token in primary_regime.split("_"):
            if token and token in regime_text:
                score += 1
            if token and token in avoid_text:
                score -= 2
        if volatility_regime in regime_text:
            score += 1
        if volatility_regime in avoid_text:
            score -= 2
        ranked.append(
            (
                entry,
                {
                    "score": score,
                    "primary_regime": primary_regime,
                    "volatility_regime": volatility_regime,
                    "missing_data": missing_data,
                    "reasons": reasons or ["没有直接原型命中，保留为低优先级可证伪假设"],
                    "role": "pre_backtest_prior_only",
                },
            )
        )
    return sorted(
        ranked,
        key=lambda item: (item[1]["score"], item[0].id),
        reverse=True,
    )


def seed_strategy_library_candidates(store) -> int:
    """Synchronize versioned public priors and retire superseded system seeds."""

    seeded = 0
    active_candidate_ids = {f"cand_{entry.id}" for entry in BUILTIN_STRATEGY_LIBRARY}
    for entry in BUILTIN_STRATEGY_LIBRARY:
        candidate_id = f"cand_{entry.id}"
        if store.load_strategy_candidate(candidate_id):
            continue
        candidate: StrategyCandidate = create_candidate(entry.validated_spec)
        candidate.id = candidate_id
        candidate.decision_reasons = [
            *candidate.decision_reasons,
            f"内置策略库来源：{entry.origin}",
            f"公开参考：{entry.source_url}",
            f"策略原型：{entry.archetype}",
        ]
        store.save_strategy_candidate(candidate)
        seeded += 1
    for payload in store.list_strategy_candidates(500):
        if (
            payload.get("source") != "system"
            or not str(payload.get("id", "")).startswith("cand_builtin_")
            or payload.get("id") in active_candidate_ids
            or payload.get("status") == "retired"
        ):
            continue
        store.retire_superseded_system_candidate(str(payload["id"]))
    return seeded


def strategy_library_summary() -> dict:
    entries = list_strategy_library()
    families = {
        family.value: sum(entry.family == family for entry in entries) for family in StrategyKind
    }
    archetypes = sorted({entry.archetype for entry in entries})
    return {
        "version": 2,
        "non_empty": bool(entries),
        "entry_count": len(entries),
        "family_counts": families,
        "archetype_count": len(archetypes),
        "archetypes": archetypes,
        "entries": [entry.model_dump(mode="json") for entry in entries],
        "profitability_claim": False,
        "usage": (
            "公开规则只作为先验假设；每次执行前必须使用最新不可变数据集、"
            "因果回测、成本压力、时间外验证与多重检验修正。"
        ),
    }
