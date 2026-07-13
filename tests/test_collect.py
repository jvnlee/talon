from datetime import date

import polars as pl
import pytest

from conftest import make_candle, utc, write_stock_info
from talon.data.store import INDICATOR_MINUTE, MINUTE_CANDLES
from talon.errors import SourceError
from talon.ingest.collect import run_collect
from talon.sources.toss import TossError

NOW = utc(2026, 7, 10, 5, 0)
SATURDAY = utc(2026, 7, 11, 5, 0)


class FakeToss:
    def __init__(self, data=None, fail=(), auth_fail=False, stock_infos=()):
        self.data = data or {}
        self.fail = set(fail)
        self.auth_fail = auth_fail
        self.stock_infos = list(stock_infos)
        self.since_calls = []

    def candles_since(
        self, symbol, interval, since, *, max_pages=30, adjusted=False, indicator=False
    ):
        if self.auth_fail:
            raise TossError(401, "unauthorized", "expired")
        if symbol in self.fail:
            raise TossError(500, "internal", "boom")
        self.since_calls.append((symbol, interval, since, indicator))
        candles = self.data.get(symbol, [])
        if since is not None:
            candles = [c for c in candles if c.ts > since]
        return list(candles)

    def stocks(self, symbols):
        return list(self.stock_infos)


@pytest.fixture
def seeded_state(state):
    state.save_universe(date(2026, 7, 9), ["005930", "000660"], {})
    return state


def default_data():
    return {
        "005930": [make_candle(utc(2026, 7, 10, 4, 58)), make_candle(utc(2026, 7, 10, 4, 59))],
        "000660": [make_candle(utc(2026, 7, 10, 4, 59))],
        "KOSPI": [make_candle(utc(2026, 7, 10, 4, 59))],
        "KOSDAQ": [make_candle(utc(2026, 7, 10, 4, 59))],
    }


def test_collect_happy_path(cfg, cal, seeded_state, series, snapshots, alerter, notifier):
    client = FakeToss(default_data())
    summary = run_collect(
        cfg,
        cal=cal,
        state=seeded_state,
        store=series,
        snapshots=snapshots,
        client=client,
        alerter=alerter,
        now=NOW,
    )
    assert summary.status == "ok"
    assert summary.symbols == 2
    assert summary.rows == 3
    assert summary.indicator_rows == 2
    assert series.last_value(MINUTE_CANDLES, "005930") == utc(2026, 7, 10, 4, 59)
    assert series.names(INDICATOR_MINUTE) == ["KOSDAQ", "KOSPI"]
    beat = seeded_state.get_heartbeat("collect")
    assert beat.ok
    runs = seeded_state.recent_runs("collect")
    assert runs[0].ok
    assert notifier.sent == []


def test_collect_drops_forming_minute(cfg, cal, seeded_state, series, snapshots, alerter):
    data = default_data()
    data["005930"].append(make_candle(utc(2026, 7, 10, 5, 0), price=1.0))
    client = FakeToss(data)
    run_collect(
        cfg,
        cal=cal,
        state=seeded_state,
        store=series,
        snapshots=snapshots,
        client=client,
        alerter=alerter,
        now=NOW,
    )
    assert series.last_value(MINUTE_CANDLES, "005930") == utc(2026, 7, 10, 4, 59)


def test_collect_incremental_since(cfg, cal, seeded_state, series, snapshots, alerter):
    client = FakeToss(default_data())
    run_collect(
        cfg,
        cal=cal,
        state=seeded_state,
        store=series,
        snapshots=snapshots,
        client=client,
        alerter=alerter,
        now=NOW,
    )
    second = run_collect(
        cfg,
        cal=cal,
        state=seeded_state,
        store=series,
        snapshots=snapshots,
        client=client,
        alerter=alerter,
        now=NOW,
    )
    assert second.rows == 0
    since_for_005930 = [c[2] for c in client.since_calls if c[0] == "005930"]
    assert since_for_005930[1] == utc(2026, 7, 10, 4, 59)


