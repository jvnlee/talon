import polars as pl

from talon.quant.regime import BREADTH_COLUMN, BreadthRegimeFilter, Regime, RegimeConfig


def frame(flags):
    return pl.DataFrame(
        {
            "symbol": [f"S{i}" for i in range(len(flags))],
            BREADTH_COLUMN: pl.Series(flags, dtype=pl.Boolean),
        }
    )


def test_columns_use_configured_ma():
    exprs = BreadthRegimeFilter(RegimeConfig(ma_days=20)).columns()
    assert exprs == {BREADTH_COLUMN: "close > Mean(close, 20)"}


def test_bull_when_breadth_high():
    regime = BreadthRegimeFilter().assess(frame([True] * 6 + [False] * 4))
    assert regime.label == "bull"
    assert regime.exposure == 1.0
    assert regime.breadth == 0.6
    assert regime.weight("momo_breakout") == 1.0
    assert regime.weight("meanrev") == 0.5


def test_neutral_between_thresholds():
    regime = BreadthRegimeFilter().assess(frame([True] * 45 + [False] * 55))
    assert regime.label == "neutral"
    assert regime.exposure == 0.6
    assert regime.weight("meanrev") == 1.0
    assert regime.weight("momo_breakout") == 0.3


def test_bear_when_breadth_low():
    regime = BreadthRegimeFilter().assess(frame([True] * 3 + [False] * 7))
    assert regime.label == "bear"
    assert regime.exposure == 0.5


def test_no_data_is_bear():
    regime = BreadthRegimeFilter().assess(frame([None, None]))
    assert regime.label == "bear"
    assert regime.breadth is None
    assert regime.exposure == 0.0


def test_unknown_strategy_weight_defaults_to_one():
    regime = Regime(label="bull", exposure=1.0, breadth=0.7, weights={"a": 0.5})
    assert regime.weight("a") == 0.5
    assert regime.weight("b") == 1.0
