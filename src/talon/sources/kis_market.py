import logging
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

RANKING_SIDES = {"buy": "0", "sell": "1"}


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
