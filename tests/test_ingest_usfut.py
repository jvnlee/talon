from datetime import UTC, date, datetime

import polars as pl

from talon.data.store import (
    US_DAILY,
    US_DAILY_SCHEMA,
    US_FUTURES_1510,
    US_FUTURES_1510_SCHEMA,
)
from talon.errors import SourceError
from talon.ingest.usfut import backfill_usfut, daily_usfut, verify_usfut
from talon.sources.dukascopy import DukascopyBar

FETCHED = datetime(2026, 7, 20, 7, 0, tzinfo=UTC)


def fresh(close: float = 7500.0, vol: float = 0.1) -> list[DukascopyBar]:
    return [DukascopyBar(22140, close, close, close, close, vol)]


def stale_target(close: float = 7500.0) -> list[DukascopyBar]:
    return [
        DukascopyBar(22140, 0.0, 0.0, 0.0, 0.0, 0.0),
        DukascopyBar(22080, close, close, close, close, 0.1),
    ]


def frozen_window() -> list[DukascopyBar]:
    return [DukascopyBar(22140, 7500.0, 7500.0, 7500.0, 7500.0, 0.0)]


def no_sleep(_seconds: float) -> None:
    return None


def cfd_frame(day: date, symbols, *, price: float = 7500.0, stale: int = 0) -> pl.DataFrame:
    rows = [
        {
            "day": day,
            "symbol": symbol,
            "price": price,
            "bar_ts": datetime(day.year, day.month, day.day, 6, 10, tzinfo=UTC),
            "stale_minutes": stale,
            "source": "dukascopy_cfd",
            "fetched_at": FETCHED,
        }
        for symbol in symbols
    ]
    return pl.DataFrame(rows, schema=US_FUTURES_1510_SCHEMA)


def us_daily(series, symbol: str, days: list[date], close: float) -> None:
    frame = pl.DataFrame(
        [
            {
                "day": day,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 1.0,
            }
            for day in days
        ],
        schema=US_DAILY_SCHEMA,
    )
    series.replace(US_DAILY, symbol, frame)


def test_backfill_loads_weekdays_and_skips_weekend(snapshots):
    calls: list[tuple[str, date]] = []

    def fetch(symbol: str, day: date) -> list[DukascopyBar] | None:
        calls.append((symbol, day))
        return fresh()

    summary = backfill_usfut(
        snapshots=snapshots,
        start=date(2026, 7, 13),
        end=date(2026, 7, 19),
        fetch=fetch,
        now=FETCHED,
        sleep=no_sleep,
        pause=0.0,
    )
    assert summary.status == "ok"
    assert summary.days == 5
    assert summary.loaded_days == 5
    assert summary.rows == 10
    assert date(2026, 7, 18) not in [day for _, day in calls]
    assert set(snapshots.read_date(US_FUTURES_1510, date(2026, 7, 13))["symbol"]) == {
        "US500",
        "USTEC",
    }


def test_backfill_resume_skips_complete_days(snapshots):
    snapshots.write_date(
        US_FUTURES_1510, date(2026, 7, 14), cfd_frame(date(2026, 7, 14), ["US500", "USTEC"])
    )
    calls: list[date] = []

    def fetch(symbol: str, day: date) -> list[DukascopyBar] | None:
        calls.append(day)
        return fresh()

    summary = backfill_usfut(
        snapshots=snapshots,
        start=date(2026, 7, 13),
        end=date(2026, 7, 16),
        fetch=fetch,
        sleep=no_sleep,
        pause=0.0,
    )
    assert summary.skipped_days == 1
    assert date(2026, 7, 14) not in calls


def test_backfill_records_stale_fallback(snapshots):
    def fetch(symbol: str, day: date) -> list[DukascopyBar] | None:
        return stale_target(7499.0)

    backfill_usfut(
        snapshots=snapshots,
        start=date(2026, 7, 13),
        end=date(2026, 7, 13),
        fetch=fetch,
        sleep=no_sleep,
        pause=0.0,
    )
    frame = snapshots.read_date(US_FUTURES_1510, date(2026, 7, 13))
    assert frame["stale_minutes"].to_list() == [1, 1]
    assert frame["price"].to_list() == [7499.0, 7499.0]


def test_backfill_stale_window_writes_no_row(snapshots):
    def fetch(symbol: str, day: date) -> list[DukascopyBar] | None:
        return frozen_window()

    summary = backfill_usfut(
        snapshots=snapshots,
        start=date(2026, 7, 13),
        end=date(2026, 7, 13),
        fetch=fetch,
        sleep=no_sleep,
        pause=0.0,
    )
    assert summary.stale_days == 1
    assert summary.loaded_days == 0
    assert not snapshots.has_date(US_FUTURES_1510, date(2026, 7, 13))


def test_backfill_counts_unavailable_days(snapshots):
    def fetch(symbol: str, day: date) -> list[DukascopyBar] | None:
        return None

    summary = backfill_usfut(
        snapshots=snapshots,
        start=date(2026, 7, 13),
        end=date(2026, 7, 15),
        fetch=fetch,
        sleep=no_sleep,
        pause=0.0,
    )
    assert summary.unavailable_days == 3
    assert summary.loaded_days == 0
    assert summary.long_gaps == []


