import time
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

import polars as pl

from talon.data.store import (
    SHORTING_BALANCE_SCHEMA,
    SHORTING_INVESTOR_SCHEMA,
    SHORTING_SCHEMA,
)
from talon.errors import SchemaDriftError
from talon.sources.krx_daily import KrxCredentials, _load_pykrx, _retry
from talon.timeutil import now_utc

SHORTING_MARKETS: tuple[str, ...] = ("KOSPI", "KOSDAQ")

_TRADE_VOLUME_COLUMNS = {
    "공매도": "short_volume",
    "매수": "total_volume_consolidated",
    "비중": "short_ratio_pct",
}
_TRADE_VALUE_COLUMNS = {
    "공매도": "short_value",
    "매수": "total_value_consolidated",
    "비중": "short_value_ratio_pct",
}
_TRADE_REQUIRED = {"공매도", "매수", "비중"}

_BALANCE_REQUIRED = {"공매도잔고", "상장주식수", "공매도금액", "시가총액"}

INVESTOR_LABELS = {
    "기관": "institution",
    "개인": "retail",
    "외국인": "foreign",
    "기타": "other",
    "합계": "total",
}
_INVESTOR_REQUIRED = set(INVESTOR_LABELS)


def _require(pdf: Any, required: set[str]) -> None:
    missing = sorted(col for col in required if col not in pdf.columns)
    if missing:
        raise SchemaDriftError(f"pykrx shorting columns missing: {missing}")


def _reset_symbols(pdf: Any) -> list[str]:
    reset = pdf.reset_index()
    return reset[reset.columns[0]].astype(str).tolist()


def _reset_dates(pdf: Any) -> list[date]:
    reset = pdf.reset_index()
    return [date.fromisoformat(str(value)[:10]) for value in reset[reset.columns[0]].tolist()]


def _i(value: Any) -> int:
    if value is None or value != value:
        return 0
    return int(value)


def _f(value: Any) -> float:
    if value is None or value != value:
        return 0.0
    return float(value)


def _trade_market_frame(
    vol_pdf: Any, val_pdf: Any, day: date, market: str, fetched_at: datetime
) -> pl.DataFrame:
    if vol_pdf is None or len(vol_pdf) == 0 or val_pdf is None or len(val_pdf) == 0:
        return pl.DataFrame(schema=SHORTING_SCHEMA)
    _require(vol_pdf, _TRADE_REQUIRED)
    _require(val_pdf, _TRADE_REQUIRED)
    symbols = _reset_symbols(vol_pdf)
    short_volume = vol_pdf["공매도"].tolist()
    total_volume = vol_pdf["매수"].tolist()
    ratio = vol_pdf["비중"].tolist()
    value_symbols = _reset_symbols(val_pdf)
    value_index = {symbol: position for position, symbol in enumerate(value_symbols)}
    short_value_col = val_pdf["공매도"].tolist()
    total_value_col = val_pdf["매수"].tolist()
    value_ratio_col = val_pdf["비중"].tolist()
    short_value: list[int] = []
    total_value: list[int] = []
    value_ratio: list[float] = []
    for symbol in symbols:
        position = value_index.get(symbol)
        if position is None:
            short_value.append(0)
            total_value.append(0)
            value_ratio.append(0.0)
        else:
            short_value.append(_i(short_value_col[position]))
            total_value.append(_i(total_value_col[position]))
            value_ratio.append(_f(value_ratio_col[position]))
    data: dict[str, Any] = {
        "day": [day] * len(symbols),
        "symbol": symbols,
        "market": [market] * len(symbols),
        "short_volume": [_i(v) for v in short_volume],
        "total_volume_consolidated": [_i(v) for v in total_volume],
        "short_ratio_pct": [_f(v) for v in ratio],
        "short_value": short_value,
        "total_value_consolidated": total_value,
        "short_value_ratio_pct": value_ratio,
        "fetched_at": [fetched_at] * len(symbols),
    }
    return pl.DataFrame(data, schema=SHORTING_SCHEMA)


