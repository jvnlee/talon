import os
from datetime import date, datetime
from pathlib import Path

import polars as pl

from talon.models import Candle, InvestorFlowRecord

MINUTE_CANDLES = "candles_1m"
DAILY_CANDLES = "candles_1d"
INTRADAY_SNAPSHOT = "intraday_snapshot"
INDEX_INTRADAY = "index_intraday"
MACRO_INTRADAY = "macro_intraday"
BREADTH_INTRADAY = "breadth_intraday"
DART_POLL = "dart_poll"
ORDERBOOK_INTRADAY = "orderbook_intraday"
CLOSE_AUCTION_INTRADAY = "close_auction_intraday"
INVESTOR_ESTIMATE_INTRADAY = "investor_estimate_intraday"
FLOW_RANKING_INTRADAY = "flow_ranking_intraday"
FRGNMEM_RANKING_INTRADAY = "frgnmem_ranking_intraday"
VOLUME_POWER_INTRADAY = "volume_power_intraday"
MEMBER_INTRADAY = "member_intraday"
PROGRAM_TRADE_INTRADAY = "program_trade_intraday"
PROGRAM_MARKET_INTRADAY = "program_market_intraday"
FRGNMEM_TREND_INTRADAY = "frgnmem_trend_intraday"
OVERTIME_PRICE = "overtime_price"
OVERTIME_RANKING = "overtime_ranking"
OVERTIME_MARKET = "overtime_market"
US_DAILY = "us_1d"
US_MINUTE = "us_1m"
US_MACRO_DAILY = "us_macro_1d"
US_EVENTS = "us_events"
US_EVENTS_HISTORY = "us_events_history"
US_EVENTS_HISTORY_NAME = "all"
US_EARNINGS = "us_earnings"
US_KR_MAP = "us_kr_map"
US_KR_MAP_NAME = "map"
MARKET_CAP = "marketcap"
INDICATOR_MINUTE = "indicators_1m"
INDICATOR_DAILY = "indicators_1d"
INDEX_DAILY = "index_1d"
INVESTOR_TRADING = "investor_trading"
INVESTOR_FLOWS = "investor_flows_1d"
VKOSPI_1D = "vkospi_1d"
SHORTING = "shorting_1d"
SHORTING_BALANCE = "shorting_balance_1d"
SHORTING_INVESTOR = "shorting_investor_1d"
KIS_MINUTES = "kis_minutes_1m"
STOCK_INFO = "stock_info"
DELISTING = "delisting"
DART_FILINGS = "dart_filings"
ADJUST_FACTORS = "adjust_factors"
ADJUST_MANIFEST = "adjust_manifest"
ADJUST_MANIFEST_NAME = "coverage"

CANDLE_SCHEMA: dict[str, pl.DataType] = {
    "ts": pl.Datetime("us", "UTC"),
    "open": pl.Float64(),
    "high": pl.Float64(),
    "low": pl.Float64(),
    "close": pl.Float64(),
    "volume": pl.Float64(),
}

DAILY_SNAPSHOT_SCHEMA: dict[str, pl.DataType] = {
    "day": pl.Date(),
    "symbol": pl.Utf8(),
    "open": pl.Float64(),
    "high": pl.Float64(),
    "low": pl.Float64(),
    "close": pl.Float64(),
    "volume": pl.Float64(),
    "value": pl.Float64(),
    "change_pct": pl.Float64(),
}

INTRADAY_SNAPSHOT_SCHEMA: dict[str, pl.DataType] = {
    "day": pl.Date(),
    "slot": pl.Utf8(),
    "symbol": pl.Utf8(),
    "captured_at": pl.Datetime("us", "UTC"),
    "open": pl.Float64(),
    "high": pl.Float64(),
    "low": pl.Float64(),
    "close": pl.Float64(),
    "volume": pl.Float64(),
    "value": pl.Float64(),
    "change_pct": pl.Float64(),
}

INDEX_INTRADAY_SCHEMA: dict[str, pl.DataType] = {
    "day": pl.Date(),
    "slot": pl.Utf8(),
    "market": pl.Utf8(),
    "name": pl.Utf8(),
    "captured_at": pl.Datetime("us", "UTC"),
    "open": pl.Float64(),
    "high": pl.Float64(),
    "low": pl.Float64(),
    "close": pl.Float64(),
    "volume": pl.Float64(),
    "value": pl.Float64(),
    "cap": pl.Float64(),
}

