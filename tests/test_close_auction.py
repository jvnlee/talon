from datetime import UTC, date, datetime, time, timedelta

import polars as pl
import pytest

from talon.data.store import (
    CLOSE_AUCTION_INTRADAY,
    INTRADAY_SNAPSHOT,
    INTRADAY_SNAPSHOT_SCHEMA,
)
from talon.errors import SourceError
from talon.ingest.close_auction import PASSES, auction_symbols, run_close_auction
from talon.ingest.intraday import DECISION_SLOT
from talon.timeutil import KST

DAY = date(2026, 7, 14)
SATURDAY = date(2026, 7, 11)


class FakeClock:
    def __init__(self, hour, minute, second=0):
        self.current = datetime.combine(DAY, time(hour, minute, second), tzinfo=KST)

    def now(self):
        return self.current.astimezone(UTC)

    def sleep(self, seconds):
        self.current += timedelta(seconds=seconds)


def orderbook_row(symbol):
    row = {"symbol": symbol}
    for level in range(1, 11):
        row[f"ask_price_{level}"] = 100.0 + level
        row[f"ask_qty_{level}"] = 10.0 * level
        row[f"bid_price_{level}"] = 100.0 - level
        row[f"bid_qty_{level}"] = 20.0 * level
    row |= {
        "total_ask_qty": 550.0,
        "total_bid_qty": 1100.0,
        "net_bid_qty": 550.0,
        "accept_hour": "152300",
        "market_phase": "30",
        "price": 100.0,
        "open": 99.0,
        "high": 101.0,
        "low": 98.0,
        "prev_close": 97.0,
        "antc_price": 101.0,
        "antc_qty": 5000.0,
        "antc_phase": "132",
        "vi_code": "N",
    }
    return row


def decision_frame(count=5):
    return pl.DataFrame(
        {
            "day": [DAY] * count,
            "slot": [DECISION_SLOT] * count,
            "symbol": [f"{i:06d}" for i in range(count)],
            "captured_at": [datetime(2026, 7, 14, 6, 10, tzinfo=UTC)] * count,
            "open": [100.0] * count,
            "high": [110.0] * count,
            "low": [90.0] * count,
            "close": [105.0] * count,
            "volume": [1000.0] * count,
            "value": [float((count - i) * 1e9) for i in range(count)],
            "change_pct": [1.0] * count,
        },
        schema=INTRADAY_SNAPSHOT_SCHEMA,
    )


@pytest.fixture
def kis_cfg(cfg):
    cfg.kis_app_key = "key"
    cfg.kis_app_secret = "secret"
    cfg.kis_sweep_size = 3
    return cfg


@pytest.fixture(autouse=True)
def fake_orderbook(monkeypatch):
    monkeypatch.setattr(
        "talon.ingest.close_auction.fetch_orderbook",
        lambda client, symbol: orderbook_row(symbol),
    )


def run(cfg, cal, state, snapshots, alerter, clock, *, day=DAY, force=False):
    return run_close_auction(
        cfg,
        cal=cal,
        state=state,
        snapshots=snapshots,
        alerter=alerter,
        today=day,
        force=force,
        now=clock.now,
        sleep=clock.sleep,
    )


def test_all_passes_capture_the_forming_close(kis_cfg, cal, state, snapshots, alerter):
    snapshots.write_date(INTRADAY_SNAPSHOT, DAY, decision_frame())
    clock = FakeClock(15, 20, 5)

    summary = run(kis_cfg, cal, state, snapshots, alerter, clock)

    assert summary.status == "ok"
    assert summary.symbols == 3
    assert summary.passes == {label: "ok" for label in PASSES}
    stored = snapshots.read_date(CLOSE_AUCTION_INTRADAY, DAY)
    assert stored.height == len(PASSES) * 3
    assert sorted(stored["slot"].unique().to_list()) == list(PASSES)
    assert stored["captured_at"].n_unique() == len(PASSES)
    assert state.get_heartbeat("close-auction").ok is True


def test_universe_is_decision_top_value_plus_pinned(kis_cfg, snapshots):
    kis_cfg.pinned_symbols = ["999999", "000001"]
    snapshots.write_date(INTRADAY_SNAPSHOT, DAY, decision_frame(5))

    symbols = auction_symbols(kis_cfg, snapshots, DAY)

    assert symbols == ["000000", "000001", "000002", "999999"]


def test_late_start_skips_expired_passes(kis_cfg, cal, state, snapshots, alerter, notifier):
    snapshots.write_date(INTRADAY_SNAPSHOT, DAY, decision_frame())
    clock = FakeClock(15, 26, 0)

    summary = run(kis_cfg, cal, state, snapshots, alerter, clock)

    assert summary.status == "ok"
    assert summary.passes["15:21"] == "missed"
    assert summary.passes["15:23"] == "missed"
    assert summary.passes["15:25"] == "missed"
    assert summary.passes["15:27"] == "ok"
    assert summary.passes["15:29"] == "ok"
    stored = snapshots.read_date(CLOSE_AUCTION_INTRADAY, DAY)
    assert sorted(stored["slot"].unique().to_list()) == ["15:27", "15:29"]
    assert any("놓침" in sent for sent in notifier.sent)


