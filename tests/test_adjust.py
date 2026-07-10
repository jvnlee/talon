from datetime import date, timedelta

import polars as pl
import pytest

from talon.data.adjust import FACTOR_SCHEMA, apply_factors, stepwise_factors

BASE = date(2018, 4, 30)


def series(closes, start=BASE):
    days = [start + timedelta(days=i) for i in range(len(closes))]
    return pl.DataFrame(
        {"day": days, "close": [float(c) for c in closes]},
        schema={"day": pl.Date(), "close": pl.Float64()},
    )


def test_split_produces_two_factor_steps():
    raw = series([2_650_000, 2_650_000, 53_000, 53_900])
    adjusted = series([53_000, 53_000, 53_000, 53_900])
    factors = stepwise_factors(raw, adjusted)

    assert factors["factor"].to_list() == pytest.approx([0.02, 0.02, 1.0, 1.0])


def test_rounding_noise_snaps_to_single_step():
    raw = series([2_650_000, 2_612_000, 2_598_000])
    adjusted = series([53_000, 52_240, 51_960])
    factors = stepwise_factors(raw, adjusted)

    assert factors["factor"].n_unique() == 1
    assert factors["factor"][0] == pytest.approx(0.02, rel=1e-6)


def test_suspension_zero_prices_are_excluded():
    raw = series([1000, 1000, 1000])
    adjusted = series([1000, 0, 1000])
    factors = stepwise_factors(raw, adjusted)

    assert factors.height == 2
    assert factors["factor"].to_list() == pytest.approx([1.0, 1.0])


def test_days_missing_from_adjusted_are_dropped():
    raw = series([1000, 1000, 1000])
    adjusted = series([1000, 1000])
    factors = stepwise_factors(raw, adjusted)

    assert factors.height == 2


def test_empty_inputs_return_empty_schema_frame():
    empty = series([])
    factors = stepwise_factors(empty, empty)

    assert factors.is_empty()
    assert dict(factors.schema) == FACTOR_SCHEMA


def test_apply_factors_scales_prices_and_volume():
    daily = pl.DataFrame(
        {
            "day": [BASE, BASE + timedelta(days=1)],
            "open": [2_600_000.0, 53_000.0],
            "high": [2_700_000.0, 54_000.0],
            "low": [2_500_000.0, 52_000.0],
            "close": [2_650_000.0, 53_900.0],
            "volume": [100.0, 5000.0],
        }
    )
    factors = pl.DataFrame(
        {"day": [BASE, BASE + timedelta(days=1)], "factor": [0.02, 1.0]},
        schema=FACTOR_SCHEMA,
    )
    adjusted = apply_factors(daily, factors)

    first = adjusted.row(0, named=True)
    assert first["close"] == pytest.approx(53_000.0)
    assert first["open"] == pytest.approx(52_000.0)
    assert first["volume"] == pytest.approx(5000.0)
    second = adjusted.row(1, named=True)
    assert second["close"] == pytest.approx(53_900.0)
    assert second["volume"] == pytest.approx(5000.0)