MACRO_INTRADAY_SCHEMA: dict[str, pl.DataType] = {
    "day": pl.Date(),
    "slot": pl.Utf8(),
    "series": pl.Utf8(),
    "captured_at": pl.Datetime("us", "UTC"),
    "price": pl.Float64(),
    "prev_close": pl.Float64(),
    "source": pl.Utf8(),
}

BREADTH_INTRADAY_SCHEMA: dict[str, pl.DataType] = {
    "day": pl.Date(),
    "slot": pl.Utf8(),
    "market": pl.Utf8(),
    "captured_at": pl.Datetime("us", "UTC"),
    "advancing": pl.Int64(),
    "declining": pl.Int64(),
    "unchanged": pl.Int64(),
    "total": pl.Int64(),
}

DART_POLL_SCHEMA: dict[str, pl.DataType] = {
    "day": pl.Date(),
    "slot": pl.Utf8(),
    "polled_at": pl.Datetime("us", "UTC"),
    "symbol": pl.Utf8(),
    "corp_code": pl.Utf8(),
    "corp_name": pl.Utf8(),
    "corp_cls": pl.Utf8(),
    "filing_type": pl.Utf8(),
    "report_nm": pl.Utf8(),
    "rcept_no": pl.Utf8(),
}

_ORDERBOOK_LEVEL_COLUMNS: dict[str, pl.DataType] = {
    f"{side}_{kind}_{level}": pl.Float64()
    for side in ("ask", "bid")
    for kind in ("price", "qty")
    for level in range(1, 11)
}

ORDERBOOK_INTRADAY_SCHEMA: dict[str, pl.DataType] = {
    "day": pl.Date(),
    "slot": pl.Utf8(),
    "symbol": pl.Utf8(),
    "captured_at": pl.Datetime("us", "UTC"),
    **_ORDERBOOK_LEVEL_COLUMNS,
    "total_ask_qty": pl.Float64(),
    "total_bid_qty": pl.Float64(),
    "net_bid_qty": pl.Float64(),
    "accept_hour": pl.Utf8(),
    "market_phase": pl.Utf8(),
    "price": pl.Float64(),
    "open": pl.Float64(),
    "high": pl.Float64(),
    "low": pl.Float64(),
    "prev_close": pl.Float64(),
    "antc_price": pl.Float64(),
    "antc_qty": pl.Float64(),
    "antc_phase": pl.Utf8(),
    "vi_code": pl.Utf8(),
}

INVESTOR_ESTIMATE_SCHEMA: dict[str, pl.DataType] = {
    "day": pl.Date(),
    "slot": pl.Utf8(),
    "symbol": pl.Utf8(),
    "captured_at": pl.Datetime("us", "UTC"),
    "bucket": pl.Int64(),
    "frgn_qty": pl.Float64(),
    "orgn_qty": pl.Float64(),
    "sum_qty": pl.Float64(),
}

FLOW_RANKING_SCHEMA: dict[str, pl.DataType] = {
    "day": pl.Date(),
    "slot": pl.Utf8(),
    "side": pl.Utf8(),
    "rank": pl.Int64(),
    "symbol": pl.Utf8(),
    "name": pl.Utf8(),
    "captured_at": pl.Datetime("us", "UTC"),
    "total_qty": pl.Float64(),
    "frgn_qty": pl.Float64(),
    "orgn_qty": pl.Float64(),
    "etc_corp_qty": pl.Float64(),
    "ivtr_qty": pl.Float64(),
    "bank_qty": pl.Float64(),
    "insu_qty": pl.Float64(),
    "mrbn_qty": pl.Float64(),
    "fund_qty": pl.Float64(),
    "etc_fin_qty": pl.Float64(),
    "frgn_amount": pl.Float64(),
    "orgn_amount": pl.Float64(),
    "etc_corp_amount": pl.Float64(),
    "price": pl.Float64(),
    "change_pct": pl.Float64(),
    "volume": pl.Float64(),
}

