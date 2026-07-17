from datetime import UTC, date, datetime

import polars as pl

from talon.data.store import MACRO_INTRADAY
from talon.errors import SourceError
from talon.ingest.briefing import run_briefing_snapshot
from talon.sources.yahoo import YahooQuote

NOW = datetime(2026, 7, 15, 22, 30, tzinfo=UTC)
TODAY = date(2026, 7, 16)


def test_stores_morning_macro_snapshot(monkeypatch, cfg, cal, state, snapshots, alerter):
    monkeypatch.setattr(
        "talon.ingest.pulse.fetch_quote", lambda symbol, **kw: YahooQuote(100.0, 99.0)
    )

    summary = run_briefing_snapshot(
        cfg, cal=cal, state=state, snapshots=snapshots, alerter=alerter,
        today=TODAY, now=lambda: NOW,
    )

    assert summary.status == "ok"
    assert summary.rows["macro"] == 3
    frame = snapshots.read_date(MACRO_INTRADAY, TODAY)
    slot_rows = frame.filter(pl.col("slot") == "07:30")
    assert set(slot_rows["series"].to_list()) == {"USDKRW", "ES_F", "NQ_F"}


def test_skips_kr_holiday(monkeypatch, cfg, cal, state, snapshots, alerter):
    summary = run_briefing_snapshot(
        cfg, cal=cal, state=state, snapshots=snapshots, alerter=alerter,
        today=date(2026, 7, 17), now=lambda: NOW,
    )

    assert summary.status == "skipped-holiday"
    assert snapshots.read_date(MACRO_INTRADAY, date(2026, 7, 17)) is None


def test_total_quote_failure_alerts(monkeypatch, cfg, cal, state, snapshots, alerter, notifier):
    def boom(symbol, **kw):
        raise SourceError("yahoo down")

    monkeypatch.setattr("talon.ingest.pulse.fetch_quote", boom)

    summary = run_briefing_snapshot(
        cfg, cal=cal, state=state, snapshots=snapshots, alerter=alerter,
        today=TODAY, now=lambda: NOW,
    )

    assert summary.status == "error"
    assert state.get_heartbeat("briefing-snapshot").ok is False
    assert any("07:30" in sent for sent in notifier.sent)