def fetch_shorting(
    day: date,
    *,
    markets: tuple[str, ...] = SHORTING_MARKETS,
    credentials: KrxCredentials | None = None,
    pause: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> pl.DataFrame:
    stock = _load_pykrx(credentials)
    text = day.strftime("%Y%m%d")
    fetched_at = now_utc()
    frames: list[pl.DataFrame] = []
    for market in markets:

        def volume_call(market: str = market) -> Any:
            return stock.get_shorting_volume_by_ticker(text, market)

        def value_call(market: str = market) -> Any:
            return stock.get_shorting_value_by_ticker(text, market)

        vol_pdf = _retry(volume_call, sleep=sleep)
        sleep(pause)
        val_pdf = _retry(value_call, sleep=sleep)
        sleep(pause)
        frames.append(_trade_market_frame(vol_pdf, val_pdf, day, market, fetched_at))
    frame = pl.concat(frames, how="vertical") if frames else pl.DataFrame(schema=SHORTING_SCHEMA)
    return frame.sort("market", "symbol")


def _balance_market_frame(pdf: Any, day: date, market: str, fetched_at: datetime) -> pl.DataFrame:
    if pdf is None or len(pdf) == 0:
        return pl.DataFrame(schema=SHORTING_BALANCE_SCHEMA)
    _require(pdf, _BALANCE_REQUIRED)
    symbols = _reset_symbols(pdf)
    qty = [_i(v) for v in pdf["공매도잔고"].tolist()]
    listed = [_i(v) for v in pdf["상장주식수"].tolist()]
    value = [_i(v) for v in pdf["공매도금액"].tolist()]
    cap = [_i(v) for v in pdf["시가총액"].tolist()]
    ratio = [
        (q / shares * 100.0) if shares > 0 else 0.0
        for q, shares in zip(qty, listed, strict=True)
    ]
    data: dict[str, Any] = {
        "day": [day] * len(symbols),
        "symbol": symbols,
        "market": [market] * len(symbols),
        "short_balance_qty": qty,
        "listed_shares": listed,
        "short_balance_value": value,
        "market_cap": cap,
        "short_balance_ratio_pct": ratio,
        "fetched_at": [fetched_at] * len(symbols),
    }
    return pl.DataFrame(data, schema=SHORTING_BALANCE_SCHEMA)


def fetch_shorting_balance(
    day: date,
    *,
    markets: tuple[str, ...] = SHORTING_MARKETS,
    credentials: KrxCredentials | None = None,
    pause: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> pl.DataFrame:
    stock = _load_pykrx(credentials)
    text = day.strftime("%Y%m%d")
    fetched_at = now_utc()
    frames: list[pl.DataFrame] = []
    for market in markets:

        def balance_call(market: str = market) -> Any:
            return stock.get_shorting_balance_by_ticker(text, market)

        pdf = _retry(balance_call, sleep=sleep)
        sleep(pause)
        frames.append(_balance_market_frame(pdf, day, market, fetched_at))
    if not frames:
        return pl.DataFrame(schema=SHORTING_BALANCE_SCHEMA)
    frame = pl.concat(frames, how="vertical")
    return frame.sort("market", "symbol")


def _investor_market_frame(
    vol_pdf: Any, val_pdf: Any, market: str, fetched_at: datetime
) -> pl.DataFrame:
    if vol_pdf is None or len(vol_pdf) == 0 or val_pdf is None or len(val_pdf) == 0:
        return pl.DataFrame(schema=SHORTING_INVESTOR_SCHEMA)
    _require(vol_pdf, _INVESTOR_REQUIRED)
    _require(val_pdf, _INVESTOR_REQUIRED)
    vol_dates = _reset_dates(vol_pdf)
    vol_columns = {label: vol_pdf[korean].tolist() for korean, label in INVESTOR_LABELS.items()}
    val_dates = _reset_dates(val_pdf)
    val_columns = {label: val_pdf[korean].tolist() for korean, label in INVESTOR_LABELS.items()}
    value_lookup: dict[tuple[date, str], int] = {}
    for position, day in enumerate(val_dates):
        for label in INVESTOR_LABELS.values():
            value_lookup[(day, label)] = _i(val_columns[label][position])
    rows: list[dict[str, Any]] = []
    for position, day in enumerate(vol_dates):
        for label in INVESTOR_LABELS.values():
            rows.append(
                {
                    "day": day,
                    "market": market,
                    "investor": label,
                    "vol_shares": _i(vol_columns[label][position]),
                    "value_krw": value_lookup.get((day, label), 0),
                    "fetched_at": fetched_at,
                }
            )
    return pl.DataFrame(rows, schema=SHORTING_INVESTOR_SCHEMA)


def fetch_shorting_investor(
    start: date,
    end: date,
    *,
    markets: tuple[str, ...] = SHORTING_MARKETS,
    credentials: KrxCredentials | None = None,
    pause: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> pl.DataFrame:
    stock = _load_pykrx(credentials)
    fromdate = start.strftime("%Y%m%d")
    todate = end.strftime("%Y%m%d")
    fetched_at = now_utc()
    frames: list[pl.DataFrame] = []
    for market in markets:

        def volume_call(market: str = market) -> Any:
            return stock.get_shorting_investor_volume_by_date(fromdate, todate, market)

        def value_call(market: str = market) -> Any:
            return stock.get_shorting_investor_value_by_date(fromdate, todate, market)

        vol_pdf = _retry(volume_call, sleep=sleep)
        sleep(pause)
        val_pdf = _retry(value_call, sleep=sleep)
        sleep(pause)
        frames.append(_investor_market_frame(vol_pdf, val_pdf, market, fetched_at))
    if not frames:
        return pl.DataFrame(schema=SHORTING_INVESTOR_SCHEMA)
    frame = pl.concat(frames, how="vertical")
    return frame.sort("day", "market", "investor")


def market_short_volume(frame: pl.DataFrame) -> int:
    if frame.is_empty():
        return 0
    return int(frame.get_column("short_volume").sum() or 0)
