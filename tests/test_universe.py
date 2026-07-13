from datetime import date

import polars as pl
import pytest

from talon.data.store import STOCK_INFO, STOCK_INFO_SCHEMA
from talon.errors import SourceError
from talon.ingest.universe import build_universe, candidate_symbols, latest_stock_info

DAY = date(2026, 7, 10)


def liquidity_frame():
    return pl.DataFrame(
        {
            "symbol": ["005930", "088980", "900140", "005935", "999990", "111110"],
            "value": [5e12, 3e12, 2e12, 1e12, 5e11, 5e8],
            "volume": [1e6, 1e6, 1e6, 1e6, 1e6, 1e6],
        }
    )


def info_frame(rows, day=DAY):
    return pl.DataFrame(
        [
            {
                "day": day,
                "symbol": symbol,
                "name": symbol,
                "market": market,
                "security_group": group,
                "share_kind": kind,
                "section": section,
                "listed_on": date(2010, 1, 4),
                "shares": 1000.0,
            }
            for symbol, market, group, kind, section in rows
        ],
        schema=STOCK_INFO_SCHEMA,
    )


KRX_INFO = info_frame(
    [
        ("005930", "KOSPI", "주권", "보통주", ""),
        ("088980", "KOSPI", "부동산투자회사", "보통주", ""),
        ("900140", "KOSDAQ", "외국주권", "보통주", "외국기업(소속부없음)"),
        ("005935", "KOSPI", "주권", "구형우선주", ""),
        ("999990", "KOSDAQ", "주권", "보통주", "관리종목(소속부없음)"),
        ("111110", "KOSDAQ", "주권", "보통주", "우량기업부"),
    ]
)


def test_only_plain_common_shares_survive_krx_classification():
    build = build_universe(
        liquidity_frame(),
        size=10,
        min_value=1e9,
        info=KRX_INFO,
        admin=None,
        pinned=[],
    )
    assert build.symbols == ["005930"]


def test_reit_and_foreign_share_no_longer_pass_as_stocks():
    assert "088980".endswith("0") and "900140".endswith("0")
    passed = build_universe(
        liquidity_frame(), size=10, min_value=1e9, info=KRX_INFO, admin=None, pinned=[]
    ).symbols
    assert "088980" not in passed
    assert "900140" not in passed


def test_kosdaq_admin_issue_excluded_by_krx_section():
    build = build_universe(
        liquidity_frame(), size=10, min_value=0.0, info=KRX_INFO, admin=None, pinned=[]
    )
    assert "999990" not in build.symbols
    assert "111110" in build.symbols


def test_admin_list_still_layered_on_top_for_kospi():
    build = build_universe(
        liquidity_frame(),
        size=10,
        min_value=1e9,
        info=KRX_INFO,
        admin={"005930"},
        pinned=[],
    )
    assert build.symbols == []
    assert build.criteria["admin_excluded"] is True


def test_symbol_missing_from_stock_info_is_excluded():
    build = build_universe(
        liquidity_frame(),
        size=10,
        min_value=0.0,
        info=KRX_INFO.filter(pl.col("symbol") != "005930"),
        admin=None,
        pinned=[],
    )
    assert "005930" not in build.symbols


def test_size_and_pinned():
    build = build_universe(
        liquidity_frame(),
        size=1,
        min_value=0.0,
        info=KRX_INFO,
        admin=None,
        pinned=["105560"],
    )
    assert build.symbols == ["105560", "005930"]


def test_candidate_symbols_orders_by_value():
    assert candidate_symbols(liquidity_frame(), 3) == ["005930", "088980", "900140"]


def test_latest_stock_info_uses_most_recent_snapshot_at_or_before_day(snapshots):
    snapshots.write_date(
        STOCK_INFO,
        date(2026, 7, 9),
        info_frame([("005930", "KOSPI", "주권", "보통주", "")], date(2026, 7, 9)),
    )
    snapshots.write_date(
        STOCK_INFO,
        date(2026, 7, 13),
        info_frame([("005930", "KOSPI", "주권", "보통주", "")], date(2026, 7, 13)),
    )

    as_of, frame = latest_stock_info(snapshots, date(2026, 7, 10), max_stale_days=10)
    assert as_of == date(2026, 7, 9)
    assert frame.get_column("day").unique().to_list() == [date(2026, 7, 9)]


def test_latest_stock_info_refuses_stale_data(snapshots):
    snapshots.write_date(
        STOCK_INFO,
        date(2026, 6, 1),
        info_frame([("005930", "KOSPI", "주권", "보통주", "")], date(2026, 6, 1)),
    )
    with pytest.raises(SourceError, match="낡았습니다"):
        latest_stock_info(snapshots, DAY, max_stale_days=10)


def test_latest_stock_info_refuses_empty_dataset(snapshots):
    with pytest.raises(SourceError, match="종목기본정보가 없습니다"):
        latest_stock_info(snapshots, DAY, max_stale_days=10)
