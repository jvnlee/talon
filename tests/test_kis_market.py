import json
from datetime import date

import httpx

from talon.sources.kis import KisClient
from talon.sources.kis_market import (
    CREDIT_DAILY_PATH,
    CREDIT_DAILY_TR,
    fetch_credit_daily,
    fetch_flow_ranking,
    fetch_frgnmem_ranking,
    fetch_frgnmem_trend,
    fetch_investor_estimate,
    fetch_member,
    fetch_orderbook,
    fetch_overseas_daily,
    fetch_overseas_index_daily,
    fetch_overtime_price,
    fetch_overtime_ranking,
    fetch_program_market,
    fetch_program_trade,
    fetch_volume_power,
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


def volume_power_payload():
    ticks = [
        {
            "stck_cntg_hour": f"1338{28 - i:02d}",
            "tday_rltv": "104.53" if i == 0 else "104.40",
            "stck_prpr": "282750",
            "prdy_ctrt": "7.51",
        }
        for i in range(3)
    ]
    return {"rt_cd": "0", "output": ticks}


def member_payload():
    data = {}
    for n in range(1, 6):
        data[f"seln_mbcr_no{n}"] = f"0005{n}"
        data[f"seln_mbcr_name{n}"] = f"매도{n}"
        data[f"total_seln_qty{n}"] = str(1872894 - n * 1000)
        data[f"seln_mbcr_rlim{n}"] = f"{11.25 - n:.2f}"
        data[f"seln_qty_icdc{n}"] = str(443 + n)
        data[f"seln_mbcr_glob_yn_{n}"] = "N"
        data[f"shnu_mbcr_no{n}"] = f"0003{n}"
        data[f"shnu_mbcr_name{n}"] = f"매수{n}"
        data[f"total_shnu_qty{n}"] = str(2039168 - n * 1000)
        data[f"shnu_mbcr_rlim{n}"] = f"{12.25 - n:.2f}"
        data[f"shnu_qty_icdc{n}"] = str(1687 + n)
        data[f"shnu_mbcr_glob_yn_{n}"] = "N"
    data["shnu_mbcr_glob_yn_2"] = "Y"
    data["shnu_mbcr_glob_yn_5"] = "Y"
    data["shnu_mbcr_glob_yn2"] = "N"
    data |= {
        "glob_total_shnu_qty": "2880869",
        "glob_total_seln_qty": "1727877",
        "glob_ntby_qty": "1152992",
        "glob_shnu_rlim": "17.30",
        "glob_seln_rlim": "10.38",
        "glob_total_shnu_qty_icdc": "7455",
        "glob_total_seln_qty_icdc": "0",
        "acml_vol": "16652520",
    }
    return {"rt_cd": "0", "output": [data]}


def program_trade_payload():
    ticks = [
        {
            "bsop_hour": "133539",
            "stck_prpr": "282750",
            "prdy_ctrt": "7.51",
            "acml_vol": "16655930",
            "whol_smtn_seln_vol": "4898913",
            "whol_smtn_shnu_vol": "5887057",
            "whol_smtn_ntby_qty": "988144",
            "whol_smtn_seln_tr_pbmn": "1366372537500",
            "whol_smtn_shnu_tr_pbmn": "1647528253000",
            "whol_smtn_ntby_tr_pbmn": "281155715500",
        },
        {
            "bsop_hour": "133534",
            "stck_prpr": "282500",
            "prdy_ctrt": "7.42",
            "acml_vol": "16650000",
            "whol_smtn_seln_vol": "4890000",
            "whol_smtn_shnu_vol": "5880000",
            "whol_smtn_ntby_qty": "990000",
            "whol_smtn_seln_tr_pbmn": "1",
            "whol_smtn_shnu_tr_pbmn": "1",
            "whol_smtn_ntby_tr_pbmn": "0",
        },
    ]
    return {"rt_cd": "0", "output": ticks}


def program_market_payload():
    rows = [
        {
            "bsop_hour": f"13{35 - i:02d}00",
            "arbt_smtn_seln_tr_pbmn": str(96452 + i),
            "arbt_smtn_shnu_tr_pbmn": str(161346 + i),
            "arbt_smtn_ntby_tr_pbmn": str(64894 + i),
            "nabt_smtn_seln_tr_pbmn": str(6957153 + i),
            "nabt_smtn_shnu_tr_pbmn": str(8960852 + i),
            "nabt_smtn_ntby_tr_pbmn": str(2003699 + i),
            "whol_smtn_ntby_tr_pbmn": str(2068592 + i),
            "bstp_nmix_prpr": "",
            "bstp_nmix_prdy_vrss": "",
            "prdy_vrss_sign": "",
        }
        for i in range(3)
    ]
    return {"rt_cd": "0", "output": rows}


def frgnmem_trend_payload():
    ticks = [
        {
            "bsop_hour": "133536",
            "stck_prpr": "282750",
            "prdy_ctrt": "7.51",
            "acml_vol": "16654886",
            "frgn_seln_vol": "1727877",
            "frgn_shnu_vol": "2886364",
            "glob_ntby_qty": "1158487",
            "frgn_ntby_qty_icdc": "2048",
        },
        {
            "bsop_hour": "133536",
            "stck_prpr": "282700",
            "prdy_ctrt": "7.49",
            "acml_vol": "16654000",
            "frgn_seln_vol": "1727800",
            "frgn_shnu_vol": "2886200",
            "glob_ntby_qty": "1158400",
            "frgn_ntby_qty_icdc": "-100",
        },
        {
            "bsop_hour": "133535",
            "stck_prpr": "282650",
            "prdy_ctrt": "7.47",
            "acml_vol": "16653000",
            "frgn_seln_vol": "1727700",
            "frgn_shnu_vol": "2886000",
            "glob_ntby_qty": "1158300",
            "frgn_ntby_qty_icdc": "50",
        },
    ]
    return {"rt_cd": "0", "output": ticks}


def overtime_price_payload():
    return {
        "rt_cd": "0",
        "output": {
            "ovtm_untp_sdpr": "263000",
            "ovtm_untp_prpr": "263000",
            "ovtm_untp_prdy_vrss": "0",
            "ovtm_untp_prdy_ctrt": "0.00",
            "ovtm_untp_prdy_vrss_sign": "3",
            "ovtm_untp_oprc": "262000",
            "ovtm_untp_hgpr": "264000",
            "ovtm_untp_lwpr": "261000",
            "ovtm_untp_vol": "12345",
            "ovtm_untp_tr_pbmn": "3245678900",
            "ovtm_untp_mxpr": "289000",
            "ovtm_untp_llam": "237000",
            "ovtm_vi_cls_code": "N",
        },
    }


def overtime_ranking_payload():
    return {
        "rt_cd": "0",
        "output1": {
            "ovtm_untp_acml_vol": "34670948",
            "ovtm_untp_acml_tr_pbmn": "100025060410",
            "ovtm_untp_exch_vol": "31426592",
            "ovtm_untp_exch_tr_pbmn": "73466909356",
            "ovtm_untp_kosdaq_vol": "3244356",
            "ovtm_untp_kosdaq_tr_pbmn": "26558151054",
            "ovtm_untp_ascn_issu_cnt": "628",
            "ovtm_untp_down_issu_cnt": "520",
            "ovtm_untp_stnr_issu_cnt": "677",
            "ovtm_untp_uplm_issu_cnt": "12",
            "ovtm_untp_lslm_issu_cnt": "1",
        },
        "output2": [
            {
                "mksc_shrn_iscd": "334690",
                "hts_kor_isnm": "RISE 팔라듐선물(H)",
                "ovtm_untp_prpr": "6380",
                "ovtm_untp_prdy_vrss": "580",
                "ovtm_untp_prdy_ctrt": "10.00",
                "ovtm_untp_prdy_vrss_sign": "1",
                "ovtm_untp_askp1": "6380",
                "ovtm_untp_bidp1": "5800",
                "ovtm_untp_vol": "1",
                "ovtm_untp_seln_rsqn": "161",
                "ovtm_untp_shnu_rsqn": "1",
                "ovtm_vrss_acml_vol_rlim": "0.01",
                "stck_prpr": "5920",
                "acml_vol": "17033",
            },
            {
                "mksc_shrn_iscd": "044480",
                "hts_kor_isnm": "빌리언스",
                "ovtm_untp_prpr": "1000",
                "ovtm_untp_prdy_vrss": "80",
                "ovtm_untp_prdy_ctrt": "8.70",
                "ovtm_untp_prdy_vrss_sign": "2",
                "ovtm_untp_askp1": "1005",
                "ovtm_untp_bidp1": "1000",
                "ovtm_untp_vol": "50",
                "ovtm_untp_seln_rsqn": "10",
                "ovtm_untp_shnu_rsqn": "20",
                "ovtm_vrss_acml_vol_rlim": "0.5",
                "stck_prpr": "990",
                "acml_vol": "20000",
            },
        ],
    }


def test_volume_power_reads_latest_tick(tmp_path):
    path = "/uapi/domestic-stock/v1/quotations/inquire-ccnl"
    with make_client(tmp_path, {path: volume_power_payload()}) as client:
        row = fetch_volume_power(client, "005930")

    assert row["symbol"] == "005930"
    assert row["strength"] == 104.53
    assert row["tick_hour"] == "133828"
    assert row["price"] == 282750.0
    assert row["change_pct"] == 7.51


def test_volume_power_empty_returns_none(tmp_path):
    path = "/uapi/domestic-stock/v1/quotations/inquire-ccnl"
    with make_client(tmp_path, {path: {"rt_cd": "0", "output": []}}) as client:
        assert fetch_volume_power(client, "005930") is None


def test_member_wide_row_reads_underscored_glob_yn(tmp_path):
    path = "/uapi/domestic-stock/v1/quotations/inquire-member"
    with make_client(tmp_path, {path: member_payload()}) as client:
        row = fetch_member(client, "005930")

    assert row["symbol"] == "005930"
    assert row["sell_member_no_1"] == "00051"
    assert row["sell_member_name_1"] == "매도1"
    assert row["sell_member_qty_1"] == 1871894.0
    assert row["sell_member_share_1"] == 10.25
    assert row["sell_member_qty_change_1"] == 444.0
    assert row["sell_member_foreign_1"] == "N"
    assert row["buy_member_no_5"] == "00035"
    assert row["buy_member_foreign_2"] == "Y"
    assert row["buy_member_foreign_5"] == "Y"
    assert row["buy_member_foreign_1"] == "N"
    assert row["foreign_buy_qty"] == 2880869.0
    assert row["foreign_sell_qty"] == 1727877.0
    assert row["foreign_net_qty"] == 1152992.0
    assert row["foreign_buy_qty"] - row["foreign_sell_qty"] == row["foreign_net_qty"]
    assert row["foreign_buy_share"] == 17.30
    assert row["volume"] == 16652520.0


def test_member_empty_returns_none(tmp_path):
    path = "/uapi/domestic-stock/v1/quotations/inquire-member"
    with make_client(tmp_path, {path: {"rt_cd": "0", "output": []}}) as client:
        assert fetch_member(client, "005930") is None


def test_program_trade_reads_row0_and_amount_identity(tmp_path):
    path = "/uapi/domestic-stock/v1/quotations/program-trade-by-stock"
    with make_client(tmp_path, {path: program_trade_payload()}) as client:
        row = fetch_program_trade(client, "005930")

    assert row["symbol"] == "005930"
    assert row["tick_hour"] == "133539"
    assert row["price"] == 282750.0
    assert row["change_pct"] == 7.51
    assert row["volume"] == 16655930.0
    assert row["sell_qty"] == 4898913.0
    assert row["buy_qty"] == 5887057.0
    assert row["net_qty"] == 988144.0
    assert row["sell_amount"] == 1366372537500.0
    assert row["buy_amount"] == 1647528253000.0
    assert row["net_amount"] == 281155715500.0
    assert row["buy_amount"] - row["sell_amount"] == row["net_amount"]


def test_program_trade_empty_returns_none(tmp_path):
    path = "/uapi/domestic-stock/v1/quotations/program-trade-by-stock"
    with make_client(tmp_path, {path: {"rt_cd": "0", "output": []}}) as client:
        assert fetch_program_trade(client, "005930") is None


def test_program_market_keeps_all_minute_rows(tmp_path):
    path = "/uapi/domestic-stock/v1/quotations/comp-program-trade-today"
    with make_client(tmp_path, {path: program_market_payload()}) as client:
        rows = fetch_program_market(client, "K")

    assert len(rows) == 3
    assert all(row["market"] == "K" for row in rows)
    assert [row["hour"] for row in rows] == ["133500", "133400", "133300"]
    assert rows[0]["arb_net_amount"] == 64894.0
    assert rows[0]["nonarb_buy_amount"] == 8960852.0
    assert rows[0]["total_net_amount"] == 2068592.0
    assert "bstp_nmix_prpr" not in rows[0]


def test_program_market_empty_returns_empty(tmp_path):
    path = "/uapi/domestic-stock/v1/quotations/comp-program-trade-today"
    with make_client(tmp_path, {path: {"rt_cd": "0"}}) as client:
        assert fetch_program_market(client, "K") == []


def test_frgnmem_trend_assigns_seq_and_keeps_same_second_rows(tmp_path):
    path = "/uapi/domestic-stock/v1/quotations/frgnmem-pchs-trend"
    with make_client(tmp_path, {path: frgnmem_trend_payload()}) as client:
        rows = fetch_frgnmem_trend(client, "005930")

    assert len(rows) == 3
    assert [row["seq"] for row in rows] == [0, 1, 2]
    assert rows[0]["tick_hour"] == rows[1]["tick_hour"] == "133536"
    assert all(row["symbol"] == "005930" for row in rows)
    assert rows[0]["foreign_net_qty"] == 1158487.0
    assert rows[0]["foreign_buy_qty"] - rows[0]["foreign_sell_qty"] == rows[0]["foreign_net_qty"]
    assert rows[1]["net_qty_change"] == -100.0


def test_frgnmem_trend_empty_returns_empty(tmp_path):
    path = "/uapi/domestic-stock/v1/quotations/frgnmem-pchs-trend"
    with make_client(tmp_path, {path: {"rt_cd": "0"}}) as client:
        assert fetch_frgnmem_trend(client, "005930") == []


def test_overtime_price_maps_fields(tmp_path):
    path = "/uapi/domestic-stock/v1/quotations/inquire-overtime-price"
    with make_client(tmp_path, {path: overtime_price_payload()}) as client:
        row = fetch_overtime_price(client, "005930")

    assert row["symbol"] == "005930"
    assert row["prev_close"] == 263000.0
    assert row["price"] == 263000.0
    assert row["change"] == 0.0
    assert row["change_pct"] == 0.0
    assert row["sign"] == "3"
    assert row["open"] == 262000.0
    assert row["high"] == 264000.0
    assert row["low"] == 261000.0
    assert row["volume"] == 12345.0
    assert row["amount"] == 3245678900.0
    assert row["upper_limit"] == 289000.0
    assert row["lower_limit"] == 237000.0
    assert row["vi_code"] == "N"


def test_overtime_price_empty_returns_none(tmp_path):
    path = "/uapi/domestic-stock/v1/quotations/inquire-overtime-price"
    with make_client(tmp_path, {path: {"rt_cd": "0", "output": {}}}) as client:
        assert fetch_overtime_price(client, "005930") is None


def test_overtime_ranking_splits_market_and_rows(tmp_path):
    path = "/uapi/domestic-stock/v1/ranking/overtime-fluctuation"
    with make_client(tmp_path, {path: overtime_ranking_payload()}) as client:
        result = fetch_overtime_ranking(client, "up")

    market = result["market"]
    assert market["volume"] == 34670948.0
    assert market["amount"] == 100025060410.0
    assert market["kospi_volume"] + market["kosdaq_volume"] == market["volume"]
    assert market["kospi_amount"] + market["kosdaq_amount"] == market["amount"]
    assert market["up_count"] == 628
    assert market["down_count"] == 520
    assert market["flat_count"] == 677
    assert market["upper_limit_count"] == 12
    assert market["lower_limit_count"] == 1
    assert isinstance(market["up_count"], int)

    rows = result["rows"]
    assert len(rows) == 2
    assert rows[0]["side"] == "up"
    assert rows[0]["rank"] == 1
    assert rows[0]["symbol"] == "334690"
    assert rows[0]["name"] == "RISE 팔라듐선물(H)"
    assert rows[0]["price"] == 6380.0
    assert rows[0]["change"] == 580.0
    assert rows[0]["change_pct"] == 10.0
    assert rows[0]["sign"] == "1"
    assert rows[0]["ask"] == 6380.0
    assert rows[0]["bid"] == 5800.0
    assert rows[0]["volume"] == 1.0
    assert rows[0]["sell_rsqn"] == 161.0
    assert rows[0]["buy_rsqn"] == 1.0
    assert rows[0]["vol_vs_day_pct"] == 0.01
    assert rows[0]["day_price"] == 5920.0
    assert rows[0]["day_volume"] == 17033.0
    assert rows[1]["rank"] == 2


def test_overtime_ranking_uses_ranking_path_and_div_code(tmp_path):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=overtime_ranking_payload())

    token_path = tmp_path / "kis_token.json"
    token_path.write_text(
        json.dumps({"access_token": "tok", "expired_at": "2099-01-01 00:00:00"})
    )
    with KisClient(
        "key",
        "secret",
        base_url="https://kis.test",
        token_path=token_path,
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    ) as client:
        fetch_overtime_ranking(client, "down")

    assert seen["path"] == "/uapi/domestic-stock/v1/ranking/overtime-fluctuation"
    assert seen["params"]["FID_DIV_CLS_CODE"] == "5"
    assert seen["params"]["FID_INPUT_ISCD"] == "0000"


