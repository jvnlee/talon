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


def alternating_curve(count=60, initial=100.0, step=0.002):
    values = []
    value = initial
    for i in range(count):
        value *= 1 + (step if i % 2 == 0 else 0.0)
        values.append(value)
    return pl.Series(values)


def test_expected_max_sharpe_two_trials_unit_variance():
    import math
    import statistics

    from talon.backtest.metrics import EULER_MASCHERONI, expected_max_sharpe

    expected = EULER_MASCHERONI * statistics.NormalDist().inv_cdf(1 - 1 / (2 * math.e))
    assert expected_max_sharpe(2, 1.0) == pytest.approx(expected)


def test_expected_max_sharpe_grows_with_trials_and_variance():
    from talon.backtest.metrics import expected_max_sharpe

    assert expected_max_sharpe(100, 1.0) > expected_max_sharpe(2, 1.0)
    assert expected_max_sharpe(10, 4.0) == pytest.approx(2 * expected_max_sharpe(10, 1.0))


def test_expected_max_sharpe_requires_two_trials():
    from talon.backtest.metrics import expected_max_sharpe

    with pytest.raises(ValueError):
        expected_max_sharpe(1, 1.0)


def test_deflated_sharpe_clears_low_variance_trials():
    from talon.backtest.metrics import deflated_sharpe

    result = deflated_sharpe(alternating_curve(), 100.0, [0.1, 0.2])
    assert result is not None
    assert result.trials == 2
    assert result.sharpe_daily == pytest.approx(0.99, rel=0.02)
    assert result.margin > 0
    assert result.probability > 0.95


def test_deflated_sharpe_fails_against_high_variance_trials():
    from talon.backtest.metrics import deflated_sharpe

    result = deflated_sharpe(alternating_curve(), 100.0, [-2.0, 2.0])
    assert result is not None
    assert result.margin < 0
    assert result.probability < 0.5


def test_deflated_sharpe_needs_history_and_trials():
    from talon.backtest.metrics import deflated_sharpe

    assert deflated_sharpe(alternating_curve(), 100.0, [0.1]) is None
    assert deflated_sharpe(pl.Series([100.0, 100.2]), 100.0, [0.1, 0.2]) is None
    assert deflated_sharpe(pl.Series([100.0] * 20), 100.0, [0.1, 0.2]) is None
