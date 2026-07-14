from datetime import date, timedelta

import polars as pl

from talon.factors.engine import compute_factors
from talon.quant.strategies import (
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


def test_liquidity_floor_excludes_thin_names():
    spec = momentum_breakout(breakout_days=5, trend_days=3, min_value=1_000_000_000.0)
    rows = [bar(i, "AAA", 100.0) for i in range(24)]
    rows.append(bar(24, "AAA", 110.0, open_=100.0, high=110.0, low=100.0, volume=5000.0))
    frame = augment(build_panel(rows), spec)

    assert candidates_on(frame, spec, d(24)) == []
