import logging
import os
from datetime import date
from pathlib import Path

import httpx
import polars as pl

from talon.data.store import normalize_daily_snapshot
from talon.errors import SchemaDriftError, SourceError

log = logging.getLogger(__name__)

MARCAP_URL_TEMPLATE = (
    "https://raw.githubusercontent.com/FinanceData/marcap/master/data/marcap-{year}.parquet"
)
MIN_ROWS_PER_DAY = 500
CAP_IDENTITY_TOLERANCE = 1e-6
CAP_MISMATCH_WARN_RATIO = 0.01

_REQUIRED_COLUMNS = {
    "Code",
    "Date",
    "Open",
    "High",
    "Low",
    "Close",
    "Volume",
    "Amount",
    "Marcap",
    "Stocks",
}


class MarcapSource:
    def __init__(
        self,
        cache_dir: Path,
        *,
        url_template: str = MARCAP_URL_TEMPLATE,
        timeout: float = 120.0,
        transport: httpx.BaseTransport | None = None,
        min_rows: int = MIN_ROWS_PER_DAY,
    ) -> None:
        self._cache_dir = cache_dir
        self._url_template = url_template
        self._min_rows = min_rows
        self._http = httpx.Client(timeout=timeout, transport=transport, follow_redirects=True)
        self._years: dict[int, pl.DataFrame] = {}
        self._refreshed: set[int] = set()

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "MarcapSource":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def year_path(self, year: int) -> Path:
        return self._cache_dir / f"marcap-{year}.parquet"

    def _download_year(self, year: int) -> None:
        url = self._url_template.format(year=year)
        try:
            response = self._http.get(url)
        except httpx.HTTPError as exc:
            raise SourceError(f"marcap download failed for {year}: {exc}") from exc
        if response.status_code != 200:
            raise SourceError(f"marcap download failed for {year}: HTTP {response.status_code}")
        path = self.year_path(year)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(response.content)
        os.replace(tmp, path)

    def _load_year(self, year: int) -> pl.DataFrame:
        cached = self._years.get(year)
        if cached is not None:
            return cached
        path = self.year_path(year)
        if not path.exists():
            self._download_year(year)
            self._refreshed.add(year)
        try:
            frame = pl.read_parquet(path)
        except Exception as exc:
            path.unlink(missing_ok=True)
            raise SourceError(f"marcap year file unreadable for {year}: {exc}") from exc
        missing = sorted(_REQUIRED_COLUMNS - set(frame.columns))
        if missing:
            raise SchemaDriftError(f"marcap columns missing for {year}: {missing}")
        frame = frame.with_columns(pl.col("Date").cast(pl.Date))
        self._years = {year: frame}
        return frame

    def _refresh_year(self, year: int) -> pl.DataFrame:
        self._refreshed.add(year)
        self._download_year(year)
        self._years.pop(year, None)
        return self._load_year(year)

    def latest_available(self, year: int) -> date | None:
        try:
            frame = self._refresh_year(year)
        except SourceError:
            frame = self._refresh_year(year - 1)
        return frame.select(pl.col("Date").max()).item()

    def snapshot(self, day: date) -> tuple[pl.DataFrame, pl.DataFrame]:
        frame = self._load_year(day.year)
        rows = frame.filter(pl.col("Date") == day)
        if rows.is_empty():
            max_date = frame.select(pl.col("Date").max()).item()
            if (max_date is None or day > max_date) and day.year not in self._refreshed:
                frame = self._refresh_year(day.year)
                rows = frame.filter(pl.col("Date") == day)
            if rows.is_empty():
                if max_date is None or day > max_date:
                    raise SourceError(
                        f"marcap {day.year} not yet published for {day} (latest {max_date})"
                    )
                raise SourceError(f"marcap has no rows for trading day {day}")
        if rows.height < self._min_rows:
            raise SourceError(f"marcap rows for {day} suspiciously low: {rows.height}")
        change_pct = (
            pl.col("ChangesRatio").cast(pl.Float64)
            if "ChangesRatio" in rows.columns
            else pl.lit(None, dtype=pl.Float64)
        )
        daily = normalize_daily_snapshot(
            rows.select(
                pl.lit(day).alias("day"),
                pl.col("Code").cast(pl.Utf8).alias("symbol"),
                pl.col("Open").cast(pl.Float64).alias("open"),
                pl.col("High").cast(pl.Float64).alias("high"),
                pl.col("Low").cast(pl.Float64).alias("low"),
                pl.col("Close").cast(pl.Float64).alias("close"),
                pl.col("Volume").cast(pl.Float64).alias("volume"),
                pl.col("Amount").cast(pl.Float64).alias("value"),
                change_pct.alias("change_pct"),
            )
        )
        caps = rows.select(
            pl.lit(day).alias("day"),
            pl.col("Code").cast(pl.Utf8).alias("symbol"),
            pl.col("Close").cast(pl.Float64).alias("close"),
            pl.col("Marcap").cast(pl.Float64).alias("cap"),
            pl.col("Volume").cast(pl.Float64).alias("volume"),
            pl.col("Amount").cast(pl.Float64).alias("value"),
            pl.col("Stocks").cast(pl.Float64).alias("shares"),
        ).filter(pl.col("cap") > 0)
        self._warn_on_cap_identity_break(day, caps)
        return daily, caps

    def _warn_on_cap_identity_break(self, day: date, caps: pl.DataFrame) -> None:
        if caps.is_empty():
            return
        mismatched = caps.filter(
            (pl.col("shares") > 0)
            & (
                (pl.col("cap") - pl.col("close") * pl.col("shares")).abs()
                > pl.col("cap") * CAP_IDENTITY_TOLERANCE
            )
        ).height
        ratio = mismatched / caps.height
        if ratio > CAP_MISMATCH_WARN_RATIO:
            log.warning(
                "marcap cap identity mismatch on %s: %d/%d rows (%.2f%%)",
                day,
                mismatched,
                caps.height,
                ratio * 100,
            )
