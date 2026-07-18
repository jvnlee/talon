from datetime import UTC, date, datetime

import polars as pl
import pytest

from talon.data.store import INVESTOR_FLOWS, INVESTOR_FLOWS_SCHEMA
from talon.errors import SourceError
from talon.ingest.flows import backfill_flows, daily_flows

START = date(2026, 7, 6)
END = date(2026, 7, 10)


def flows_frame(day: date) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "day": [day, day],
            "symbol": ["005930", "005930"],
            "investor": ["individual", "foreigner"],
            "fetched_at": [datetime(2026, 7, 16, tzinfo=UTC)] * 2,
            "sell_volume": [1.0, 2.0],
            "buy_volume": [2.0, 1.0],
            "net_volume": [1.0, -1.0],
            "sell_value": [100.0, 200.0],
            "buy_value": [200.0, 100.0],
            "net_value": [100.0, -100.0],
        },
        schema=INVESTOR_FLOWS_SCHEMA,
    )


def test_backfill_flows_loads_sessions(cfg, cal, state, snapshots):
    calls: list[date] = []

    def fetch(day: date) -> pl.DataFrame:
        calls.append(day)
        return flows_frame(day)

    summary = backfill_flows(
        cfg, cal=cal, state=state, snapshots=snapshots, start=START, end=END, fetch=fetch
    )
    assert summary.status == "ok"
    assert summary.sessions == 5
    assert summary.loaded == 5
    assert len(calls) == 5
    assert snapshots.has_date(INVESTOR_FLOWS, date(2026, 7, 8))


def test_backfill_flows_skips_existing(cfg, cal, state, snapshots):
    snapshots.write_date(INVESTOR_FLOWS, date(2026, 7, 7), flows_frame(date(2026, 7, 7)))
    calls: list[date] = []

    def fetch(day: date) -> pl.DataFrame:
        calls.append(day)
        return flows_frame(day)

    summary = backfill_flows(
        cfg, cal=cal, state=state, snapshots=snapshots, start=START, end=END, fetch=fetch
    )
    assert summary.skipped == 1
    assert summary.loaded == 4
    assert date(2026, 7, 7) not in calls


def test_backfill_flows_aborts_after_consecutive_failures(cfg, cal, state, snapshots):
    calls: list[date] = []

    def fetch(day: date) -> pl.DataFrame:
        calls.append(day)
        raise SourceError("boom")

    summary = backfill_flows(
        cfg, cal=cal, state=state, snapshots=snapshots, start=START, end=END, fetch=fetch
    )
    assert summary.status == "aborted"
    assert len(calls) == 3
    assert len(summary.failed) == 3


def test_backfill_flows_partial_on_isolated_failure(cfg, cal, state, snapshots):
    def fetch(day: date) -> pl.DataFrame:
        if day == date(2026, 7, 8):
            raise SourceError("boom")
        return flows_frame(day)

    summary = backfill_flows(
        cfg, cal=cal, state=state, snapshots=snapshots, start=START, end=END, fetch=fetch
    )
    assert summary.status == "partial"
    assert summary.loaded == 4
    assert summary.failed == ["2026-07-08"]


def kst(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, 16, hour - 9, minute, tzinfo=UTC)


def test_daily_flows_waits_for_confirmation_time(cfg, cal, snapshots):
    calls: list[date] = []

    def fetch(day: date) -> pl.DataFrame:
        calls.append(day)
        return flows_frame(day)

    result = daily_flows(cfg, cal=cal, snapshots=snapshots, now=kst(16, 40), fetch=fetch)
    assert date(2026, 7, 16) not in calls
    assert calls
    assert "days" in result

    calls.clear()
    result = daily_flows(cfg, cal=cal, snapshots=snapshots, now=kst(18, 30), fetch=fetch)
    assert calls == [date(2026, 7, 16)]
    assert result == "1/1 days, 2 rows"


def test_daily_flows_up_to_date(cfg, cal, snapshots):
    for day in cal.sessions_between(date(2026, 7, 1), date(2026, 7, 16)):
        snapshots.write_date(INVESTOR_FLOWS, day, flows_frame(day))
    result = daily_flows(cfg, cal=cal, snapshots=snapshots, now=kst(18, 30), fetch=None)
    assert result == "up-to-date"


def test_daily_flows_counts_errors(cfg, cal, snapshots):
    for day in cal.sessions_between(date(2026, 7, 1), date(2026, 7, 15)):
        snapshots.write_date(INVESTOR_FLOWS, day, flows_frame(day))

    def fetch(day: date) -> pl.DataFrame:
        raise SourceError("boom")

    result = daily_flows(cfg, cal=cal, snapshots=snapshots, now=kst(18, 30), fetch=fetch)
    assert result == "0/1 days, 0 rows, errors: 1"
    assert not snapshots.has_date(INVESTOR_FLOWS, date(2026, 7, 16))


@pytest.mark.parametrize("hour", [16, 18])
def test_daily_flows_catches_up_missed_days(cfg, cal, snapshots, hour):
    sessions = cal.sessions_between(date(2026, 7, 1), date(2026, 7, 15))
    for day in sessions[:-1]:
        snapshots.write_date(INVESTOR_FLOWS, day, flows_frame(day))

    calls: list[date] = []

    def fetch(day: date) -> pl.DataFrame:
        calls.append(day)
        return flows_frame(day)

    daily_flows(cfg, cal=cal, snapshots=snapshots, now=kst(hour, 40), fetch=fetch)
    assert date(2026, 7, 15) in calls
