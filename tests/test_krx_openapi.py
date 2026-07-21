import json
from datetime import date

import httpx
import pytest

from talon.errors import SchemaDriftError, SourceError
from talon.sources.krx_openapi import STOCK_ENDPOINTS, KrxOpenApiSource

DAY = date(2026, 7, 9)


def record(symbol, *, close="70000", high="71000", low="69000", opened="70500", volume="1000"):
    return {
        "BAS_DD": "20260709",
        "ISU_CD": symbol,
        "ISU_NM": f"종목{symbol}",
        "MKT_NM": "KOSPI",
        "TDD_OPNPRC": opened,
        "TDD_HGPRC": high,
        "TDD_LWPRC": low,
        "TDD_CLSPRC": close,
        "ACC_TRDVOL": volume,
        "ACC_TRDVAL": "1,234,567,890",
        "MKTCAP": "400,000,000,000",
        "LIST_SHRS": "5,969,782,550",
        "FLUC_RT": "0.18",
    }


def source_with(responses, **kwargs):
    def handler(request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.rsplit("/", 1)[-1]
        return responses[endpoint](request)

    return KrxOpenApiSource(
        "test-key",
        transport=httpx.MockTransport(handler),
        throttle=0.0,
        **kwargs,
    )


def ok(rows):
    return lambda request: httpx.Response(200, json={"OutBlock_1": rows})


def test_snapshot_maps_krx_fields():
    source = source_with(
        {
            "stk_bydd_trd": ok([record("005930")]),
            "ksq_bydd_trd": ok([record("035720", close="45000")]),
        }
    )
    daily, caps = source.snapshot(DAY)

    assert daily["symbol"].to_list() == ["005930", "035720"]
    row = daily.filter(daily["symbol"] == "005930").row(0, named=True)
    assert row["day"] == DAY
    assert row["open"] == 70500.0
    assert row["high"] == 71000.0
    assert row["low"] == 69000.0
    assert row["close"] == 70000.0
    assert row["volume"] == 1000.0
    assert row["value"] == 1_234_567_890.0
    assert row["change_pct"] == 0.18

    cap_row = caps.filter(caps["symbol"] == "005930").row(0, named=True)
    assert cap_row["cap"] == 400_000_000_000.0
    assert cap_row["shares"] == 5_969_782_550.0


def test_snapshot_keeps_halted_rows_with_null_prices():
    source = source_with(
        {
            "stk_bydd_trd": ok(
                [record("005930"), record("000000", opened="0", high="0", low="0", volume="0")]
            ),
            "ksq_bydd_trd": ok([]),
        }
    )
    daily, caps = source.snapshot(DAY)
    assert daily["symbol"].to_list() == ["000000", "005930"]
    assert caps["symbol"].to_list() == ["000000", "005930"]
    halted = daily.filter(daily["symbol"] == "000000").row(0, named=True)
    assert halted["open"] is None
    assert halted["high"] is None
    assert halted["low"] is None
    assert halted["close"] == 70000.0


def test_empty_response_means_no_data_not_an_error():
    source = source_with({"stk_bydd_trd": ok([]), "ksq_bydd_trd": ok([])})
    daily, caps = source.snapshot(date(2026, 6, 3))
    assert daily.is_empty()
    assert caps.is_empty()


def test_401_names_the_likely_cause():
    def denied(request):
        return httpx.Response(401, json={"respMsg": "Unauthorized API Call", "respCode": "401"})

    source = source_with({"stk_bydd_trd": denied, "ksq_bydd_trd": denied})
    with pytest.raises(SourceError, match="이용 신청"):
        source.snapshot(DAY)


def test_schema_drift_when_fields_disappear():
    broken = dict(record("005930"))
    del broken["ACC_TRDVAL"]
    source = source_with({"stk_bydd_trd": ok([broken]), "ksq_bydd_trd": ok([])})
    with pytest.raises(SchemaDriftError, match="ACC_TRDVAL"):
        source.snapshot(DAY)


def test_rejects_days_before_service_start():
    source = source_with({"stk_bydd_trd": ok([]), "ksq_bydd_trd": ok([])})
    with pytest.raises(SourceError, match="2010-01-04"):
        source.snapshot(date(2009, 12, 31))


def test_queries_both_markets_with_basdd():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, dict(request.url.params), request.headers["AUTH_KEY"]))
        return httpx.Response(200, content=json.dumps({"OutBlock_1": []}))

    source = KrxOpenApiSource("secret", transport=httpx.MockTransport(handler), throttle=0.0)
    source.snapshot(DAY)

    assert [path.rsplit("/", 1)[-1] for path, _, _ in seen] == [
        endpoint.split("/")[-1] for endpoint in STOCK_ENDPOINTS
    ]
    assert all(params == {"basDd": "20260709"} for _, params, _ in seen)
    assert all(key == "secret" for _, _, key in seen)


