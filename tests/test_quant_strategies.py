from datetime import date, timedelta

import polars as pl

from talon.factors.engine import compute_factors
from talon.quant.signals import verify_intraday
from talon.quant.strategies import (
    CLOSE_BET_V1_GRID,
    close_bet_v1,
    close_strength,
    default_strategies,
    mean_reversion,
    momentum_breakout,
    pullback,
)

BASE = date(2026, 1, 5)


def d(i):
    return BASE + timedelta(days=i)


def bar(i, symbol, close, open_=None, high=None, low=None, volume=1000.0):
    open_ = open_ if open_ is not None else close
    return {
        "day": d(i),
        "symbol": symbol,
        "open": float(open_),
        "high": float(high if high is not None else max(open_, close)),
        "low": float(low if low is not None else min(open_, close)),
        "close": float(close),
        "volume": float(volume),
        "value": float(close) * float(volume),
    }


def build_panel(rows):
    return (
        pl.DataFrame(rows)
        .sort("symbol", "day")
        .with_columns(pl.col("close").shift(1).over("symbol").alias("prev_close"))
        .sort("day", "symbol")
    )


def augment(panel, spec):
    return compute_factors(panel, spec.columns())


def candidates_on(frame, spec, day):
    return spec.candidates(frame.filter(pl.col("day") == day))


def test_momentum_breakout_fires_on_new_high_with_volume():
    spec = momentum_breakout(breakout_days=5, trend_days=3, min_value=0.0)
    rows = [bar(i, "AAA", 100.0) for i in range(24)]
    rows.append(bar(24, "AAA", 110.0, open_=100.0, high=110.0, low=100.0, volume=5000.0))
    frame = augment(build_panel(rows), spec)

    quiet = candidates_on(frame, spec, d(23))
    breakout = candidates_on(frame, spec, d(24))

    assert quiet == []
    assert [c.symbol for c in breakout] == ["AAA"]
    candidate = breakout[0]
    assert candidate.stop < 110.0 < candidate.target
    assert candidate.min_open > 110.0
    assert candidate.score > 0


def test_momentum_breakout_needs_volume_surge():
    spec = momentum_breakout(breakout_days=5, trend_days=3, min_value=0.0)
    rows = [bar(i, "AAA", 100.0) for i in range(24)]
    rows.append(bar(24, "AAA", 110.0, open_=100.0, high=110.0, low=100.0, volume=1000.0))
    frame = augment(build_panel(rows), spec)

    assert candidates_on(frame, spec, d(24)) == []


def test_pullback_fires_on_dip_rebound_in_uptrend():
    spec = pullback(trend_days=10, fast_days=5, dip_days=3, min_value=0.0)
    rows = [bar(i, "AAA", 100.0 + i) for i in range(1, 20)]
    rows.append(bar(20, "AAA", 116.0, open_=118.0, high=118.0, low=115.0))
    rows.append(bar(21, "AAA", 114.0, open_=116.0, high=116.0, low=113.0))
    rows.append(bar(22, "AAA", 117.0, open_=114.0, high=117.0, low=114.0))
    frame = augment(build_panel(rows), spec)

    dipping = candidates_on(frame, spec, d(21))
    rebound = candidates_on(frame, spec, d(22))

    assert dipping == []
    assert [c.symbol for c in rebound] == ["AAA"]
    candidate = rebound[0]
    assert candidate.stop < 117.0 < candidate.target
    assert candidate.min_open > 117.0


def test_mean_reversion_fires_on_oversold_dip_in_uptrend():
    spec = mean_reversion(band_days=5, trend_days=20, z_entry=-1.5, min_value=0.0)
    rows = [bar(i, "AAA", 80.0 + i) for i in range(1, 26)]
    for offset, close in enumerate((105.0, 105.4, 105.2, 105.3)):
        rows.append(bar(26 + offset, "AAA", close))
    rows.append(bar(30, "AAA", 103.0, open_=105.0, high=105.0, low=103.0))
    frame = augment(build_panel(rows), spec)

    calm = candidates_on(frame, spec, d(29))
    oversold = candidates_on(frame, spec, d(30))

    assert calm == []
    assert [c.symbol for c in oversold] == ["AAA"]
    candidate = oversold[0]
    assert candidate.stop < 103.0
    assert candidate.target > 103.0
    assert candidate.min_open < 103.0

    with_bounce = build_panel([*rows, bar(31, "AAA", 106.0, open_=103.0, high=106.0, low=103.0)])
    bounced = augment(with_bounce, spec).filter(pl.col("day") == d(31))
    assert spec.wants_exit(bounced, "AAA") is True


def test_close_strength_fires_on_high_close_with_volume():
    spec = close_strength(strength_pct=3.0, volume_surge=2.0, min_value=0.0)
    rows = [bar(i, "AAA", 100.0) for i in range(1, 25)]
    rows.append(bar(25, "AAA", 106.0, open_=101.0, high=106.0, low=100.0, volume=5000.0))
    rows.append(bar(26, "AAA", 105.0, open_=104.0, high=106.0, low=103.0, volume=5000.0))
    frame = augment(build_panel(rows), spec)

    fired = candidates_on(frame, spec, d(25))
    off_high = candidates_on(frame, spec, d(26))

    assert [c.symbol for c in fired] == ["AAA"]
    candidate = fired[0]
    assert candidate.execution == "close_overnight"
    assert candidate.score > 0
    assert candidate.stop < 106.0 < candidate.target
    assert off_high == []