FRGNMEM_RANKING_SCHEMA: dict[str, pl.DataType] = {
    "day": pl.Date(),
    "slot": pl.Utf8(),
    "side": pl.Utf8(),
    "rank": pl.Int64(),
    "symbol": pl.Utf8(),
    "name": pl.Utf8(),
    "captured_at": pl.Datetime("us", "UTC"),
    "net_qty": pl.Float64(),
    "buy_qty": pl.Float64(),
    "sell_qty": pl.Float64(),
    "price": pl.Float64(),
    "change_pct": pl.Float64(),
    "volume": pl.Float64(),
}

VOLUME_POWER_INTRADAY_SCHEMA: dict[str, pl.DataType] = {
    "day": pl.Date(),
    "slot": pl.Utf8(),
    "symbol": pl.Utf8(),
    "captured_at": pl.Datetime("us", "UTC"),
    "strength": pl.Float64(),
    "tick_hour": pl.Utf8(),
    "price": pl.Float64(),
    "change_pct": pl.Float64(),
}

_MEMBER_LEVEL_COLUMNS: dict[str, pl.DataType] = {
    f"{side}_member_{field}_{level}": dtype
    for side in ("sell", "buy")
    for field, dtype in (
        ("no", pl.Utf8()),
        ("name", pl.Utf8()),
        ("qty", pl.Float64()),
        ("share", pl.Float64()),
        ("qty_change", pl.Float64()),
        ("foreign", pl.Utf8()),
    )
    for level in range(1, 6)
}

MEMBER_INTRADAY_SCHEMA: dict[str, pl.DataType] = {
    "day": pl.Date(),
    "slot": pl.Utf8(),
    "symbol": pl.Utf8(),
    "captured_at": pl.Datetime("us", "UTC"),
    **_MEMBER_LEVEL_COLUMNS,
    "foreign_buy_qty": pl.Float64(),
    "foreign_sell_qty": pl.Float64(),
    "foreign_net_qty": pl.Float64(),
    "foreign_buy_share": pl.Float64(),
    "foreign_sell_share": pl.Float64(),
    "foreign_buy_qty_change": pl.Float64(),
    "foreign_sell_qty_change": pl.Float64(),
    "volume": pl.Float64(),
}

PROGRAM_TRADE_INTRADAY_SCHEMA: dict[str, pl.DataType] = {
    "day": pl.Date(),
    "slot": pl.Utf8(),
    "symbol": pl.Utf8(),
    "captured_at": pl.Datetime("us", "UTC"),
    "tick_hour": pl.Utf8(),
    "price": pl.Float64(),
    "change_pct": pl.Float64(),
    "volume": pl.Float64(),
    "sell_qty": pl.Float64(),
    "buy_qty": pl.Float64(),
    "net_qty": pl.Float64(),
    "sell_amount": pl.Float64(),
    "buy_amount": pl.Float64(),
    "net_amount": pl.Float64(),
}

PROGRAM_MARKET_INTRADAY_SCHEMA: dict[str, pl.DataType] = {
    "day": pl.Date(),
    "slot": pl.Utf8(),
    "captured_at": pl.Datetime("us", "UTC"),
    "market": pl.Utf8(),
    "hour": pl.Utf8(),
    "arb_sell_amount": pl.Float64(),
    "arb_buy_amount": pl.Float64(),
    "arb_net_amount": pl.Float64(),
    "nonarb_sell_amount": pl.Float64(),
    "nonarb_buy_amount": pl.Float64(),
    "nonarb_net_amount": pl.Float64(),
    "total_net_amount": pl.Float64(),
}

FRGNMEM_TREND_INTRADAY_SCHEMA: dict[str, pl.DataType] = {
    "day": pl.Date(),
    "slot": pl.Utf8(),
    "symbol": pl.Utf8(),
    "captured_at": pl.Datetime("us", "UTC"),
    "seq": pl.Int64(),
    "tick_hour": pl.Utf8(),
    "price": pl.Float64(),
    "change_pct": pl.Float64(),
    "volume": pl.Float64(),
    "foreign_sell_qty": pl.Float64(),
    "foreign_buy_qty": pl.Float64(),
    "foreign_net_qty": pl.Float64(),
    "net_qty_change": pl.Float64(),
}

