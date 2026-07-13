import logging
from datetime import date

import polars as pl

from talon.data.store import (
    ADJUST_FACTORS,
    DAILY_CANDLES,
    STOCK_INFO,
    DatePartitionedStore,
    ParquetStore,
)
from talon.quant.universe import TRADABLE_STOCK, tradable_stock

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
    TRADABLE_STOCK,
)


def load_panel(
    snapshots: DatePartitionedStore,
    series: ParquetStore,
    *,
    start: date | None = None,
    end: date | None = None,
    symbols: list[str] | None = None,
    max_info_stale_days: int = 10,
) -> pl.DataFrame:
    daily_scan = snapshots.scan(DAILY_CANDLES)
    if daily_scan is None:
        raise ValueError("일봉 스냅샷이 없습니다 (talon backfill-daily 먼저 실행)")
    factors_dir = series.root / ADJUST_FACTORS
    if not factors_dir.exists() or not any(factors_dir.glob("*.parquet")):
        raise ValueError("수정계수가 없습니다 (talon adjust build 먼저 실행)")
    info_scan = snapshots.scan(STOCK_INFO)
    if info_scan is None:
        raise ValueError("종목기본정보가 없습니다 (talon stock-info backfill 먼저 실행)")

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

    info = info_scan.select(
        pl.col("day").alias("info_day"), "symbol", tradable_stock().alias(TRADABLE_STOCK)
    )

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

    as_of = _info_as_of(panel, snapshots.dates(STOCK_INFO), max_info_stale_days)
    panel = (
        panel.join(as_of, on="day", how="left")
        .join(info.collect(), on=["info_day", "symbol"], how="left")
        .with_columns(pl.col(TRADABLE_STOCK).fill_null(False))
    )
    return panel.select(PANEL_COLUMNS).sort("day", "symbol")


def _info_as_of(
    panel: pl.DataFrame,
    info_days: list[date],
    max_stale_days: int,
) -> pl.DataFrame:
    mapping: list[tuple[date, date]] = []
    for day in sorted(set(panel.get_column("day").unique())):
        known = [info_day for info_day in info_days if info_day <= day]
        if not known:
            raise ValueError(
                f"{day} 이전 종목기본정보가 없습니다 "
                f"(talon stock-info backfill --end {day} 를 먼저 실행하세요)"
            )
        latest = known[-1]
        stale_days = (day - latest).days
        if stale_days > max_stale_days:
            raise ValueError(
                f"{day} 종목기본정보가 {latest} 기준으로 {stale_days}일 낡았습니다 "
                f"(허용 {max_stale_days}일). "
                f"talon stock-info backfill --start {latest} --end {day} 를 먼저 실행하세요"
            )
        mapping.append((day, latest))
    return pl.DataFrame(
        mapping,
        schema={"day": pl.Date(), "info_day": pl.Date()},
        orient="row",
    )


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
