from datetime import UTC, date, datetime, time

import polars as pl
import pytest

from talon.data.store import (
    INTRADAY_SNAPSHOT,
    INTRADAY_SNAPSHOT_SCHEMA,
    OVERTIME_MARKET,
    OVERTIME_PRICE,
    OVERTIME_RANKING,
)
from talon.ingest.intraday import DECISION_SLOT
from talon.ingest.overtime import run_overtime
from talon.timeutil import KST

DAY = date(2026, 7, 14)
SATURDAY = date(2026, 7, 11)


class FakeClock:
    def __init__(self, hour, minute, second=0):
        self.current = datetime.combine(DAY, time(hour, minute, second), tzinfo=KST)

    def now(self):
        return self.current.astimezone(UTC)


def overtime_price_row(symbol):
    return {
        "symbol": symbol,
        "prev_close": 100.0,
        "price": 101.0,
        "change": 1.0,
        "change_pct": 1.0,
        "sign": "2",
        "open": 100.0,
        "high": 102.0,
        "low": 99.0,
        "volume": 5000.0,
        "amount": 500000.0,
        "upper_limit": 110.0,
        "lower_limit": 90.0,
        "vi_code": "N",
    }


def overtime_ranking_result(side):
    return {
        "market": {
            "volume": 100.0,
            "amount": 200.0,
            "kospi_volume": 60.0,
            "kospi_amount": 120.0,
            "kosdaq_volume": 40.0,
            "kosdaq_amount": 80.0,
            "up_count": 10,
            "down_count": 5,
            "flat_count": 3,
            "upper_limit_count": 1,
            "lower_limit_count": 0,
        },
        "rows": [
            {
                "side": side,
                "rank": 1,
                "symbol": "334690",
                "name": "테스트",
                "price": 100.0,
                "change": 10.0,
                "change_pct": 10.0,
                "sign": "2",
                "ask": 100.0,
                "bid": 99.0,
                "volume": 5.0,
                "sell_rsqn": 1.0,
                "buy_rsqn": 2.0,
                "vol_vs_day_pct": 0.5,
                "day_price": 95.0,
                "day_volume": 1000.0,
            }
        ],
    }


def decision_frame(count=3):
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
def fake_overtime(monkeypatch):
    monkeypatch.setattr(
        "talon.ingest.overtime.fetch_overtime_price",
        lambda client, symbol: overtime_price_row(symbol),
    )
    monkeypatch.setattr(
        "talon.ingest.overtime.fetch_overtime_ranking",
        lambda client, side: overtime_ranking_result(side),
    )


def run(cfg, cal, state, snapshots, alerter, clock, *, day=DAY, force=False):
    return run_overtime(
        cfg,
        cal=cal,
        state=state,
        snapshots=snapshots,
        alerter=alerter,
        today=day,
        force=force,
        now=clock.now,
    )


def test_holiday_is_skipped(monkeypatch, kis_cfg, cal, state, snapshots, alerter):
    def unexpected(client, symbol):
        raise AssertionError("휴장일에는 KIS를 부르면 안 됩니다")

    monkeypatch.setattr("talon.ingest.overtime.fetch_overtime_price", unexpected)
    clock = FakeClock(18, 10)

    summary = run(kis_cfg, cal, state, snapshots, alerter, clock, day=SATURDAY)

    assert summary.status == "skipped-holiday"


def test_no_kis_keys_reports_and_stores_nothing(cfg, cal, state, snapshots, alerter, notifier):
    snapshots.write_date(INTRADAY_SNAPSHOT, DAY, decision_frame())
    clock = FakeClock(18, 10)

    summary = run(cfg, cal, state, snapshots, alerter, clock)

    assert summary.status == "no-kis"
    assert snapshots.read_date(OVERTIME_PRICE, DAY) is None
    assert any("앱키" in sent for sent in notifier.sent)


def test_before_session_close_is_too_early(monkeypatch, kis_cfg, cal, state, snapshots, alerter):
    def unexpected(client, symbol):
        raise AssertionError("18시 전에는 KIS를 부르면 안 됩니다")

    monkeypatch.setattr("talon.ingest.overtime.fetch_overtime_price", unexpected)
    snapshots.write_date(INTRADAY_SNAPSHOT, DAY, decision_frame())
    clock = FakeClock(17, 59)

    summary = run(kis_cfg, cal, state, snapshots, alerter, clock)

    assert summary.status == "too-early"
    assert snapshots.read_date(OVERTIME_PRICE, DAY) is None


