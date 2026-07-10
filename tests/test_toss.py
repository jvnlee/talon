import json

import httpx
import pytest

from conftest import utc
from talon.sources.toss import TossClient, TossError, rate_group

TOKEN_RESPONSE = {"access_token": "tok-1", "token_type": "Bearer", "expires_in": 3600}


def envelope(result):
    return {"result": result}


def candle_raw(ts: str, price: str = "100", volume: str = "10"):
    return {
        "timestamp": ts,
        "openPrice": price,
        "highPrice": price,
        "lowPrice": price,
        "closePrice": price,
        "volume": volume,
        "currency": "KRW",
    }


def make_client(handler, **kwargs):
    sleeps: list[float] = []
    client = TossClient(
        "cid",
        "csec",
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
        rps=0,
        **kwargs,
    )
    return client, sleeps


def test_rate_group_mapping():
    assert rate_group("/oauth2/token") == "AUTH"
    assert rate_group("/api/v1/candles") == "MARKET_DATA_CHART"
    assert rate_group("/api/v1/market-indicators/KOSPI/candles") == "MARKET_INDICATOR_CHART"
    assert rate_group("/api/v1/market-indicators/KOSPI/investor-trading") == "MARKET_INDICATOR"
    assert rate_group("/api/v1/prices") == "MARKET_DATA"
    assert rate_group("/api/v1/stocks") == "STOCK"
    assert rate_group("/api/v1/market-calendar/KR") == "MARKET_INFO"


def test_token_form_and_candle_parsing():
    token_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            body = request.read().decode()
            token_calls.append(body)
            assert "grant_type=client_credentials" in body
            assert "client_id=cid" in body
            assert "client_secret=csec" in body
            return httpx.Response(200, json=TOKEN_RESPONSE)
        assert request.headers["Authorization"] == "Bearer tok-1"
        assert request.url.params["adjusted"] == "false"
        return httpx.Response(
            200,
            json=envelope(
                {
                    "candles": [candle_raw("2026-07-10T09:01:00+09:00", "72400.5")],
                    "nextBefore": None,
                }
            ),
        )

    client, _ = make_client(handler)
    page = client.candles("005930", "1m")
    assert len(token_calls) == 1
    assert page.next_before is None
    candle = page.candles[0]
    assert candle.ts == utc(2026, 7, 10, 0, 1)
    assert candle.open == 72400.5

    client.candles("005930", "1m")
    assert len(token_calls) == 1


def test_candles_since_pagination_and_filtering():
    befores = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json=TOKEN_RESPONSE)
        befores.append(request.url.params.get("before"))
        if request.url.params.get("before") is None:
            return httpx.Response(
                200,
                json=envelope(
                    {
                        "candles": [
                            candle_raw("2026-07-10T09:04:00+09:00"),
                            candle_raw("2026-07-10T09:03:00+09:00"),
                        ],
                        "nextBefore": "2026-07-10T09:02:00+09:00",
                    }
                ),
            )
        return httpx.Response(
            200,
            json=envelope(
                {
                    "candles": [
                        candle_raw("2026-07-10T09:02:00+09:00"),
                        candle_raw("2026-07-10T09:01:00+09:00"),
                    ],
                    "nextBefore": None,
                }
            ),
        )

    client, _ = make_client(handler)
    candles = client.candles_since("005930", "1m", utc(2026, 7, 10, 0, 1))
    assert befores == [None, "2026-07-10T09:02:00+09:00"]
    assert [c.ts for c in candles] == [
        utc(2026, 7, 10, 0, 2),
        utc(2026, 7, 10, 0, 3),
        utc(2026, 7, 10, 0, 4),
    ]


def test_candles_since_stops_at_since_without_next_page():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json=TOKEN_RESPONSE)
        calls.append(request.url.params.get("before"))
        return httpx.Response(
            200,
            json=envelope(
                {
                    "candles": [candle_raw("2026-07-10T09:04:00+09:00")],
                    "nextBefore": "2026-07-10T09:03:00+09:00",
                }
            ),
        )

    client, _ = make_client(handler)
    candles = client.candles_since("005930", "1m", utc(2026, 7, 10, 0, 4))
    assert len(calls) == 1
    assert candles == []


def test_rate_limit_retry_honors_reset_header():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json=TOKEN_RESPONSE)
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(
                429,
                headers={"X-RateLimit-Reset": "0.7"},
                json={"error": {"requestId": "r", "code": "rate-limit-exceeded", "message": ""}},
            )
        return httpx.Response(200, json=envelope({"candles": [], "nextBefore": None}))

    client, sleeps = make_client(handler)
    page = client.candles("005930", "1m")
    assert page.candles == []
    assert 0.7 in sleeps


