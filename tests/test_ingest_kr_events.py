from datetime import UTC, date, datetime

import polars as pl

from talon.data.store import (
    KR_EVENTS,
    KR_EVENTS_HISTORY,
    KR_EVENTS_HISTORY_NAME,
    KR_EVENTS_HISTORY_SCHEMA,
    KR_EVENTS_SCHEMA,
)
from talon.ingest.kr_events import backfill_kr_events, daily_kr_events, verify_kr_events
from talon.markets.kr import KrxCalendar

NOW = datetime(2026, 8, 20, 6, 10, tzinfo=UTC)
TODAY = date(2026, 8, 20)


def run_backfill(cfg, cal, state, snapshots, series, today=TODAY):
    return backfill_kr_events(
        cfg,
        cal=cal,
        state=state,
        snapshots=snapshots,
        series=series,
        today=today,
        now=lambda: NOW,
    )


def test_backfill_writes_forward_snapshot_and_confirmed_history(
    cfg, cal, state, snapshots, series
):
    summary = run_backfill(cfg, cal, state, snapshots, series)

    assert summary.status == "ok"
    assert summary.day == TODAY
    assert summary.snapshot_rows > 0
    assert summary.history_rows > 300

    snap = snapshots.read_date(KR_EVENTS, TODAY)
    assert snap["event_day"].min() >= TODAY
    assert set(snap["source"].to_list()) == {"rule"}

    hist = series.read(KR_EVENTS_HISTORY, KR_EVENTS_HISTORY_NAME)
    assert hist["event_day"].max() <= TODAY
    hist_keys = set(hist["event_key"].to_list())
    assert "expiry_witching:2025-12" in hist_keys
    assert "ex_dividend_yearend:2024" in hist_keys
    assert "rebalance_k200:2020-12" in hist_keys


def test_backfill_is_idempotent(cfg, cal, state, snapshots, series):
    run_backfill(cfg, cal, state, snapshots, series)
    first = series.read(KR_EVENTS_HISTORY, KR_EVENTS_HISTORY_NAME).height
    snapshot_first = snapshots.read_date(KR_EVENTS, TODAY).height

    summary = run_backfill(cfg, cal, state, snapshots, series)

    assert series.read(KR_EVENTS_HISTORY, KR_EVENTS_HISTORY_NAME).height == first
    assert snapshots.read_date(KR_EVENTS, TODAY).height == snapshot_first
    assert summary.history_rows == 0


def test_today_event_belongs_to_both_snapshot_and_history(cfg, cal, snapshots, series):
    day = date(2026, 6, 11)
    daily_kr_events(
        cfg,
        cal=cal,
        snapshots=snapshots,
        series=series,
        today=day,
        now=lambda: datetime(2026, 6, 11, 6, 10, tzinfo=UTC),
    )

    snap = snapshots.read_date(KR_EVENTS, day)
    snapshot_witch = snap.filter(
        (pl.col("category") == "expiry_witching") & (pl.col("event_day") == day)
    )
    assert snapshot_witch.height == 1

    hist = series.read(KR_EVENTS_HISTORY, KR_EVENTS_HISTORY_NAME)
    history_witch = hist.filter(pl.col("event_key") == "expiry_witching:2026-06")
    assert history_witch["event_day"].to_list() == [day]


def test_event_key_self_heals_when_closure_shifts_the_date(cfg, snapshots, series):
    clean = KrxCalendar(closures={})
    daily_kr_events(
        cfg,
        cal=clean,
        snapshots=snapshots,
        series=series,
        today=TODAY,
        now=lambda: NOW,
    )
    hist = series.read(KR_EVENTS_HISTORY, KR_EVENTS_HISTORY_NAME)
    before = hist.height
    original = hist.filter(pl.col("event_key") == "expiry_option:2026-08")
    assert original["event_day"].to_list() == [date(2026, 8, 13)]

    shifted = KrxCalendar(closures={date(2026, 8, 13): "임시휴장"})
    daily_kr_events(
        cfg,
        cal=shifted,
        snapshots=snapshots,
        series=series,
        today=TODAY,
        now=lambda: NOW,
    )
    healed = series.read(KR_EVENTS_HISTORY, KR_EVENTS_HISTORY_NAME)
    moved = healed.filter(pl.col("event_key") == "expiry_option:2026-08")
    assert moved["event_day"].to_list() == [date(2026, 8, 12)]
    assert healed.height == before


