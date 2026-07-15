from datetime import date

import polars as pl
import pytest

from talon.data.store import (
    FLOW_RANKING_INTRADAY,
    FRGNMEM_RANKING_INTRADAY,
    INVESTOR_ESTIMATE_INTRADAY,
    ORDERBOOK_INTRADAY,
)
from talon.errors import SourceError
from talon.ingest.kis_sweep import collect_kis_sweep, sweep_symbols

DAY = date(2026, 7, 14)
SLOT = "15:10"


def stock_frame(count=5):
    return pl.DataFrame(
        {
            "day": [DAY] * count,
            "symbol": [f"{i:06d}" for i in range(count)],
            "open": [100.0] * count,
            "high": [110.0] * count,
            "low": [90.0] * count,
            "close": [105.0] * count,
            "volume": [1000.0] * count,
            "value": [float((count - i) * 1e9) for i in range(count)],
            "change_pct": [1.0] * count,
        }
    )


def orderbook_row(symbol):
    row = {"symbol": symbol}
    for level in range(1, 11):
        row[f"ask_price_{level}"] = 100.0 + level
        row[f"ask_qty_{level}"] = 10.0 * level
        row[f"bid_price_{level}"] = 100.0 - level
        row[f"bid_qty_{level}"] = 20.0 * level
    row |= {
        "total_ask_qty": 550.0,
        "total_bid_qty": 1100.0,
        "net_bid_qty": 550.0,
        "accept_hour": "151000",
        "market_phase": "20",
        "price": 100.0,
        "open": 99.0,
        "high": 101.0,
        "low": 98.0,
        "prev_close": 97.0,
        "antc_price": 99.0,
        "antc_qty": 1000.0,
        "antc_phase": "112",
        "vi_code": "N",
    }
    return row


def investor_row(symbol):
    return {"symbol": symbol, "bucket": 3, "frgn_qty": 1000.0, "orgn_qty": -500.0, "sum_qty": 500.0}


def ranking_row(side, symbol="005930"):
    return {
        "side": side,
        "rank": 1,
        "symbol": symbol,
        "name": "삼성전자",
        "total_qty": 900.0,
        "frgn_qty": 400.0,
        "orgn_qty": 400.0,
        "etc_corp_qty": 100.0,
        "ivtr_qty": 100.0,
        "bank_qty": 0.0,
        "insu_qty": 0.0,
        "mrbn_qty": 0.0,
        "fund_qty": 300.0,
        "etc_fin_qty": 0.0,
        "frgn_amount": 1000.0,
        "orgn_amount": 900.0,
        "etc_corp_amount": 100.0,
        "price": 283000.0,
        "change_pct": 6.39,
        "volume": 1234.0,
    }


def frgnmem_row(side, symbol="005930"):
    return {
        "side": side,
        "rank": 1,
        "symbol": symbol,
        "name": "삼성전자",
        "net_qty": 500.0,
        "buy_qty": 1200.0,
        "sell_qty": 700.0,
        "price": 283000.0,
        "change_pct": 6.39,
        "volume": 1234.0,
    }


@pytest.fixture
def kis_cfg(cfg):
    cfg.kis_app_key = "key"
    cfg.kis_app_secret = "secret"
    cfg.kis_sweep_size = 3
    return cfg


@pytest.fixture(autouse=True)
def fake_endpoints(monkeypatch):
    monkeypatch.setattr(
        "talon.ingest.kis_sweep.fetch_orderbook", lambda client, symbol: orderbook_row(symbol)
    )
    monkeypatch.setattr(
        "talon.ingest.kis_sweep.fetch_investor_estimate",
        lambda client, symbol: investor_row(symbol),
    )
    monkeypatch.setattr(
        "talon.ingest.kis_sweep.fetch_flow_ranking", lambda client, side: [ranking_row(side)]
    )
    monkeypatch.setattr(
        "talon.ingest.kis_sweep.fetch_frgnmem_ranking", lambda client, side: [frgnmem_row(side)]
    )


