import logging
import re
from typing import NamedTuple

import httpx

from talon.errors import SourceError

log = logging.getLogger(__name__)

VKOSPI_URL = "https://www.investing.com/indices/kospi-volatility"
VKOSPI_IDENTITY = "KSVKOSPI"
VKOSPI_SANE_RANGE = (5.0, 120.0)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
_PRICE_RE = re.compile(r'data-test="instrument-price-last">([0-9.,]+)<')
_CHANGE_RE = re.compile(r'data-test="instrument-price-change">([+\-0-9.,]+)<')


class VkospiQuote(NamedTuple):
    price: float
    prev_close: float | None


def parse_vkospi(html: str) -> VkospiQuote:
    if VKOSPI_IDENTITY not in html:
        raise SourceError("investing.com 응답에서 KSVKOSPI 정체성 확인 실패")
    price_match = _PRICE_RE.search(html)
    if price_match is None:
        raise SourceError("investing.com VKOSPI 가격 파싱 실패 (마크업 변경 의심)")
    price = float(price_match.group(1).replace(",", ""))
    low, high = VKOSPI_SANE_RANGE
    if not low <= price <= high:
        raise SourceError(f"VKOSPI 값 {price}가 정상 범위({low}~{high}) 밖입니다")
    change_match = _CHANGE_RE.search(html)
    prev_close = None
    if change_match is not None:
        change = float(change_match.group(1).replace(",", "").replace("+", ""))
        prev_close = round(price - change, 4)
    return VkospiQuote(price, prev_close)


def fetch_vkospi(
    *,
    url: str = VKOSPI_URL,
    timeout: float = 30.0,
    transport: httpx.BaseTransport | None = None,
) -> VkospiQuote:
    try:
        with httpx.Client(
            timeout=timeout, transport=transport, follow_redirects=True, headers=_HEADERS
        ) as client:
            response = client.get(url)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise SourceError(f"investing.com VKOSPI 요청 실패: {exc}") from exc
    return parse_vkospi(response.text)
