from datetime import UTC, datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def now_utc() -> datetime:
    return datetime.now(UTC)


def to_utc(value: datetime, assume: ZoneInfo = KST) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=assume)
    return value.astimezone(UTC)


def minute_floor(value: datetime) -> datetime:
    return value.replace(second=0, microsecond=0)


def iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()
