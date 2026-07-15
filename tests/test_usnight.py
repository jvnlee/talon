from datetime import UTC, date, datetime

import polars as pl

from talon.data.store import CANDLE_SCHEMA, US_DAILY, US_DAILY_SCHEMA, US_MINUTE
from talon.errors import SourceError
from talon.ingest.usnight import run_us_night


def daily_frame(days=2):
    return pl.DataFrame(
        {
            "day": [date(2026, 7, 13 + i) for i in range(days)],
            "open": [100.0] * days,
            "high": [110.0] * days,
            "low": [90.0] * days,
            "close": [105.0] * days,
            "volume": [1e6] * days,
        },
        schema=US_DAILY_SCHEMA,
    )


def minute_frame(rows=3):
    return pl.DataFrame(
        {
            "ts": [datetime(2026, 7, 14, 13, 30 + i, tzinfo=UTC) for i in range(rows)],
            "open": [100.0] * rows,
            "high": [101.0] * rows,
            "low": [99.0] * rows,
            "close": [100.5] * rows,
            "volume": [500.0] * rows,
        },
        schema=CANDLE_SCHEMA,
    )


def patch_ok(monkeypatch):
    monkeypatch.setattr(
        "talon.ingest.usnight.fetch_daily_bars", lambda symbol, **kw: daily_frame()
    )
    monkeypatch.setattr(
        "talon.ingest.usnight.fetch_minute_bars", lambda symbol, **kw: minute_frame()
    )


def test_stores_daily_and_minute_bars(monkeypatch, cfg, state, series, alerter):
    patch_ok(monkeypatch)

    summary = run_us_night(cfg, state=state, series=series, alerter=alerter, symbols=["SKHY"])

    assert summary.status == "ok"
    assert summary.daily_rows == 2
    assert summary.minute_rows == 3
    assert series.read(US_DAILY, "SKHY").height == 2
    assert series.read(US_MINUTE, "SKHY").height == 3


def test_reruns_are_idempotent(monkeypatch, cfg, state, series, alerter):
    patch_ok(monkeypatch)
    run_us_night(cfg, state=state, series=series, alerter=alerter, symbols=["SKHY"])

    summary = run_us_night(cfg, state=state, series=series, alerter=alerter, symbols=["SKHY"])

    assert summary.daily_rows == 0
    assert series.read(US_DAILY, "SKHY").height == 2


def test_partial_failure_keeps_the_rest(monkeypatch, cfg, state, series, alerter, notifier):
    def flaky_daily(symbol, **kw):
        if symbol == "GONE":
            raise SourceError("delisted")
        return daily_frame()

    monkeypatch.setattr("talon.ingest.usnight.fetch_daily_bars", flaky_daily)
    monkeypatch.setattr(
        "talon.ingest.usnight.fetch_minute_bars", lambda symbol, **kw: minute_frame()
    )

    summary = run_us_night(
        cfg, state=state, series=series, alerter=alerter, symbols=["SKHY", "GONE"]
    )

    assert summary.status == "partial"
    assert summary.failed == ["GONE"]
    assert series.read(US_DAILY, "SKHY") is not None
    assert series.read(US_DAILY, "GONE") is None
    assert any("GONE" in sent for sent in notifier.sent)


def test_total_failure_alerts(monkeypatch, cfg, state, series, alerter, notifier):
    def boom(symbol, **kw):
        raise SourceError("yahoo down")

    monkeypatch.setattr("talon.ingest.usnight.fetch_daily_bars", boom)

    summary = run_us_night(cfg, state=state, series=series, alerter=alerter, symbols=["SKHY"])

    assert summary.status == "error"
    assert state.get_heartbeat("us-night").ok is False
    assert any("전부 실패" in sent for sent in notifier.sent)


def test_empty_symbol_is_counted_as_failed(monkeypatch, cfg, state, series, alerter):
    monkeypatch.setattr(
        "talon.ingest.usnight.fetch_daily_bars",
        lambda symbol, **kw: pl.DataFrame(schema=US_DAILY_SCHEMA),
    )
    monkeypatch.setattr(
        "talon.ingest.usnight.fetch_minute_bars",
        lambda symbol, **kw: pl.DataFrame(schema=CANDLE_SCHEMA),
    )

    summary = run_us_night(cfg, state=state, series=series, alerter=alerter, symbols=["SKHY"])

    assert summary.status == "error"
    assert summary.failed == ["SKHY"]


def test_default_symbols_come_from_config(monkeypatch, cfg, state, series, alerter):
    seen = []

    def record(symbol, **kw):
        seen.append(symbol)
        return daily_frame()

    monkeypatch.setattr("talon.ingest.usnight.fetch_daily_bars", record)
    monkeypatch.setattr(
        "talon.ingest.usnight.fetch_minute_bars", lambda symbol, **kw: minute_frame()
    )

    run_us_night(cfg, state=state, series=series, alerter=alerter)

    assert seen == list(cfg.us_night_symbols)
    assert "SKHY" in seen
    assert "EWY" in seen