def test_collect_skipped_when_closed(cfg, cal, seeded_state, series, snapshots, alerter):
    client = FakeToss(default_data())
    summary = run_collect(
        cfg,
        cal=cal,
        state=seeded_state,
        store=series,
        snapshots=snapshots,
        client=client,
        alerter=alerter,
        now=SATURDAY,
    )
    assert summary.status == "skipped-closed"
    assert seeded_state.get_heartbeat("collect").detail == {"status": "skipped-closed"}
    assert client.since_calls == []


def test_collect_degraded_on_failures(cfg, cal, seeded_state, series, snapshots, alerter, notifier):
    client = FakeToss(default_data(), fail={"000660"})
    summary = run_collect(
        cfg,
        cal=cal,
        state=seeded_state,
        store=series,
        snapshots=snapshots,
        client=client,
        alerter=alerter,
        now=NOW,
    )
    assert summary.status == "degraded"
    assert summary.failed == ["000660"]
    assert not seeded_state.get_heartbeat("collect").ok
    assert any("분봉 수집 실패" in text for text in notifier.sent)


def test_collect_auth_error_aborts(cfg, cal, seeded_state, series, snapshots, alerter, notifier):
    client = FakeToss(default_data(), auth_fail=True)
    summary = run_collect(
        cfg,
        cal=cal,
        state=seeded_state,
        store=series,
        snapshots=snapshots,
        client=client,
        alerter=alerter,
        now=NOW,
    )
    assert summary.status == "error"
    assert any("분봉 수집 실패" in text for text in notifier.sent)
    assert seeded_state.recent_runs("collect")[0].ok is False


def _bootstrap_caps():
    return pl.DataFrame(
        {
            "symbol": ["005930", "000660"],
            "value": [5e12, 3e12],
            "volume": [1e6, 1e6],
        }
    )


def test_collect_bootstraps_universe(cfg, cal, state, series, snapshots, alerter, monkeypatch):
    monkeypatch.setattr("talon.ingest.collect.fetch_market_cap", lambda day: _bootstrap_caps())
    monkeypatch.setattr("talon.ingest.universe.fetch_admin_issues", lambda: None)
    write_stock_info(snapshots, [date(2026, 7, 10)], ["005930", "000660"])
    client = FakeToss(default_data())
    summary = run_collect(
        cfg,
        cal=cal,
        state=state,
        store=series,
        snapshots=snapshots,
        client=client,
        alerter=alerter,
        now=NOW,
    )
    assert summary.status == "ok"
    snapshot = state.latest_universe()
    assert snapshot is not None
    assert snapshot.symbols == ["005930", "000660"]
    assert snapshot.day == date(2026, 7, 10)


def test_collect_bootstrap_falls_back_to_listing(
    cfg, cal, state, series, snapshots, alerter, monkeypatch
):
    def krx_down(day):
        raise SourceError("krx blocked")

    monkeypatch.setattr("talon.ingest.collect.fetch_market_cap", krx_down)
    monkeypatch.setattr(
        "talon.ingest.collect.fetch_krx_listing",
        lambda day: (pl.DataFrame(), _bootstrap_caps()),
    )
    monkeypatch.setattr("talon.ingest.universe.fetch_admin_issues", lambda: None)
    write_stock_info(snapshots, [date(2026, 7, 10)], ["005930", "000660"])
    client = FakeToss(default_data())
    summary = run_collect(
        cfg,
        cal=cal,
        state=state,
        store=series,
        snapshots=snapshots,
        client=client,
        alerter=alerter,
        now=NOW,
    )
    assert summary.status == "ok"
    snapshot = state.latest_universe()
    assert snapshot is not None
    assert snapshot.symbols == ["005930", "000660"]
    assert snapshot.day == date(2026, 7, 10)
