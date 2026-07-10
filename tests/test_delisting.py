from datetime import date

import httpx
import pytest

from talon.errors import SchemaDriftError, SourceError
from talon.sources.delisting import (
    CORPORATE_ACTION,
    TERMINAL,
    UNKNOWN,
    classify_delisting,
    fetch_delisting_registry,
)

HEADER = (
    ",Symbol,Name,Market,SecuGroup,Kind,ListingDate,DelistingDate,Reason,"
    "ArrantEnforceDate,ArrantEndDate,Industry,ParValue,ListingShares,ToSymbol,ToName"
)

ROWS = [
    "0,117930,한진해운,KOSPI,주권,,2009-12-29,2017-03-07,파산선고,2017-02-23,2017-03-06,해운,5000,,,",
    "1,000030,우리은행,KOSPI,주권,,2002-06-24,2019-02-13,지주회사의 완전자회사화,,,은행,5000,,,",
    "2,5930,가상종목,KOSDAQ,주권,,2010-01-01,2020-01-01,기타,,,,,,,",
]


def csv_bytes(rows=None):
    body = "\n".join([HEADER, *(rows if rows is not None else ROWS)])
    return ("﻿" + body).encode("utf-8")


def transport_for(payloads):
    def handler(request):
        body = payloads.get(request.url.path)
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, content=body)

    return httpx.MockTransport(handler)


def path_for(day):
    return f"/FinanceData/fdr_krx_data_cache/master/data/listing/delisting/{day}.csv"


def test_registry_parses_and_classifies():
    transport = transport_for({path_for(date(2026, 7, 11)): csv_bytes()})
    registry = fetch_delisting_registry(date(2026, 7, 11), transport=transport)

    assert registry.height == 3
    assert registry["classification"].to_list() == [TERMINAL, CORPORATE_ACTION, UNKNOWN]
    hanjin = registry.filter(registry["symbol"] == "117930").row(0, named=True)
    assert hanjin["delisting_date"] == date(2017, 3, 7)
    assert hanjin["arrant_end_date"] == date(2017, 3, 6)
    assert registry["symbol"].to_list()[-1] == "005930"


def test_registry_walks_back_to_latest_snapshot():
    transport = transport_for({path_for(date(2026, 7, 9)): csv_bytes()})
    registry = fetch_delisting_registry(date(2026, 7, 11), transport=transport)
    assert registry.height == 3


def test_registry_unavailable_raises():
    transport = transport_for({})
    with pytest.raises(SourceError, match="unavailable"):
        fetch_delisting_registry(date(2026, 7, 11), lookback_days=3, transport=transport)


def test_registry_schema_drift_raises():
    header = ",Symbol,Name,Market"
    payload = ("﻿" + "\n".join([header, "0,117930,한진해운,KOSPI"])).encode("utf-8")
    transport = transport_for({path_for(date(2026, 7, 11)): payload})
    with pytest.raises(SchemaDriftError, match="columns missing"):
        fetch_delisting_registry(date(2026, 7, 11), transport=transport)


def test_classify_terminal_when_arrant_end_present():
    assert classify_delisting("감사의견 거절", date(2020, 1, 1)) == TERMINAL
    assert classify_delisting("신청에 의한 상장폐지", date(2020, 1, 1)) == TERMINAL


def test_classify_corporate_action_without_arrangement_trading():
    assert classify_delisting("피흡수합병", None) == CORPORATE_ACTION
    assert classify_delisting("지주회사의 완전자회사화", None) == CORPORATE_ACTION
    assert classify_delisting("포괄적 주식교환", None) == CORPORATE_ACTION
    assert classify_delisting("우선주 상장폐지", None) == CORPORATE_ACTION
    assert classify_delisting("상장폐지 신청('18.11.5)", None) == CORPORATE_ACTION


def test_classify_unknown_is_safe_default():
    assert classify_delisting("기타", None) == UNKNOWN
    assert classify_delisting(None, None) == UNKNOWN
