from datetime import date, timedelta

import polars as pl
import pytest

from conftest import make_candle, utc
from talon.data.store import (
    CANDLE_SCHEMA,
    DAILY_CANDLES,
    DAILY_SNAPSHOT_SCHEMA,
    INDICATOR_DAILY,
    INVESTOR_TRADING,
    MARKET_CAP,
    MINUTE_CANDLES,
)
from talon.errors import SourceError
from talon.ingest.eod import run_eod
from talon.models import CandlePage, InvestorFlowRecord
from talon.sources.crosscheck import CrosscheckResult, Discrepancy

DAY = date(2026, 7, 10)
SATURDAY = date(2026, 7, 11)


def ohlcv_frame():
    return pl.DataFrame(
        {
            "day": [DAY, DAY],
            "symbol": ["005930", "000660"],
            "open": [70000.0, 250000.0],
            "high": [71000.0, 255000.0],
            "low": [69000.0, 248000.0],
            "close": [70500.0, 252000.0],
            "volume": [1000.0, 2000.0],
            "value": [5e12, 3e12],
            "change_pct": [0.5, -0.3],
        },
        schema=DAILY_SNAPSHOT_SCHEMA,
    )


def caps_frame():
    return pl.DataFrame(
        {
            "day": [DAY, DAY],
            "symbol": ["005930", "000660"],
            "close": [70500.0, 252000.0],
            "cap": [4e14, 1.8e14],
            "volume": [1000.0, 2000.0],
            "value": [5e12, 3e12],
            "shares": [5.9e9, 7.3e8],
        }
    )


def investor_record():
    return InvestorFlowRecord.from_toss(
        {
            "date": DAY.isoformat(),
            "updatedAt": "2026-07-10T18:10:00+09:00",
            "individual": {"buyAmount": "1", "sellAmount": "2"},
            "foreigner": {"buyAmount": "3", "sellAmount": "4"},
            "institution": {"buyAmount": "5", "sellAmount": "6", "breakdown": {}},
            "otherCorporation": {"buyAmount": "7", "sellAmount": "8"},
        }
    )


def seed_minutes(series, cal, symbol, high, low, *, coverage=1.0):
    opened = cal.session_open(DAY)
    closed = cal.session_close(DAY)
    total = round((closed - opened).total_seconds() / 60) + 1
    mid = (high + low) / 2
    rows = [
        {
            "ts": opened + timedelta(minutes=index),
            "open": mid,
            "high": high if index == 0 else mid,
            "low": low if index == 0 else mid,
            "close": mid,
            "volume": 10.0,
        }
        for index in range(max(int(total * coverage), 1))
    ]
    series.upsert(MINUTE_CANDLES, symbol, pl.DataFrame(rows, schema=CANDLE_SCHEMA))


class FakeToss:
    def __init__(self):
        self.indicator_calls = []

    def candles(self, symbol, interval, *, count=200, before=None, adjusted=False):
        return CandlePage(candles=[], next_before=None)

    def candles_since(
        self, symbol, interval, since, *, max_pages=30, adjusted=False, indicator=False
    ):
        self.indicator_calls.append((symbol, interval, indicator))
        return [make_candle(utc(2026, 7, 9, 15, 0))]

    def investor_trading(self, symbol, **kwargs):
        return [investor_record()]

    def stocks(self, symbols):
        return []


def _blocked_listing(day):
    raise SourceError("krx listing blocked")


@pytest.fixture
def sources(monkeypatch):
    monkeypatch.setattr("talon.ingest.eod.fetch_daily_ohlcv", lambda day: ohlcv_frame())
    monkeypatch.setattr("talon.ingest.eod.fetch_market_cap", lambda day: caps_frame())
    monkeypatch.setattr("talon.ingest.eod.fetch_krx_listing", _blocked_listing)
    monkeypatch.setattr(
        "talon.ingest.eod.crosscheck_daily",
        lambda snapshot, day, sample, tolerance_pct: CrosscheckResult(checked=len(sample)),
    )
    monkeypatch.setattr("talon.ingest.universe.fetch_admin_issues", lambda: None)


