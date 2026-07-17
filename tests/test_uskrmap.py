import re
from datetime import date

import polars as pl

from talon.data.store import US_KR_MAP_SCHEMA
from talon.data.uskrmap import (
    US_KR_MAP_SEED,
    build_us_kr_map,
    mapped_kr_symbols,
    select_active,
)


def test_builds_frame_with_schema():
    frame = build_us_kr_map()

    assert frame.height == len(US_KR_MAP_SEED)
    assert dict(frame.schema) == US_KR_MAP_SCHEMA


def test_all_kr_symbols_are_six_digit_codes():
    for row in US_KR_MAP_SEED:
        for symbol in row["kr_symbols"]:
            assert re.fullmatch(r"\d{6}", symbol), (row["us_symbol"], symbol)


def test_tsla_battery_row_is_point_in_time_versioned():
    frame = build_us_kr_map()

    before = select_active(frame, date(2021, 6, 1)).filter(pl.col("us_symbol") == "TSLA")
    after = select_active(frame, date(2022, 6, 1)).filter(pl.col("us_symbol") == "TSLA")

    assert before.height == 1
    assert "373220" not in before["kr_symbols"].to_list()[0]
    assert after.height == 1
    assert "373220" in after["kr_symbols"].to_list()[0]


def test_skhy_only_active_after_listing():
    frame = build_us_kr_map()

    assert mapped_kr_symbols(frame, "SKHY", date(2026, 7, 1)) == []
    assert mapped_kr_symbols(frame, "SKHY", date(2026, 7, 14)) == ["000660"]


def test_nvda_hanmi_link_appears_from_2023_with_low_lead():
    frame = build_us_kr_map()

    assert "042700" not in mapped_kr_symbols(frame, "NVDA", date(2022, 6, 1))
    assert "042700" in mapped_kr_symbols(frame, "NVDA", date(2024, 1, 2))
    hanmi = frame.filter(
        (pl.col("us_symbol") == "NVDA") & pl.col("kr_symbols").list.contains("042700")
    )
    assert hanmi["lead_strength"].to_list() == ["low"]


def test_context_only_rows_map_to_no_kr_symbols():
    frame = build_us_kr_map()

    for us_symbol in ("CPNG", "GRVY"):
        rows = frame.filter(pl.col("us_symbol") == us_symbol)
        assert rows["link_type"].to_list() == ["context_only"]
        assert rows["kr_symbols"].to_list() == [[]]
