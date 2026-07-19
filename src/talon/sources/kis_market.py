import logging
from datetime import date, datetime, timedelta
from typing import Any

from talon.sources.kis import KisClient

log = logging.getLogger(__name__)

KRX_ONLY = "J"
ORDERBOOK_PATH = "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn"
ORDERBOOK_TR = "FHKST01010200"
INVESTOR_ESTIMATE_PATH = "/uapi/domestic-stock/v1/quotations/investor-trend-estimate"
INVESTOR_ESTIMATE_TR = "HHPTJ04160200"
FLOW_RANKING_PATH = "/uapi/domestic-stock/v1/quotations/foreign-institution-total"
FLOW_RANKING_TR = "FHPTJ04400000"
FRGNMEM_RANKING_PATH = "/uapi/domestic-stock/v1/quotations/frgnmem-trade-estimate"
FRGNMEM_RANKING_TR = "FHKST644100C0"
VOLUME_POWER_PATH = "/uapi/domestic-stock/v1/quotations/inquire-ccnl"
VOLUME_POWER_TR = "FHKST01010300"
MEMBER_PATH = "/uapi/domestic-stock/v1/quotations/inquire-member"
MEMBER_TR = "FHKST01010600"
PROGRAM_TRADE_PATH = "/uapi/domestic-stock/v1/quotations/program-trade-by-stock"
PROGRAM_TRADE_TR = "FHPPG04650101"
PROGRAM_MARKET_PATH = "/uapi/domestic-stock/v1/quotations/comp-program-trade-today"
PROGRAM_MARKET_TR = "FHPPG04600101"
FRGNMEM_TREND_PATH = "/uapi/domestic-stock/v1/quotations/frgnmem-pchs-trend"
FRGNMEM_TREND_TR = "FHKST644400C0"
OVERTIME_PRICE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-overtime-price"
OVERTIME_PRICE_TR = "FHPST02300000"
OVERTIME_RANKING_PATH = "/uapi/domestic-stock/v1/ranking/overtime-fluctuation"
OVERTIME_RANKING_TR = "FHPST02340000"
OVERSEAS_DAILY_PATH = "/uapi/overseas-price/v1/quotations/dailyprice"
OVERSEAS_DAILY_TR = "HHDFS76240000"
OVERSEAS_INDEX_PATH = "/uapi/overseas-price/v1/quotations/inquire-daily-chartprice"
OVERSEAS_INDEX_TR = "FHKST03030100"
MINUTE_CHART_PATH = "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice"
MINUTE_CHART_TR = "FHKST03010230"

RANKING_SIDES = {"buy": "0", "sell": "1"}
OVERTIME_RANKING_SIDES = {"up": "2", "down": "5"}
MEMBER_SIDES = {"sell": "seln", "buy": "shnu"}
FRGNMEM_TREND_PARTNER = "99999"


