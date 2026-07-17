from datetime import UTC, date, datetime

import pytest

from talon.errors import SourceError
from talon.sources.ecos import parse_usdkrw

CAPTURED = datetime(2026, 7, 18, 0, 0, tzinfo=UTC)


def good_payload():
    return {
        "StatisticSearch": {
            "list_total_count": 2,
            "row": [
                {"TIME": "20260716", "DATA_VALUE": "1382.4"},
                {"TIME": "20260717", "DATA_VALUE": "1379.1"},
            ],
        }
    }


def test_parse_usdkrw():
    frame = parse_usdkrw(good_payload(), CAPTURED)

    assert frame["day"].to_list() == [date(2026, 7, 16), date(2026, 7, 17)]
    assert frame["value"].to_list() == [1382.4, 1379.1]
    assert frame["source"].to_list() == ["ecos", "ecos"]


def test_parse_usdkrw_rejects_error_result():
    payload = {"RESULT": {"CODE": "INFO-100", "MESSAGE": "인증키 오류"}}

    with pytest.raises(SourceError, match="INFO-100"):
        parse_usdkrw(payload, CAPTURED)


def test_parse_usdkrw_rejects_out_of_range():
    payload = good_payload()
    payload["StatisticSearch"]["row"][0]["DATA_VALUE"] = "13.8"

    with pytest.raises(SourceError, match="정상 범위"):
        parse_usdkrw(payload, CAPTURED)


def test_parse_usdkrw_rejects_empty():
    payload = {"StatisticSearch": {"row": []}}

    with pytest.raises(SourceError, match="한 건도"):
        parse_usdkrw(payload, CAPTURED)