OVERTIME_PRICE_SCHEMA: dict[str, pl.DataType] = {
    "day": pl.Date(),
    "symbol": pl.Utf8(),
    "captured_at": pl.Datetime("us", "UTC"),
    "prev_close": pl.Float64(),
    "price": pl.Float64(),
    "change": pl.Float64(),
    "change_pct": pl.Float64(),
    "sign": pl.Utf8(),
    "open": pl.Float64(),
    "high": pl.Float64(),
    "low": pl.Float64(),
    "volume": pl.Float64(),
    "amount": pl.Float64(),
    "upper_limit": pl.Float64(),
    "lower_limit": pl.Float64(),
    "vi_code": pl.Utf8(),
}

OVERTIME_RANKING_SCHEMA: dict[str, pl.DataType] = {
    "day": pl.Date(),
    "side": pl.Utf8(),
    "rank": pl.Int64(),
    "symbol": pl.Utf8(),
    "name": pl.Utf8(),
    "captured_at": pl.Datetime("us", "UTC"),
    "price": pl.Float64(),
    "change": pl.Float64(),
    "change_pct": pl.Float64(),
    "sign": pl.Utf8(),
    "ask": pl.Float64(),
    "bid": pl.Float64(),
    "volume": pl.Float64(),
    "sell_rsqn": pl.Float64(),
    "buy_rsqn": pl.Float64(),
    "vol_vs_day_pct": pl.Float64(),
    "day_price": pl.Float64(),
    "day_volume": pl.Float64(),
}

OVERTIME_MARKET_SCHEMA: dict[str, pl.DataType] = {
    "day": pl.Date(),
    "scope": pl.Utf8(),
    "captured_at": pl.Datetime("us", "UTC"),
    "volume": pl.Float64(),
    "amount": pl.Float64(),
    "kospi_volume": pl.Float64(),
    "kospi_amount": pl.Float64(),
    "kosdaq_volume": pl.Float64(),
    "kosdaq_amount": pl.Float64(),
    "up_count": pl.Int64(),
    "down_count": pl.Int64(),
    "flat_count": pl.Int64(),
    "upper_limit_count": pl.Int64(),
    "lower_limit_count": pl.Int64(),
}

US_DAILY_SCHEMA: dict[str, pl.DataType] = {
    "day": pl.Date(),
    "open": pl.Float64(),
    "high": pl.Float64(),
    "low": pl.Float64(),
    "close": pl.Float64(),
    "volume": pl.Float64(),
}

US_MACRO_DAILY_SCHEMA: dict[str, pl.DataType] = {
    "day": pl.Date(),
    "value": pl.Float64(),
    "source": pl.Utf8(),
    "captured_at": pl.Datetime("us", "UTC"),
}

US_EVENTS_SCHEMA: dict[str, pl.DataType] = {
    "day": pl.Date(),
    "event_day": pl.Date(),
    "kst_at": pl.Datetime("us", "UTC"),
    "category": pl.Utf8(),
    "tier": pl.Utf8(),
    "source": pl.Utf8(),
    "in_hold_window": pl.Boolean(),
    "captured_at": pl.Datetime("us", "UTC"),
}

US_EVENTS_HISTORY_SCHEMA: dict[str, pl.DataType] = {
    "event_key": pl.Utf8(),
    "event_day": pl.Date(),
    "kst_at": pl.Datetime("us", "UTC"),
    "category": pl.Utf8(),
    "tier": pl.Utf8(),
    "source": pl.Utf8(),
}

US_EARNINGS_SCHEMA: dict[str, pl.DataType] = {
    "day": pl.Date(),
    "symbol": pl.Utf8(),
    "report_day": pl.Date(),
    "when": pl.Utf8(),
    "confirmed": pl.Boolean(),
    "in_hold_window": pl.Boolean(),
    "source": pl.Utf8(),
    "captured_at": pl.Datetime("us", "UTC"),
}

US_KR_MAP_SCHEMA: dict[str, pl.DataType] = {
    "us_symbol": pl.Utf8(),
    "kr_theme": pl.Utf8(),
    "kr_symbols": pl.List(pl.Utf8()),
    "link_type": pl.Utf8(),
    "lead_strength": pl.Utf8(),
    "live_at_1510": pl.Boolean(),
    "effective_from": pl.Date(),
    "effective_to": pl.Date(),
    "source_note": pl.Utf8(),
}

