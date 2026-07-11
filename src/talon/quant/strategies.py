from collections.abc import Callable

from talon.quant.signals import StrategySpec

MIN_TRADING_VALUE = 1_000_000_000.0


def _atr(days: int = 14) -> str:
    true_range = "Greater(high - low, Greater(Abs(high - Ref(close, 1)), Abs(low - Ref(close, 1))))"
    return f"Mean({true_range}, {days})"


def momentum_breakout(
    *,
    breakout_days: int = 60,
    trend_days: int = 20,
    volume_surge: float = 1.5,
    min_value: float = MIN_TRADING_VALUE,
    stop_atr: float = 2.0,
    target_atr: float = 4.0,
    exit_ema_days: int = 10,
    min_open_atr: float = 0.1,
    max_hold_days: int = 30,
) -> StrategySpec:
    atr = _atr()
    return StrategySpec(
        name="momo_breakout",
        entry=(
            f"close >= Ref(Max(high, {breakout_days}), 1)",
            f"close > Mean(close, {trend_days})",
            f"volume >= Mean(volume, 20) * {volume_surge}",
            f"Mean(value, 20) >= {min_value}",
        ),
        score=f"Delta(close, {trend_days}) / Ref(close, {trend_days})",
        stop=f"close - {atr} * {stop_atr}",
        target=f"close + {atr} * {target_atr}",
        exit=f"close < EMA(close, {exit_ema_days})",
        min_open=f"close + {atr} * {min_open_atr}",
        max_hold_days=max_hold_days,
    )


def pullback(
    *,
    trend_days: int = 60,
    fast_days: int = 20,
    dip_days: int = 5,
    min_value: float = MIN_TRADING_VALUE,
    stop_atr: float = 0.5,
    target_atr: float = 3.0,
    min_open_atr: float = 0.1,
    max_hold_days: int = 15,
) -> StrategySpec:
    atr = _atr()
    return StrategySpec(
        name="pullback",
        entry=(
            f"close > Mean(close, {trend_days})",
            f"Mean(close, {fast_days}) > Mean(close, {trend_days})",
            f"Min(low, {dip_days}) <= Mean(close, {fast_days})",
            "close > prev_close",
            f"Mean(value, 20) >= {min_value}",
        ),
        score=f"close / Mean(close, {trend_days})",
        stop=f"Min(low, {dip_days}) - {atr} * {stop_atr}",
        target=f"close + {atr} * {target_atr}",
        exit=f"close < Mean(close, {trend_days})",
        min_open=f"close + {atr} * {min_open_atr}",
        max_hold_days=max_hold_days,
    )


def mean_reversion(
    *,
    band_days: int = 20,
    z_entry: float = -2.0,
    trend_days: int = 120,
    min_value: float = MIN_TRADING_VALUE,
    stop_atr: float = 2.5,
    gap_guard_atr: float = 0.5,
    max_hold_days: int = 10,
) -> StrategySpec:
    atr = _atr()
    zscore = f"(close - Mean(close, {band_days})) / Std(close, {band_days})"
    return StrategySpec(
        name="meanrev",
        entry=(
            f"{zscore} <= {z_entry}",
            f"close > Mean(close, {trend_days})",
            f"Mean(value, 20) >= {min_value}",
        ),
        score=f"-({zscore})",
        stop=f"close - {atr} * {stop_atr}",
        target=f"Mean(close, {band_days})",
        exit=f"{zscore} >= 0",
        min_open=f"close - {atr} * {gap_guard_atr}",
        max_hold_days=max_hold_days,
    )


STRATEGY_FACTORIES: dict[str, Callable[..., StrategySpec]] = {
    "momo_breakout": momentum_breakout,
    "pullback": pullback,
    "meanrev": mean_reversion,
}


def default_strategies() -> list[StrategySpec]:
    return [factory() for factory in STRATEGY_FACTORIES.values()]
