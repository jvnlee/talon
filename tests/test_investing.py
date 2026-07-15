import httpx
import pytest

from talon.errors import SourceError
from talon.sources.investing import fetch_vkospi, parse_vkospi

GOOD_HTML = """
<h1>KOSPI Volatility (KSVKOSPI)</h1>
<span data-test="instrument-price-last">32.15</span>
<span data-test="instrument-price-change">+1.25</span>
"""


def test_parses_price_and_prev_close():
    quote = parse_vkospi(GOOD_HTML)

    assert quote.price == 32.15
    assert quote.prev_close == 30.9


def test_negative_change_raises_prev_close():
    html = GOOD_HTML.replace("+1.25", "-2.15")

    quote = parse_vkospi(html)

    assert quote.prev_close == 34.3


def test_missing_identity_is_rejected():
    html = GOOD_HTML.replace("KSVKOSPI", "SOMETHING")

    with pytest.raises(SourceError, match="정체성"):
        parse_vkospi(html)


def test_out_of_range_value_is_rejected():
    html = GOOD_HTML.replace("32.15", "412.00")

    with pytest.raises(SourceError, match="정상 범위"):
        parse_vkospi(html)


def test_markup_drift_is_rejected():
    html = GOOD_HTML.replace('data-test="instrument-price-last"', 'class="price"')

    with pytest.raises(SourceError, match="파싱 실패"):
        parse_vkospi(html)


def test_fetch_uses_http_and_parses(monkeypatch):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=GOOD_HTML))

    quote = fetch_vkospi(transport=transport)

    assert quote.price == 32.15


def test_fetch_wraps_http_errors():
    transport = httpx.MockTransport(lambda request: httpx.Response(403, text="blocked"))

    with pytest.raises(SourceError, match="요청 실패"):
        fetch_vkospi(transport=transport)