def run(cfg, snapshots, frame):
    return collect_kis_sweep(cfg, snapshots=snapshots, slot=SLOT, day=DAY, stock_frame=frame)


def test_all_parts_collect(kis_cfg, snapshots):
    summary = run(kis_cfg, snapshots, stock_frame())

    assert summary.parts == {
        "kis_orderbook": "ok",
        "kis_investor": "ok",
        "kis_flow_rank": "ok",
        "kis_frgnmem": "ok",
    }
    book = snapshots.read_date(ORDERBOOK_INTRADAY, DAY)
    assert book.height == 3
    assert book["slot"].unique().to_list() == [SLOT]
    assert book["captured_at"].null_count() == 0
    estimate = snapshots.read_date(INVESTOR_ESTIMATE_INTRADAY, DAY)
    assert estimate["bucket"].unique().to_list() == [3]
    flow = snapshots.read_date(FLOW_RANKING_INTRADAY, DAY)
    assert sorted(flow["side"].to_list()) == ["buy", "sell"]
    frgnmem = snapshots.read_date(FRGNMEM_RANKING_INTRADAY, DAY)
    assert frgnmem.height == 2


def test_sweep_targets_top_value_plus_pinned(kis_cfg):
    kis_cfg.pinned_symbols = ["999999", "000001"]

    symbols = sweep_symbols(kis_cfg, stock_frame(5))

    assert symbols == ["000000", "000001", "000002", "999999"]


def test_skipped_without_kis_keys(cfg, snapshots):
    summary = run(cfg, snapshots, stock_frame())

    assert set(summary.parts.values()) == {"skipped-no-kis"}
    assert snapshots.read_date(ORDERBOOK_INTRADAY, DAY) is None


def test_rankings_still_run_without_stock_frame(kis_cfg, snapshots):
    summary = run(kis_cfg, snapshots, None)

    assert summary.parts["kis_orderbook"] == "skipped-no-universe"
    assert summary.parts["kis_investor"] == "skipped-no-universe"
    assert summary.parts["kis_flow_rank"] == "ok"
    assert snapshots.read_date(FLOW_RANKING_INTRADAY, DAY) is not None


def test_one_part_failing_keeps_the_rest(monkeypatch, kis_cfg, snapshots):
    def boom(client, symbol):
        raise SourceError("kis down")

    monkeypatch.setattr("talon.ingest.kis_sweep.fetch_orderbook", boom)

    summary = run(kis_cfg, snapshots, stock_frame())

    assert summary.parts["kis_orderbook"].startswith("error")
    assert summary.parts["kis_investor"] == "ok"
    assert snapshots.read_date(ORDERBOOK_INTRADAY, DAY) is None
    assert snapshots.read_date(INVESTOR_ESTIMATE_INTRADAY, DAY) is not None


def test_few_symbol_failures_are_tolerated(monkeypatch, kis_cfg, snapshots):
    kis_cfg.kis_sweep_size = 5

    def flaky(client, symbol):
        if symbol == "000004":
            raise SourceError("timeout")
        return orderbook_row(symbol)

    monkeypatch.setattr("talon.ingest.kis_sweep.fetch_orderbook", flaky)

    summary = run(kis_cfg, snapshots, stock_frame(5))

    assert summary.parts["kis_orderbook"].startswith("partial")
    assert snapshots.read_date(ORDERBOOK_INTRADAY, DAY).height == 4


def test_empty_estimates_are_reported(monkeypatch, kis_cfg, snapshots):
    monkeypatch.setattr(
        "talon.ingest.kis_sweep.fetch_investor_estimate", lambda client, symbol: None
    )

    summary = run(kis_cfg, snapshots, stock_frame())

    assert summary.parts["kis_investor"] == "empty"
    assert snapshots.read_date(INVESTOR_ESTIMATE_INTRADAY, DAY) is None
