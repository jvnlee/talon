from datetime import date

import polars as pl
import pytest

from talon.data.store import STOCK_INFO_SCHEMA
from talon.ingest.universe import build_universe
from talon.quant.universe import TRADABLE_STOCK, LiquidityUniverse, tradable_stock

DAY = date(2026, 7, 10)

ROWS = [
    ("005930", "주권", "보통주", "", 1e6, 9e9),
    ("088980", "부동산투자회사", "보통주", "", 1e6, 8e9),
    ("035420", "주권", "보통주", "우량기업부", 1e6, 7e9),
    ("005935", "주권", "구형우선주", "", 1e6, 6e9),
    ("123450", "주권", "보통주", "", 0.0, 5e9),
    ("999990", "주권", "보통주", "관리종목(소속부없음)", 1e6, 5e9),
    ("222220", "주권", "보통주", "", 1e6, 5e8),
]


def info_frame():
    return pl.DataFrame(
        [
            {
                "day": DAY,
                "symbol": symbol,
                "name": symbol,
                "market": "KOSPI",
                "security_group": group,
                "share_kind": kind,
                "section": section,
                "listed_on": date(2010, 1, 4),
                "shares": 1000.0,
            }
            for symbol, group, kind, section, _, _ in ROWS
        ],
        schema=STOCK_INFO_SCHEMA,
    )


def liquidity_frame():
    return pl.DataFrame(
        {
            "symbol": [row[0] for row in ROWS],
            "volume": [row[4] for row in ROWS],
            "value": [row[5] for row in ROWS],
        }
    )


def day_frame():
    return liquidity_frame().join(
        info_frame().select("symbol", tradable_stock().alias(TRADABLE_STOCK)),
        on="symbol",
        how="left",
    )


def test_filters_by_krx_classification_volume_and_thinness():
    picked = LiquidityUniverse(size=10, min_value=1_000_000_000.0).filter(day_frame())
    assert picked.get_column("symbol").to_list() == ["005930", "035420"]


def test_caps_at_size_by_trading_value():
    picked = LiquidityUniverse(size=1, min_value=0.0).filter(day_frame())
    assert picked.get_column("symbol").to_list() == ["005930"]


def test_size_must_be_positive():
    with pytest.raises(ValueError, match="유니버스"):
        LiquidityUniverse(size=0)


def test_refuses_a_panel_without_the_classification_column():
    with pytest.raises(ValueError, match="stock-info backfill"):
        LiquidityUniverse().filter(liquidity_frame())


def test_backtest_and_live_pick_the_same_symbols():
    live = build_universe(
        liquidity_frame(),
        size=3,
        min_value=1_000_000_000.0,
        info=info_frame(),
        admin=None,
        pinned=[],
    )
    picked = LiquidityUniverse(size=3, min_value=1_000_000_000.0).filter(day_frame())
    assert picked.get_column("symbol").to_list() == live.symbols
