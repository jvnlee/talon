import polars as pl
import pytest

from talon.ingest.universe import build_universe
from talon.quant.universe import LiquidityUniverse


def day_frame(rows):
    return pl.DataFrame(
        rows,
        schema={"symbol": pl.Utf8(), "volume": pl.Float64(), "value": pl.Float64()},
        orient="row",
    )


FRAME = day_frame(
    [
        ("005930", 1e6, 9e9),
        ("000660", 1e6, 8e9),
        ("035420", 1e6, 7e9),
        ("005935", 1e6, 6e9),
        ("123450", 0.0, 5e9),
        ("222220", 1e6, 5e8),
    ]
)


def test_filters_preferred_zero_volume_and_thin_names():
    picked = LiquidityUniverse(size=10, min_value=1_000_000_000.0).filter(FRAME)
    assert picked.get_column("symbol").to_list() == ["005930", "000660", "035420"]


def test_caps_at_size_by_trading_value():
    picked = LiquidityUniverse(size=2, min_value=0.0).filter(FRAME)
    assert picked.get_column("symbol").to_list() == ["005930", "000660"]


def test_size_must_be_positive():
    with pytest.raises(ValueError, match="유니버스"):
        LiquidityUniverse(size=0)


def test_matches_live_universe_fallback_path():
    live = build_universe(
        FRAME,
        size=3,
        min_value=1_000_000_000.0,
        admin=None,
        stock_info=None,
        pinned=[],
    )
    picked = LiquidityUniverse(size=3, min_value=1_000_000_000.0).filter(FRAME)
    assert picked.get_column("symbol").to_list() == live.symbols
