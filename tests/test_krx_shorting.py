from datetime import UTC, date, datetime

import pandas as pd
import polars as pl
import pytest

import talon.sources.krx_shorting as krx_shorting
from talon.errors import SchemaDriftError
from talon.sources.krx_shorting import (
    _balance_market_frame,
    _investor_market_frame,
    _trade_market_frame,
    fetch_shorting,
    fetch_shorting_investor,
    market_short_volume,
)

DAY = date(2026, 7, 21)
FETCHED = datetime(2026, 7, 22, 9, 30, tzinfo=UTC)


def trade_frame(symbols, short, total, ratio):
    return pd.DataFrame(
        {"공매도": short, "매수": total, "비중": ratio},
        index=pd.Index(symbols, name="티커"),
    )


def test_trade_market_frame_maps_and_keeps_krx_ratio():
    vol = trade_frame(["005930", "000660"], [468511, 100], [31095272, 1000], [1.51, 10.0])
    val = trade_frame(["005930", "000660"], [1000, 20], [200000, 400], [0.5, 5.0])
    frame = _trade_market_frame(vol, val, DAY, "KOSPI", FETCHED)
    row = frame.filter(pl.col("symbol") == "005930").row(0, named=True)
    assert row["market"] == "KOSPI"
    assert row["short_volume"] == 468511
    assert row["total_volume_consolidated"] == 31095272
    assert row["short_ratio_pct"] == pytest.approx(1.51)
    assert row["short_value"] == 1000
    assert row["total_value_consolidated"] == 200000
    assert row["short_value_ratio_pct"] == pytest.approx(0.5)


def test_trade_market_frame_empty_returns_typed_empty():
    frame = _trade_market_frame(pd.DataFrame(), pd.DataFrame(), DAY, "KOSPI", FETCHED)
    assert frame.is_empty()
    assert "short_ratio_pct" in frame.columns


def test_trade_market_frame_schema_drift_raises():
    vol = trade_frame(["005930"], [1], [2], [50.0]).drop(columns=["비중"])
    val = trade_frame(["005930"], [1], [2], [50.0])
    with pytest.raises(SchemaDriftError):
        _trade_market_frame(vol, val, DAY, "KOSPI", FETCHED)


class FakeStock:
    def __init__(self):
        self.volume_calls = []
        self.value_calls = []

    def get_shorting_volume_by_ticker(self, text, market):
        self.volume_calls.append((text, market))
        base = 100 if market == "KOSPI" else 5
        return trade_frame([f"{market}1"], [base], [base * 10], [10.0])

    def get_shorting_value_by_ticker(self, text, market):
        self.value_calls.append((text, market))
        base = 1000 if market == "KOSPI" else 50
        return trade_frame([f"{market}1"], [base], [base * 10], [10.0])


def test_fetch_shorting_loops_markets_and_concats(monkeypatch):
    fake = FakeStock()
    monkeypatch.setattr(krx_shorting, "_load_pykrx", lambda credentials: fake)
    frame = fetch_shorting(DAY, sleep=lambda _: None)
    assert fake.volume_calls == [("20260721", "KOSPI"), ("20260721", "KOSDAQ")]
    assert set(frame.get_column("market")) == {"KOSPI", "KOSDAQ"}
    assert frame.height == 2
    assert market_short_volume(frame) == 105


def balance_frame(symbols, qty, listed, value, cap, ratio):
    return pd.DataFrame(
        {
            "공매도잔고": qty,
            "상장주식수": listed,
            "공매도금액": value,
            "시가총액": cap,
            "비중": ratio,
        },
        index=pd.Index(symbols, name="티커"),
    )


def test_balance_market_frame_recomputes_ratio_from_qty():
    pdf = balance_frame(["005930"], [100], [1000], [50000], [700000], [0.0625])
    frame = _balance_market_frame(pdf, DAY, "KOSPI", FETCHED)
    row = frame.row(0, named=True)
    assert row["short_balance_qty"] == 100
    assert row["listed_shares"] == 1000
    assert row["short_balance_value"] == 50000
    assert row["market_cap"] == 700000
    assert row["short_balance_ratio_pct"] == pytest.approx(10.0)


def test_balance_market_frame_zero_listed_yields_zero_ratio():
    pdf = balance_frame(["005930"], [0], [0], [0], [0], [0.0])
    frame = _balance_market_frame(pdf, DAY, "KOSPI", FETCHED)
    assert frame.row(0, named=True)["short_balance_ratio_pct"] == 0.0


def test_balance_market_frame_schema_drift_raises():
    pdf = balance_frame(["005930"], [1], [2], [3], [4], [5.0]).drop(columns=["상장주식수"])
    with pytest.raises(SchemaDriftError):
        _balance_market_frame(pdf, DAY, "KOSPI", FETCHED)


def investor_frame(days, columns):
    return pd.DataFrame(
        columns,
        index=pd.DatetimeIndex([pd.Timestamp(day) for day in days], name="날짜"),
    )


def test_investor_market_frame_melts_and_joins_measures():
    days = [date(2025, 6, 5)]
    vol = investor_frame(
        days,
        {"기관": [10], "개인": [20], "외국인": [30], "기타": [40], "합계": [100]},
    )
    val = investor_frame(
        days,
        {"기관": [1], "개인": [2], "외국인": [3], "기타": [4], "합계": [10]},
    )
    frame = _investor_market_frame(vol, val, "KOSPI", FETCHED)
    assert frame.height == 5
    assert set(frame.get_column("investor")) == {
        "institution",
        "retail",
        "foreign",
        "other",
        "total",
    }
    foreign = frame.filter(pl.col("investor") == "foreign").row(0, named=True)
    assert foreign["day"] == date(2025, 6, 5)
    assert foreign["vol_shares"] == 30
    assert foreign["value_krw"] == 3
    total = frame.filter(pl.col("investor") == "total").row(0, named=True)
    assert total["vol_shares"] == 100


class FakeInvestorStock:
    def get_shorting_investor_volume_by_date(self, fromdate, todate, market):
        base = 100 if market == "KOSPI" else 5
        return investor_frame(
            [date(2025, 1, 2), date(2025, 1, 3)],
            {
                "기관": [base, base],
                "개인": [0, 0],
                "외국인": [0, 0],
                "기타": [0, 0],
                "합계": [base, base],
            },
        )

    def get_shorting_investor_value_by_date(self, fromdate, todate, market):
        base = 1000 if market == "KOSPI" else 50
        return investor_frame(
            [date(2025, 1, 2), date(2025, 1, 3)],
            {
                "기관": [base, base],
                "개인": [0, 0],
                "외국인": [0, 0],
                "기타": [0, 0],
                "합계": [base, base],
            },
        )


def test_fetch_shorting_investor_range_covers_markets_and_days(monkeypatch):
    monkeypatch.setattr(krx_shorting, "_load_pykrx", lambda credentials: FakeInvestorStock())
    frame = fetch_shorting_investor(date(2025, 1, 1), date(2025, 1, 31), sleep=lambda _: None)
    assert set(frame.get_column("market")) == {"KOSPI", "KOSDAQ"}
    assert frame.select("day").n_unique() == 2
    assert frame.height == 2 * 2 * 5


def test_market_short_volume_on_empty():
    assert market_short_volume(pl.DataFrame(schema={"short_volume": pl.Int64()})) == 0