def _num(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _int(value: Any) -> int | None:
    number = _num(value)
    return int(number) if number is not None else None


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def fetch_orderbook(client: KisClient, symbol: str) -> dict[str, Any] | None:
    payload = client.get(
        ORDERBOOK_PATH,
        ORDERBOOK_TR,
        {"FID_COND_MRKT_DIV_CODE": KRX_ONLY, "FID_INPUT_ISCD": symbol},
    )
    book = payload.get("output1")
    expected = payload.get("output2")
    if not isinstance(book, dict) or not book:
        return None
    if not isinstance(expected, dict):
        expected = {}
    row: dict[str, Any] = {"symbol": symbol}
    for level in range(1, 11):
        row[f"ask_price_{level}"] = _num(book.get(f"askp{level}"))
        row[f"ask_qty_{level}"] = _num(book.get(f"askp_rsqn{level}"))
        row[f"bid_price_{level}"] = _num(book.get(f"bidp{level}"))
        row[f"bid_qty_{level}"] = _num(book.get(f"bidp_rsqn{level}"))
    row["total_ask_qty"] = _num(book.get("total_askp_rsqn"))
    row["total_bid_qty"] = _num(book.get("total_bidp_rsqn"))
    row["net_bid_qty"] = _num(book.get("ntby_aspr_rsqn"))
    row["accept_hour"] = _text(book.get("aspr_acpt_hour"))
    row["market_phase"] = _text(book.get("new_mkop_cls_code"))
    row["price"] = _num(expected.get("stck_prpr"))
    row["open"] = _num(expected.get("stck_oprc"))
    row["high"] = _num(expected.get("stck_hgpr"))
    row["low"] = _num(expected.get("stck_lwpr"))
    row["prev_close"] = _num(expected.get("stck_sdpr"))
    row["antc_price"] = _num(expected.get("antc_cnpr"))
    row["antc_qty"] = _num(expected.get("antc_vol"))
    row["antc_phase"] = _text(expected.get("antc_mkop_cls_code"))
    row["vi_code"] = _text(expected.get("vi_cls_code"))
    return row


def fetch_investor_estimate(client: KisClient, symbol: str) -> dict[str, Any] | None:
    payload = client.get(INVESTOR_ESTIMATE_PATH, INVESTOR_ESTIMATE_TR, {"MKSC_SHRN_ISCD": symbol})
    buckets = payload.get("output2")
    if not isinstance(buckets, list) or not buckets:
        return None

    def bucket_index(row: dict[str, Any]) -> int:
        value = _num(row.get("bsop_hour_gb"))
        return int(value) if value is not None else -1

    latest = max((row for row in buckets if isinstance(row, dict)), key=bucket_index, default=None)
    if latest is None or bucket_index(latest) < 0:
        return None
    return {
        "symbol": symbol,
        "bucket": bucket_index(latest),
        "frgn_qty": _num(latest.get("frgn_fake_ntby_qty")),
        "orgn_qty": _num(latest.get("orgn_fake_ntby_qty")),
        "sum_qty": _num(latest.get("sum_fake_ntby_qty")),
    }


def fetch_flow_ranking(client: KisClient, side: str) -> list[dict[str, Any]]:
    payload = client.get(
        FLOW_RANKING_PATH,
        FLOW_RANKING_TR,
        {
            "FID_COND_MRKT_DIV_CODE": "V",
            "FID_COND_SCR_DIV_CODE": "16449",
            "FID_INPUT_ISCD": "0000",
            "FID_DIV_CLS_CODE": "1",
            "FID_RANK_SORT_CLS_CODE": RANKING_SIDES[side],
            "FID_ETC_CLS_CODE": "0",
        },
    )
    rows = payload.get("output")
    if not isinstance(rows, list):
        return []
    records = []
    for rank, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        symbol = _text(row.get("mksc_shrn_iscd"))
        if symbol is None:
            continue
        records.append(
            {
                "side": side,
                "rank": rank,
                "symbol": symbol,
                "name": _text(row.get("hts_kor_isnm")),
                "total_qty": _num(row.get("ntby_qty")),
                "frgn_qty": _num(row.get("frgn_ntby_qty")),
                "orgn_qty": _num(row.get("orgn_ntby_qty")),
                "etc_corp_qty": _num(row.get("etc_corp_ntby_vol")),
                "ivtr_qty": _num(row.get("ivtr_ntby_qty")),
                "bank_qty": _num(row.get("bank_ntby_qty")),
                "insu_qty": _num(row.get("insu_ntby_qty")),
                "mrbn_qty": _num(row.get("mrbn_ntby_qty")),
                "fund_qty": _num(row.get("fund_ntby_qty")),
                "etc_fin_qty": _num(row.get("etc_orgt_ntby_vol")),
                "frgn_amount": _num(row.get("frgn_ntby_tr_pbmn")),
                "orgn_amount": _num(row.get("orgn_ntby_tr_pbmn")),
                "etc_corp_amount": _num(row.get("etc_corp_ntby_tr_pbmn")),
                "price": _num(row.get("stck_prpr")),
                "change_pct": _num(row.get("prdy_ctrt")),
                "volume": _num(row.get("acml_vol")),
            }
        )
    return records


def fetch_frgnmem_ranking(client: KisClient, side: str) -> list[dict[str, Any]]:
    payload = client.get(
        FRGNMEM_RANKING_PATH,
        FRGNMEM_RANKING_TR,
        {
            "FID_COND_MRKT_DIV_CODE": KRX_ONLY,
            "FID_COND_SCR_DIV_CODE": "16441",
            "FID_INPUT_ISCD": "0000",
            "FID_RANK_SORT_CLS_CODE": "0",
            "FID_RANK_SORT_CLS_CODE_2": RANKING_SIDES[side],
        },
    )
    rows = payload.get("output")
    if not isinstance(rows, list):
        return []
    records = []
    for rank, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        symbol = _text(row.get("stck_shrn_iscd"))
        if symbol is None:
            continue
        records.append(
            {
                "side": side,
                "rank": rank,
                "symbol": symbol,
                "name": _text(row.get("hts_kor_isnm")),
                "net_qty": _num(row.get("glob_ntsl_qty")),
                "buy_qty": _num(row.get("glob_total_shnu_qty")),
                "sell_qty": _num(row.get("glob_total_seln_qty")),
                "price": _num(row.get("stck_prpr")),
                "change_pct": _num(row.get("prdy_ctrt")),
                "volume": _num(row.get("acml_vol")),
            }
        )
    return records


def fetch_volume_power(client: KisClient, symbol: str) -> dict[str, Any] | None:
    payload = client.get(
        VOLUME_POWER_PATH,
        VOLUME_POWER_TR,
        {"FID_COND_MRKT_DIV_CODE": KRX_ONLY, "FID_INPUT_ISCD": symbol},
    )
    ticks = payload.get("output")
    if not isinstance(ticks, list) or not ticks:
        return None
    latest = ticks[0]
    if not isinstance(latest, dict):
        return None
    return {
        "symbol": symbol,
        "strength": _num(latest.get("tday_rltv")),
        "tick_hour": _text(latest.get("stck_cntg_hour")),
        "price": _num(latest.get("stck_prpr")),
        "change_pct": _num(latest.get("prdy_ctrt")),
    }


def fetch_member(client: KisClient, symbol: str) -> dict[str, Any] | None:
    payload = client.get(
        MEMBER_PATH,
        MEMBER_TR,
        {"FID_COND_MRKT_DIV_CODE": KRX_ONLY, "FID_INPUT_ISCD": symbol},
    )
    rows = payload.get("output")
    if not isinstance(rows, list) or not rows:
        return None
    data = rows[0]
    if not isinstance(data, dict):
        return None
    row: dict[str, Any] = {"symbol": symbol}
    for side, prefix in MEMBER_SIDES.items():
        for n in range(1, 6):
            row[f"{side}_member_no_{n}"] = _text(data.get(f"{prefix}_mbcr_no{n}"))
            row[f"{side}_member_name_{n}"] = _text(data.get(f"{prefix}_mbcr_name{n}"))
            row[f"{side}_member_qty_{n}"] = _num(data.get(f"total_{prefix}_qty{n}"))
            row[f"{side}_member_share_{n}"] = _num(data.get(f"{prefix}_mbcr_rlim{n}"))
            row[f"{side}_member_qty_change_{n}"] = _num(data.get(f"{prefix}_qty_icdc{n}"))
            row[f"{side}_member_foreign_{n}"] = _text(data.get(f"{prefix}_mbcr_glob_yn_{n}"))
    row["foreign_buy_qty"] = _num(data.get("glob_total_shnu_qty"))
    row["foreign_sell_qty"] = _num(data.get("glob_total_seln_qty"))
    row["foreign_net_qty"] = _num(data.get("glob_ntby_qty"))
    row["foreign_buy_share"] = _num(data.get("glob_shnu_rlim"))
    row["foreign_sell_share"] = _num(data.get("glob_seln_rlim"))
    row["foreign_buy_qty_change"] = _num(data.get("glob_total_shnu_qty_icdc"))
    row["foreign_sell_qty_change"] = _num(data.get("glob_total_seln_qty_icdc"))
    row["volume"] = _num(data.get("acml_vol"))
    return row


def fetch_program_trade(client: KisClient, symbol: str) -> dict[str, Any] | None:
    payload = client.get(
        PROGRAM_TRADE_PATH,
        PROGRAM_TRADE_TR,
        {"FID_COND_MRKT_DIV_CODE": KRX_ONLY, "FID_INPUT_ISCD": symbol},
    )
    ticks = payload.get("output")
    if not isinstance(ticks, list) or not ticks:
        return None
    latest = ticks[0]
    if not isinstance(latest, dict):
        return None
    return {
        "symbol": symbol,
        "tick_hour": _text(latest.get("bsop_hour")),
        "price": _num(latest.get("stck_prpr")),
        "change_pct": _num(latest.get("prdy_ctrt")),
        "volume": _num(latest.get("acml_vol")),
        "sell_qty": _num(latest.get("whol_smtn_seln_vol")),
        "buy_qty": _num(latest.get("whol_smtn_shnu_vol")),
        "net_qty": _num(latest.get("whol_smtn_ntby_qty")),
        "sell_amount": _num(latest.get("whol_smtn_seln_tr_pbmn")),
        "buy_amount": _num(latest.get("whol_smtn_shnu_tr_pbmn")),
        "net_amount": _num(latest.get("whol_smtn_ntby_tr_pbmn")),
    }


def fetch_program_market(client: KisClient, market: str) -> list[dict[str, Any]]:
    payload = client.get(
        PROGRAM_MARKET_PATH,
        PROGRAM_MARKET_TR,
        {
            "FID_COND_MRKT_DIV_CODE": KRX_ONLY,
            "FID_MRKT_CLS_CODE": market,
            "FID_SCTN_CLS_CODE": "",
            "FID_INPUT_ISCD": "",
            "FID_COND_MRKT_DIV_CODE1": "",
            "FID_INPUT_HOUR_1": "",
        },
    )
    rows = payload.get("output")
    if not isinstance(rows, list):
        return []
    records = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        records.append(
            {
                "market": market,
                "hour": _text(row.get("bsop_hour")),
                "arb_sell_amount": _num(row.get("arbt_smtn_seln_tr_pbmn")),
                "arb_buy_amount": _num(row.get("arbt_smtn_shnu_tr_pbmn")),
                "arb_net_amount": _num(row.get("arbt_smtn_ntby_tr_pbmn")),
                "nonarb_sell_amount": _num(row.get("nabt_smtn_seln_tr_pbmn")),
                "nonarb_buy_amount": _num(row.get("nabt_smtn_shnu_tr_pbmn")),
                "nonarb_net_amount": _num(row.get("nabt_smtn_ntby_tr_pbmn")),
                "total_net_amount": _num(row.get("whol_smtn_ntby_tr_pbmn")),
            }
        )
    return records


def fetch_frgnmem_trend(client: KisClient, symbol: str) -> list[dict[str, Any]]:
    payload = client.get(
        FRGNMEM_TREND_PATH,
        FRGNMEM_TREND_TR,
        {
            "FID_COND_MRKT_DIV_CODE": KRX_ONLY,
            "FID_INPUT_ISCD": symbol,
            "FID_INPUT_ISCD_2": FRGNMEM_TREND_PARTNER,
        },
    )
    ticks = payload.get("output")
    if not isinstance(ticks, list):
        return []
    records = []
    for seq, tick in enumerate(ticks):
        if not isinstance(tick, dict):
            continue
        records.append(
            {
                "symbol": symbol,
                "seq": seq,
                "tick_hour": _text(tick.get("bsop_hour")),
                "price": _num(tick.get("stck_prpr")),
                "change_pct": _num(tick.get("prdy_ctrt")),
                "volume": _num(tick.get("acml_vol")),
                "foreign_sell_qty": _num(tick.get("frgn_seln_vol")),
                "foreign_buy_qty": _num(tick.get("frgn_shnu_vol")),
                "foreign_net_qty": _num(tick.get("glob_ntby_qty")),
                "net_qty_change": _num(tick.get("frgn_ntby_qty_icdc")),
            }
        )
    return records


def fetch_overtime_price(client: KisClient, symbol: str) -> dict[str, Any] | None:
    payload = client.get(
        OVERTIME_PRICE_PATH,
        OVERTIME_PRICE_TR,
        {"FID_COND_MRKT_DIV_CODE": KRX_ONLY, "FID_INPUT_ISCD": symbol},
    )
    data = payload.get("output")
    if not isinstance(data, dict) or not data:
        return None
    return {
        "symbol": symbol,
        "prev_close": _num(data.get("ovtm_untp_sdpr")),
        "price": _num(data.get("ovtm_untp_prpr")),
        "change": _num(data.get("ovtm_untp_prdy_vrss")),
        "change_pct": _num(data.get("ovtm_untp_prdy_ctrt")),
        "sign": _text(data.get("ovtm_untp_prdy_vrss_sign")),
        "open": _num(data.get("ovtm_untp_oprc")),
        "high": _num(data.get("ovtm_untp_hgpr")),
        "low": _num(data.get("ovtm_untp_lwpr")),
        "volume": _num(data.get("ovtm_untp_vol")),
        "amount": _num(data.get("ovtm_untp_tr_pbmn")),
        "upper_limit": _num(data.get("ovtm_untp_mxpr")),
        "lower_limit": _num(data.get("ovtm_untp_llam")),
        "vi_code": _text(data.get("ovtm_vi_cls_code")),
    }


def fetch_overtime_ranking(client: KisClient, side: str) -> dict[str, Any]:
    payload = client.get(
        OVERTIME_RANKING_PATH,
        OVERTIME_RANKING_TR,
        {
            "FID_COND_MRKT_DIV_CODE": KRX_ONLY,
            "FID_MRKT_CLS_CODE": "",
            "FID_COND_SCR_DIV_CODE": "20234",
            "FID_INPUT_ISCD": "0000",
            "FID_DIV_CLS_CODE": OVERTIME_RANKING_SIDES[side],
            "FID_INPUT_PRICE_1": "",
            "FID_INPUT_PRICE_2": "",
            "FID_VOL_CNT": "",
            "FID_TRGT_CLS_CODE": "",
            "FID_TRGT_EXLS_CLS_CODE": "",
        },
    )
    aggregate = payload.get("output1")
    if isinstance(aggregate, dict) and aggregate:
        market: dict[str, Any] | None = {
            "volume": _num(aggregate.get("ovtm_untp_acml_vol")),
            "amount": _num(aggregate.get("ovtm_untp_acml_tr_pbmn")),
            "kospi_volume": _num(aggregate.get("ovtm_untp_exch_vol")),
            "kospi_amount": _num(aggregate.get("ovtm_untp_exch_tr_pbmn")),
            "kosdaq_volume": _num(aggregate.get("ovtm_untp_kosdaq_vol")),
            "kosdaq_amount": _num(aggregate.get("ovtm_untp_kosdaq_tr_pbmn")),
            "up_count": _int(aggregate.get("ovtm_untp_ascn_issu_cnt")),
            "down_count": _int(aggregate.get("ovtm_untp_down_issu_cnt")),
            "flat_count": _int(aggregate.get("ovtm_untp_stnr_issu_cnt")),
            "upper_limit_count": _int(aggregate.get("ovtm_untp_uplm_issu_cnt")),
            "lower_limit_count": _int(aggregate.get("ovtm_untp_lslm_issu_cnt")),
        }
    else:
        market = None
    rows = payload.get("output2")
    records = []
    if isinstance(rows, list):
        for rank, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                continue
            symbol = _text(row.get("mksc_shrn_iscd"))
            if symbol is None:
                continue
            records.append(
                {
                    "side": side,
                    "rank": rank,
                    "symbol": symbol,
                    "name": _text(row.get("hts_kor_isnm")),
                    "price": _num(row.get("ovtm_untp_prpr")),
                    "change": _num(row.get("ovtm_untp_prdy_vrss")),
                    "change_pct": _num(row.get("ovtm_untp_prdy_ctrt")),
                    "sign": _text(row.get("ovtm_untp_prdy_vrss_sign")),
                    "ask": _num(row.get("ovtm_untp_askp1")),
                    "bid": _num(row.get("ovtm_untp_bidp1")),
                    "volume": _num(row.get("ovtm_untp_vol")),
                    "sell_rsqn": _num(row.get("ovtm_untp_seln_rsqn")),
                    "buy_rsqn": _num(row.get("ovtm_untp_shnu_rsqn")),
                    "vol_vs_day_pct": _num(row.get("ovtm_vrss_acml_vol_rlim")),
                    "day_price": _num(row.get("stck_prpr")),
                    "day_volume": _num(row.get("acml_vol")),
                }
            )
    return {"market": market, "rows": records}


def _parse_yyyymmdd(value: Any) -> date | None:
    text = _text(value)
    if text is None or len(text) != 8 or not text.isdigit():
        return None
    return date(int(text[:4]), int(text[4:6]), int(text[6:8]))


def fetch_overseas_daily(client: KisClient, excd: str, symbol: str) -> list[dict[str, Any]]:
    payload = client.get(
        OVERSEAS_DAILY_PATH,
        OVERSEAS_DAILY_TR,
        {"AUTH": "", "EXCD": excd, "SYMB": symbol, "GUBN": "0", "BYMD": "", "MODP": "1"},
    )
    rows = payload.get("output2")
    if not isinstance(rows, list):
        return []
    records = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        day = _parse_yyyymmdd(row.get("xymd"))
        close = _num(row.get("clos"))
        if day is None or close is None:
            continue
        records.append(
            {
                "day": day,
                "open": _num(row.get("open")),
                "high": _num(row.get("high")),
                "low": _num(row.get("low")),
                "close": close,
                "volume": _num(row.get("tvol")),
            }
        )
    return sorted(records, key=lambda record: record["day"])


def fetch_overseas_index_daily(
    client: KisClient, code: str, start: date, end: date
) -> list[dict[str, Any]]:
    payload = client.get(
        OVERSEAS_INDEX_PATH,
        OVERSEAS_INDEX_TR,
        {
            "FID_COND_MRKT_DIV_CODE": "N",
            "FID_INPUT_ISCD": code,
            "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
            "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
            "FID_PERIOD_DIV_CODE": "D",
        },
    )
    rows = payload.get("output2")
    if not isinstance(rows, list):
        return []
    records = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        day = _parse_yyyymmdd(row.get("stck_bsop_date"))
        close = _num(row.get("ovrs_nmix_prpr"))
        if day is None or close is None:
            continue
        records.append(
            {
                "day": day,
                "open": _num(row.get("ovrs_nmix_oprc")),
                "high": _num(row.get("ovrs_nmix_hgpr")),
                "low": _num(row.get("ovrs_nmix_lwpr")),
                "close": close,
                "volume": _num(row.get("acml_vol")),
            }
        )
    return sorted(records, key=lambda record: record["day"])


def _decrement_minute(time_text: str) -> str:
    moment = datetime(2000, 1, 1, int(time_text[:2]), int(time_text[2:4]), int(time_text[4:6]))
    return (moment - timedelta(minutes=1)).strftime("%H%M%S")


def fetch_minute_chart(
    client: KisClient, symbol: str, day: date, *, anchor: str, max_pages: int = 1
) -> list[dict[str, Any]]:
    request_date = day.strftime("%Y%m%d")
    rows: dict[str, dict[str, Any]] = {}
    cursor = anchor
    seen_oldest: str | None = None
    for _ in range(max(1, max_pages)):
        payload = client.get(
            MINUTE_CHART_PATH,
            MINUTE_CHART_TR,
            {
                "FID_COND_MRKT_DIV_CODE": KRX_ONLY,
                "FID_INPUT_ISCD": symbol,
                "FID_INPUT_DATE_1": request_date,
                "FID_INPUT_HOUR_1": cursor,
                "FID_PW_DATA_INCU_YN": "N",
                "FID_FAKE_TICK_INCU_YN": "",
            },
        )
        bars = payload.get("output2")
        if not isinstance(bars, list) or not bars:
            break
        page_times: list[str] = []
        added = 0
        for bar in bars:
            if not isinstance(bar, dict):
                continue
            if _parse_yyyymmdd(bar.get("stck_bsop_date")) != day:
                continue
            time_text = _text(bar.get("stck_cntg_hour"))
            if time_text is None or len(time_text) != 6 or not time_text.isdigit():
                continue
            page_times.append(time_text)
            if time_text in rows:
                continue
            rows[time_text] = {
                "time": time_text,
                "open": _num(bar.get("stck_oprc")),
                "high": _num(bar.get("stck_hgpr")),
                "low": _num(bar.get("stck_lwpr")),
                "close": _num(bar.get("stck_prpr")),
                "volume": _num(bar.get("cntg_vol")),
                "cum_value": _num(bar.get("acml_tr_pbmn")),
            }
            added += 1
        if not page_times:
            break
        oldest = min(page_times)
        if seen_oldest is not None and oldest >= seen_oldest:
            break
        seen_oldest = oldest
        if added == 0:
            break
        cursor = _decrement_minute(oldest)
    return [rows[key] for key in sorted(rows)]