def test_force_runs_before_the_session(kis_cfg, cal, state, snapshots, alerter):
    snapshots.write_date(INTRADAY_SNAPSHOT, DAY, decision_frame())
    clock = FakeClock(17, 59)

    summary = run(kis_cfg, cal, state, snapshots, alerter, clock, force=True)

    assert summary.status == "ok"
    assert snapshots.read_date(OVERTIME_PRICE, DAY).height == 3


def test_ok_path_fills_price_ranking_and_market(kis_cfg, cal, state, snapshots, alerter):
    snapshots.write_date(INTRADAY_SNAPSHOT, DAY, decision_frame())
    clock = FakeClock(18, 10)

    summary = run(kis_cfg, cal, state, snapshots, alerter, clock)

    assert summary.status == "ok"
    assert summary.symbols == 3
    assert summary.parts == {"overtime_price": "ok", "overtime_rank": "ok"}
    price = snapshots.read_date(OVERTIME_PRICE, DAY)
    assert price.height == 3
    assert "slot" not in price.columns
    assert price["captured_at"].null_count() == 0
    ranking = snapshots.read_date(OVERTIME_RANKING, DAY)
    assert sorted(ranking["side"].to_list()) == ["down", "up"]
    market = snapshots.read_date(OVERTIME_MARKET, DAY)
    assert market.height == 1
    assert market["scope"].to_list() == ["all"]
    assert market["up_count"].to_list() == [10]
    assert state.get_heartbeat("overtime").ok is True


def test_price_failure_keeps_rank_and_alerts_partial(
    monkeypatch, kis_cfg, cal, state, snapshots, alerter, notifier
):
    snapshots.write_date(INTRADAY_SNAPSHOT, DAY, decision_frame())

    def boom(client, symbol):
        raise Exception("kis down")

    monkeypatch.setattr("talon.ingest.overtime.fetch_overtime_price", boom)
    clock = FakeClock(18, 10)

    summary = run(kis_cfg, cal, state, snapshots, alerter, clock)

    assert summary.status == "ok"
    assert summary.parts["overtime_price"].startswith("error")
    assert summary.parts["overtime_rank"] == "ok"
    assert snapshots.read_date(OVERTIME_PRICE, DAY) is None
    assert snapshots.read_date(OVERTIME_RANKING, DAY) is not None
    assert any("일부" in sent for sent in notifier.sent)


def test_all_parts_failing_reports_error(
    monkeypatch, kis_cfg, cal, state, snapshots, alerter, notifier
):
    snapshots.write_date(INTRADAY_SNAPSHOT, DAY, decision_frame())

    def boom(*args, **kwargs):
        raise Exception("kis down")

    monkeypatch.setattr("talon.ingest.overtime.fetch_overtime_price", boom)
    monkeypatch.setattr("talon.ingest.overtime.fetch_overtime_ranking", boom)
    clock = FakeClock(18, 10)

    summary = run(kis_cfg, cal, state, snapshots, alerter, clock)

    assert summary.status == "error"
    assert any("한 행도 안 남" in sent for sent in notifier.sent)
    assert state.get_heartbeat("overtime").ok is False


def test_missing_snapshot_falls_back_to_pinned(kis_cfg, cal, state, snapshots, alerter):
    kis_cfg.pinned_symbols = ["005930"]
    clock = FakeClock(18, 10)

    summary = run(kis_cfg, cal, state, snapshots, alerter, clock)

    assert summary.status == "ok"
    assert summary.symbols == 1
    assert snapshots.read_date(OVERTIME_PRICE, DAY)["symbol"].to_list() == ["005930"]


def test_without_snapshot_or_pinned_reports_no_universe(
    kis_cfg, cal, state, snapshots, alerter, notifier
):
    clock = FakeClock(18, 10)

    summary = run(kis_cfg, cal, state, snapshots, alerter, clock)

    assert summary.status == "no-universe"
    assert snapshots.read_date(OVERTIME_PRICE, DAY) is None
    assert any("pinned" in sent for sent in notifier.sent)
