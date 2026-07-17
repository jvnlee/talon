from datetime import UTC, date, datetime

import polars as pl
import pytest

from talon.data.store import US_EARNINGS, US_EVENTS, US_EVENTS_HISTORY, US_EVENTS_HISTORY_NAME
from talon.ingest.us_calendar import hold_window, ism_days, run_us_calendar
from talon.markets.us import UsCalendar

NOW = datetime(2026, 7, 16, 21, 0, tzinfo=UTC)
TODAY = date(2026, 7, 16)


@pytest.fixture(scope="module")
def uscal() -> UsCalendar:
    return UsCalendar()


def fake_release_dates(release_id, api_key, *, start, end, **kw):
    data = {
        10: [date(2016, 2, 10), date(2026, 7, 17), date(2026, 8, 12)],
        50: [date(2026, 8, 7)],
    }
    return [day for day in data.get(release_id, []) if start <= day <= end]


def fake_fomc_calendar(**kw):
    return {date(2026, 7, 29), date(2026, 9, 16)}


def fake_fomc_history(year, **kw):
    return {date(year, 1, 27)}


def fake_earnings(day, **kw):
    if day == date(2026, 7, 22):
        return [{"symbol": "TSLA", "when": "amc"}, {"symbol": "ZZZZ", "when": "bmo"}]
    return []


def run(cfg, cal, uscal, state, snapshots, series, alerter, *, backfill=False):
    return run_us_calendar(
        cfg,
        cal=cal,
        uscal=uscal,
        state=state,
        snapshots=snapshots,
        series=series,
        alerter=alerter,
        today=TODAY,
        backfill=backfill,
        now=lambda: NOW,
        fetch_release_dates=fake_release_dates,
        fetch_fomc_calendar=fake_fomc_calendar,
        fetch_fomc_history=fake_fomc_history,
        fetch_earnings=fake_earnings,
        sleep=lambda seconds: None,
    )


def test_hold_window_spans_kr_closure_and_weekend(cal):
    start, end, decision = hold_window(cal, TODAY)

    assert decision == TODAY
    assert start == cal.session_close(TODAY)
    assert end == cal.session_open(date(2026, 7, 20))


def test_hold_window_from_weekend_uses_next_kr_session(cal):
    _start, end, decision = hold_window(cal, date(2026, 7, 18))

    assert decision == date(2026, 7, 20)
    assert end == cal.session_open(date(2026, 7, 21))


def test_ism_days_first_and_third_us_sessions(uscal):
    days = ism_days(uscal, date(2026, 8, 1), date(2026, 9, 30))

    assert (date(2026, 8, 3), "ism_mfg") in days
    assert (date(2026, 8, 5), "ism_svc") in days
    assert (date(2026, 9, 1), "ism_mfg") in days


def test_forward_snapshot_flags_hold_window(
    cfg, cal, uscal, state, snapshots, series, alerter
):
    cfg.fred_api_key = "test-key"
    cfg.us_events_forward_days = 70

    summary = run(cfg, cal, uscal, state, snapshots, series, alerter)

    assert summary.status == "ok"
    frame = snapshots.read_date(US_EVENTS, TODAY)
    cpi = frame.filter(pl.col("category") == "cpi").sort("event_day")
    assert cpi["event_day"].to_list() == [date(2026, 7, 17), date(2026, 8, 12)]
    assert cpi["in_hold_window"].to_list() == [True, False]
    assert cpi["tier"].to_list() == ["skip", "skip"]
    fomc = frame.filter(pl.col("category") == "fomc")
    assert date(2026, 7, 29) in fomc["event_day"].to_list()
    market = frame.filter(pl.col("category") == "holiday")
    assert date(2026, 9, 7) in market["event_day"].to_list()
    witching = frame.filter(pl.col("category") == "witching")
    assert witching["event_day"].to_list() == [date(2026, 9, 18)]


def test_earnings_snapshot_filters_watchlist(
    cfg, cal, uscal, state, snapshots, series, alerter
):
    cfg.fred_api_key = "test-key"

    summary = run(cfg, cal, uscal, state, snapshots, series, alerter)

    assert summary.earnings == 1
    frame = snapshots.read_date(US_EARNINGS, TODAY)
    assert frame["symbol"].to_list() == ["TSLA"]
    assert frame["when"].to_list() == ["amc"]
    assert frame["in_hold_window"].to_list() == [False]
    assert frame["source"].to_list() == ["nasdaq"]


def test_missing_fred_key_degrades_to_partial(
    cfg, cal, uscal, state, snapshots, series, alerter, notifier
):
    cfg.fred_api_key = ""

    summary = run(cfg, cal, uscal, state, snapshots, series, alerter)

    assert summary.status == "partial"
    assert summary.parts["fred"] == "skipped-no-key"
    assert any("FRED API 키" in sent for sent in notifier.sent)


def test_backfill_writes_history(cfg, cal, uscal, state, snapshots, series, alerter):
    cfg.fred_api_key = "test-key"

    summary = run(cfg, cal, uscal, state, snapshots, series, alerter, backfill=True)

    assert summary.history_rows > 400
    frame = series.read(US_EVENTS_HISTORY, US_EVENTS_HISTORY_NAME)
    keys = set(frame["event_key"].to_list())
    assert "cpi:2016-02-10" in keys
    assert "fomc:2016-01-27" in keys
    assert "fomc:2020-01-27" in keys
    categories = set(frame["category"].to_list())
    assert {"holiday", "witching", "ism_mfg", "ism_svc"} <= categories


def test_backfill_is_idempotent(cfg, cal, uscal, state, snapshots, series, alerter):
    cfg.fred_api_key = "test-key"

    run(cfg, cal, uscal, state, snapshots, series, alerter, backfill=True)
    first = series.read(US_EVENTS_HISTORY, US_EVENTS_HISTORY_NAME).height
    summary = run(cfg, cal, uscal, state, snapshots, series, alerter, backfill=True)

    assert series.read(US_EVENTS_HISTORY, US_EVENTS_HISTORY_NAME).height == first
    assert summary.history_rows == 0
