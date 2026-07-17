import logging
import time
from collections.abc import Callable
from datetime import date

import httpx

from talon.errors import SchemaDriftError, SourceError

log = logging.getLogger(__name__)

OTP_URL = "https://open.krx.co.kr/contents/COM/GenerateOTP.jspx"
DATA_URL = "https://open.krx.co.kr/contents/OPN/99/OPN99000001.jspx"
SCREEN_BLD = "MKD/01/0110/01100305/mkd01100305_01"
SCREEN_PAGE = "/contents/MKD/01/0110/01100305/MKD01100305.jsp"
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": f"https://open.krx.co.kr{SCREEN_PAGE}",
}
MAX_ATTEMPTS = 3
RETRY_WAIT = 2.0


def _parse_holidays(payload: object) -> dict[date, str]:
    if not isinstance(payload, dict) or "block1" not in payload:
        raise SchemaDriftError("KRX 휴장일 응답에 block1이 없습니다")
    holidays: dict[date, str] = {}
    for row in payload["block1"]:
        try:
            day = date.fromisoformat(row["calnd_dd"])
            name = str(row["holdy_nm"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SchemaDriftError(f"KRX 휴장일 행을 해석할 수 없습니다: {row!r}") from exc
        holidays[day] = name
    return holidays


def _fetch_year(client: httpx.Client, year: int) -> dict[date, str]:
    otp = client.get(OTP_URL, params={"bld": SCREEN_BLD, "name": "form"})
    otp.raise_for_status()
    response = client.post(
        DATA_URL,
        data={
            "code": otp.text,
            "search_bas_yy": str(year),
            "gridTp": "KRX",
            "pagePath": SCREEN_PAGE,
        },
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        snippet = response.text[:80]
        raise SchemaDriftError(f"KRX 휴장일 응답이 JSON이 아닙니다: {snippet!r}") from exc
    return _parse_holidays(payload)


def fetch_holidays(
    year: int,
    *,
    timeout: float = 10.0,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[date, str]:
    last_error: Exception | None = None
    with httpx.Client(headers=HEADERS, timeout=timeout, transport=transport) as client:
        for attempt in range(MAX_ATTEMPTS):
            if attempt:
                sleep(RETRY_WAIT * attempt)
            try:
                return _fetch_year(client, year)
            except SchemaDriftError:
                raise
            except (httpx.HTTPError, SourceError) as exc:
                log.warning("KRX 휴장일 조회 실패 (%d년, %d차): %s", year, attempt + 1, exc)
                last_error = exc
    raise SourceError(f"KRX 휴장일 조회가 계속 실패합니다 ({year}년): {last_error}")