def test_snapshot_replaces_shifted_forward_event_on_reprocess(cfg, snapshots, series):
    capture = date(2026, 8, 1)
    clean = KrxCalendar(closures={})
    daily_kr_events(
        cfg,
        cal=clean,
        snapshots=snapshots,
        series=series,
        today=capture,
        now=lambda: datetime(2026, 8, 1, 6, 10, tzinfo=UTC),
    )
    first = snapshots.read_date(KR_EVENTS, capture)
    first_option = first.filter(pl.col("category") == "expiry_option")["event_day"].to_list()
    assert date(2026, 8, 13) in first_option

    shifted = KrxCalendar(closures={date(2026, 8, 13): "임시휴장"})
    daily_kr_events(
        cfg,
        cal=shifted,
        snapshots=snapshots,
        series=series,
        today=capture,
        now=lambda: datetime(2026, 8, 1, 6, 10, tzinfo=UTC),
    )
    healed = snapshots.read_date(KR_EVENTS, capture)
    healed_option = healed.filter(pl.col("category") == "expiry_option")["event_day"].to_list()
    assert date(2026, 8, 13) not in healed_option
    assert date(2026, 8, 12) in healed_option
    assert healed.height == first.height


def test_future_day_is_clamped_to_wall_clock(cfg, cal, snapshots, series):
    summary = daily_kr_events(
        cfg,
        cal=cal,
        snapshots=snapshots,
        series=series,
        today=date(2026, 10, 15),
        now=lambda: NOW,
    )
    assert summary.day == TODAY
    hist = series.read(KR_EVENTS_HISTORY, KR_EVENTS_HISTORY_NAME)
    assert hist["event_day"].max() <= TODAY
    assert snapshots.read_date(KR_EVENTS, date(2026, 10, 15)) is None


def test_verify_reports_ok_after_backfill(cfg, cal, state, snapshots, series):
    run_backfill(cfg, cal, state, snapshots, series)
    report = verify_kr_events(cfg, cal=cal, snapshots=snapshots, series=series)
    assert report.status == "ok"
    assert report.counts["expiry_option"] > 100
    assert "rows" in report.history
    assert "rows" in report.snapshot


def test_verify_empty_store_reports_empty_status(cfg, cal, snapshots, series):
    report = verify_kr_events(cfg, cal=cal, snapshots=snapshots, series=series)
    assert report.status == "empty"
    assert report.history == "empty"
    assert report.snapshot == "empty"


def test_verify_flags_stale_forward_snapshot(cfg, cal, snapshots, series):
    frame = pl.DataFrame(
        [
            {
                "day": TODAY,
                "event_day": date(2026, 8, 13),
                "category": "expiry_option",
                "tier": "note",
                "source": "rule",
                "detail": "테스트",
                "captured_at": NOW,
            }
        ],
        schema=KR_EVENTS_SCHEMA,
    )
    snapshots.write_date(KR_EVENTS, TODAY, frame)
    report = verify_kr_events(cfg, cal=cal, snapshots=snapshots, series=series)
    assert report.status == "issues"
    assert "stale-forward" in report.snapshot


def test_verify_flags_non_session_history_event(cfg, cal, snapshots, series):
    frame = pl.DataFrame(
        [
            {
                "event_key": "expiry_option:2026-08",
                "event_day": date(2026, 8, 15),
                "category": "expiry_option",
                "tier": "note",
                "source": "rule",
                "detail": "테스트",
            }
        ],
        schema=KR_EVENTS_HISTORY_SCHEMA,
    )
    series.upsert(KR_EVENTS_HISTORY, KR_EVENTS_HISTORY_NAME, frame, key="event_key")
    report = verify_kr_events(cfg, cal=cal, snapshots=snapshots, series=series)
    assert report.status == "issues"
    assert "non-session" in report.history
