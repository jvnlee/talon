from datetime import date

import polars as pl
import pytest

from talon.data.store import DAILY_SNAPSHOT_SCHEMA, INTRADAY_SNAPSHOT, MACRO_INTRADAY
from talon.errors import SourceError
from talon.ingest.intraday import AUCTION_SLOT, DECISION_SLOT, run_intraday
from talon.sources.investing import VkospiQuote
from talon.sources.krx_index import INDEX_SNAPSHOT_SCHEMA
from talon.sources.yahoo import YahooQuote

DAY = date(2026, 7, 14)
SATURDAY = date(2026, 7, 11)


@pytest.fixture(autouse=True)
def offline_pulse(monkeypatch):
    monkeypatch.setattr(
        "talon.ingest.pulse.fetch_index_snapshot",
        lambda day, market, **kw: pl.DataFrame(schema=INDEX_SNAPSHOT_SCHEMA),
    )
    monkeypatch.setattr(
        "talon.ingest.pulse.fetch_quote", lambda symbol, **kw: YahooQuote(100.0, 99.0)
    )
    monkeypatch.setattr("talon.ingest.pulse.fetch_vkospi", lambda **kw: VkospiQuote(32.0, 31.0))


def snapshot_frame(volume: float, symbols: int = 1_200):
    return pl.DataFrame(
        {
            "day": [DAY] * symbols,
            "symbol": [f"{i:06d}" for i in range(symbols)],
            "open": [70000.0] * symbols,
            "high": [71000.0] * symbols,
            "low": [69000.0] * symbols,
            "close": [70500.0] * symbols,
            "volume": [volume] * symbols,
            "value": [5e12] * symbols,
            "change_pct": [0.5] * symbols,
        },
        schema=DAILY_SNAPSHOT_SCHEMA,
    )


@pytest.fixture
def krx_login(cfg):
    cfg.krx_id = "id"
    cfg.krx_password = "pw"
    return cfg


def run(cfg, cal, state, snapshots, alerter, *, slot=DECISION_SLOT, day=DAY, force=False):
    return run_intraday(
        cfg,
        cal=cal,
        state=state,
        snapshots=snapshots,
        alerter=alerter,
        slot=slot,
        today=day,
        force=force,
    )


def test_captures_the_decision_snapshot(monkeypatch, krx_login, cal, state, snapshots, alerter):
    monkeypatch.setattr(
        "talon.ingest.intraday.fetch_daily_ohlcv", lambda day, **kw: snapshot_frame(1000.0)
    )

    summary = run(krx_login, cal, state, snapshots, alerter)

    assert summary.status == "ok"
    assert summary.rows == 1_200
    stored = snapshots.read_date(INTRADAY_SNAPSHOT, DAY)
    assert stored["slot"].unique().to_list() == [DECISION_SLOT]
    assert stored["captured_at"].null_count() == 0


def test_both_slots_coexist_in_one_day(monkeypatch, krx_login, cal, state, snapshots, alerter):
    monkeypatch.setattr(
        "talon.ingest.intraday.fetch_daily_ohlcv", lambda day, **kw: snapshot_frame(1000.0)
    )
    run(krx_login, cal, state, snapshots, alerter, slot=DECISION_SLOT)
    monkeypatch.setattr(
        "talon.ingest.intraday.fetch_daily_ohlcv", lambda day, **kw: snapshot_frame(1400.0)
    )
    run(krx_login, cal, state, snapshots, alerter, slot=AUCTION_SLOT)

    stored = snapshots.read_date(INTRADAY_SNAPSHOT, DAY)
    volumes = dict(stored.filter(pl.col("symbol") == "000000").select("slot", "volume").iter_rows())
    assert volumes == {DECISION_SLOT: 1000.0, AUCTION_SLOT: 1400.0}


