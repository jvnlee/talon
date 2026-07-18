from datetime import date

import polars as pl

from talon.backtest.limits import audit_price_limits

DAY = date(2020, 6, 15)


def panel_frame(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "day": row.get("day", DAY),
                "symbol": row["symbol"],
                "market": row.get("market", "KOSPI"),
                "raw_prev_close": row.get("base", 1_000.0),
                "raw_high": row["high"],
                "raw_low": row["low"],
                "limit_up_price": row.get("upper", 1_300.0),
                "limit_down_price": row.get("lower", 700.0),
                "limit_up": row.get("high", 0.0) == row.get("upper", 1_300.0),
                "limit_down": False,
                "limit_up_touch": row.get("high", 0.0) >= row.get("upper", 1_300.0),
                "limit_down_touch": False,
            }
            for row in rows
        ]
    )


def test_audit_clean_panel():
    panel = panel_frame([{"symbol": "AAA", "high": 1_100.0, "low": 950.0}])
    report = audit_price_limits(panel)
    assert report.rows == 1
    assert report.checked == 1
    assert report.upper_violations == 0
    assert report.rule_suspects == 0


def test_audit_flags_small_excess_as_suspect():
    panel = panel_frame([{"symbol": "AAA", "high": 1_305.0, "low": 950.0}])
    report = audit_price_limits(panel)
    assert report.upper_violations == 1
    assert report.rule_suspects == 1
    assert report.suspect_symbols == ["AAA"]
    assert report.samples[0]["symbol"] == "AAA"


def test_audit_classifies_huge_excess_as_no_limit_session():
    panel = panel_frame([{"symbol": "AAA", "high": 2_600.0, "low": 950.0}])
    report = audit_price_limits(panel)
    assert report.upper_violations == 1
    assert report.rule_suspects == 0
    assert report.no_limit_session_violations == 1


def test_audit_excuses_delisted_symbols():
    panel = panel_frame([{"symbol": "AAA", "high": 1_305.0, "low": 950.0}])
    delisting = pl.DataFrame({"symbol": ["AAA"]})
    report = audit_price_limits(panel, delisting=delisting)
    assert report.rule_suspects == 0
    assert report.no_limit_session_violations == 1


def test_audit_skips_null_limit_rows():
    panel = panel_frame([{"symbol": "AAA", "high": 1_100.0, "low": 950.0}]).with_columns(
        pl.lit(None, dtype=pl.Float64).alias("limit_up_price")
    )
    report = audit_price_limits(panel)
    assert report.checked == 0
    assert report.skipped == 1