def test_backfill_records_long_404_gap(snapshots):
    def fetch(symbol: str, day: date) -> list[DukascopyBar] | None:
        return None

    summary = backfill_usfut(
        snapshots=snapshots,
        start=date(2026, 7, 1),
        end=date(2026, 7, 31),
        fetch=fetch,
        sleep=no_sleep,
        pause=0.0,
    )
    assert len(summary.long_gaps) == 1
    assert summary.long_gaps[0].endswith("(23d)")


def test_backfill_partial_on_source_error(snapshots):
    def fetch(symbol: str, day: date) -> list[DukascopyBar] | None:
        if symbol == "USTEC" and day == date(2026, 7, 14):
            raise SourceError("boom")
        return fresh()

    summary = backfill_usfut(
        snapshots=snapshots,
        start=date(2026, 7, 13),
        end=date(2026, 7, 15),
        fetch=fetch,
        sleep=no_sleep,
        pause=0.0,
    )
    assert summary.status == "partial"
    assert summary.failed == ["2026-07-14 USTEC"]
    assert snapshots.read_date(US_FUTURES_1510, date(2026, 7, 14))["symbol"].to_list() == ["US500"]


def test_daily_self_heals_and_skips_not_ready(snapshots):
    def fetch(symbol: str, day: date) -> list[DukascopyBar] | None:
        if day == date(2026, 7, 17):
            return None
        return fresh()

    result = daily_usfut(
        snapshots=snapshots,
        now=datetime(2026, 7, 17, 7, 0, tzinfo=UTC),
        fetch=fetch,
        sleep=no_sleep,
        pause=0.0,
    )
    assert result == "6/7 days, not-ready 1"


def test_daily_up_to_date(snapshots):
    for day in (
        date(2026, 7, 9),
        date(2026, 7, 10),
        date(2026, 7, 13),
        date(2026, 7, 14),
        date(2026, 7, 15),
        date(2026, 7, 16),
        date(2026, 7, 17),
    ):
        snapshots.write_date(US_FUTURES_1510, day, cfd_frame(day, ["US500", "USTEC"]))

    def fetch(symbol: str, day: date) -> list[DukascopyBar] | None:
        raise AssertionError("unexpected fetch")

    result = daily_usfut(
        snapshots=snapshots,
        now=datetime(2026, 7, 17, 7, 0, tzinfo=UTC),
        fetch=fetch,
        pause=0.0,
    )
    assert result == "up-to-date"


def _seed_sessions(snapshots, days: list[date], price: float = 7500.0) -> None:
    for day in days:
        snapshots.write_date(US_FUTURES_1510, day, cfd_frame(day, ["US500", "USTEC"], price=price))


def test_verify_clean(snapshots, series, cal):
    days = [date(2026, 7, 13), date(2026, 7, 14), date(2026, 7, 15), date(2026, 7, 16)]
    _seed_sessions(snapshots, days)
    us_daily(series, "^GSPC", [date(2026, 7, 10), *days], 7500.0)
    us_daily(series, "^IXIC", [date(2026, 7, 10), *days], 7500.0)
    report = verify_usfut(snapshots=snapshots, series=series, cal=cal)
    assert report.status == "ok"
    assert report.symbols == {"US500": 4, "USTEC": 4}
    assert report.missing_sessions == []
    assert report.level_checked == 8
    assert report.level_violations == 0
    assert report.duplicate_keys == 0
    assert report.bar_ts_violations == 0
    assert report.stale_distribution == {"0": 8}


def test_verify_flags_unexpected_missing_session(snapshots, series, cal):
    _seed_sessions(snapshots, [date(2026, 7, 13), date(2026, 7, 15)])
    report = verify_usfut(snapshots=snapshots, series=series, cal=cal)
    assert report.status == "issues"
    assert "2026-07-14" in report.missing_sessions


def test_verify_separates_known_holiday_gap(snapshots, series, cal):
    _seed_sessions(snapshots, [date(2026, 4, 2), date(2026, 4, 6)])
    report = verify_usfut(snapshots=snapshots, series=series, cal=cal)
    assert report.status == "ok"
    assert report.known_holiday_gaps == ["2026-04-03"]
    assert report.missing_sessions == []


def test_verify_flags_level_band_violation(snapshots, series, cal):
    days = [date(2026, 7, 13), date(2026, 7, 14)]
    _seed_sessions(snapshots, days, price=7500.0)
    us_daily(series, "^GSPC", [date(2026, 7, 10), *days], 5000.0)
    report = verify_usfut(snapshots=snapshots, series=series, cal=cal)
    assert report.status == "issues"
    assert report.level_violations == 2
    assert report.examples


def test_verify_reports_stale_distribution(snapshots, series, cal):
    snapshots.write_date(
        US_FUTURES_1510,
        date(2026, 7, 13),
        cfd_frame(date(2026, 7, 13), ["US500", "USTEC"], stale=0),
    )
    snapshots.write_date(
        US_FUTURES_1510,
        date(2026, 7, 14),
        cfd_frame(date(2026, 7, 14), ["US500", "USTEC"], stale=2),
    )
    report = verify_usfut(snapshots=snapshots, series=series, cal=cal)
    assert report.stale_distribution == {"0": 2, "2": 2}


def test_verify_empty(snapshots, series, cal):
    report = verify_usfut(snapshots=snapshots, series=series, cal=cal)
    assert report.status == "empty"
