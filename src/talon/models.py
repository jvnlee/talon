import json
from datetime import UTC, date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from talon.timeutil import KST


class Candle(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    ts: datetime = Field(alias="timestamp")
    open: float = Field(alias="openPrice")
    high: float = Field(alias="highPrice")
    low: float = Field(alias="lowPrice")
    close: float = Field(alias="closePrice")
    volume: float

    @field_validator("ts")
    @classmethod
    def _normalize_ts(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=KST)
        return value.astimezone(UTC)


class CandlePage(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    candles: list[Candle]
    next_before: str | None = Field(default=None, alias="nextBefore")


class InvestorFlowRecord(BaseModel):
    day: date
    updated_at: datetime
    individual_buy: float
    individual_sell: float
    foreigner_buy: float
    foreigner_sell: float
    institution_buy: float
    institution_sell: float
    other_buy: float
    other_sell: float
    institution_breakdown: str

    @classmethod
    def from_toss(cls, raw: dict[str, Any]) -> "InvestorFlowRecord":
        def amounts(section: str) -> tuple[float, float]:
            entry = raw[section]
            return float(entry["buyAmount"]), float(entry["sellAmount"])

        individual = amounts("individual")
        foreigner = amounts("foreigner")
        institution = amounts("institution")
        other = amounts("otherCorporation")
        updated = datetime.fromisoformat(raw["updatedAt"])
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=KST)
        return cls(
            day=date.fromisoformat(raw["date"]),
            updated_at=updated.astimezone(UTC),
            individual_buy=individual[0],
            individual_sell=individual[1],
            foreigner_buy=foreigner[0],
            foreigner_sell=foreigner[1],
            institution_buy=institution[0],
            institution_sell=institution[1],
            other_buy=other[0],
            other_sell=other[1],
            institution_breakdown=json.dumps(raw["institution"].get("breakdown", {})),
        )


class Heartbeat(BaseModel):
    job: str
    ts: datetime
    ok: bool
    detail: dict[str, Any] = {}


class JobRun(BaseModel):
    id: int
    job: str
    started_at: datetime
    finished_at: datetime | None = None
    ok: bool | None = None
    detail: dict[str, Any] = {}


class UniverseSnapshot(BaseModel):
    day: date
    symbols: list[str]
    criteria: dict[str, Any] = {}
    created_at: datetime


class CollectSummary(BaseModel):
    status: str
    symbols: int = 0
    failed: list[str] = []
    rows: int = 0
    indicator_rows: int = 0


class EodSummary(BaseModel):
    status: str
    day: date | None = None
    steps: dict[str, str] = {}
    universe_size: int = 0


class IntradaySummary(BaseModel):
    status: str
    day: date
    slot: str
    rows: int = 0
    extras: dict[str, str] = {}


class PulseSummary(BaseModel):
    parts: dict[str, str] = {}
    rows: dict[str, int] = {}


class CloseAuctionSummary(BaseModel):
    status: str
    day: date
    symbols: int = 0
    passes: dict[str, str] = {}
    rows: dict[str, int] = {}


class OvertimeSummary(BaseModel):
    status: str
    day: date
    symbols: int = 0
    parts: dict[str, str] = {}
    rows: dict[str, int] = {}


class UsNightSummary(BaseModel):
    status: str
    symbols: int = 0
    daily_rows: int = 0
    minute_rows: int = 0
    failed: list[str] = []


class MinuteBackfillSummary(BaseModel):
    status: str
    symbols: int = 0
    rows: int = 0
    oldest: datetime | None = None
    failures: list[str] = []


class BackfillSummary(BaseModel):
    status: str
    sessions: int = 0
    loaded: int = 0
    skipped: int = 0
    failed: list[str] = []


class ReconcileDay(BaseModel):
    day: date
    status: str
    rows: int = 0
    corrections: dict[str, int] = {}
    added: int = 0
    detail: str = ""


class ReconcileSummary(BaseModel):
    status: str
    sessions: int = 0
    days: list[ReconcileDay] = []
    filled: list[str] = []
    corrected: list[str] = []
    unavailable: list[str] = []
    errors: list[str] = []


class IndexBackfillSummary(BaseModel):
    status: str
    rows: dict[str, int] = {}
    failed: list[str] = []


class AdjustSummary(BaseModel):
    status: str
    symbols: int = 0
    computed: int = 0
    skipped: int = 0
    empty: list[str] = []
    failed: list[str] = []


class WatchdogSummary(BaseModel):
    status: str
    issues: list[str] = []


class HolidaySyncSummary(BaseModel):
    status: str
    years: list[int] = []
    known: int = 0
    added: list[str] = []
    errors: list[str] = []
