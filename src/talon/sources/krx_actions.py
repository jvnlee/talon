import logging
import time
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

import polars as pl

from talon.data.store import (
    MARKET_ALERTS_SCHEMA,
    SHORT_OVERHEAT_SCHEMA,
    TRADING_HALTS_SCHEMA,
    VI_EVENTS_SCHEMA,
)
from talon.errors import SchemaDriftError, SourceError
from talon.sources.krx_daily import KrxCredentials, _load_pykrx, _retry
from talon.sources.krx_index import KRX_JSON_URL
from talon.timeutil import now_utc

log = logging.getLogger(__name__)

KRX_ACTIONS_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd",
    "X-Requested-With": "XMLHttpRequest",
}

VI_BLD = "dbms/MDC/STAT/issue/MDCSTAT22401"
VI_SCREEN_URL = "https://data.krx.co.kr/contents/MDC/STAT/issue/MDCSTAT224.jsp"
VI_KIND_LABELS = {"동적VI": "dynamic", "정적VI": "static"}
VI_KINDS = tuple(VI_KIND_LABELS.values())

ALERT_LEVEL_BLDS = {
    "caution": "dbms/MDC/STAT/issue/MDCSTAT22801",
    "warning": "dbms/MDC/STAT/issue/MDCSTAT23101",
    "risk": "dbms/MDC/STAT/issue/MDCSTAT23401",
}
ALERT_LEVELS = tuple(ALERT_LEVEL_BLDS)
ALERT_SCREEN_URLS = {
    "caution": "https://data.krx.co.kr/contents/MDC/STAT/issue/MDCSTAT228.jsp",
    "warning": "https://data.krx.co.kr/contents/MDC/STAT/issue/MDCSTAT231.jsp",
    "risk": "https://data.krx.co.kr/contents/MDC/STAT/issue/MDCSTAT234.jsp",
}

OVERHEAT_BLD = "dbms/MDC/STAT/srt/MDCSTAT30901"
OVERHEAT_DATA_KEY = "OutBlock_1"
OVERHEAT_SCREEN_URL = "https://data.krx.co.kr/contents/MDC/STAT/srt/MDCSTAT309.jsp"
OVERHEAT_DTEC_TYPES = ("유형2", "유형3", "연장")

HALTS_SNAPSHOT_BLD = "dbms/MDC/STAT/issue/MDCSTAT21201"
HALTS_HISTORY_BLD = "dbms/MDC/STAT/issue/MDCSTAT21301"
HALTS_SNAPSHOT_URL = "https://data.krx.co.kr/contents/MDC/STAT/issue/MDCSTAT212.jsp"
HALTS_HISTORY_URL = "https://data.krx.co.kr/contents/MDC/STAT/issue/MDCSTAT213.jsp"

_VI_REQUIRED = {"TRD_DD", "ISU_CD", "VI_KIND_NM", "VI_TG_TM"}
_ALERT_REQUIRED = {"ISU_CD", "ISU_NM", "DESIGN_DD"}
_OVERHEAT_REQUIRED = {"BAS_DD", "ISU_CD", "MKTACT_APPL_DD"}
_HALTS_SNAPSHOT_REQUIRED = {"ISU_CD", "ISU_NM", "HALT_DESNRELS_DDTM"}
_HALTS_HISTORY_REQUIRED = {"TRD_HALT_DD", "RESUMP_DD"}


def _num(raw: str | None) -> float | None:
    if raw is None:
        return None
    text = raw.replace(",", "").strip()
    if not text or text == "-":
        return None
    return float(text)


def _day(raw: str | None) -> date | None:
    if raw is None:
        return None
    text = raw.strip()
    if not text or text == "-":
        return None
    return datetime.strptime(text, "%Y/%m/%d").date()


def _clock(raw: str | None) -> str | None:
    if raw is None:
        return None
    text = raw.strip()
    if not text or text == "-":
        return None
    return text


