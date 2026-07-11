import os
from datetime import date, datetime
from pathlib import Path

import polars as pl

from talon.models import Candle, InvestorFlowRecord

MINUTE_CANDLES = "candles_1m"
DAILY_CANDLES = "candles_1d"
MARKET_CAP = "marketcap"
INDICATOR_MINUTE = "indicators_1m"
INDICATOR_DAILY = "indicators_1d"
INVESTOR_TRADING = "investor_trading"
DELISTING = "delisting"
ADJUST_FACTORS = "adjust_factors"
ADJUST_MANIFEST = "adjust_manifest"

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

MARKET_CAP_SCHEMA: dict[str, pl.DataType] = {
    "day": pl.Date(),
    "symbol": pl.Utf8(),
    "close": pl.Float64(),
    "cap": pl.Float64(),
    "volume": pl.Float64(),
    "value": pl.Float64(),
    "shares": pl.Float64(),
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
