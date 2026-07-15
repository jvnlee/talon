import logging
import math
import time
from collections.abc import Callable
from typing import Any, NamedTuple

import polars as pl

from talon.data.store import CANDLE_SCHEMA, US_DAILY_SCHEMA
from talon.errors import SourceError

log = logging.getLogger(__name__)


class YahooQuote(NamedTuple):
    price: float
    prev_close: float | None


def _load_yfinance() -> Any:
    try:
        import yfinance
    except ImportError as exc:
        raise SourceError("yfinance가 설치되어 있지 않습니다 (uv sync 필요)") from exc
    return yfinance


def _retry(
    func: Callable[[], Any],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return func()
        except Exception as exc:
            last_error = exc
            if attempt < attempts - 1:
                sleep(base_delay * (attempt + 1))
    raise SourceError(f"yahoo request failed: {last_error}") from last_error


def _clean(value: Any) -> float | None:
    if value is None:
        return None
    number = float(value)
    if math.isnan(number):
        return None
    return number


def fetch_quote(symbol: str, *, sleep: Callable[[float], None] = time.sleep) -> YahooQuote:
    yf = _load_yfinance()

    def pull() -> YahooQuote:
        ticker = yf.Ticker(symbol)
        price = None
        prev_close = None
        try:
            info = ticker.fast_info
            price = _clean(getattr(info, "last_price", None))
            prev_close = _clean(getattr(info, "previous_close", None))
        except Exception:
            log.debug("fast_info unavailable for %s", symbol)
        if price is None:
            bars = ticker.history(period="1d", interval="1m", prepost=True)
            if bars is not None and len(bars) > 0:
                price = _clean(bars["Close"].iloc[-1])
        if price is None:
            raise SourceError(f"{symbol} 시세가 비어 있습니다")
        return YahooQuote(price, prev_close)

    result: YahooQuote = _retry(pull, sleep=sleep)
    return result


def fetch_daily_bars(
    symbol: str,
    *,
    period: str = "10d",
    sleep: Callable[[float], None] = time.sleep,
) -> pl.DataFrame:
    yf = _load_yfinance()
    pdf = _retry(
        lambda: yf.Ticker(symbol).history(period=period, interval="1d", auto_adjust=False),
        sleep=sleep,
    )
    if pdf is None or len(pdf) == 0:
        return pl.DataFrame(schema=US_DAILY_SCHEMA)
    rows = []
    for ts, row in pdf.iterrows():
        close = _clean(row.get("Close"))
        if close is None:
            continue
        rows.append(
            {
                "day": ts.date(),
                "open": _clean(row.get("Open")),
                "high": _clean(row.get("High")),
                "low": _clean(row.get("Low")),
                "close": close,
                "volume": _clean(row.get("Volume")),
            }
        )
    return pl.DataFrame(rows, schema=US_DAILY_SCHEMA)


def fetch_minute_bars(
    symbol: str,
    *,
    period: str = "5d",
    prepost: bool = True,
    sleep: Callable[[float], None] = time.sleep,
) -> pl.DataFrame:
    yf = _load_yfinance()
    pdf = _retry(
        lambda: yf.Ticker(symbol).history(
            period=period, interval="1m", prepost=prepost, auto_adjust=False
        ),
        sleep=sleep,
    )
    if pdf is None or len(pdf) == 0:
        return pl.DataFrame(schema=CANDLE_SCHEMA)
    rows = []
    for ts, row in pdf.iterrows():
        close = _clean(row.get("Close"))
        if close is None:
            continue
        rows.append(
            {
                "ts": ts.to_pydatetime(),
                "open": _clean(row.get("Open")),
                "high": _clean(row.get("High")),
                "low": _clean(row.get("Low")),
                "close": close,
                "volume": _clean(row.get("Volume")),
            }
        )
    if not rows:
        return pl.DataFrame(schema=CANDLE_SCHEMA)
    frame = pl.DataFrame(rows, schema_overrides={"ts": pl.Datetime("us", "UTC")})
    return frame.select(
        pl.col(column).cast(dtype) for column, dtype in CANDLE_SCHEMA.items()
    )
