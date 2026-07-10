from datetime import date

import pandas as pd
import pytest

from talon.data.store import DAILY_SNAPSHOT_SCHEMA, MARKET_CAP_SCHEMA
from talon.errors import SchemaDriftError
from talon.sources.fdr_daily import fetch_krx_listing

DAY = date(2026, 7, 10)


def listing_pdf():
    return pd.DataFrame(
        {
            "Code": ["005930", "000660", "999990", "888880"],
            "Name": ["삼성전자", "SK하이닉스", "쓰레기행", "거래정지"],
            "Close": [70500, 252000, 0, 1000],
            "Open": [70000, 250000, 0, 0],
            "High": [71000, 255000, 0, 0],
            "Low": [69000, 248000, 0, 0],
            "Volume": [1000, 2000, 0, 0],
            "Amount": [5e12, 3e12, 0, 0],
            "Marcap": [4e14, 1.8e14, 0, 5e9],
            "Stocks": [5.9e9, 7.3e8, 1, 1],
            "ChagesRatio": [0.5, -0.3, 0.0, 0.0],
        }
    )


def test_listing_parses_and_filters(monkeypatch):
    monkeypatch.setattr("FinanceDataReader.StockListing", lambda market: listing_pdf())
    daily, caps = fetch_krx_listing(DAY)

    assert dict(daily.schema) == DAILY_SNAPSHOT_SCHEMA
    assert daily.get_column("symbol").to_list() == ["005930", "000660"]
    assert daily.get_column("close").to_list() == [70500.0, 252000.0]
    assert daily.get_column("value").to_list() == [5e12, 3e12]
    assert daily.get_column("day").to_list() == [DAY, DAY]

    assert dict(caps.schema) == MARKET_CAP_SCHEMA
    assert caps.get_column("symbol").to_list() == ["005930", "000660", "888880"]
    assert caps.get_column("cap").to_list() == [4e14, 1.8e14, 5e9]


def test_listing_schema_drift(monkeypatch):
    pdf = listing_pdf().drop(columns=["Marcap"])
    monkeypatch.setattr("FinanceDataReader.StockListing", lambda market: pdf)
    with pytest.raises(SchemaDriftError):
        fetch_krx_listing(DAY)


def test_listing_empty(monkeypatch):
    monkeypatch.setattr("FinanceDataReader.StockListing", lambda market: pd.DataFrame())
    daily, caps = fetch_krx_listing(DAY)
    assert daily.is_empty()
    assert caps.is_empty()


def test_listing_optional_columns_null(monkeypatch):
    pdf = listing_pdf().drop(columns=["ChagesRatio"])
    monkeypatch.setattr("FinanceDataReader.StockListing", lambda market: pdf)
    daily, _ = fetch_krx_listing(DAY)
    assert daily.get_column("change_pct").to_list() == [None, None]