def test_overseas_daily_parses_and_sorts(tmp_path):
    payload = {
        "rt_cd": "0",
        "output1": {"rsym": "DNASNVDA"},
        "output2": [
            {"xymd": "20260717", "clos": "204.92", "open": "202.40", "high": "206.20",
             "low": "197.97", "tvol": "150000000"},
            {"xymd": "20260716", "clos": "207.40", "open": "205.00", "high": "208.00",
             "low": "204.10", "tvol": "140000000"},
            {"xymd": "", "clos": "1.0"},
        ],
    }
    client = make_client(
        tmp_path, {"/uapi/overseas-price/v1/quotations/dailyprice": payload}
    )

    rows = fetch_overseas_daily(client, "NAS", "NVDA")

    assert [row["day"].isoformat() for row in rows] == ["2026-07-16", "2026-07-17"]
    assert rows[-1]["close"] == 204.92
    assert rows[-1]["volume"] == 150000000.0


def credit_row0():
    return {
        "deal_date": "20260720",
        "stlm_date": "20260722",
        "stck_prpr": "244000",
        "stck_oprc": "241000",
        "stck_hgpr": "257500",
        "stck_lwpr": "240000",
        "prdy_ctrt": "1.23",
        "acml_vol": "26804038",
        "whol_loan_new_stcn": "2771679",
        "whol_loan_rdmp_stcn": "2770223",
        "whol_loan_rmnd_stcn": "23300119",
        "whol_loan_new_amt": "54977186",
        "whol_loan_rdmp_amt": "59421164",
        "whol_loan_rmnd_amt": "501478648",
        "whol_loan_rmnd_rate": "0.39",
        "whol_loan_gvrt": "10.33",
        "whol_stln_new_stcn": "4445",
        "whol_stln_rdmp_stcn": "5070",
        "whol_stln_rmnd_stcn": "5636",
        "whol_stln_new_amt": "110592",
        "whol_stln_rdmp_amt": "127127",
        "whol_stln_rmnd_amt": "104130",
        "whol_stln_rmnd_rate": "0.00",
        "whol_stln_gvrt": "0.01",
    }


