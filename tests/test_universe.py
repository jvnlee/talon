import polars as pl

from talon.ingest.universe import build_universe, candidate_symbols
from talon.models import StockInfo


def liquidity_frame():
    return pl.DataFrame(
        {
            "symbol": ["005930", "000660", "005935", "123450", "999990", "111110"],
            "value": [5e12, 3e12, 2e12, 1e12, 5e11, 5e8],
            "volume": [1e6, 1e6, 1e6, 0.0, 1e6, 1e6],
        }
    )


def stock_info(symbol: str, security_type: str = "STOCK", common: bool = True) -> StockInfo:
    return StockInfo.model_validate(
        {
            "symbol": symbol,
            "name": symbol,
            "market": "KOSPI",
            "securityType": security_type,
            "isCommonShare": common,
            "status": "ACTIVE",
        }
    )


def test_build_universe_heuristic_filters():
    build = build_universe(
        liquidity_frame(),
        size=10,
        min_value=1e9,
        admin={"999990"},
        stock_info=None,
        pinned=[],
    )
    assert build.symbols == ["005930", "000660"]
    assert build.criteria["admin_excluded"] is True
    assert build.criteria["toss_filtered"] is False


def test_build_universe_with_stock_info():
    info = {
        "005930": stock_info("005930"),
        "000660": stock_info("000660", security_type="ETF"),
        "005935": stock_info("005935", common=False),
        "999990": stock_info("999990"),
    }
    build = build_universe(
        liquidity_frame(),
        size=10,
        min_value=1e9,
        admin=None,
        stock_info=info,
        pinned=[],
    )
    assert build.symbols == ["005930", "999990"]
    assert build.criteria["toss_filtered"] is True


def test_build_universe_size_and_pinned():
    build = build_universe(
        liquidity_frame(),
        size=1,
        min_value=0.0,
        admin=None,
        stock_info=None,
        pinned=["105560", "005930"],
    )
    assert build.symbols == ["105560", "005930"]


def test_candidate_symbols_orders_by_value():
    assert candidate_symbols(liquidity_frame(), 3) == ["005930", "000660", "005935"]
