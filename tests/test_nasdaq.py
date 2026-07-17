import json
from datetime import date

import httpx
import pytest

from talon.errors import SourceError
from talon.sources.nasdaq import fetch_earnings_calendar


def _transport(payload: dict, status_code: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "Mozilla" in request.headers["User-Agent"]
        return httpx.Response(status_code, text=json.dumps(payload))

    return httpx.MockTransport(handler)


def test_fetch_earnings_calendar_maps_report_times():
    payload = {
        "data": {
            "rows": [
                {"symbol": "TSLA", "time": "time-after-hours"},
                {"symbol": "pm ", "time": "time-pre-market"},
                {"symbol": "XYZ", "time": "time-not-supplied"},
                {"symbol": "", "time": "time-after-hours"},
            ]
        },
        "message": None,
        "status": {"rCode": 200},
    }

    rows = fetch_earnings_calendar(date(2026, 7, 22), transport=_transport(payload))

    assert rows == [
        {"symbol": "TSLA", "when": "amc"},
        {"symbol": "PM", "when": "bmo"},
        {"symbol": "XYZ", "when": "unknown"},
    ]


def test_fetch_earnings_calendar_handles_empty_days():
    payload = {"data": None, "message": None, "status": {"rCode": 200}}

    assert fetch_earnings_calendar(date(2026, 7, 19), transport=_transport(payload)) == []


def test_fetch_earnings_calendar_rejects_error_status():
    payload = {"data": None, "status": {"rCode": 400, "bCodeMessage": "bad"}}

    with pytest.raises(SourceError, match="오류 응답"):
        fetch_earnings_calendar(date(2026, 7, 22), transport=_transport(payload))


def test_fetch_earnings_calendar_rejects_http_error():
    with pytest.raises(SourceError, match="요청 실패"):
        fetch_earnings_calendar(date(2026, 7, 22), transport=_transport({}, status_code=403))