def info_record(symbol, *, market="KOSPI", group="주권", kind="보통주", section=""):
    return {
        "ISU_CD": f"KR7{symbol}008",
        "ISU_SRT_CD": symbol,
        "ISU_NM": f"종목{symbol}보통주",
        "ISU_ABBRV": f"종목{symbol}",
        "ISU_ENG_NM": f"Stock {symbol}",
        "LIST_DD": "20150821",
        "MKT_TP_NM": market,
        "SECUGRP_NM": group,
        "SECT_TP_NM": section,
        "KIND_STKCERT_TP_NM": kind,
        "PARVAL": "1000",
        "LIST_SHRS": "45,252,759",
    }


def test_stock_info_maps_krx_classification_fields():
    source = source_with(
        {
            "stk_isu_base_info": ok([info_record("005930")]),
            "ksq_isu_base_info": ok([info_record("035720", market="KOSDAQ", section="우량기업부")]),
        }
    )
    info = source.stock_info(DAY)

    assert info["symbol"].to_list() == ["005930", "035720"]
    row = info.filter(info["symbol"] == "005930").row(0, named=True)
    assert row["day"] == DAY
    assert row["name"] == "종목005930"
    assert row["market"] == "KOSPI"
    assert row["security_group"] == "주권"
    assert row["share_kind"] == "보통주"
    assert row["section"] == ""
    assert row["listed_on"] == date(2015, 8, 21)
    assert row["shares"] == 45_252_759.0


def test_stock_info_keeps_the_classifications_that_disqualify_a_stock():
    source = source_with(
        {
            "stk_isu_base_info": ok(
                [
                    info_record("395400", group="부동산투자회사"),
                    info_record("005935", kind="구형우선주"),
                ]
            ),
            "ksq_isu_base_info": ok(
                [
                    info_record("900140", market="KOSDAQ", group="외국주권"),
                    info_record("123450", market="KOSDAQ", section="관리종목(소속부없음)"),
                ]
            ),
        }
    )
    info = source.stock_info(DAY)

    by_symbol = {row["symbol"]: row for row in info.iter_rows(named=True)}
    assert by_symbol["395400"]["security_group"] == "부동산투자회사"
    assert by_symbol["005935"]["share_kind"] == "구형우선주"
    assert by_symbol["900140"]["security_group"] == "외국주권"
    assert by_symbol["123450"]["section"] == "관리종목(소속부없음)"


def test_stock_info_empty_on_holidays():
    source = source_with({"stk_isu_base_info": ok([]), "ksq_isu_base_info": ok([])})
    assert source.stock_info(date(2026, 6, 3)).is_empty()


def test_stock_info_schema_drift():
    broken = dict(info_record("005930"))
    del broken["SECUGRP_NM"]
    source = source_with({"stk_isu_base_info": ok([broken]), "ksq_isu_base_info": ok([])})
    with pytest.raises(SchemaDriftError, match="SECUGRP_NM"):
        source.stock_info(DAY)