def run(cfg, cal, state, snapshots, series, alerter, *, toss=None, day=DAY, force=False):
    return run_eod(
        cfg,
        cal=cal,
        state=state,
        snapshots=snapshots,
        series=series,
        toss=toss,
        alerter=alerter,
        today=day,
        force=force,
    )


def test_eod_happy_path(cfg, cal, state, snapshots, series, alerter, notifier, sources):
    toss = FakeToss()
    summary = run(cfg, cal, state, snapshots, series, alerter, toss=toss)
    assert summary.status == "ok"
    assert snapshots.has_date(DAILY_CANDLES, DAY)
    assert snapshots.has_date(MARKET_CAP, DAY)
    assert series.read(INDICATOR_DAILY, "KOSPI") is not None
    assert series.read(INVESTOR_TRADING, "KOSPI").height == 1
    assert state.latest_universe().symbols == ["005930", "000660"]
    assert summary.universe_size == 2
    assert state.get_heartbeat("eod").ok
    assert notifier.sent == []
    assert len(toss.indicator_calls) == len(cfg.indicator_daily_symbols)


def test_eod_idempotent(cfg, cal, state, snapshots, series, alerter, sources):
    run(cfg, cal, state, snapshots, series, alerter, toss=FakeToss())
    summary = run(cfg, cal, state, snapshots, series, alerter, toss=FakeToss())
    assert summary.status == "already-done"


def test_eod_skips_holiday(cfg, cal, state, snapshots, series, alerter, sources):
    summary = run(cfg, cal, state, snapshots, series, alerter, day=SATURDAY)
    assert summary.status == "skipped-holiday"


def test_eod_data_not_ready(cfg, cal, state, snapshots, series, alerter, notifier, monkeypatch):
    monkeypatch.setattr(
        "talon.ingest.eod.fetch_daily_ohlcv",
        lambda day: pl.DataFrame(schema=DAILY_SNAPSHOT_SCHEMA),
    )
    monkeypatch.setattr("talon.ingest.eod.fetch_krx_listing", _blocked_listing)
    summary = run(cfg, cal, state, snapshots, series, alerter)
    assert summary.status == "data-not-ready"
    assert not snapshots.has_date(DAILY_CANDLES, DAY)
    assert state.recent_runs("eod")[0].ok is False
    assert any("일봉 데이터" in text for text in notifier.sent)


def test_eod_all_sources_down(cfg, cal, state, snapshots, series, alerter, notifier, monkeypatch):
    def boom(day):
        raise SourceError("krx down")

    monkeypatch.setattr("talon.ingest.eod.fetch_daily_ohlcv", boom)
    monkeypatch.setattr("talon.ingest.eod.fetch_krx_listing", _blocked_listing)
    summary = run(cfg, cal, state, snapshots, series, alerter)
    assert summary.status == "data-not-ready"
    assert "error" in summary.steps["pykrx"]
    assert "error" in summary.steps["fdr_listing"]
    assert any("어느 소스에서도" in text for text in notifier.sent)


def test_eod_unexpected_error(cfg, cal, state, snapshots, series, alerter, notifier, monkeypatch):
    def boom(day):
        raise RuntimeError("bug")

    monkeypatch.setattr("talon.ingest.eod.fetch_daily_ohlcv", boom)
    summary = run(cfg, cal, state, snapshots, series, alerter)
    assert summary.status == "error"
    assert any("EOD 잡 실패" in text for text in notifier.sent)
    assert state.recent_runs("eod")[0].ok is False


@pytest.fixture
def listing_only(monkeypatch):
    monkeypatch.setattr(
        "talon.ingest.eod.fetch_daily_ohlcv",
        lambda day: pl.DataFrame(schema=DAILY_SNAPSHOT_SCHEMA),
    )
    monkeypatch.setattr(
        "talon.ingest.eod.fetch_krx_listing", lambda day: (ohlcv_frame(), caps_frame())
    )
    monkeypatch.setattr("talon.ingest.universe.fetch_admin_issues", lambda: None)


