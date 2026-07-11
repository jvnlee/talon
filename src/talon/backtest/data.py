import logging
from datetime import date

import polars as pl

from talon.data.store import ADJUST_FACTORS, DAILY_CANDLES, DatePartitionedStore, ParquetStore

log = logging.getLogger(__name__)

PANEL_COLUMNS = (
    "day",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "value",
    "raw_close",
    "factor",
    "prev_close",
)


def load_panel(
    snapshots: DatePartitionedStore,
    series: ParquetStore,
    *,
    start: date | None = None,
    end: date | None = None,
    symbols: list[str] | None = None,
) -> pl.DataFrame:
    daily_scan = snapshots.scan(DAILY_CANDLES)
    if daily_scan is None:
        raise ValueError("일봉 스냅샷이 없습니다 (talon backfill-daily 먼저 실행)")
    factors_dir = series.root / ADJUST_FACTORS
    if not factors_dir.exists() or not any(factors_dir.glob("*.parquet")):
        raise ValueError("수정계수가 없습니다 (talon adjust build 먼저 실행)")

    if symbols is not None:
        daily_scan = daily_scan.filter(pl.col("symbol").is_in(symbols))
    daily = daily_scan.select("day", "symbol", "open", "high", "low", "close", "volume", "value")

    factors = (
        pl.scan_parquet(factors_dir / "*.parquet", include_file_paths="path")
        .with_columns(pl.col("path").str.extract(r"([^/]+)\.parquet$").alias("symbol"))
        .select("symbol", "day", "factor")
    )
    if symbols is not None:
        factors = factors.filter(pl.col("symbol").is_in(symbols))

    joined = daily.join(factors, on=["symbol", "day"], how="inner").collect()
    raw_height = daily.select(pl.len()).collect().item()
    if joined.height < raw_height:
        log.warning("수정계수 미보유로 제외된 행: %d", raw_height - joined.height)

    panel = (
        joined.sort("symbol", "day")
        .with_columns(
            pl.col("close").alias("raw_close"),
            (pl.col("open") * pl.col("factor")).alias("open"),
            (pl.col("high") * pl.col("factor")).alias("high"),
            (pl.col("low") * pl.col("factor")).alias("low"),
            (pl.col("close") * pl.col("factor")).alias("close"),
            (pl.col("volume") / pl.col("factor")).alias("volume"),
        )
        .with_columns(pl.col("close").shift(1).over("symbol").alias("prev_close"))
    )
    if start is not None:
        panel = panel.filter(pl.col("day") >= start)
    if end is not None:
        panel = panel.filter(pl.col("day") <= end)
    return panel.select(PANEL_COLUMNS).sort("day", "symbol")


class MarketView:
    def __init__(self, panel: pl.DataFrame, day: date) -> None:
        self._panel = panel
        self.day = day

    def history(self, symbol: str, days: int | None = None) -> pl.DataFrame:
        frame = self._panel.filter((pl.col("symbol") == symbol) & (pl.col("day") <= self.day)).sort(
            "day"
        )
        if days is not None:
            return frame.tail(days)
        return frame

    def cross_section(self) -> pl.DataFrame:
        return self._panel.filter(pl.col("day") == self.day)
