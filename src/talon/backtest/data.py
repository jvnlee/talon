import logging
from datetime import date, time
from typing import cast

import polars as pl

from talon.data.store import (
    ADJUST_FACTORS,
    DAILY_CANDLES,
    MINUTE_CANDLES,
    STOCK_INFO,
    DatePartitionedStore,
    ParquetStore,
)
from talon.markets.kr import krx_calendar
from talon.markets.kr_limits import price_limit_exprs
from talon.quant.universe import TRADABLE_STOCK, tradable_stock

log = logging.getLogger(__name__)

DECISION_TIME = time(15, 10)
_SESSION_OPEN = time(9, 0)
_COVERAGE_LIMIT = time(9, 1)

INTRADAY_STATE_COLUMNS = ("close_1510", "high_1510", "low_1510", "volume_1510")

DERIVED_COLUMNS = (
    "market",
    "raw_high",
    "raw_low",
    "raw_prev_close",
    "overnight_ret",
    "intraday_ret",
    "limit_up_price",
    "limit_down_price",
    "limit_up",
    "limit_down",
    "limit_up_touch",
    "limit_down_touch",
)

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
    *INTRADAY_STATE_COLUMNS,
    "intraday_exact",
    "option_expiry",
    TRADABLE_STOCK,
    *DERIVED_COLUMNS,
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
    daily = daily_scan.select(
        "day", "symbol", "open", "high", "low", "close", "volume", "value", "change_pct"
    )

    factors = (
        pl.scan_parquet(factors_dir / "*.parquet", include_file_paths="path")
        .with_columns(pl.col("path").str.extract(r"([^/]+)\.parquet$").alias("symbol"))
        .select("symbol", "day", "factor")
    )
    if symbols is not None:
        factors = factors.filter(pl.col("symbol").is_in(symbols))

    info = info_scan.select(
        pl.col("day").alias("info_day"),
        "symbol",
        "market",
        tradable_stock().alias(TRADABLE_STOCK),
    )

    joined = daily.join(factors, on=["symbol", "day"], how="inner").collect()
    raw_height = daily.select(pl.len()).collect().item()
    if joined.height < raw_height:
        log.warning("수정계수 미보유로 제외된 행: %d", raw_height - joined.height)

    panel = (
        joined.sort("symbol", "day")
        .with_columns(
            pl.col("close").alias("raw_close"),
            pl.col("high").alias("raw_high"),
            pl.col("low").alias("raw_low"),
            (pl.col("open") * pl.col("factor")).alias("open"),
            (pl.col("high") * pl.col("factor")).alias("high"),
            (pl.col("low") * pl.col("factor")).alias("low"),
            (pl.col("close") * pl.col("factor")).alias("close"),
            (pl.col("volume") / pl.col("factor")).alias("volume"),
        )
        .with_columns(
            pl.col("close").shift(1).over("symbol").alias("prev_close"),
            pl.col("raw_close").shift(1).over("symbol").alias("raw_prev_close"),
            pl.col("factor").shift(1).over("symbol").alias("_prev_factor"),
        )
        .with_columns(
            pl.when(pl.col("prev_close") > 0)
            .then(pl.col("open") / pl.col("prev_close") - 1)
            .alias("overnight_ret"),
            pl.when(pl.col("open") > 0)
            .then(pl.col("close") / pl.col("open") - 1)
            .alias("intraday_ret"),
        )
    )
    if start is not None:
        panel = panel.filter(pl.col("day") >= start)
    if end is not None:
        panel = panel.filter(pl.col("day") <= end)

    panel = _with_intraday_states(panel, series, symbols)
    panel = _with_option_expiry(panel)

    as_of = _info_as_of(panel, snapshots.dates(STOCK_INFO), max_info_stale_days)
    panel = (
        panel.join(as_of, on="day", how="left")
        .join(info.collect(), on=["info_day", "symbol"], how="left")
        .with_columns(pl.col(TRADABLE_STOCK).fill_null(False))
    )
    panel = _with_price_limits(panel)
    return panel.select(PANEL_COLUMNS).sort("day", "symbol")


