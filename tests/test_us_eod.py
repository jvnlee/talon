from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest

from talon.data.store import US_DAILY, US_DAILY_SCHEMA, US_MACRO_DAILY, US_MACRO_DAILY_SCHEMA
from talon.errors import SourceError
from talon.ingest.us_eod import run_us_eod
from talon.markets.us import UsCalendar

NOW = datetime(2026, 7, 17, 22, 0, tzinfo=UTC)
EXPECTED_SESSION = date(2026, 7, 17)


@pytest.fixture(scope="module")
def uscal() -> UsCalendar:
    return UsCalendar()


def bars_frame(days: list[date], close: float = 100.0) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "day": days,
            "open": [close] * len(days),
            "high": [close * 1.1] * len(days),
            "low": [close * 0.9] * len(days),
            "close": [close] * len(days),
            "volume": [1e6] * len(days),
        },
        schema=US_DAILY_SCHEMA,
    )


def macro_frame(days: list[date], source: str, captured_at: datetime) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "day": days,
            "value": [float(index + 1) for index in range(len(days))],
            "source": [source] * len(days),
            "captured_at": [captured_at] * len(days),
        },
        schema=US_MACRO_DAILY_SCHEMA,
    )


def good_macro(series_id, captured_at, **kw):
    return macro_frame([date(2026, 7, 16), EXPECTED_SESSION], f"fred:{series_id}", captured_at)


def good_vix(captured_at, **kw):
    return macro_frame([date(2026, 7, 16), EXPECTED_SESSION], "cboe", captured_at)


def run(cfg, uscal, state, series, alerter, fetch, *, full=False, vix=good_vix, macro=good_macro):
    return run_us_eod(
        cfg,
        uscal=uscal,
        state=state,
        series=series,
        alerter=alerter,
        full=full,
        now=lambda: NOW,
        fetch_daily=fetch,
        fetch_macro_series=macro,
        fetch_vix=vix,
    )


def test_first_run_seeds_full_history(cfg, uscal, state, series, alerter):
    cfg.us_eod_symbols = ["AAA"]
    starts = []

    def fetch(symbol, *, start, **kw):
        starts.append(start)
        return bars_frame([date(2026, 7, 15), date(2026, 7, 16), EXPECTED_SESSION])

    summary = run(cfg, uscal, state, series, alerter, fetch)

    assert summary.status == "ok"
    assert summary.seeded == 1
    assert starts == [cfg.us_backfill_start]
    assert series.read(US_DAILY, "AAA").height == 3


def test_incremental_run_upserts_new_rows(cfg, uscal, state, series, alerter):
    cfg.us_eod_symbols = ["AAA"]
    stored = bars_frame(
        [date(2026, 7, 13), date(2026, 7, 14), date(2026, 7, 15), date(2026, 7, 16)]
    )
    series.replace(US_DAILY, "AAA", stored)
    starts = []

    def fetch(symbol, *, start, **kw):
        starts.append(start)
        return bars_frame(
            [date(2026, 7, 13), date(2026, 7, 14), date(2026, 7, 15), date(2026, 7, 16),
             EXPECTED_SESSION]
        )

    summary = run(cfg, uscal, state, series, alerter, fetch)

    assert summary.status == "ok"
    assert summary.updated == 1
    assert summary.reseeded == 0
    assert starts == [date(2026, 7, 16) - timedelta(days=cfg.us_eod_overlap_days)]
    assert series.read(US_DAILY, "AAA").height == 5


def test_restated_history_triggers_full_reseed(cfg, uscal, state, series, alerter):
    cfg.us_eod_symbols = ["AAA"]
    series.replace(US_DAILY, "AAA", bars_frame([date(2026, 7, 15), date(2026, 7, 16)], close=100.0))
    starts = []

    def fetch(symbol, *, start, **kw):
        starts.append(start)
        if len(starts) == 1:
            return bars_frame([date(2026, 7, 15), date(2026, 7, 16), EXPECTED_SESSION], close=50.0)
        return bars_frame(
            [date(2026, 7, 10), date(2026, 7, 15), date(2026, 7, 16), EXPECTED_SESSION], close=50.0
        )

    summary = run(cfg, uscal, state, series, alerter, fetch)

    assert summary.status == "ok"
    assert summary.reseeded == 1
    assert starts[1] == cfg.us_backfill_start
    stored = series.read(US_DAILY, "AAA")
    assert stored.height == 4
    assert set(stored["close"].to_list()) == {50.0}


def test_missing_expected_session_is_reported_stale(cfg, uscal, state, series, alerter, notifier):
    cfg.us_eod_symbols = ["AAA"]

    def fetch(symbol, *, start, **kw):
        return bars_frame([date(2026, 7, 15), date(2026, 7, 16)])

    summary = run(cfg, uscal, state, series, alerter, fetch)

    assert summary.status == "partial"
    assert summary.stale == ["AAA"]
    assert any("기대 세션" in sent for sent in notifier.sent)


def test_vix_falls_back_to_fred(cfg, uscal, state, series, alerter):
    cfg.us_eod_symbols = []
    requested = []

    def broken_vix(captured_at, **kw):
        raise SourceError("cboe 404")

    def macro(series_id, captured_at, **kw):
        requested.append(series_id)
        return good_macro(series_id, captured_at)

    summary = run(cfg, uscal, state, series, alerter, lambda *a, **k: bars_frame([]),
                  vix=broken_vix, macro=macro)

    assert summary.macro["VIX"] == "ok"
    assert "VIXCLS" in requested
    assert series.read(US_MACRO_DAILY, "VIX")["source"].to_list()[0] == "fred:VIXCLS"


def test_total_failure_alerts(cfg, uscal, state, series, alerter, notifier):
    cfg.us_eod_symbols = ["AAA", "BBB"]

    def fetch(symbol, *, start, **kw):
        raise SourceError("yahoo down")

    def broken_macro(series_id, captured_at, **kw):
        raise SourceError("fred down")

    def broken_vix(captured_at, **kw):
        raise SourceError("cboe down")

    summary = run(cfg, uscal, state, series, alerter, fetch, vix=broken_vix, macro=broken_macro)

    assert summary.status == "error"
    assert state.get_heartbeat("us-eod").ok is False
    assert any("전부 실패" in sent for sent in notifier.sent)


def test_stale_macro_series_marks_partial(cfg, uscal, state, series, alerter):
    cfg.us_eod_symbols = []

    def stale_macro(series_id, captured_at, **kw):
        return macro_frame([date(2026, 6, 30)], f"fred:{series_id}", captured_at)

    def stale_vix(captured_at, **kw):
        return macro_frame([date(2026, 6, 30)], "cboe", captured_at)

    summary = run(cfg, uscal, state, series, alerter, lambda *a, **k: bars_frame([]),
                  vix=stale_vix, macro=stale_macro)

    assert summary.status == "partial"
    assert summary.macro["VIX"].startswith("stale")
