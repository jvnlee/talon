from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from talon.backtest.engine import (
    EQUITY_SCHEMA,
    REJECTION_SCHEMA,
    TRADES_SCHEMA,
    BacktestResult,
)
from talon.backtest.metrics import summarize
from talon.backtest.report import daily_returns, write_tearsheet

pytest.importorskip("quantstats")

BASE = date(2026, 1, 5)


def equity_frame(values, cash_ratio=1.0):
    days = [BASE + timedelta(days=i) for i in range(len(values))]
    return pl.DataFrame(
        {
            "day": days,
            "cash": [v * cash_ratio for v in values],
            "position_value": [v * (1 - cash_ratio) for v in values],
            "equity": [float(v) for v in values],
            "positions": [0] * len(values),
        },
        schema=EQUITY_SCHEMA,
    )


def make_result(values, initial_cash=10_000_000.0):
    equity = equity_frame(values)
    trades = pl.DataFrame([], schema=TRADES_SCHEMA)
    rejections = pl.DataFrame([], schema=REJECTION_SCHEMA)
    stats = summarize(equity, trades, initial_cash=initial_cash, open_positions=0)
    return BacktestResult(equity=equity, trades=trades, rejections=rejections, stats=stats)


def test_daily_returns_chain_from_initial_cash():
    equity = equity_frame([10_100_000.0, 10_201_000.0, 10_098_990.0])
    returns = daily_returns(equity, 10_000_000.0)

    assert returns.iloc[0] == pytest.approx(0.01)
    assert returns.iloc[1] == pytest.approx(0.01)
    assert returns.iloc[2] == pytest.approx(-0.01)
    assert list(returns.index.date) == [BASE + timedelta(days=i) for i in range(3)]


def test_daily_returns_rejects_empty_curve():
    with pytest.raises(ValueError, match="에쿼티"):
        daily_returns(pl.DataFrame([], schema=EQUITY_SCHEMA), 10_000_000.0)


def test_tearsheet_writes_html(tmp_path):
    rng = np.random.default_rng(9)
    values = 10_000_000.0 * np.cumprod(1 + rng.normal(0.0005, 0.01, 90))
    result = make_result(list(values))

    path = write_tearsheet(result, tmp_path / "report" / "tearsheet.html", title="talon-test")

    assert path.exists()
    content = path.read_text()
    assert "talon-test" in content
    assert len(content) > 10_000
