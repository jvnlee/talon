import json

import httpx

from talon.sources.kis import KisClient
from talon.sources.kis_market import (
    fetch_flow_ranking,
    fetch_frgnmem_ranking,
    fetch_investor_estimate,
    fetch_orderbook,
)

TOKEN_JSON = {
    "access_token": "tok",
    "expires_in": 86400,
    "access_token_token_expired": "2026-07-16 11:59:59",
}


def orderbook_payload():
    book = {f"askp{i}": str(283000 + i * 100) for i in range(1, 11)}
    book |= {f"bidp{i}": str(282900 - i * 100) for i in range(1, 11)}
    book |= {f"askp_rsqn{i}": str(1000 * i) for i in range(1, 11)}
    book |= {f"bidp_rsqn{i}": str(2000 * i) for i in range(1, 11)}
    book |= {
        "total_askp_rsqn": "839311",
        "total_bidp_rsqn": "155490",
        "ntby_aspr_rsqn": "-683821",
        "aspr_acpt_hour": "114335",
        "new_mkop_cls_code": "20",
    }
    expected = {
        "stck_prpr": "283000",
        "stck_oprc": "283500",
        "stck_hgpr": "284000",
        "stck_lwpr": "270000",
        "stck_sdpr": "266000",
        "antc_cnpr": "283500",
        "antc_vol": "883985",
        "antc_mkop_cls_code": "112",
        "vi_cls_code": "N",
    }
    return {"rt_cd": "0", "output1": book, "output2": expected}


def investor_payload():
    return {
        "rt_cd": "0",
        "output2": [
            {
                "bsop_hour_gb": "2",
                "frgn_fake_ntby_qty": "500000",
                "orgn_fake_ntby_qty": "-10000",
                "sum_fake_ntby_qty": "490000",
            },
            {
                "bsop_hour_gb": "3",
                "frgn_fake_ntby_qty": "887000",
                "orgn_fake_ntby_qty": "-45000",
                "sum_fake_ntby_qty": "842000",
            },
            {
                "bsop_hour_gb": "1",
                "frgn_fake_ntby_qty": "100000",
                "orgn_fake_ntby_qty": "0",
                "sum_fake_ntby_qty": "100000",
            },
        ],
    }


def flow_payload():
    return {
        "rt_cd": "0",
        "output": [
            {
                "mksc_shrn_iscd": "005930",
                "hts_kor_isnm": "삼성전자",
                "ntby_qty": "900000",
                "frgn_ntby_qty": "466000",
                "orgn_ntby_qty": "400000",
                "etc_corp_ntby_vol": "34000",
                "ivtr_ntby_qty": "100000",
                "bank_ntby_qty": "50000",
                "insu_ntby_qty": "50000",
                "mrbn_ntby_qty": "0",
                "fund_ntby_qty": "200000",
                "etc_orgt_ntby_vol": "0",
                "frgn_ntby_tr_pbmn": "993978",
                "orgn_ntby_tr_pbmn": "850000",
                "etc_corp_ntby_tr_pbmn": "70000",
                "stck_prpr": "283000",
                "prdy_ctrt": "6.39",
                "acml_vol": "12345678",
            }
        ],
    }


def frgnmem_payload():
    return {
        "rt_cd": "0",
        "output": [
            {
                "stck_shrn_iscd": "005930",
                "hts_kor_isnm": "삼성전자",
                "glob_ntsl_qty": "506271",
                "glob_total_shnu_qty": "1200000",
                "glob_total_seln_qty": "693729",
                "stck_prpr": "283000",
                "prdy_ctrt": "6.39",
                "prdy_vrss": "17000",
                "acml_vol": "12345678",
            }
        ],
    }


def make_client(tmp_path, responses_by_path):
    def handler(request: httpx.Request) -> httpx.Response:
        payload = responses_by_path.get(request.url.path)
        assert payload is not None, request.url.path
        return httpx.Response(200, json=payload)

    token_path = tmp_path / "kis_token.json"
    token_path.write_text(
        json.dumps({"access_token": "tok", "expired_at": "2099-01-01 00:00:00"})
    )
    return KisClient(
        "key",
        "secret",
        base_url="https://kis.test",
        token_path=token_path,
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )


def test_orderbook_row_is_flat_and_typed(tmp_path):
    path = "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn"
    with make_client(tmp_path, {path: orderbook_payload()}) as client:
        row = fetch_orderbook(client, "005930")

    assert row["symbol"] == "005930"
    assert row["ask_price_1"] == 283100.0
    assert row["bid_qty_10"] == 20000.0
    assert row["total_ask_qty"] == 839311.0
    assert row["net_bid_qty"] == -683821.0
    assert row["accept_hour"] == "114335"
    assert row["market_phase"] == "20"
    assert row["antc_price"] == 283500.0
    assert row["antc_qty"] == 883985.0
    assert row["vi_code"] == "N"


def test_orderbook_empty_output_returns_none(tmp_path):
    path = "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn"
    with make_client(tmp_path, {path: {"rt_cd": "0", "output1": {}, "output2": {}}}) as client:
        assert fetch_orderbook(client, "005930") is None


def test_investor_estimate_picks_latest_bucket(tmp_path):
    path = "/uapi/domestic-stock/v1/quotations/investor-trend-estimate"
    with make_client(tmp_path, {path: investor_payload()}) as client:
        row = fetch_investor_estimate(client, "005930")

    assert row["bucket"] == 3
    assert row["frgn_qty"] == 887000.0
    assert row["orgn_qty"] == -45000.0
    assert row["sum_qty"] == 842000.0


def test_investor_estimate_empty_returns_none(tmp_path):
    path = "/uapi/domestic-stock/v1/quotations/investor-trend-estimate"
    with make_client(tmp_path, {path: {"rt_cd": "0", "output2": []}}) as client:
        assert fetch_investor_estimate(client, "005930") is None


def test_flow_ranking_rows_carry_side_and_rank(tmp_path):
    path = "/uapi/domestic-stock/v1/quotations/foreign-institution-total"
    with make_client(tmp_path, {path: flow_payload()}) as client:
        rows = fetch_flow_ranking(client, "buy")

    assert len(rows) == 1
    row = rows[0]
    assert row["side"] == "buy"
    assert row["rank"] == 1
    assert row["symbol"] == "005930"
    assert row["frgn_qty"] == 466000.0
    assert row["fund_qty"] == 200000.0
    assert row["frgn_amount"] == 993978.0


def test_frgnmem_ranking_positive_means_net_buy(tmp_path):
    path = "/uapi/domestic-stock/v1/quotations/frgnmem-trade-estimate"
    with make_client(tmp_path, {path: frgnmem_payload()}) as client:
        rows = fetch_frgnmem_ranking(client, "buy")

    assert rows[0]["net_qty"] == 506271.0
    assert rows[0]["buy_qty"] == 1200000.0
    assert rows[0]["sell_qty"] == 693729.0