def test_eod_falls_back_to_fdr_listing(
    cfg, cal, state, snapshots, series, alerter, notifier, listing_only
):
    seed_minutes(series, cal, "005930", 71000.0, 69000.0)
    seed_minutes(series, cal, "000660", 255000.0, 248000.0)
    summary = run(cfg, cal, state, snapshots, series, alerter, toss=FakeToss())
    assert summary.status == "ok"
    assert "fdr-listing" in summary.steps["daily"]
    assert summary.steps["crosscheck"] == "skipped (fdr-listing)"
    assert snapshots.has_date(DAILY_CANDLES, DAY)
    assert snapshots.has_date(MARKET_CAP, DAY)
    assert state.latest_universe().symbols == ["005930", "000660"]
    assert any("FDR 전종목 스냅샷" in text for text in notifier.sent)
    assert not any("검증 불가" in text for text in notifier.sent)


def test_eod_fallback_rejected_on_minute_mismatch(
    cfg, cal, state, snapshots, series, alerter, notifier, listing_only
):
    seed_minutes(series, cal, "005930", 80000.0, 60000.0)
    summary = run(cfg, cal, state, snapshots, series, alerter, toss=FakeToss())
    assert summary.status == "data-not-ready"
    assert summary.steps["fdr_listing"] == "stale-or-mismatch"
    assert not snapshots.has_date(DAILY_CANDLES, DAY)


def test_eod_fallback_unverified_without_minutes(
    cfg, cal, state, snapshots, series, alerter, notifier, listing_only
):
    summary = run(cfg, cal, state, snapshots, series, alerter, toss=None)
    assert summary.status == "ok"
    assert snapshots.has_date(DAILY_CANDLES, DAY)
    assert any("검증 불가" in text for text in notifier.sent)


def test_eod_fallback_unverified_on_thin_minute_coverage(
    cfg, cal, state, snapshots, series, alerter, notifier, listing_only
):
    seed_minutes(series, cal, "005930", 80000.0, 60000.0, coverage=0.5)
    seed_minutes(series, cal, "000660", 300000.0, 200000.0, coverage=0.5)
    summary = run(cfg, cal, state, snapshots, series, alerter, toss=FakeToss())
    assert summary.status == "ok"
    assert snapshots.has_date(DAILY_CANDLES, DAY)
    assert any("검증 불가" in text for text in notifier.sent)


def test_eod_crosscheck_mismatch_alerts(
    cfg, cal, state, snapshots, series, alerter, notifier, sources, monkeypatch
):
    result = CrosscheckResult(
        checked=1,
        discrepancies=[Discrepancy(symbol="005930", field="close", ours=1.0, theirs=2.0)],
    )
    monkeypatch.setattr(
        "talon.ingest.eod.crosscheck_daily",
        lambda snapshot, day, sample, tolerance_pct: result,
    )
    summary = run(cfg, cal, state, snapshots, series, alerter, toss=FakeToss())
    assert summary.status == "ok"
    assert any("정합성 불일치" in text for text in notifier.sent)


def test_eod_without_toss_degrades_steps(cfg, cal, state, snapshots, series, alerter, sources):
    summary = run(cfg, cal, state, snapshots, series, alerter, toss=None)
    assert summary.status == "ok"
    assert summary.steps["indicators"] == "skipped-no-toss"
    assert summary.steps["investor"] == "skipped-no-toss"


def test_eod_marketcap_failure_falls_back(
    cfg, cal, state, snapshots, series, alerter, notifier, sources, monkeypatch
):
    def boom(day):
        raise SourceError("cap down")

    monkeypatch.setattr("talon.ingest.eod.fetch_market_cap", boom)
    summary = run(cfg, cal, state, snapshots, series, alerter, toss=FakeToss())
    assert summary.status == "ok"
    assert not snapshots.has_date(MARKET_CAP, DAY)
    assert state.latest_universe() is not None
    assert any("시가총액 수집 실패" in text for text in notifier.sent)
