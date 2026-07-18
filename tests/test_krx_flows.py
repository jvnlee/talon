from datetime import UTC, date, datetime

import pandas as pd
import polars as pl
import pytest

import talon.sources.krx_flows as krx_flows
from talon.errors import SchemaDriftError, SourceError
from talon.sources.krx_flows import (
    INVESTOR_LABELS,
    _flows_frame,
    clearing_residual_pct,
    fetch_investor_flows,
)

DAY = date(2026, 7, 16)
FETCHED = datetime(2026, 7, 16, 9, 5, tzinfo=UTC)


def krx_frame(rows: dict[str, list[float]], symbols: list[str]) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "종목명": [f"이름{s}" for s in symbols],
            "매도거래량": rows.get("sell_volume", [0.0] * len(symbols)),
            "매수거래량": rows.get("buy_volume", [0.0] * len(symbols)),
            "순매수거래량": rows.get("net_volume", [0.0] * len(symbols)),
            "매도거래대금": rows.get("sell_value", [0.0] * len(symbols)),
            "매수거래대금": rows.get("buy_value", [0.0] * len(symbols)),
            "순매수거래대금": rows.get("net_value", [0.0] * len(symbols)),
        },
        index=pd.Index(symbols, name="티커"),
    )
    return frame


def test_flows_frame_maps_columns_and_symbols():
    pdf = krx_frame(
        {
            "sell_volume": [10.0, 20.0],
            "buy_volume": [15.0, 5.0],
            "net_volume": [5.0, -15.0],
            "sell_value": [1000.0, 2000.0],
            "buy_value": [1500.0, 500.0],
            "net_value": [500.0, -1500.0],
        },
        ["005930", "000660"],
    )
    frame = _flows_frame(pdf, DAY, "foreigner", FETCHED)
    assert frame.height == 2
    row = frame.filter(pl.col("symbol") == "005930").row(0, named=True)
    assert row["investor"] == "foreigner"
    assert row["day"] == DAY
    assert row["net_value"] == 500.0
    assert row["buy_volume"] == 15.0


def test_flows_frame_empty_returns_typed_empty():
    frame = _flows_frame(pd.DataFrame(), DAY, "foreigner", FETCHED)
    assert frame.is_empty()
    assert "net_value" in frame.columns


def test_flows_frame_schema_drift_raises():
    pdf = krx_frame({}, ["005930"]).drop(columns=["순매수거래대금"])
    with pytest.raises(SchemaDriftError):
        _flows_frame(pdf, DAY, "foreigner", FETCHED)


class FakeStock:
    def __init__(self, empty_for: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self.empty_for = empty_for or set()

    def get_market_net_purchases_of_equities_by_ticker(self, start, end, market, investor):
        assert start == end == "20260716"
        assert market == "ALL"
        self.calls.append(investor)
        if investor in self.empty_for:
            return pd.DataFrame()
        return krx_frame({"net_value": [1.0], "buy_value": [2.0], "sell_value": [1.0]}, ["005930"])


def test_fetch_investor_flows_covers_all_investors(monkeypatch):
    fake = FakeStock()
    monkeypatch.setattr(krx_flows, "_load_pykrx", lambda credentials: fake)
    frame = fetch_investor_flows(DAY, sleep=lambda _: None)
    assert fake.calls == list(INVESTOR_LABELS)
    assert frame.height == len(INVESTOR_LABELS)
    assert set(frame.get_column("investor")) == set(INVESTOR_LABELS.values())


def test_fetch_investor_flows_requires_core_investors(monkeypatch):
    fake = FakeStock(empty_for={"개인"})
    monkeypatch.setattr(krx_flows, "_load_pykrx", lambda credentials: fake)
    with pytest.raises(SourceError, match="individual"):
        fetch_investor_flows(DAY, sleep=lambda _: None)


def test_clearing_residual_pct():
    balanced = pl.DataFrame(
        {"buy_value": [100.0, 200.0], "net_value": [50.0, -50.0]},
    )
    assert clearing_residual_pct(balanced) == 0.0
    skewed = pl.DataFrame({"buy_value": [100.0, 100.0], "net_value": [50.0, -30.0]})
    assert clearing_residual_pct(skewed) == pytest.approx(10.0)
    assert clearing_residual_pct(pl.DataFrame({"buy_value": [0.0], "net_value": [0.0]})) == 0.0
