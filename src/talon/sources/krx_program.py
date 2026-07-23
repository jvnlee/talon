import time
from collections.abc import Callable
from datetime import date, datetime

import polars as pl

from talon.data.store import PROGRAM_MARKET_1D_SCHEMA
from talon.sources.krx_actions import _fetch_rows, _num, _require
from talon.sources.krx_daily import KrxCredentials
from talon.timeutil import now_utc

PROGRAM_MARKET_BLD = "dbms/MDC/STAT/standard/MDCSTAT02601"
PROGRAM_MARKET_SCREEN_URL = "https://data.krx.co.kr/contents/MDC/STAT/standard/MDCSTAT026.jsp"

PROGRAM_MARKETS = ("STK", "KSQ")

COMPONENT_LABELS = {"차익": "arb", "비차익": "nonarb", "전체": "total"}
COMPONENTS = ("arb", "nonarb", "total")

_PROGRAM_MARKET_REQUIRED = {
    "ITM_TP_NM",
    "ASK_TRDVOL",
    "BID_TRDVOL",
    "NETBID_TRDVOL",
    "ASK_TRDVAL",
    "BID_TRDVAL",
    "NETBID_TRDVAL",
}


def program_market_frame(
    day: date, market: str, rows: list[dict[str, str]], fetched_at: datetime
) -> pl.DataFrame:
    _require(rows, _PROGRAM_MARKET_REQUIRED, "program-market")
    records = [
        {
            "day": day,
            "market": market,
            "component": COMPONENT_LABELS.get(row.get("ITM_TP_NM", ""), row.get("ITM_TP_NM")),
            "sell_qty": _num(row.get("ASK_TRDVOL")),
            "buy_qty": _num(row.get("BID_TRDVOL")),
            "net_qty": _num(row.get("NETBID_TRDVOL")),
            "sell_value": _num(row.get("ASK_TRDVAL")),
            "buy_value": _num(row.get("BID_TRDVAL")),
            "net_value": _num(row.get("NETBID_TRDVAL")),
            "fetched_at": fetched_at,
        }
        for row in rows
    ]
    frame = pl.DataFrame(records, schema=PROGRAM_MARKET_1D_SCHEMA)
    if frame.is_empty():
        return frame
    return frame.sort(["market", "component"])


def fetch_program_market(
    day: date,
    market: str,
    *,
    credentials: KrxCredentials | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> pl.DataFrame:
    text = day.strftime("%Y%m%d")
    rows = _fetch_rows(
        PROGRAM_MARKET_BLD,
        {
            "mktId": market,
            "strtDd": text,
            "endDd": text,
            "share": "1",
            "money": "1",
            "csvxls_isNo": "false",
        },
        credentials=credentials,
        sleep=sleep,
    )
    return program_market_frame(day, market, rows, now_utc())
