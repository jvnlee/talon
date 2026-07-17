import json
from datetime import UTC, date, datetime

import httpx
import pytest

from talon.errors import SourceError
from talon.sources.fred import (
    fetch_release_dates,
    parse_fredgraph,
    parse_vix_history,
)

CAPTURED = datetime(2026, 7, 18, 0, 0, tzinfo=UTC)

FREDGRAPH_CSV = """observation_date,DGS10
2026-07-14,4.58
2026-07-15,.
2026-07-16,4.55
"""

VIX_CSV = """DATE,OPEN,HIGH,LOW,CLOSE
07/15/2026,16.200000,16.570000,15.640000,15.670000
07/16/2026,15.820000,17.230000,15.770000,16.730000
"""


def test_parse_fredgraph_skips_missing_values():
    frame = parse_fredgraph(FREDGRAPH_CSV, "DGS10", CAPTURED)

    assert frame["day"].to_list() == [date(2026, 7, 14), date(2026, 7, 16)]
    assert frame["value"].to_list() == [4.58, 4.55]
    assert frame["source"].to_list() == ["fred:DGS10", "fred:DGS10"]


def test_parse_fredgraph_rejects_header_drift():
    drifted = FREDGRAPH_CSV.replace("observation_date", "DATE")

    with pytest.raises(SourceError, match="헤더"):
        parse_fredgraph(drifted, "DGS10", CAPTURED)


def test_parse_fredgraph_rejects_wrong_series():
    with pytest.raises(SourceError, match="헤더"):
        parse_fredgraph(FREDGRAPH_CSV, "DGS2", CAPTURED)


def test_parse_fredgraph_rejects_empty_observations():
    empty = "observation_date,DGS10\n2026-07-14,.\n"

    with pytest.raises(SourceError, match="관측치"):
        parse_fredgraph(empty, "DGS10", CAPTURED)


def test_parse_vix_history():
    frame = parse_vix_history(VIX_CSV, CAPTURED)

    assert frame["day"].to_list() == [date(2026, 7, 15), date(2026, 7, 16)]
    assert frame["value"].to_list() == [15.67, 16.73]
    assert frame["source"].to_list() == ["cboe", "cboe"]


def test_parse_vix_history_rejects_header_drift():
    with pytest.raises(SourceError, match="헤더"):
        parse_vix_history(VIX_CSV.replace("CLOSE", "LAST"), CAPTURED)


def _release_transport(payload: dict, seen: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, text=json.dumps(payload))

    return httpx.MockTransport(handler)


def test_fetch_release_dates_filters_range():
    payload = {
        "release_dates": [
            {"release_id": 10, "date": "2016-02-10"},
            {"release_id": 10, "date": "2026-07-17"},
            {"release_id": 10, "date": "2026-08-12"},
            {"release_id": 10, "date": "2026-09-11"},
        ]
    }
    seen: dict = {}

    days = fetch_release_dates(
        10,
        "test-key",
        start=date(2026, 7, 1),
        end=date(2026, 8, 31),
        transport=_release_transport(payload, seen),
    )

    assert days == [date(2026, 7, 17), date(2026, 8, 12)]
    assert seen["params"]["include_release_dates_with_no_data"] == "true"
    assert seen["params"]["realtime_end"] == "9999-12-31"


def test_fetch_release_dates_requires_key():
    with pytest.raises(SourceError, match="FRED API 키"):
        fetch_release_dates(10, "", start=date(2026, 1, 1), end=date(2026, 12, 31))


def test_fetch_release_dates_rejects_malformed_payload():
    seen: dict = {}
    transport = _release_transport({"unexpected": []}, seen)

    with pytest.raises(SourceError, match="release_dates"):
        fetch_release_dates(
            10, "test-key", start=date(2026, 1, 1), end=date(2026, 12, 31), transport=transport
        )
