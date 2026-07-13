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


def test_snapshot_drops_halted_zero_price_rows():
    """거래정지 종목은 시가/고가/저가가 0으로 온다. marcap·pykrx와 같은 기준으로 걸러야
    reconcile이 매일 같은 종목을 '신규'로 오인하지 않는다."""
    source = source_with(
        {
            "stk_bydd_trd": ok(
                [record("005930"), record("000000", opened="0", high="0", low="0", volume="0")]
            ),
            "ksq_bydd_trd": ok([]),
        }
    )
    daily, caps = source.snapshot(DAY)
    assert daily["symbol"].to_list() == ["005930"]
    assert caps["symbol"].to_list() == ["005930"]


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
