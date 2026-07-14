from datetime import UTC, datetime, timedelta

from conftest import make_candle
from talon.data.store import MINUTE_CANDLES, candles_to_frame
from talon.ingest.minutes import backfill_minutes


class FakeToss:
    def __init__(self, pages: dict[str, list]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    def candles_since(self, symbol, interval, since, *, max_pages=30, **kw):
        self.calls.append(symbol)
        if symbol not in self.pages:
            raise RuntimeError(f"no data for {symbol}")
        return self.pages[symbol]


def minutes(start: datetime, count: int) -> list:
    return [make_candle(start + timedelta(minutes=i), price=100.0 + i) for i in range(count)]


def test_backfill_extends_history_backwards(series):
    recent = minutes(datetime(2026, 7, 13, 6, 0, tzinfo=UTC), 5)
    series.upsert(MINUTE_CANDLES, "005930", candles_to_frame(recent), key="ts")
    older = minutes(datetime(2026, 4, 20, 0, 0, tzinfo=UTC), 10)
    client = FakeToss({"005930": older + recent})

    summary = backfill_minutes(series, client, ["005930"], max_pages=200)

    assert summary.status == "ok"
    assert summary.rows == 10
    assert summary.oldest == datetime(2026, 4, 20, 0, 0, tzinfo=UTC)
    stored = series.read(MINUTE_CANDLES, "005930")
    assert stored.height == 15
    assert stored["ts"].is_sorted()


def test_backfill_reports_failures_without_dying(series):
    client = FakeToss({"005930": minutes(datetime(2026, 4, 20, tzinfo=UTC), 3)})

    summary = backfill_minutes(series, client, ["005930", "BROKEN"], max_pages=10)

    assert summary.status == "partial"
    assert summary.failures == ["BROKEN"]
    assert summary.symbols == 1
    assert series.read(MINUTE_CANDLES, "005930").height == 3


def test_backfill_is_idempotent(series):
    client = FakeToss({"005930": minutes(datetime(2026, 4, 20, tzinfo=UTC), 6)})

    first = backfill_minutes(series, client, ["005930"], max_pages=10)
    second = backfill_minutes(series, client, ["005930"], max_pages=10)

    assert first.rows == 6
    assert second.rows == 0
    assert series.read(MINUTE_CANDLES, "005930").height == 6


def test_empty_response_is_not_a_failure(series):
    client = FakeToss({"005930": []})

    summary = backfill_minutes(series, client, ["005930"], max_pages=10)

    assert summary.status == "ok"
    assert summary.rows == 0
    assert series.read(MINUTE_CANDLES, "005930") is None
