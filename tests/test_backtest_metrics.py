from datetime import date, timedelta

import polars as pl
import pytest

from talon.backtest.engine import EQUITY_SCHEMA, TRADES_SCHEMA
from talon.backtest.metrics import summarize

BASE = date(2026, 1, 5)


def equity_frame(values):
    return pl.DataFrame(
        {
            "day": [BASE + timedelta(days=i) for i in range(len(values))],
            "cash": [float(v) for v in values],
            "position_value": [0.0] * len(values),
            "equity": [float(v) for v in values],
            "positions": [0] * len(values),
        },
        schema=EQUITY_SCHEMA,
    )


def trades_frame(pnls, holding_days=2):
    rows = []
    for i, pnl in enumerate(pnls):
        entry = BASE + timedelta(days=i)
        rows.append(
            {
                "symbol": f"S{i}",
                "entry_day": entry,
                "exit_day": entry + timedelta(days=holding_days),
                "holding_days": holding_days,
                "entry_price": 100.0,
                "exit_price": 100.0 + pnl / 100,
                "entry_notional": 10_000.0,
                "exit_notional": 10_000.0 + pnl,
                "fees": 10.0,
                "pnl": float(pnl),
                "return_pct": pnl / 10_000.0,
                "reason": "strategy",
            }
        )
    return pl.DataFrame(rows, schema=TRADES_SCHEMA)


def test_summarize_empty_equity():
    stats = summarize(
        pl.DataFrame(schema=EQUITY_SCHEMA),
        pl.DataFrame(schema=TRADES_SCHEMA),
        initial_cash=100.0,
        open_positions=0,
    )
    assert stats.final_equity == 100.0
    assert stats.total_return_pct == 0.0
    assert stats.trades == 0


def test_summarize_core_stats():
    stats = summarize(
        equity_frame([110, 99, 120]),
        trades_frame([10, -5, 20]),
        initial_cash=100.0,
        open_positions=1,
    )
    assert stats.total_return_pct == pytest.approx(20.0)
    assert stats.mdd_pct == pytest.approx(10.0)
    assert stats.trades == 3
    assert stats.wins == 2
    assert stats.win_rate_pct == pytest.approx(200 / 3)
    assert stats.profit_factor == pytest.approx(6.0)
    assert stats.avg_holding_days == pytest.approx(2.0)
    assert stats.total_fees == pytest.approx(30.0)
    assert stats.open_positions == 1
    assert stats.cagr_pct is not None and stats.cagr_pct > 0
    assert stats.sharpe is not None


def test_profit_factor_none_without_losses():
    stats = summarize(
        equity_frame([110, 120]),
        trades_frame([10, 20]),
        initial_cash=100.0,
        open_positions=0,
    )
    assert stats.profit_factor is None
    assert stats.win_rate_pct == pytest.approx(100.0)