def test_rerunning_a_slot_overwrites_it(monkeypatch, krx_login, cal, state, snapshots, alerter):
    monkeypatch.setattr(
        "talon.ingest.intraday.fetch_daily_ohlcv", lambda day, **kw: snapshot_frame(1000.0)
    )
    run(krx_login, cal, state, snapshots, alerter)
    monkeypatch.setattr(
        "talon.ingest.intraday.fetch_daily_ohlcv", lambda day, **kw: snapshot_frame(1100.0)
    )
    run(krx_login, cal, state, snapshots, alerter)

    stored = snapshots.read_date(INTRADAY_SNAPSHOT, DAY)
    assert stored.height == 1_200
    assert stored["volume"].unique().to_list() == [1100.0]


def test_thin_response_is_not_stored(monkeypatch, krx_login, cal, state, snapshots, alerter):
    monkeypatch.setattr(
        "talon.ingest.intraday.fetch_daily_ohlcv",
        lambda day, **kw: snapshot_frame(1000.0, symbols=40),
    )

    summary = run(krx_login, cal, state, snapshots, alerter)

    assert summary.status == "data-not-ready"
    assert snapshots.read_date(INTRADAY_SNAPSHOT, DAY) is None
    assert state.get_heartbeat("intraday").ok is False


def test_source_failure_alerts_and_stores_nothing(
    monkeypatch, krx_login, cal, state, snapshots, alerter, notifier
):
    def boom(day, **kw):
        raise SourceError("krx blocked")

    monkeypatch.setattr("talon.ingest.intraday.fetch_daily_ohlcv", boom)

    summary = run(krx_login, cal, state, snapshots, alerter)

    assert summary.status == "error"
    assert snapshots.read_date(INTRADAY_SNAPSHOT, DAY) is None
    assert any("krx blocked" in sent for sent in notifier.sent)


def test_holiday_is_skipped(monkeypatch, krx_login, cal, state, snapshots, alerter):
    def unexpected(day, **kw):
        raise AssertionError("휴장일에는 KRX를 부르면 안 됩니다")

    monkeypatch.setattr("talon.ingest.intraday.fetch_daily_ohlcv", unexpected)

    summary = run(krx_login, cal, state, snapshots, alerter, day=SATURDAY)

    assert summary.status == "skipped-holiday"


def test_unknown_slot_is_rejected(krx_login, cal, state, snapshots, alerter):
    with pytest.raises(ValueError, match="슬롯"):
        run(krx_login, cal, state, snapshots, alerter, slot="12:00")


def test_summary_carries_pulse_statuses(monkeypatch, krx_login, cal, state, snapshots, alerter):
    monkeypatch.setattr(
        "talon.ingest.intraday.fetch_daily_ohlcv", lambda day, **kw: snapshot_frame(1000.0)
    )

    summary = run(krx_login, cal, state, snapshots, alerter)

    assert summary.extras["index"] == "empty"
    assert summary.extras["macro"] == "ok"
    assert summary.extras["vkospi"] == "ok"
    assert summary.extras["breadth"] == "ok"
    assert summary.extras["dart"] == "skipped-no-key"
    stored = snapshots.read_date(MACRO_INTRADAY, DAY)
    assert stored.height == 4


def test_stock_failure_still_collects_macro(
    monkeypatch, krx_login, cal, state, snapshots, alerter
):
    def boom(day, **kw):
        raise SourceError("krx blocked")

    monkeypatch.setattr("talon.ingest.intraday.fetch_daily_ohlcv", boom)

    summary = run(krx_login, cal, state, snapshots, alerter)

    assert summary.status == "error"
    assert summary.extras["macro"] == "ok"
    assert summary.extras["breadth"] == "skipped-no-snapshot"
    assert snapshots.read_date(MACRO_INTRADAY, DAY) is not None


def test_pulse_failure_alerts_but_keeps_snapshot_ok(
    monkeypatch, krx_login, cal, state, snapshots, alerter, notifier
):
    monkeypatch.setattr(
        "talon.ingest.intraday.fetch_daily_ohlcv", lambda day, **kw: snapshot_frame(1000.0)
    )

    def boom(symbol, **kw):
        raise SourceError("yahoo down")

    monkeypatch.setattr("talon.ingest.pulse.fetch_quote", boom)

    summary = run(krx_login, cal, state, snapshots, alerter)

    assert summary.status == "ok"
    assert summary.extras["macro"].startswith("error")
    assert any("부가 수집 실패" in sent for sent in notifier.sent)