def test_firing_after_close_misses_everything(kis_cfg, cal, state, snapshots, alerter, notifier):
    snapshots.write_date(INTRADAY_SNAPSHOT, DAY, decision_frame())
    clock = FakeClock(15, 40, 0)

    summary = run(kis_cfg, cal, state, snapshots, alerter, clock)

    assert summary.status == "missed"
    assert snapshots.read_date(CLOSE_AUCTION_INTRADAY, DAY) is None
    assert any("놓쳤" in sent for sent in notifier.sent)
    assert state.get_heartbeat("close-auction").ok is False


def test_symbol_failures_within_ratio_keep_the_pass(
    monkeypatch, kis_cfg, cal, state, snapshots, alerter
):
    kis_cfg.kis_sweep_size = 5
    snapshots.write_date(INTRADAY_SNAPSHOT, DAY, decision_frame(5))

    def flaky(client, symbol):
        if symbol == "000004":
            raise SourceError("timeout")
        return orderbook_row(symbol)

    monkeypatch.setattr("talon.ingest.close_auction.fetch_orderbook", flaky)
    clock = FakeClock(15, 20, 5)

    summary = run(kis_cfg, cal, state, snapshots, alerter, clock)

    assert summary.status == "ok"
    assert all(status.startswith("partial") for status in summary.passes.values())
    stored = snapshots.read_date(CLOSE_AUCTION_INTRADAY, DAY)
    assert stored.height == len(PASSES) * 4


def test_kis_outage_in_one_pass_recovers_in_the_next(
    monkeypatch, kis_cfg, cal, state, snapshots, alerter, notifier
):
    snapshots.write_date(INTRADAY_SNAPSHOT, DAY, decision_frame())
    clock = FakeClock(15, 20, 5)

    def flaky(client, symbol):
        if clock.current.time() < time(15, 22):
            raise SourceError("kis down")
        return orderbook_row(symbol)

    monkeypatch.setattr("talon.ingest.close_auction.fetch_orderbook", flaky)

    summary = run(kis_cfg, cal, state, snapshots, alerter, clock)

    assert summary.status == "ok"
    assert summary.passes["15:21"].startswith("error")
    assert summary.passes["15:23"] == "ok"
    stored = snapshots.read_date(CLOSE_AUCTION_INTRADAY, DAY)
    assert "15:21" not in stored["slot"].unique().to_list()
    assert any("일부 패스 누락" in sent for sent in notifier.sent)


def test_missing_snapshot_falls_back_to_pinned(kis_cfg, cal, state, snapshots, alerter):
    kis_cfg.pinned_symbols = ["005930"]
    clock = FakeClock(15, 20, 5)

    summary = run(kis_cfg, cal, state, snapshots, alerter, clock)

    assert summary.status == "ok"
    assert summary.symbols == 1
    stored = snapshots.read_date(CLOSE_AUCTION_INTRADAY, DAY)
    assert stored["symbol"].unique().to_list() == ["005930"]


def test_without_snapshot_or_pinned_reports_no_universe(
    kis_cfg, cal, state, snapshots, alerter, notifier
):
    clock = FakeClock(15, 20, 5)

    summary = run(kis_cfg, cal, state, snapshots, alerter, clock)

    assert summary.status == "no-universe"
    assert snapshots.read_date(CLOSE_AUCTION_INTRADAY, DAY) is None
    assert any("pinned" in sent for sent in notifier.sent)


def test_no_kis_keys_reports_and_stores_nothing(cfg, cal, state, snapshots, alerter, notifier):
    snapshots.write_date(INTRADAY_SNAPSHOT, DAY, decision_frame())
    clock = FakeClock(15, 20, 5)

    summary = run(cfg, cal, state, snapshots, alerter, clock)

    assert summary.status == "no-kis"
    assert snapshots.read_date(CLOSE_AUCTION_INTRADAY, DAY) is None
    assert any("앱키" in sent for sent in notifier.sent)


def test_holiday_is_skipped(monkeypatch, kis_cfg, cal, state, snapshots, alerter):
    def unexpected(client, symbol):
        raise AssertionError("휴장일에는 KIS를 부르면 안 됩니다")

    monkeypatch.setattr("talon.ingest.close_auction.fetch_orderbook", unexpected)
    clock = FakeClock(15, 20, 5)

    summary = run(kis_cfg, cal, state, snapshots, alerter, clock, day=SATURDAY)

    assert summary.status == "skipped-holiday"