def credit_payload():
    older = credit_row0() | {"deal_date": "20260717", "stlm_date": "20260721"}
    blank = credit_row0() | {"deal_date": ""}
    return {"rt_cd": "0", "output": [credit_row0(), older, blank]}


def test_fetch_credit_daily_maps_scaled_price_absolute_qty_raw_amt(tmp_path):
    with make_client(tmp_path, {CREDIT_DAILY_PATH: credit_payload()}) as client:
        rows = fetch_credit_daily(client, "005930", date(2026, 7, 23))

    assert len(rows) == 2
    row = rows[0]
    assert row["day"] == date(2026, 7, 20)
    assert row["settle_day"] == date(2026, 7, 22)
    assert row["close"] == 244000.0
    assert row["open"] == 241000.0
    assert row["high"] == 257500.0
    assert row["low"] == 240000.0
    assert row["change_pct"] == 1.23
    assert row["volume"] == 26804038.0
    assert row["loan_new_qty"] == 2771679.0
    assert row["loan_repay_qty"] == 2770223.0
    assert row["loan_balance_qty"] == 23300119.0
    assert row["loan_new_amt"] == 54977186.0
    assert row["loan_balance_amt"] == 501478648.0
    assert row["loan_balance_rate"] == 0.39
    assert row["loan_give_rate"] == 10.33
    assert row["short_balance_qty"] == 5636.0
    assert row["short_balance_amt"] == 104130.0
    assert row["short_give_rate"] == 0.01


