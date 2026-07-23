from datetime import date

import polars as pl
import pytest

import talon.sources.krx_program as kp
from talon.errors import SchemaDriftError

ARB_ROW = {
    "ITM_TP_NM": "차익",
    "ASK_TRDVOL": "2,307,149",
    "BID_TRDVOL": "2,182,460",
    "NETBID_TRDVOL": "-124,689",
    "ASK_TRDVAL": "447,084,535,800",
    "BID_TRDVAL": "407,608,914,208",
    "NETBID_TRDVAL": "-39,475,621,592",
}
NONARB_ROW = {
    "ITM_TP_NM": "비차익",
    "ASK_TRDVOL": "231,437,715",
    "BID_TRDVOL": "265,541,740",
    "NETBID_TRDVOL": "34,104,025",
    "ASK_TRDVAL": "9,510,721,003,676",
    "BID_TRDVAL": "11,467,273,659,659",
    "NETBID_TRDVAL": "1,956,552,655,983",
}
TOTAL_ROW = {
    "ITM_TP_NM": "전체",
    "ASK_TRDVOL": "233,744,864",
    "BID_TRDVOL": "267,724,200",
    "NETBID_TRDVOL": "33,979,336",
    "ASK_TRDVAL": "9,957,805,539,476",
    "BID_TRDVAL": "11,874,882,573,867",
    "NETBID_TRDVAL": "1,917,077,034,391",
}
ROWS = [ARB_ROW, NONARB_ROW, TOTAL_ROW]


def capture(monkeypatch, rows):
    calls: list[tuple[str, dict[str, str], str]] = []

    def fake(bld, params, *, credentials, sleep, data_key="output"):
        calls.append((bld, params, data_key))
        return rows

    monkeypatch.setattr(kp, "_fetch_rows", fake)
    return calls


def test_fetch_assembles_params_and_maps_components(monkeypatch):
    calls = capture(monkeypatch, ROWS)
    frame = kp.fetch_program_market(date(2026, 7, 23), "STK")
    bld, params, data_key = calls[0]
    assert bld == kp.PROGRAM_MARKET_BLD
    assert data_key == "output"
    assert params["mktId"] == "STK"
    assert params["strtDd"] == "20260723"
    assert params["endDd"] == "20260723"
    assert params["share"] == "1"
    assert params["money"] == "1"
    assert set(frame["component"].to_list()) == {"arb", "nonarb", "total"}
    assert frame.height == 3
    assert frame["market"].unique().to_list() == ["STK"]
    total = frame.filter(pl.col("component") == "total").row(0, named=True)
    assert total["sell_qty"] == 233744864.0
    assert total["buy_value"] == 11874882573867.0
    assert total["net_value"] == 1917077034391.0


def test_identities_hold_across_components(monkeypatch):
    capture(monkeypatch, ROWS)
    frame = kp.fetch_program_market(date(2026, 7, 23), "KSQ")
    rows = {r["component"]: r for r in frame.iter_rows(named=True)}
    for row in rows.values():
        assert row["net_qty"] == row["buy_qty"] - row["sell_qty"]
        assert row["net_value"] == row["buy_value"] - row["sell_value"]
    for field in ("sell_qty", "buy_qty", "sell_value", "buy_value"):
        assert rows["total"][field] == rows["arb"][field] + rows["nonarb"][field]


def test_schema_drift_on_missing_column(monkeypatch):
    bad = [{k: v for k, v in TOTAL_ROW.items() if k != "NETBID_TRDVAL"}]
    capture(monkeypatch, bad)
    with pytest.raises(SchemaDriftError, match="program-market columns missing"):
        kp.fetch_program_market(date(2026, 7, 23), "STK")


def test_empty_response_is_empty_frame(monkeypatch):
    capture(monkeypatch, [])
    frame = kp.fetch_program_market(date(2002, 1, 2), "STK")
    assert frame.is_empty()
    assert frame.columns == list(kp.PROGRAM_MARKET_1D_SCHEMA)
