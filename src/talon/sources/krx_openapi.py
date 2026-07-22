import logging
import time
from collections.abc import Callable
from datetime import date
from typing import Any

import httpx
import polars as pl

from talon.data.store import (
    DAILY_SNAPSHOT_SCHEMA,
    MARKET_CAP_SCHEMA,
    STOCK_INFO_SCHEMA,
    normalize_daily_snapshot,
)
from talon.errors import SchemaDriftError, SourceError

log = logging.getLogger(__name__)

BASE_URL = "https://data-dbg.krx.co.kr/svc/apis"
STOCK_ENDPOINTS = ("sto/stk_bydd_trd", "sto/ksq_bydd_trd")
INFO_ENDPOINTS = ("sto/stk_isu_base_info", "sto/ksq_isu_base_info")
EARLIEST_DAY = date(2010, 1, 4)
TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)
RETRIES = 2
RETRY_BACKOFF_SECONDS = 2.0

_REQUIRED_FIELDS = (
    "ISU_CD",
    "TDD_OPNPRC",
    "TDD_HGPRC",
    "TDD_LWPRC",
    "TDD_CLSPRC",
    "ACC_TRDVOL",
    "ACC_TRDVAL",
    "MKTCAP",
    "LIST_SHRS",
)

_INFO_FIELDS = (
    "ISU_SRT_CD",
    "ISU_ABBRV",
    "MKT_TP_NM",
    "SECUGRP_NM",
    "KIND_STKCERT_TP_NM",
    "SECT_TP_NM",
    "LIST_DD",
    "LIST_SHRS",
)


def _number(raw: Any) -> float | None:
    text = str(raw).replace(",", "").strip()
    if not text or text == "-":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _text(raw: Any) -> str:
    return "" if raw is None else str(raw).strip()


def _listing_day(raw: Any) -> date | None:
    text = _text(raw).replace("-", "")
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError:
        return None


class KrxOpenApiSource:
    def __init__(
        self,
        auth_key: str,
        *,
        base_url: str = BASE_URL,
        timeout: httpx.Timeout | float = TIMEOUT,
        retries: int = RETRIES,
        throttle: float = 0.2,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not auth_key:
            raise SourceError("KRX Open API 인증키가 없습니다 (TALON_KRX_API_KEY)")
        self._throttle = throttle
        self._retries = retries
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"AUTH_KEY": auth_key},
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def _request(self, endpoint: str, day: date) -> httpx.Response:
        params = {"basDd": day.strftime("%Y%m%d")}
        attempt = 0
        while True:
            try:
                return self._http.get(f"/{endpoint}", params=params)
            except httpx.TimeoutException as exc:
                attempt += 1
                if attempt > self._retries:
                    raise SourceError(f"KRX Open API 요청 실패 ({endpoint}): {exc}") from exc
                log.warning(
                    "KRX Open API timed out (%s), retry %d/%d", endpoint, attempt, self._retries
                )
                self._sleep(RETRY_BACKOFF_SECONDS * attempt)
            except httpx.HTTPError as exc:
                raise SourceError(f"KRX Open API 요청 실패 ({endpoint}): {exc}") from exc

    def rows(self, endpoint: str, day: date) -> list[dict[str, Any]]:
        response = self._request(endpoint, day)
        if response.status_code == 401:
            raise SourceError(
                f"KRX Open API 인증 거부 ({endpoint}): 인증키가 유효하지 않거나 "
                "해당 서비스 이용 신청이 승인되지 않았거나 유효기간이 만료되었습니다"
            )
        if response.status_code != 200:
            raise SourceError(
                f"KRX Open API HTTP {response.status_code} ({endpoint}): {response.text[:200]}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceError(f"KRX Open API 응답이 JSON이 아닙니다 ({endpoint})") from exc
        for value in payload.values():
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
        raise SchemaDriftError(f"KRX Open API 응답에 레코드 배열이 없습니다 ({endpoint})")

    def _fetch(
        self,
        day: date,
        endpoints: tuple[str, ...],
        required: tuple[str, ...],
        label: str,
    ) -> list[dict[str, Any]]:
        if day < EARLIEST_DAY:
            raise SourceError(f"KRX Open API는 {EARLIEST_DAY}부터 제공됩니다 (요청: {day})")
        records: list[dict[str, Any]] = []
        for index, endpoint in enumerate(endpoints):
            if index and self._throttle:
                self._sleep(self._throttle)
            records += self.rows(endpoint, day)
        if records:
            missing = sorted(f for f in required if f not in records[0])
            if missing:
                raise SchemaDriftError(f"KRX Open API {label} 필드 누락: {missing}")
        return records

    def fetch(self, day: date) -> list[dict[str, Any]]:
        return self._fetch(day, STOCK_ENDPOINTS, _REQUIRED_FIELDS, "일별매매정보")

    def stock_info(self, day: date) -> pl.DataFrame:
        rows = self._fetch(day, INFO_ENDPOINTS, _INFO_FIELDS, "종목기본정보")
        if not rows:
            return pl.DataFrame(schema=STOCK_INFO_SCHEMA)
        return pl.DataFrame(
            {
                "day": [day] * len(rows),
                "symbol": [_text(row["ISU_SRT_CD"]) for row in rows],
                "name": [_text(row["ISU_ABBRV"]) for row in rows],
                "market": [_text(row["MKT_TP_NM"]) for row in rows],
                "security_group": [_text(row["SECUGRP_NM"]) for row in rows],
                "share_kind": [_text(row["KIND_STKCERT_TP_NM"]) for row in rows],
                "section": [_text(row["SECT_TP_NM"]) for row in rows],
                "listed_on": [_listing_day(row["LIST_DD"]) for row in rows],
                "shares": [_number(row["LIST_SHRS"]) for row in rows],
            },
            schema=STOCK_INFO_SCHEMA,
        ).sort("symbol")

    def snapshot(self, day: date) -> tuple[pl.DataFrame, pl.DataFrame]:
        rows = self.fetch(day)
        if not rows:
            return (
                pl.DataFrame(schema=DAILY_SNAPSHOT_SCHEMA),
                pl.DataFrame(schema=MARKET_CAP_SCHEMA),
            )
        symbols = [str(row["ISU_CD"]) for row in rows]
        columns = {name: [_number(row[name]) for row in rows] for name in _REQUIRED_FIELDS[1:]}
        change_pct = [_number(row.get("FLUC_RT")) for row in rows]

        daily = pl.DataFrame(
            {
                "day": [day] * len(symbols),
                "symbol": symbols,
                "open": columns["TDD_OPNPRC"],
                "high": columns["TDD_HGPRC"],
                "low": columns["TDD_LWPRC"],
                "close": columns["TDD_CLSPRC"],
                "volume": columns["ACC_TRDVOL"],
                "value": columns["ACC_TRDVAL"],
                "change_pct": change_pct,
            },
            schema=DAILY_SNAPSHOT_SCHEMA,
        )
        caps = pl.DataFrame(
            {
                "day": [day] * len(symbols),
                "symbol": symbols,
                "close": columns["TDD_CLSPRC"],
                "cap": columns["MKTCAP"],
                "volume": columns["ACC_TRDVOL"],
                "value": columns["ACC_TRDVAL"],
                "shares": columns["LIST_SHRS"],
            },
            schema=MARKET_CAP_SCHEMA,
        )
        listed = normalize_daily_snapshot(daily).sort("symbol")
        return (
            listed,
            caps.join(listed.select("symbol"), on="symbol", how="semi")
            .filter(pl.col("cap") > 0)
            .sort("symbol"),
        )