MARKET_CAP_SCHEMA: dict[str, pl.DataType] = {
    "day": pl.Date(),
    "symbol": pl.Utf8(),
    "close": pl.Float64(),
    "cap": pl.Float64(),
    "volume": pl.Float64(),
    "value": pl.Float64(),
    "shares": pl.Float64(),
}

STOCK_INFO_SCHEMA: dict[str, pl.DataType] = {
    "day": pl.Date(),
    "symbol": pl.Utf8(),
    "name": pl.Utf8(),
    "market": pl.Utf8(),
    "security_group": pl.Utf8(),
    "share_kind": pl.Utf8(),
    "section": pl.Utf8(),
    "listed_on": pl.Date(),
    "shares": pl.Float64(),
}

INVESTOR_FLOWS_SCHEMA: dict[str, pl.DataType] = {
    "day": pl.Date(),
    "symbol": pl.Utf8(),
    "investor": pl.Utf8(),
    "fetched_at": pl.Datetime("us", "UTC"),
    "sell_volume": pl.Float64(),
    "buy_volume": pl.Float64(),
    "net_volume": pl.Float64(),
    "sell_value": pl.Float64(),
    "buy_value": pl.Float64(),
    "net_value": pl.Float64(),
}

VKOSPI_1D_SCHEMA: dict[str, pl.DataType] = {
    "day": pl.Date(),
    "open": pl.Float64(),
    "high": pl.Float64(),
    "low": pl.Float64(),
    "close": pl.Float64(),
    "change": pl.Float64(),
    "change_pct": pl.Float64(),
    "source": pl.Utf8(),
    "fetched_at": pl.Datetime("us", "UTC"),
}

SHORTING_SCHEMA: dict[str, pl.DataType] = {
    "day": pl.Date(),
    "symbol": pl.Utf8(),
    "market": pl.Utf8(),
    "short_volume": pl.Int64(),
    "total_volume_consolidated": pl.Int64(),
    "short_ratio_pct": pl.Float64(),
    "short_value": pl.Int64(),
    "total_value_consolidated": pl.Int64(),
    "short_value_ratio_pct": pl.Float64(),
    "fetched_at": pl.Datetime("us", "UTC"),
}

SHORTING_BALANCE_SCHEMA: dict[str, pl.DataType] = {
    "day": pl.Date(),
    "symbol": pl.Utf8(),
    "market": pl.Utf8(),
    "short_balance_qty": pl.Int64(),
    "listed_shares": pl.Int64(),
    "short_balance_value": pl.Int64(),
    "market_cap": pl.Int64(),
    "short_balance_ratio_pct": pl.Float64(),
    "fetched_at": pl.Datetime("us", "UTC"),
}

SHORTING_INVESTOR_SCHEMA: dict[str, pl.DataType] = {
    "day": pl.Date(),
    "market": pl.Utf8(),
    "investor": pl.Utf8(),
    "vol_shares": pl.Int64(),
    "value_krw": pl.Int64(),
    "fetched_at": pl.Datetime("us", "UTC"),
}

KIS_MINUTES_SCHEMA: dict[str, pl.DataType] = {
    "day": pl.Date(),
    "symbol": pl.Utf8(),
    "ts": pl.Datetime("us", "UTC"),
    "open": pl.Float64(),
    "high": pl.Float64(),
    "low": pl.Float64(),
    "close": pl.Float64(),
    "volume": pl.Float64(),
    "cum_value": pl.Float64(),
    "fetched_at": pl.Datetime("us", "UTC"),
}

INVESTOR_SCHEMA: dict[str, pl.DataType] = {
    "day": pl.Date(),
    "updated_at": pl.Datetime("us", "UTC"),
    "individual_buy": pl.Float64(),
    "individual_sell": pl.Float64(),
    "foreigner_buy": pl.Float64(),
    "foreigner_sell": pl.Float64(),
    "institution_buy": pl.Float64(),
    "institution_sell": pl.Float64(),
    "other_buy": pl.Float64(),
    "other_sell": pl.Float64(),
    "institution_breakdown": pl.Utf8(),
}


