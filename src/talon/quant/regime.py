from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import cast

import polars as pl

BULL = "bull"
NEUTRAL = "neutral"
BEAR = "bear"

BREADTH_COLUMN = "regime__above_ma"


@dataclass(frozen=True)
class Regime:
    label: str
    exposure: float
    breadth: float | None
    weights: Mapping[str, float]

    def weight(self, strategy: str) -> float:
        return self.weights.get(strategy, 1.0)


@dataclass(frozen=True)
class RegimeConfig:
    ma_days: int = 60
    bull_breadth: float = 0.55
    bear_breadth: float = 0.35
    bull_exposure: float = 1.0
    neutral_exposure: float = 0.6
    bear_exposure: float = 0.5
    bull_weights: Mapping[str, float] = field(
        default_factory=lambda: {"momo_breakout": 1.0, "pullback": 1.0, "meanrev": 0.5}
    )
    neutral_weights: Mapping[str, float] = field(
        default_factory=lambda: {"momo_breakout": 0.3, "pullback": 0.7, "meanrev": 1.0}
    )


class BreadthRegimeFilter:
    def __init__(self, config: RegimeConfig | None = None) -> None:
        self.config = config if config is not None else RegimeConfig()

    def columns(self) -> dict[str, str]:
        return {BREADTH_COLUMN: f"close > Mean(close, {self.config.ma_days})"}

    def assess(self, day_frame: pl.DataFrame) -> Regime:
        values = day_frame.get_column(BREADTH_COLUMN).drop_nulls()
        if values.is_empty():
            return Regime(label=BEAR, exposure=0.0, breadth=None, weights={})
        breadth = cast(float, values.cast(pl.Float64).mean())
        config = self.config
        if breadth >= config.bull_breadth:
            return Regime(
                label=BULL,
                exposure=config.bull_exposure,
                breadth=breadth,
                weights=config.bull_weights,
            )
        if breadth <= config.bear_breadth:
            return Regime(label=BEAR, exposure=config.bear_exposure, breadth=breadth, weights={})
        return Regime(
            label=NEUTRAL,
            exposure=config.neutral_exposure,
            breadth=breadth,
            weights=config.neutral_weights,
        )
