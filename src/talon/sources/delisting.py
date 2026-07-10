import io
import logging
from datetime import date, timedelta

import httpx
import polars as pl

from talon.errors import SchemaDriftError, SourceError

log = logging.getLogger(__name__)

DELISTING_URL_TEMPLATE = (
    "https://raw.githubusercontent.com/FinanceData/fdr_krx_data_cache"
    "/master/data/listing/delisting/{day}.csv"
)

TERMINAL = "terminal"
CORPORATE_ACTION = "corporate_action"
UNKNOWN = "unknown"

DELISTING_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.Utf8(),
    "name": pl.Utf8(),
    "market": pl.Utf8(),
    "secu_group": pl.Utf8(),
    "listing_date": pl.Date(),
    "delisting_date": pl.Date(),
    "reason": pl.Utf8(),
    "arrant_end_date": pl.Date(),
    "to_symbol": pl.Utf8(),
    "classification": pl.Utf8(),
}

_CSV_REQUIRED = {
    "Symbol",
    "Name",
    "Market",
    "SecuGroup",
    "ListingDate",
    "DelistingDate",
    "Reason",
    "ArrantEndDate",
    "ToSymbol",
}

_CORPORATE_ACTION_KEYWORDS = (
    "합병",
    "주식교환",
    "교환계약",
    "전환",
    "자진",
    "신청에 의한",
    "상장폐지 신청",
    "우선주",
    "지주회사",
    "완전자회사",
    "이전상장",
    "존속기간",
)


def classify_delisting(reason: str | None, arrant_end_date: date | None) -> str:
    if arrant_end_date is not None:
        return TERMINAL
    text = reason or ""
    if any(keyword in text for keyword in _CORPORATE_ACTION_KEYWORDS):
        return CORPORATE_ACTION
    return UNKNOWN


def fetch_delisting_registry(
    today: date,
    *,
    lookback_days: int = 7,
    url_template: str = DELISTING_URL_TEMPLATE,
    timeout: float = 30.0,
    transport: httpx.BaseTransport | None = None,
) -> pl.DataFrame:
    last_status = "no snapshot tried"
    with httpx.Client(timeout=timeout, transport=transport, follow_redirects=True) as client:
        for offset in range(lookback_days):
            day = today - timedelta(days=offset)
            url = url_template.format(day=day.isoformat())
            try:
                response = client.get(url)
            except httpx.HTTPError as exc:
                raise SourceError(f"delisting registry fetch failed: {exc}") from exc
            if response.status_code == 200:
                registry = _parse_registry(response.content)
                log.info("delisting registry: %s snapshot, %d rows", day, registry.height)
                return registry
            last_status = f"HTTP {response.status_code} for {day}"
    raise SourceError(f"delisting registry unavailable: {last_status}")


def _parse_registry(content: bytes) -> pl.DataFrame:
    raw = pl.read_csv(io.BytesIO(content), infer_schema_length=0)
    missing = sorted(_CSV_REQUIRED - set(raw.columns))
    if missing:
        raise SchemaDriftError(f"delisting csv columns missing: {missing}")

    def date_col(name: str) -> pl.Expr:
        return pl.col(name).str.to_date("%Y-%m-%d", strict=False)

    frame = raw.select(
        pl.col("Symbol").str.zfill(6).alias("symbol"),
        pl.col("Name").alias("name"),
        pl.col("Market").alias("market"),
        pl.col("SecuGroup").alias("secu_group"),
        date_col("ListingDate").alias("listing_date"),
        date_col("DelistingDate").alias("delisting_date"),
        pl.col("Reason").alias("reason"),
        date_col("ArrantEndDate").alias("arrant_end_date"),
        pl.col("ToSymbol").alias("to_symbol"),
    )
    classification = (
        pl.struct("reason", "arrant_end_date")
        .map_elements(
            lambda row: classify_delisting(row["reason"], row["arrant_end_date"]),
            return_dtype=pl.Utf8,
        )
        .alias("classification")
    )
    return (
        frame.with_columns(classification)
        .filter(pl.col("symbol").is_not_null())
        .sort("delisting_date", nulls_last=True)
    )
