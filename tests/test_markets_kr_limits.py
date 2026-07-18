from datetime import date

import polars as pl
import pytest

from talon.markets.kr_limits import price_limit_exprs, tick_size_expr

PRE = date(2020, 6, 15)
POST = date(2023, 1, 25)


def limits(base: float, market: str, day: date) -> tuple[float, float]:
    frame = pl.DataFrame({"base": [base], "market": [market], "day": [day]})
    upper, lower = price_limit_exprs(pl.col("base"), pl.col("market"), pl.col("day"))
    row = frame.select(upper.alias("upper"), lower.alias("lower")).row(0)
    return row[0], row[1]


def tick(price: float, market: str, day: date) -> float:
    frame = pl.DataFrame({"price": [price], "market": [market], "day": [day]})
    return frame.select(tick_size_expr(pl.col("price"), pl.col("market"), pl.col("day"))).item()


@pytest.mark.parametrize(
    ("price", "market", "day", "expected"),
    [
        (999, "KOSPI", PRE, 1),
        (1_000, "KOSPI", PRE, 5),
        (9_999, "KOSPI", PRE, 10),
        (49_999, "KOSPI", PRE, 50),
        (99_999, "KOSPI", PRE, 100),
        (499_999, "KOSPI", PRE, 500),
        (500_000, "KOSPI", PRE, 1_000),
        (49_999, "KOSDAQ", PRE, 50),
        (50_000, "KOSDAQ", PRE, 100),
        (600_000, "KOSDAQ", PRE, 100),
        (1_999, "KOSPI", POST, 1),
        (2_000, "KOSPI", POST, 5),
        (19_999, "KOSDAQ", POST, 10),
        (20_000, "KOSDAQ", POST, 50),
        (199_999, "KOSDAQ", POST, 100),
        (500_000, "KOSDAQ", POST, 1_000),
    ],
)
def test_tick_bands(price, market, day, expected):
    assert tick(price, market, day) == expected


def test_limit_exact_at_integer_boundary():
    assert limits(1_000, "KOSPI", PRE) == (1_300, 700)


def test_limit_truncates_to_base_tick():
    assert limits(251, "KOSPI", PRE) == (326, 176)
    assert limits(12_340, "KOSDAQ", PRE) == (16_040, 8_640)


def test_limit_era_switch_changes_truncation():
    assert limits(1_511, "KOSDAQ", date(2023, 1, 24)) == (1_961, 1_061)
    assert limits(1_511, "KOSDAQ", POST) == (1_964, 1_058)


def test_konex_uses_15_percent():
    assert limits(1_000, "KONEX", POST) == (1_150, 850)