def test_server_error_retries_then_succeeds():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json=TOKEN_RESPONSE)
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(
                500, json={"error": {"requestId": "r", "code": "internal", "message": ""}}
            )
        return httpx.Response(200, json=envelope({"candles": [], "nextBefore": None}))

    client, sleeps = make_client(handler)
    client.candles("005930", "1m")
    assert len(attempts) == 3
    assert sleeps == [0.5, 1.0]


def test_persistent_server_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json=TOKEN_RESPONSE)
        return httpx.Response(
            500, json={"error": {"requestId": "r", "code": "internal", "message": "boom"}}
        )

    client, _ = make_client(handler, max_attempts=3)
    with pytest.raises(TossError) as excinfo:
        client.candles("005930", "1m")
    assert excinfo.value.code == "internal"
    assert excinfo.value.status == 500


def test_401_refreshes_token_once():
    token_count = [0]
    data_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            token_count[0] += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"tok-{token_count[0]}",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        data_calls.append(request.headers["Authorization"])
        if len(data_calls) == 1:
            return httpx.Response(
                401, json={"error": {"requestId": "r", "code": "unauthorized", "message": ""}}
            )
        return httpx.Response(200, json=envelope({"candles": [], "nextBefore": None}))

    client, _ = make_client(handler)
    client.candles("005930", "1m")
    assert token_count[0] == 2
    assert data_calls == ["Bearer tok-1", "Bearer tok-2"]


def test_domain_error_maps_code():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json=TOKEN_RESPONSE)
        return httpx.Response(
            400,
            json={
                "error": {"requestId": "r", "code": "invalid-request", "message": "bad interval"}
            },
        )

    client, _ = make_client(handler)
    with pytest.raises(TossError) as excinfo:
        client.candles("005930", "2m")
    assert excinfo.value.code == "invalid-request"


def test_oauth_error_format():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": "invalid_client", "error_description": "Client authentication failed."},
        )

    client, _ = make_client(handler)
    with pytest.raises(TossError) as excinfo:
        client.candles("005930", "1m")
    assert excinfo.value.code == "invalid_client"


def test_stocks_chunks_requests():
    symbol_batches = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json=TOKEN_RESPONSE)
        symbols = request.url.params["symbols"].split(",")
        symbol_batches.append(len(symbols))
        return httpx.Response(
            200,
            json=envelope(
                [
                    {
                        "symbol": symbol,
                        "name": "이름",
                        "englishName": "Name",
                        "isinCode": "KR0000000000",
                        "market": "KOSPI",
                        "securityType": "STOCK",
                        "isCommonShare": True,
                        "status": "ACTIVE",
                        "currency": "KRW",
                        "sharesOutstanding": "100",
                    }
                    for symbol in symbols
                ]
            ),
        )

    client, _ = make_client(handler)
    infos = client.stocks([f"{i:06d}" for i in range(250)])
    assert symbol_batches == [200, 50]
    assert len(infos) == 250
    assert infos[0].security_type == "STOCK"


def test_investor_trading_parsing():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json=TOKEN_RESPONSE)
        assert request.url.path == "/api/v1/market-indicators/KOSPI/investor-trading"
        return httpx.Response(
            200,
            json=envelope(
                {
                    "nextUntil": None,
                    "records": [
                        {
                            "date": "2026-07-10",
                            "updatedAt": "2026-07-10T18:10:00+09:00",
                            "individual": {"buyAmount": "5200", "sellAmount": "5350"},
                            "foreigner": {"buyAmount": "100", "sellAmount": "90"},
                            "institution": {
                                "buyAmount": "2100",
                                "sellAmount": "2180",
                                "breakdown": {"pensionFund": {"buyAmount": "1", "sellAmount": "2"}},
                            },
                            "otherCorporation": {"buyAmount": "5", "sellAmount": "8"},
                        }
                    ],
                }
            ),
        )

    client, _ = make_client(handler)
    records = client.investor_trading("KOSPI")
    assert len(records) == 1
    record = records[0]
    assert record.individual_buy == 5200.0
    assert record.updated_at == utc(2026, 7, 10, 9, 10)
    assert json.loads(record.institution_breakdown)["pensionFund"]["buyAmount"] == "1"