def test_close_strength_skips_limit_up_close():
    spec = close_strength(strength_pct=3.0, volume_surge=2.0, min_value=0.0)
    rows = [bar(i, "AAA", 100.0) for i in range(1, 25)]
    rows.append(bar(25, "AAA", 130.0, open_=110.0, high=130.0, low=110.0, volume=5000.0))
    frame = augment(build_panel(rows), spec)

    assert candidates_on(frame, spec, d(25)) == []


def test_default_book_is_empty_pending_redesign():
    assert default_strategies() == []


def close_bet_panel(rows, expiry_days=()):
    panel = build_panel(rows)
    expiry = (
        pl.col("day").is_in(list(expiry_days)) if expiry_days else pl.lit(False)
    ).alias("option_expiry")
    return panel.with_columns(
        pl.col("close").alias("close_1510"),
        pl.col("high").alias("high_1510"),
        pl.col("low").alias("low_1510"),
        pl.col("volume").alias("volume_1510"),
        expiry,
    )


def quiet_history(days=21):
    return [bar(i, "AAA", 100.0) for i in range(1, days + 1)]


def surge_bar(high=104.0, low=100.0, volume=3000.0, open_=101.0):
    return bar(22, "AAA", 104.0, open_=open_, high=high, low=low, volume=volume)


def test_close_bet_fires_when_all_three_conditions_hold():
    spec = close_bet_v1(strength_pct=3.0, volume_mult=2.0, tail_max=0.4)
    rows = [*quiet_history(), surge_bar()]
    frame = augment(close_bet_panel(rows), spec)

    quiet = candidates_on(frame, spec, d(21))
    fired = candidates_on(frame, spec, d(22))

    assert quiet == []
    assert [c.symbol for c in fired] == ["AAA"]
    candidate = fired[0]
    assert candidate.execution == "close_overnight"
    assert candidate.ref_price == 104.0
    assert candidate.stop == 104.0 * 0.95
    assert candidate.target is None
    assert candidate.score > 0


def test_close_bet_requires_volume_multiple():
    spec = close_bet_v1(strength_pct=3.0, volume_mult=2.0, tail_max=0.4)
    rows = [*quiet_history(), surge_bar(volume=1500.0)]
    frame = augment(close_bet_panel(rows), spec)

    assert candidates_on(frame, spec, d(22)) == []


def test_close_bet_rejects_a_long_upper_tail():
    spec = close_bet_v1(strength_pct=3.0, volume_mult=2.0, tail_max=0.4)
    rows = [*quiet_history(), surge_bar(high=110.0)]
    frame = augment(close_bet_panel(rows), spec)

    assert candidates_on(frame, spec, d(22)) == []


def test_close_bet_passes_a_rangeless_day():
    spec = close_bet_v1(strength_pct=3.0, volume_mult=2.0, tail_max=0.3)
    rows = [*quiet_history(), surge_bar(high=104.0, low=104.0, open_=104.0)]
    frame = augment(close_bet_panel(rows), spec)

    assert [c.symbol for c in candidates_on(frame, spec, d(22))] == ["AAA"]


def test_close_bet_sits_out_option_expiry():
    spec = close_bet_v1(strength_pct=3.0, volume_mult=2.0, tail_max=0.4)
    rows = [*quiet_history(), surge_bar()]
    frame = augment(close_bet_panel(rows, expiry_days=(d(22),)), spec)

    assert candidates_on(frame, spec, d(22)) == []


def test_close_bet_survives_the_intraday_lookahead_gate():
    assert verify_intraday([close_bet_v1()]) == []


def test_close_bet_grid_is_the_declared_27():
    assert len(CLOSE_BET_V1_GRID) == 27
    assert len({tuple(sorted(p.items())) for p in CLOSE_BET_V1_GRID}) == 27
    assert {p["strength_pct"] for p in CLOSE_BET_V1_GRID} == {2.0, 3.0, 4.0}
    assert {p["volume_mult"] for p in CLOSE_BET_V1_GRID} == {1.5, 2.0, 2.5}
    assert {p["tail_max"] for p in CLOSE_BET_V1_GRID} == {0.3, 0.4, 0.5}
    defaults = {"strength_pct": 3.0, "volume_mult": 2.0, "tail_max": 0.4}
    assert defaults in CLOSE_BET_V1_GRID


def test_liquidity_floor_excludes_thin_names():
    spec = momentum_breakout(breakout_days=5, trend_days=3, min_value=1_000_000_000.0)
    rows = [bar(i, "AAA", 100.0) for i in range(24)]
    rows.append(bar(24, "AAA", 110.0, open_=100.0, high=110.0, low=100.0, volume=5000.0))
    frame = augment(build_panel(rows), spec)

    assert candidates_on(frame, spec, d(24)) == []