def normalize_daily_snapshot(frame: pl.DataFrame) -> pl.DataFrame:
    no_trade = pl.col("high").is_null() | (pl.col("high") <= 0)
    return frame.filter(pl.col("close") > 0).with_columns(
        pl.when(no_trade)
        .then(pl.lit(None, dtype=pl.Float64))
        .otherwise(pl.col(column))
        .alias(column)
        for column in ("open", "high", "low")
    )


def candles_to_frame(candles: list[Candle]) -> pl.DataFrame:
    rows = [candle.model_dump() for candle in candles]
    return pl.DataFrame(rows, schema=CANDLE_SCHEMA)


def investor_records_to_frame(records: list[InvestorFlowRecord]) -> pl.DataFrame:
    rows = [record.model_dump() for record in records]
    return pl.DataFrame(rows, schema=INVESTOR_SCHEMA)


def _atomic_write(frame: pl.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    frame.write_parquet(tmp)
    os.replace(tmp, path)


class ParquetStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, dataset: str, name: str) -> Path:
        return self.root / dataset / f"{name}.parquet"

    def upsert(self, dataset: str, name: str, frame: pl.DataFrame, key: str = "ts") -> int:
        if frame.is_empty():
            return 0
        path = self.path(dataset, name)
        if path.exists():
            existing = pl.read_parquet(path)
            merged = pl.concat([existing, frame], how="vertical_relaxed")
        else:
            existing = None
            merged = frame
        merged = merged.unique(subset=[key], keep="last").sort(key)
        _atomic_write(merged, path)
        return merged.height - (existing.height if existing is not None else 0)

    def replace(self, dataset: str, name: str, frame: pl.DataFrame) -> int:
        _atomic_write(frame, self.path(dataset, name))
        return frame.height

    def read(self, dataset: str, name: str) -> pl.DataFrame | None:
        path = self.path(dataset, name)
        if not path.exists():
            return None
        return pl.read_parquet(path)

    def last_value(self, dataset: str, name: str, column: str = "ts") -> datetime | None:
        path = self.path(dataset, name)
        if not path.exists():
            return None
        return pl.scan_parquet(path).select(pl.col(column).max()).collect().item()

    def first_value(self, dataset: str, name: str, column: str = "ts") -> datetime | None:
        path = self.path(dataset, name)
        if not path.exists():
            return None
        return pl.scan_parquet(path).select(pl.col(column).min()).collect().item()

    def names(self, dataset: str) -> list[str]:
        directory = self.root / dataset
        if not directory.exists():
            return []
        return sorted(p.stem for p in directory.glob("*.parquet"))


class DatePartitionedStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, dataset: str, day: date) -> Path:
        return self.root / dataset / f"{day.isoformat()}.parquet"

    def write_date(self, dataset: str, day: date, frame: pl.DataFrame) -> None:
        _atomic_write(frame, self.path(dataset, day))

    def upsert_date(
        self, dataset: str, day: date, frame: pl.DataFrame, key: tuple[str, ...]
    ) -> int:
        if frame.is_empty():
            return 0
        path = self.path(dataset, day)
        if path.exists():
            merged = pl.concat([pl.read_parquet(path), frame], how="vertical_relaxed")
        else:
            merged = frame
        merged = merged.unique(subset=list(key), keep="last").sort(list(key))
        _atomic_write(merged, path)
        return frame.height

    def has_date(self, dataset: str, day: date) -> bool:
        return self.path(dataset, day).exists()

    def read_date(self, dataset: str, day: date) -> pl.DataFrame | None:
        path = self.path(dataset, day)
        if not path.exists():
            return None
        return pl.read_parquet(path)

    def dates(self, dataset: str) -> list[date]:
        directory = self.root / dataset
        if not directory.exists():
            return []
        return sorted(date.fromisoformat(p.stem) for p in directory.glob("*.parquet"))

    def scan(self, dataset: str) -> pl.LazyFrame | None:
        directory = self.root / dataset
        if not directory.exists() or not any(directory.glob("*.parquet")):
            return None
        return pl.scan_parquet(directory / "*.parquet")

    def latest(self, dataset: str) -> tuple[date, pl.DataFrame] | None:
        dates = self.dates(dataset)
        if not dates:
            return None
        day = dates[-1]
        frame = self.read_date(dataset, day)
        if frame is None:
            return None
        return day, frame
