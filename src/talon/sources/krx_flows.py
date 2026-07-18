import time
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

import polars as pl

from talon.data.store import INVESTOR_FLOWS_SCHEMA
from talon.errors import SchemaDriftError, SourceError
from talon.sources.krx_daily import KrxCredentials, _load_pykrx, _retry
from talon.timeutil import now_utc

INVESTOR_LABELS: dict[str, str] = {
    "금융투자": "financial_investment",
    "보험": "insurance",
    "투신": "investment_trust",
    "사모": "private_fund",
    "은행": "bank",
    "기타금융": "other_finance",
    "연기금": "pension",
    "기타법인": "other_corporation",
    "개인": "individual",
    "외국인": "foreigner",
    "기타외국인": "other_foreigner",
}

REQUIRED_INVESTORS = ("individual", "foreigner")

_FLOW_COLUMNS = {
    "매도거래량": "sell_volume",
    "매수거래량": "buy_volume",
    "순매수거래량": "net_volume",
    "매도거래대금": "sell_value",
    "매수거래대금": "buy_value",
    "순매수거래대금": "net_value",
}


def _flows_frame(pdf: Any, day: date, investor: str, fetched_at: datetime) -> pl.DataFrame:
    if pdf is None or len(pdf) == 0:
        return pl.DataFrame(schema=INVESTOR_FLOWS_SCHEMA)
    missing = sorted(col for col in _FLOW_COLUMNS if col not in pdf.columns)
    if missing:
        raise SchemaDriftError(f"pykrx investor columns missing: {missing}")
    reset = pdf.reset_index()
    symbols = reset[reset.columns[0]].astype(str).tolist()
    data: dict[str, Any] = {
        "day": [day] * len(symbols),
        "symbol": symbols,
        "investor": [investor] * len(symbols),
        "fetched_at": [fetched_at] * len(symbols),
    }
    for source_col, target_col in _FLOW_COLUMNS.items():
        data[target_col] = [float(v) for v in pdf[source_col].tolist()]
    return pl.DataFrame(data, schema=INVESTOR_FLOWS_SCHEMA)


def fetch_investor_flows(
    day: date,
    *,
    credentials: KrxCredentials | None = None,
    pause: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> pl.DataFrame:
    stock = _load_pykrx(credentials)
    text = day.strftime("%Y%m%d")
    fetched_at = now_utc()
    frames: list[pl.DataFrame] = []
    for korean, investor in INVESTOR_LABELS.items():

        def call(korean: str = korean) -> Any:
            return stock.get_market_net_purchases_of_equities_by_ticker(text, text, "ALL", korean)

        pdf = _retry(call, sleep=sleep)
        frames.append(_flows_frame(pdf, day, investor, fetched_at))
        sleep(pause)
    frame = pl.concat(frames, how="vertical")
    present = set(frame.get_column("investor").unique().to_list())
    absent = [investor for investor in REQUIRED_INVESTORS if investor not in present]
    if absent:
        raise SourceError(f"{day} 투자자별 수급 응답이 비었습니다: {absent}")
    return frame.sort("investor", "symbol")


def clearing_residual_pct(frame: pl.DataFrame) -> float:
    total = float(frame.get_column("buy_value").sum() or 0.0)
    if not total:
        return 0.0
    residual = float(frame.get_column("net_value").sum() or 0.0)
    return abs(residual) / total * 100
