from datetime import date

import pytest

from talon.backtest.costs import KrCostModel

MODEL = KrCostModel()


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2016, 7, 1), 0.0030),
        (date(2019, 6, 2), 0.0030),
        (date(2019, 6, 3), 0.0025),
        (date(2020, 12, 31), 0.0025),
        (date(2021, 1, 1), 0.0023),
        (date(2023, 6, 1), 0.0020),
        (date(2024, 2, 1), 0.0018),
        (date(2025, 7, 1), 0.0015),
        (date(2026, 7, 11), 0.0020),
    ],
)
def test_sell_tax_schedule(day, expected):
    assert MODEL.sell_tax_rate(day) == pytest.approx(expected)


def test_buy_fee_is_commission_only():
    assert MODEL.buy_fee(1_000_000, date(2026, 7, 10)) == pytest.approx(150.0)


def test_sell_fee_includes_tax():
    assert MODEL.sell_fee(1_000_000, date(2026, 7, 10)) == pytest.approx(150.0 + 2_000.0)
    assert MODEL.sell_fee(1_000_000, date(2025, 7, 10)) == pytest.approx(150.0 + 1_500.0)