def _str(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _require(rows: list[dict[str, Any]], required: set[str], label: str) -> None:
    if not rows:
        return
    missing = sorted(col for col in required if col not in rows[0])
    if missing:
        raise SchemaDriftError(f"KRX {label} columns missing: {missing}")


def _fetch_rows(
    bld: str,
    params: dict[str, str],
    *,
    credentials: KrxCredentials | None,
    sleep: Callable[[float], None],
    data_key: str = "output",
) -> list[dict[str, Any]]:
    _load_pykrx(credentials)
    from pykrx.website.comm.webio import get_session

    def request() -> Any:
        krx_session = get_session()
        if krx_session is None:
            raise SourceError("KRX 로그인 세션을 얻지 못했습니다")
        headers = dict(krx_session.get_headers())
        headers.update(KRX_ACTIONS_HEADERS)
        response = krx_session.session.post(
            KRX_JSON_URL,
            headers=headers,
            data={"bld": bld, "locale": "ko_KR", **params},
        )
        response.raise_for_status()
        return response.json()

    body = _retry(request, sleep=sleep)
    if not isinstance(body, dict):
        return []
    rows = body.get(data_key, [])
    return rows if isinstance(rows, list) else []


def vi_events_frame(rows: list[dict[str, Any]], fetched_at: datetime) -> pl.DataFrame:
    _require(rows, _VI_REQUIRED, "VI")
    records = [
        {
            "day": _day(row.get("TRD_DD")),
            "symbol": str(row.get("ISU_CD", "")),
            "name": _str(row.get("ISU_NM")),
            "market": _str(row.get("MKT_NM")),
            "vi_kind": VI_KIND_LABELS.get(row.get("VI_KIND_NM", ""), row.get("VI_KIND_NM")),
            "trigger_time": _clock(row.get("VI_TG_TM")),
            "release_time": _clock(row.get("VI_RELEAS_TM")),
            "reference_price": _num(row.get("VI_TG_BAS_PRC")),
            "trigger_price": _num(row.get("VI_TG_PRC")),
            "divergence_pct": _num(row.get("VI_TG_PRC_DIVRG_RT")),
            "fetched_at": fetched_at,
        }
        for row in rows
    ]
    frame = pl.DataFrame(records, schema=VI_EVENTS_SCHEMA)
    if frame.is_empty():
        return frame
    return frame.sort(["day", "symbol", "vi_kind", "trigger_time"])


def fetch_vi_events(
    start: date,
    end: date,
    *,
    credentials: KrxCredentials | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> pl.DataFrame:
    rows = _fetch_rows(
        VI_BLD,
        {
            "mktId": "ALL",
            "viKindCd": "ALL",
            "strtDd": start.strftime("%Y%m%d"),
            "endDd": end.strftime("%Y%m%d"),
            "isuCd": "ALL",
            "isuCd2": "ALL",
            "param1isuCd_finder_stkisu1": "ALL",
            "codeNmisuCd_finder_stkisu1": "",
            "inqTpCd1": "01",
            "prcDetailView": "1",
            "share": "1",
            "money": "1",
            "csvxls_isNo": "false",
        },
        credentials=credentials,
        sleep=sleep,
    )
    return vi_events_frame(rows, now_utc())


def market_alerts_frame(
    day: date,
    rows_by_level: dict[str, list[dict[str, Any]]],
    fetched_at: datetime,
) -> pl.DataFrame:
    records: list[dict[str, Any]] = []
    for level, rows in rows_by_level.items():
        _require(rows, _ALERT_REQUIRED, f"market-alert:{level}")
        for row in rows:
            release = row.get("RELEASE_DD")
            records.append(
                {
                    "day": day,
                    "level": level,
                    "symbol": str(row.get("ISU_CD", "")),
                    "isin": _str(row.get("ISU_CD_FULL")),
                    "name": _str(row.get("ISU_NM")),
                    "market": _str(row.get("MKT_NM")),
                    "design_dd": _day(row.get("DESIGN_DD")),
                    "release_dd": _day(release),
                    "fetched_at": fetched_at,
                }
            )
    frame = pl.DataFrame(records, schema=MARKET_ALERTS_SCHEMA)
    if frame.is_empty():
        return frame
    return frame.sort(["level", "symbol"])


def fetch_market_alerts(
    day: date,
    *,
    credentials: KrxCredentials | None = None,
    pause: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> pl.DataFrame:
    fetched_at = now_utc()
    rows_by_level: dict[str, list[dict[str, Any]]] = {}
    for level, bld in ALERT_LEVEL_BLDS.items():
        rows_by_level[level] = _fetch_rows(
            bld,
            {
                "mktId": "ALL",
                "inqTp": "1",
                "share": "1",
                "money": "1",
                "csvxls_isNo": "false",
            },
            credentials=credentials,
            sleep=sleep,
        )
        sleep(pause)
    return market_alerts_frame(day, rows_by_level, fetched_at)


def short_overheat_frame(rows: list[dict[str, Any]], fetched_at: datetime) -> pl.DataFrame:
    _require(rows, _OVERHEAT_REQUIRED, "short-overheat")
    records = [
        {
            "day": _day(row.get("BAS_DD")),
            "symbol": str(row.get("ISU_CD", "")),
            "isin": _str(row.get("ISU_CD_FULL")),
            "name": _str(row.get("ISU_ABBRV")),
            "market": _str(row.get("MKT_NM")),
            "mkt_id": _str(row.get("MKT_ID")),
            "restrict_apply_dd": _day(row.get("MKTACT_APPL_DD")),
            "release_dd": _day(row.get("RELEAS_DD")),
            "valu_pd_tr_dys": _num(row.get("VALU_PD_TR_DYS")),
            "tdd_srtsell_wt": _num(row.get("TDD_SRTSELL_WT")),
            "prc_yd": _num(row.get("PRC_YD")),
            "tdd_srtsell_trdval_incdec_rt": _num(row.get("TDD_SRTSELL_TRDVAL_INCDEC_RT")),
            "valu_pd_avg_srtsell_wt": _num(row.get("VALU_PD_AVG_SRTSELL_WT")),
            "dtec_type": _str(row.get("SRTSELL_IMPSBL_DTEC_TP_NM")),
            "fetched_at": fetched_at,
        }
        for row in rows
    ]
    frame = pl.DataFrame(records, schema=SHORT_OVERHEAT_SCHEMA)
    if frame.is_empty():
        return frame
    return frame.sort(["day", "symbol"])


def fetch_short_overheat(
    start: date,
    end: date,
    *,
    credentials: KrxCredentials | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> pl.DataFrame:
    rows = _fetch_rows(
        OVERHEAT_BLD,
        {
            "searchType": "1",
            "mktTpCd": "0",
            "isuCd": "",
            "isuCd2": "",
            "strtDd": start.strftime("%Y%m%d"),
            "endDd": end.strftime("%Y%m%d"),
            "share": "1",
            "money": "1",
            "csvxls_isNo": "false",
        },
        credentials=credentials,
        sleep=sleep,
        data_key=OVERHEAT_DATA_KEY,
    )
    return short_overheat_frame(rows, now_utc())


def trading_halts_frame(rows: list[dict[str, Any]], fetched_at: datetime) -> pl.DataFrame:
    _require(rows, _HALTS_SNAPSHOT_REQUIRED, "trading-halt")
    records = [
        {
            "day": _day(row.get("HALT_DESNRELS_DDTM")),
            "symbol": str(row.get("ISU_CD", "")),
            "isin": _str(row.get("ISU_CD_FULL")),
            "name": _str(row.get("ISU_NM")),
            "market": _str(row.get("MKT_NM")),
            "reason": _str(row.get("HALT_RSN_NM")),
            "last_trade_day": _day(row.get("LST_TRD_DD")),
            "resume_day": None,
            "fetched_at": fetched_at,
        }
        for row in rows
    ]
    frame = pl.DataFrame(records, schema=TRADING_HALTS_SCHEMA)
    if frame.is_empty():
        return frame
    return frame.sort(["day", "symbol"])


def fetch_trading_halts(
    *,
    credentials: KrxCredentials | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> pl.DataFrame:
    rows = _fetch_rows(
        HALTS_SNAPSHOT_BLD,
        {"mktId": "ALL", "share": "1", "money": "1", "csvxls_isNo": "false"},
        credentials=credentials,
        sleep=sleep,
    )
    return trading_halts_frame(rows, now_utc())


def halt_resume_map(rows: list[dict[str, Any]]) -> dict[date, date]:
    _require(rows, _HALTS_HISTORY_REQUIRED, "trading-halt-history")
    result: dict[date, date] = {}
    for row in rows:
        halt = _day(row.get("TRD_HALT_DD"))
        resume = _day(row.get("RESUMP_DD"))
        if halt is not None and resume is not None:
            result[halt] = resume
    return result


def fetch_halt_history(
    isin: str,
    start: date,
    end: date,
    *,
    credentials: KrxCredentials | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[date, date]:
    rows = _fetch_rows(
        HALTS_HISTORY_BLD,
        {
            "isuCd": isin,
            "isuCd2": isin,
            "param1isuCd_finder_stkisu0": "ALL",
            "codeNmisuCd_finder_stkisu0": "",
            "tboxisuCd_finder_stkisu0": "",
            "strtDd": start.strftime("%Y%m%d"),
            "endDd": end.strftime("%Y%m%d"),
            "share": "1",
            "money": "1",
            "csvxls_isNo": "false",
        },
        credentials=credentials,
        sleep=sleep,
    )
    return halt_resume_map(rows)