def _with_price_limits(panel: pl.DataFrame) -> pl.DataFrame:
    upper, lower = price_limit_exprs(pl.col("raw_prev_close"), pl.col("market"), pl.col("day"))
    implied_base = pl.col("raw_close") / (1 + pl.col("change_pct") / 100)
    base_intact = (
        (implied_base - pl.col("raw_prev_close")).abs() / pl.col("raw_prev_close") <= 0.005
    ).fill_null(True)
    known = (
        pl.col("raw_prev_close").is_not_null()
        & pl.col("market").is_not_null()
        & (pl.col("factor") == pl.col("_prev_factor"))
        & base_intact
    )
    return panel.with_columns(
        pl.when(known).then(upper).alias("limit_up_price"),
        pl.when(known).then(lower).alias("limit_down_price"),
    ).with_columns(
        (pl.col("raw_close") == pl.col("limit_up_price")).alias("limit_up"),
        (pl.col("raw_close") == pl.col("limit_down_price")).alias("limit_down"),
        (pl.col("raw_high") >= pl.col("limit_up_price")).alias("limit_up_touch"),
        (pl.col("raw_low") <= pl.col("limit_down_price")).alias("limit_down_touch"),
    )


def _minute_1510_states(
    series: ParquetStore,
    symbols: list[str] | None,
    days: pl.Series,
) -> pl.DataFrame | None:
    minutes_dir = series.root / MINUTE_CANDLES
    if not minutes_dir.exists() or not any(minutes_dir.glob("*.parquet")):
        return None
    scan = pl.scan_parquet(minutes_dir / "*.parquet", include_file_paths="path").with_columns(
        pl.col("path").str.extract(r"([^/]+)\.parquet$").alias("symbol")
    )
    if symbols is not None:
        scan = scan.filter(pl.col("symbol").is_in(symbols))
    kst = pl.col("ts").dt.convert_time_zone("Asia/Seoul")
    scan = scan.with_columns(kst.dt.date().alias("day"), kst.dt.time().alias("bucket")).filter(
        pl.col("day").is_between(days.min(), days.max())
    )
    in_window = (pl.col("bucket") > _SESSION_OPEN) & (pl.col("bucket") <= DECISION_TIME)
    traded = in_window & (pl.col("volume") > 0)
    traded_bucket = pl.col("bucket").filter(traded)
    states = (
        scan.group_by("symbol", "day")
        .agg(
            pl.col("bucket").min().alias("first_bucket"),
            pl.col("close").filter(traded).sort_by(traded_bucket).last().alias("close_1510"),
            pl.col("high").filter(traded).max().alias("high_1510"),
            pl.col("low").filter(traded).min().alias("low_1510"),
            pl.col("volume").filter(in_window).sum().alias("volume_1510"),
        )
        .filter((pl.col("first_bucket") <= _COVERAGE_LIMIT) & pl.col("close_1510").is_not_null())
        .drop("first_bucket")
        .collect()
    )
    return states if states.height else None


def _with_intraday_states(
    panel: pl.DataFrame,
    series: ParquetStore,
    symbols: list[str] | None,
) -> pl.DataFrame:
    states = None if panel.is_empty() else _minute_1510_states(series, symbols, panel["day"])
    if states is None:
        return panel.with_columns(
            pl.col("close").alias("close_1510"),
            pl.col("high").alias("high_1510"),
            pl.col("low").alias("low_1510"),
            pl.col("volume").alias("volume_1510"),
            pl.lit(False).alias("intraday_exact"),
        )
    return (
        panel.join(states, on=["symbol", "day"], how="left")
        .with_columns(pl.col("close_1510").is_not_null().alias("intraday_exact"))
        .with_columns(
            (pl.col("close_1510") * pl.col("factor"))
            .fill_null(pl.col("close"))
            .alias("close_1510"),
            (pl.col("high_1510") * pl.col("factor")).fill_null(pl.col("high")).alias("high_1510"),
            (pl.col("low_1510") * pl.col("factor")).fill_null(pl.col("low")).alias("low_1510"),
            (pl.col("volume_1510") / pl.col("factor"))
            .fill_null(pl.col("volume"))
            .alias("volume_1510"),
        )
    )


def _with_option_expiry(panel: pl.DataFrame) -> pl.DataFrame:
    if panel.is_empty():
        return panel.with_columns(pl.lit(False).alias("option_expiry"))
    first = cast(date, panel["day"].min())
    last = cast(date, panel["day"].max())
    expiry = krx_calendar().option_expiry_days(first, last)
    flag = pl.col("day").is_in(sorted(expiry)) if expiry else pl.lit(False)
    return panel.with_columns(flag.alias("option_expiry"))


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
