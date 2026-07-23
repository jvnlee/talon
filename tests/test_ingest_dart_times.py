from datetime import UTC, date, datetime, timedelta

import polars as pl

from talon.data.store import (
    DART_FILING_TIMES,
    DART_FILING_TIMES_SCHEMA,
    DART_FILINGS,
)
from talon.ingest.dart_times import (
    DAILY_SELF_HEAL_SESSIONS,
    backfill_dart_times,
    daily_dart_times,
    verify_dart_times,
)
from talon.sources.dart import DART_FILINGS_SCHEMA
from talon.sources.dart_web import DisclosureRow

NOW = datetime(2026, 7, 23, 6, 10, tzinfo=UTC)


def _rows(day: date, count: int, *, cross: bool = False) -> list[DisclosureRow]:
    prefix = day.strftime("%Y%m%d")
    out = []
    for i in range(count):
        rc = f"{prefix}{i:06d}"
        out.append(DisclosureRow(rc, f"09:{i:02d}", "종목", "주요사항보고서"))
    if cross:
        out.append(DisclosureRow("20200101000099", "16:00", "타사", "정정보고서"))
    return out


def test_backfill_writes_partitions_and_resumes(cfg, state, snapshots):
    table = {
        date(2023, 6, 1): _rows(date(2023, 6, 1), 3),
        date(2023, 6, 2): _rows(date(2023, 6, 2), 2),
    }
    calls: list[date] = []

    def fetch(day: date) -> list[DisclosureRow]:
        calls.append(day)
        return table.get(day, [])

    summary = backfill_dart_times(
        cfg,
        state=state,
        snapshots=snapshots,
        start=date(2023, 6, 1),
        end=date(2023, 6, 2),
        fetch=fetch,
        now=lambda: NOW,
    )
    assert summary.status == "ok"
    assert summary.loaded == 2
    frame = snapshots.read_date(DART_FILING_TIMES, date(2023, 6, 1))
    assert frame is not None
    assert frame.height == 3
    assert dict(frame.schema) == DART_FILING_TIMES_SCHEMA
    assert set(frame["source"].to_list()) == {"dart_web"}

    rerun = backfill_dart_times(
        cfg,
        state=state,
        snapshots=snapshots,
        start=date(2023, 6, 1),
        end=date(2023, 6, 2),
        fetch=fetch,
        now=lambda: NOW,
    )
    assert rerun.skipped == 2
    assert len(calls) == 2


def test_backfill_skips_out_of_horizon(cfg, state, snapshots):
    calls: list[date] = []

    def fetch(day: date) -> list[DisclosureRow]:
        calls.append(day)
        return []

    summary = backfill_dart_times(
        cfg,
        state=state,
        snapshots=snapshots,
        start=date(2004, 12, 30),
        end=date(2005, 1, 3),
        fetch=fetch,
        now=lambda: NOW,
    )
    assert summary.out_of_horizon == 4
    assert calls == [date(2005, 1, 3)]


def test_backfill_aborts_on_consecutive_failures(cfg, state, snapshots):
    from talon.errors import SourceError

    def fetch(day: date) -> list[DisclosureRow]:
        raise SourceError("boom")

    summary = backfill_dart_times(
        cfg,
        state=state,
        snapshots=snapshots,
        start=date(2023, 6, 1),
        end=date(2023, 6, 30),
        fetch=fetch,
        now=lambda: NOW,
    )
    assert summary.status == "aborted"
    assert len(summary.failed) == 3


def test_daily_self_heals_recent_sessions(cfg, cal, snapshots):
    today = date(2026, 7, 23)
    fetched: list[date] = []

    def fetch(day: date) -> list[DisclosureRow]:
        fetched.append(day)
        return _rows(day, 1)

    summary = daily_dart_times(
        cfg,
        cal=cal,
        snapshots=snapshots,
        today=today,
        fetch=fetch,
        now=lambda: NOW,
    )
    end = cal.latest_trading_day(today)
    expected = cal.sessions_between(
        end - timedelta(days=DAILY_SELF_HEAL_SESSIONS * 2 + 7), end
    )[-DAILY_SELF_HEAL_SESSIONS:]
    assert summary.status == "ok"
    assert summary.days == DAILY_SELF_HEAL_SESSIONS
    assert fetched == expected
    assert date(2026, 7, 17) not in fetched
    assert snapshots.has_date(DART_FILING_TIMES, today)


def test_daily_bridges_long_holiday_closure(cfg, cal, snapshots):
    reopen = date(2017, 10, 10)
    last_before_closure = date(2017, 9, 29)
    fetched: list[date] = []

    def fetch(day: date) -> list[DisclosureRow]:
        fetched.append(day)
        return _rows(day, 1)

    daily_dart_times(
        cfg,
        cal=cal,
        snapshots=snapshots,
        today=reopen,
        fetch=fetch,
        now=lambda: NOW,
    )
    assert (reopen - last_before_closure).days > DAILY_SELF_HEAL_SESSIONS
    assert last_before_closure in fetched
    assert snapshots.has_date(DART_FILING_TIMES, last_before_closure)


def _seed_times(snapshots, day: date, rows: list[DisclosureRow]) -> None:
    frame = pl.DataFrame(
        [
            {
                "day": day,
                "rcept_no": r.rcept_no,
                "received_time": r.received_time,
                "corp_name": r.corp_name,
                "title": r.title,
                "source": "dart_web",
                "fetched_at": NOW,
            }
            for r in rows
        ],
        schema=DART_FILING_TIMES_SCHEMA,
    )
    snapshots.write_date(DART_FILING_TIMES, day, frame)


def _seed_filings(snapshots, day: date, rcept_nos: list[str]) -> None:
    frame = pl.DataFrame(
        [
            {
                "day": day,
                "symbol": "005930",
                "corp_code": "00126380",
                "corp_name": "종목",
                "corp_cls": "Y",
                "filing_type": "A",
                "report_nm": "보고서",
                "rcept_no": rc,
            }
            for rc in rcept_nos
        ],
        schema=DART_FILINGS_SCHEMA,
    )
    snapshots.write_date(DART_FILINGS, day, frame)


def test_verify_reports_coverage_and_cross_day(cfg, snapshots):
    _seed_times(snapshots, date(2023, 6, 1), _rows(date(2023, 6, 1), 3, cross=True))
    _seed_filings(
        snapshots,
        date(2023, 6, 1),
        ["20230601000000", "20230601000001", "20230601999999"],
    )
    report = verify_dart_times(cfg, snapshots=snapshots)
    assert report.status == "ok"
    assert report.rows == 4
    assert report.cross_day_rows == 1
    assert report.duplicate_keys == 0
    assert report.coverage["2023"] == "2/3 = 66.7%"


def test_verify_detects_bad_time(cfg, snapshots):
    _seed_times(
        snapshots,
        date(2023, 6, 1),
        [DisclosureRow("20230601000001", "9am", "종목", "보고서")],
    )
    report = verify_dart_times(cfg, snapshots=snapshots)
    assert report.status.startswith("issues")
    assert report.bad_time_format == 1


def test_verify_empty(cfg, snapshots):
    report = verify_dart_times(cfg, snapshots=snapshots)
    assert report.status == "empty"