def test_fetch_credit_daily_skips_blank_deal_date(tmp_path):
    with make_client(tmp_path, {CREDIT_DAILY_PATH: credit_payload()}) as client:
        rows = fetch_credit_daily(client, "005930", date(2026, 7, 23))
    assert [row["day"] for row in rows] == [date(2026, 7, 20), date(2026, 7, 17)]


def test_fetch_credit_daily_nonlist_output_returns_empty(tmp_path):
    with make_client(tmp_path, {CREDIT_DAILY_PATH: {"rt_cd": "0", "output": {}}}) as client:
        assert fetch_credit_daily(client, "005930", date(2026, 7, 23)) == []


def test_fetch_credit_daily_sends_tr_screen_and_anchor(tmp_path):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["tr_id"] = request.headers["tr_id"]
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=credit_payload())

    token_path = tmp_path / "kis_token.json"
    token_path.write_text(
        json.dumps({"access_token": "tok", "expired_at": "2099-01-01 00:00:00"})
    )
    with KisClient(
        "key",
        "secret",
        base_url="https://kis.test",
        token_path=token_path,
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    ) as client:
        fetch_credit_daily(client, "005930", date(2026, 7, 23))

    assert seen["path"] == CREDIT_DAILY_PATH
    assert seen["tr_id"] == CREDIT_DAILY_TR
    params = seen["params"]
    assert params["FID_COND_MRKT_DIV_CODE"] == "J"
    assert params["FID_COND_SCR_DIV_CODE"] == "20476"
    assert params["FID_INPUT_ISCD"] == "005930"
    assert params["FID_INPUT_DATE_1"] == "20260723"


def test_overseas_index_daily_parses(tmp_path):
    payload = {
        "rt_cd": "0",
        "output1": {"hts_kor_isnm": "S&P500"},
        "output2": [
            {"stck_bsop_date": "20260717", "ovrs_nmix_prpr": "7533.77",
             "ovrs_nmix_oprc": "7500.00", "ovrs_nmix_hgpr": "7550.00",
             "ovrs_nmix_lwpr": "7480.00", "acml_vol": "0"},
        ],
    }
    client = make_client(
        tmp_path, {"/uapi/overseas-price/v1/quotations/inquire-daily-chartprice": payload}
    )

    rows = fetch_overseas_index_daily(
        client, "SPX", date(2026, 7, 1), date(2026, 7, 17)
    )

    assert len(rows) == 1
    assert rows[0]["day"] == date(2026, 7, 17)
    assert rows[0]["close"] == 7533.77
