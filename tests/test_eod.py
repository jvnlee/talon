from datetime import date, timedelta

import polars as pl
import pytest

from conftest import make_candle, utc, write_stock_info
from talon.data.store import (
    CANDLE_SCHEMA,
    DAILY_CANDLES,
    DAILY_SNAPSHOT_SCHEMA,
    INDICATOR_DAILY,
    INVESTOR_TRADING,
    MACRO_INTRADAY,
    MACRO_INTRADAY_SCHEMA,
    MARKET_CAP,
    MINUTE_CANDLES,
    VKOSPI_1D,
    VKOSPI_1D_SCHEMA,
)
from talon.errors import SourceError
from talon.ingest.eod import run_eod
from talon.models import CandlePage, DartTimesDailySummary, InvestorFlowRecord
from talon.sources.crosscheck import CrosscheckResult, Discrepancy

DAY = date(2026, 7, 10)
PREV_DAY = date(2026, 7, 9)
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


def _blocked_listing(day):
    raise SourceError("krx listing blocked")


@pytest.fixture
def sources(monkeypatch, snapshots):
    monkeypatch.setattr("talon.ingest.eod.fetch_daily_ohlcv", lambda day, **kw: ohlcv_frame())
    monkeypatch.setattr("talon.ingest.eod.fetch_market_cap", lambda day, **kw: caps_frame())
    monkeypatch.setattr("talon.ingest.eod.fetch_krx_listing", _blocked_listing)
    monkeypatch.setattr(
        "talon.ingest.eod.crosscheck_daily",
        lambda snapshot, day, sample, **kw: CrosscheckResult(checked=len(sample)),
    )
    monkeypatch.setattr("talon.ingest.universe.fetch_admin_issues", set)
    write_stock_info(snapshots, [PREV_DAY], ["005930", "000660"])


@pytest.fixture(autouse=True)
def _stub_dart_times(monkeypatch):
    monkeypatch.setattr(
        "talon.ingest.eod.daily_dart_times",
        lambda cfg, **k: DartTimesDailySummary(status="ok"),
    )


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
        lambda day, **kw: pl.DataFrame(schema=DAILY_SNAPSHOT_SCHEMA),
    )
    monkeypatch.setattr("talon.ingest.eod.fetch_krx_listing", _blocked_listing)
    summary = run(cfg, cal, state, snapshots, series, alerter)
    assert summary.status == "data-not-ready"
    assert not snapshots.has_date(DAILY_CANDLES, DAY)
    assert state.recent_runs("eod")[0].ok is False
    assert any("일봉 데이터" in text for text in notifier.sent)


def test_eod_all_sources_down(cfg, cal, state, snapshots, series, alerter, notifier, monkeypatch):
    def boom(day, **kw):
        raise SourceError("krx down")

    monkeypatch.setattr("talon.ingest.eod.fetch_daily_ohlcv", boom)
    monkeypatch.setattr("talon.ingest.eod.fetch_krx_listing", _blocked_listing)
    summary = run(cfg, cal, state, snapshots, series, alerter)
    assert summary.status == "data-not-ready"
    assert "error" in summary.steps["pykrx"]
    assert "error" in summary.steps["fdr_listing"]
    assert any("어느 소스에서도" in text for text in notifier.sent)


def test_eod_unexpected_error(cfg, cal, state, snapshots, series, alerter, notifier, monkeypatch):
    def boom(day, **kw):
        raise RuntimeError("bug")

    monkeypatch.setattr("talon.ingest.eod.fetch_daily_ohlcv", boom)
    summary = run(cfg, cal, state, snapshots, series, alerter)
    assert summary.status == "error"
    assert any("EOD 잡 실패" in text for text in notifier.sent)
    assert state.recent_runs("eod")[0].ok is False


def test_eod_passes_krx_login_to_pykrx(cfg, cal, state, snapshots, series, alerter, monkeypatch):
    seen = {}

    def capture(day, *, credentials=None, **kw):
        seen["credentials"] = credentials
        return ohlcv_frame()

    monkeypatch.setattr("talon.ingest.eod.fetch_daily_ohlcv", capture)
    monkeypatch.setattr("talon.ingest.eod.fetch_market_cap", lambda day, **kw: caps_frame())
    monkeypatch.setattr(
        "talon.ingest.eod.crosscheck_daily",
        lambda snapshot, day, sample, **kw: CrosscheckResult(checked=len(sample)),
    )
    monkeypatch.setattr("talon.ingest.universe.fetch_admin_issues", set)
    monkeypatch.setattr("talon.ingest.eod.daily_flows", lambda *a, **k: "up-to-date")
    monkeypatch.setattr("talon.ingest.eod.daily_vkospi", lambda *a, **k: "up-to-date")
    write_stock_info(snapshots, [PREV_DAY], ["005930", "000660"])
    cfg = cfg.model_copy(update={"krx_id": "tester", "krx_password": "secret"})

    summary = run(cfg, cal, state, snapshots, series, alerter, toss=FakeToss())
    assert summary.status == "ok"
    assert "pykrx" in summary.steps["daily"]
    assert seen["credentials"] == ("tester", "secret")


def test_eod_crosscheck_skips_volume_before_settlement(
    cfg, cal, state, snapshots, series, alerter, sources, monkeypatch
):
    seen = {}

    def capture(snapshot, day, sample, *, tolerance_pct, fields, **kw):
        seen["fields"] = fields
        return CrosscheckResult(checked=len(sample))

    monkeypatch.setattr("talon.ingest.eod.crosscheck_daily", capture)
    summary = run(cfg, cal, state, snapshots, series, alerter, toss=FakeToss())
    assert summary.status == "ok"
    assert seen["fields"] == ("close",)


@pytest.fixture
def listing_only(monkeypatch, snapshots):
    monkeypatch.setattr(
        "talon.ingest.eod.fetch_daily_ohlcv",
        lambda day, **kw: pl.DataFrame(schema=DAILY_SNAPSHOT_SCHEMA),
    )
    monkeypatch.setattr(
        "talon.ingest.eod.fetch_krx_listing", lambda day: (ohlcv_frame(), caps_frame())
    )
    monkeypatch.setattr("talon.ingest.universe.fetch_admin_issues", set)
    write_stock_info(snapshots, [PREV_DAY], ["005930", "000660"])


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
        lambda snapshot, day, sample, **kw: result,
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
    def boom(day, **kw):
        raise SourceError("cap down")

    monkeypatch.setattr("talon.ingest.eod.fetch_market_cap", boom)
    summary = run(cfg, cal, state, snapshots, series, alerter, toss=FakeToss())
    assert summary.status == "ok"
    assert not snapshots.has_date(MARKET_CAP, DAY)
    assert state.latest_universe() is not None
    assert any("시가총액 수집 실패" in text for text in notifier.sent)


def test_eod_alerts_when_the_admin_list_is_unavailable(
    cfg, cal, state, snapshots, series, alerter, notifier, sources, monkeypatch
):
    monkeypatch.setattr("talon.ingest.universe.fetch_admin_issues", lambda: None)
    summary = run(cfg, cal, state, snapshots, series, alerter, toss=FakeToss())

    assert summary.status == "ok"
    assert state.latest_universe().criteria["admin_excluded"] is False
    assert any("관리종목 목록을 받지 못해" in text for text in notifier.sent)


def test_eod_universe_fails_loudly_without_stock_info(
    cfg, cal, state, snapshots, series, alerter, notifier, sources, monkeypatch
):
    monkeypatch.setattr("talon.ingest.universe.latest_stock_info", _no_stock_info)
    summary = run(cfg, cal, state, snapshots, series, alerter, toss=FakeToss())

    assert summary.status == "degraded"
    assert summary.universe_size == 0
    assert "종목기본정보" in summary.steps["universe"]
    assert any("유니버스 갱신 실패" in text for text in notifier.sent)


def _no_stock_info(snapshots, day, *, max_stale_days):
    raise SourceError("종목기본정보가 없습니다")


def test_eod_skips_investor_flows_without_krx_login(
    cfg, cal, state, snapshots, series, alerter, sources
):
    summary = run(cfg, cal, state, snapshots, series, alerter, toss=FakeToss())
    assert summary.steps["flows"] == "skipped-no-krx-login"


def test_eod_skips_kis_minutes_without_kis(cfg, cal, state, snapshots, series, alerter, sources):
    summary = run(cfg, cal, state, snapshots, series, alerter, toss=FakeToss())
    assert summary.steps["kis_minutes"] == "skipped-no-kis"


def test_eod_skips_shorting_without_krx_login(cfg, cal, state, snapshots, series, alerter, sources):
    summary = run(cfg, cal, state, snapshots, series, alerter, toss=FakeToss())
    assert summary.steps["shorting"] == "skipped-no-krx-login"


def test_eod_records_shorting_step(
    cfg, cal, state, snapshots, series, alerter, sources, monkeypatch
):
    monkeypatch.setattr("talon.ingest.eod.daily_flows", lambda *a, **k: "up-to-date")
    monkeypatch.setattr(
        "talon.ingest.eod.daily_shorting", lambda *a, **k: "trade 1/1, balance 1/1, investor 1/1"
    )
    cfg = cfg.model_copy(update={"krx_id": "u", "krx_password": "p"})
    summary = run(cfg, cal, state, snapshots, series, alerter, toss=FakeToss())
    assert summary.steps["shorting"] == "trade 1/1, balance 1/1, investor 1/1"


def test_eod_shorting_error_is_captured(
    cfg, cal, state, snapshots, series, alerter, sources, monkeypatch
):
    def boom(*a, **k):
        raise SourceError("공매도 실패")

    monkeypatch.setattr("talon.ingest.eod.daily_flows", lambda *a, **k: "up-to-date")
    monkeypatch.setattr("talon.ingest.eod.daily_shorting", boom)
    cfg = cfg.model_copy(update={"krx_id": "u", "krx_password": "p"})
    summary = run(cfg, cal, state, snapshots, series, alerter, toss=FakeToss())
    assert summary.status == "ok"
    assert summary.steps["shorting"] == "error: 공매도 실패"


def test_eod_records_kis_minutes_step(
    cfg, cal, state, snapshots, series, alerter, sources, monkeypatch
):
    monkeypatch.setattr("talon.ingest.eod.daily_kis_minutes", lambda *a, **k: "2/2 days, 500 rows")
    cfg = cfg.model_copy(update={"kis_app_key": "k", "kis_app_secret": "s"})
    summary = run(cfg, cal, state, snapshots, series, alerter, toss=FakeToss())
    assert summary.steps["kis_minutes"] == "2/2 days, 500 rows"


def test_eod_kis_minutes_error_is_captured(
    cfg, cal, state, snapshots, series, alerter, sources, monkeypatch
):
    def boom(*a, **k):
        raise SourceError("분봉 실패")

    monkeypatch.setattr("talon.ingest.eod.daily_kis_minutes", boom)
    cfg = cfg.model_copy(update={"kis_app_key": "k", "kis_app_secret": "s"})
    summary = run(cfg, cal, state, snapshots, series, alerter, toss=FakeToss())
    assert summary.status == "ok"
    assert summary.steps["kis_minutes"] == "error: 분봉 실패"


def test_eod_skips_usfut_when_disabled(cfg, cal, state, snapshots, series, alerter, sources):
    summary = run(cfg, cal, state, snapshots, series, alerter, toss=FakeToss())
    assert summary.steps["usfut"] == "skipped-disabled"


def test_eod_records_usfut_step(
    cfg, cal, state, snapshots, series, alerter, sources, monkeypatch
):
    monkeypatch.setattr("talon.ingest.eod.daily_usfut", lambda **k: "2/2 days")
    cfg = cfg.model_copy(update={"usfut_enabled": True})
    summary = run(cfg, cal, state, snapshots, series, alerter, toss=FakeToss())
    assert summary.steps["usfut"] == "2/2 days"


def test_eod_usfut_error_is_captured(
    cfg, cal, state, snapshots, series, alerter, sources, monkeypatch
):
    def boom(**k):
        raise SourceError("프록시 실패")

    monkeypatch.setattr("talon.ingest.eod.daily_usfut", boom)
    cfg = cfg.model_copy(update={"usfut_enabled": True})
    summary = run(cfg, cal, state, snapshots, series, alerter, toss=FakeToss())
    assert summary.status == "ok"
    assert summary.steps["usfut"] == "error: 프록시 실패"


def test_eod_records_kr_events_step(cfg, cal, state, snapshots, series, alerter, sources):
    from talon.data.store import KR_EVENTS_HISTORY, KR_EVENTS_HISTORY_NAME

    summary = run(cfg, cal, state, snapshots, series, alerter, toss=FakeToss())
    assert "snapshot" in summary.steps["kr_events"]
    assert "history" in summary.steps["kr_events"]
    assert series.read(KR_EVENTS_HISTORY, KR_EVENTS_HISTORY_NAME) is not None


def test_eod_kr_events_error_is_captured(
    cfg, cal, state, snapshots, series, alerter, sources, monkeypatch
):
    def boom(*a, **k):
        raise SourceError("캘린더 실패")

    monkeypatch.setattr("talon.ingest.eod.daily_kr_events", boom)
    summary = run(cfg, cal, state, snapshots, series, alerter, toss=FakeToss())
    assert summary.status == "ok"
    assert summary.steps["kr_events"] == "error: 캘린더 실패"


def test_eod_skips_vkospi_without_krx_login(
    cfg, cal, state, snapshots, series, alerter, sources
):
    summary = run(cfg, cal, state, snapshots, series, alerter, toss=FakeToss())
    assert summary.steps["vkospi"] == "skipped-no-krx-login"


def test_eod_records_vkospi_step(
    cfg, cal, state, snapshots, series, alerter, sources, monkeypatch
):
    monkeypatch.setattr("talon.ingest.eod.daily_flows", lambda *a, **k: "up-to-date")
    monkeypatch.setattr("talon.ingest.eod.daily_vkospi", lambda *a, **k: "1/1 days")
    cfg = cfg.model_copy(update={"krx_id": "tester", "krx_password": "secret"})
    summary = run(cfg, cal, state, snapshots, series, alerter, toss=FakeToss())
    assert summary.steps["vkospi"] == "1/1 days"


def test_eod_vkospi_error_is_captured(
    cfg, cal, state, snapshots, series, alerter, sources, monkeypatch
):
    def boom(*a, **k):
        raise SourceError("변동성지수 실패")

    monkeypatch.setattr("talon.ingest.eod.daily_flows", lambda *a, **k: "up-to-date")
    monkeypatch.setattr("talon.ingest.eod.daily_vkospi", boom)
    cfg = cfg.model_copy(update={"krx_id": "tester", "krx_password": "secret"})
    summary = run(cfg, cal, state, snapshots, series, alerter, toss=FakeToss())
    assert summary.status == "ok"
    assert summary.steps["vkospi"] == "error: 변동성지수 실패"


def test_eod_vkospi_appends_intraday_gap(
    cfg, cal, state, snapshots, series, alerter, sources, monkeypatch
):
    monkeypatch.setattr("talon.ingest.eod.daily_flows", lambda *a, **k: "up-to-date")
    monkeypatch.setattr("talon.ingest.eod.daily_vkospi", lambda *a, **k: "1/1 days")
    series.upsert(
        VKOSPI_1D,
        "VKOSPI",
        pl.DataFrame(
            [
                {
                    "day": DAY,
                    "open": 20.0,
                    "high": 20.5,
                    "low": 19.5,
                    "close": 20.0,
                    "change": 0.1,
                    "change_pct": 0.5,
                    "source": "krx",
                    "fetched_at": utc(2026, 7, 10, 7, 0),
                }
            ],
            schema=VKOSPI_1D_SCHEMA,
        ),
        key="day",
    )
    snapshots.write_date(
        MACRO_INTRADAY,
        DAY,
        pl.DataFrame(
            [
                {
                    "day": DAY,
                    "slot": "15:35",
                    "series": "VKOSPI",
                    "captured_at": utc(2026, 7, 10, 6, 35),
                    "price": 19.76,
                    "prev_close": 19.9,
                    "source": "krx",
                }
            ],
            schema=MACRO_INTRADAY_SCHEMA,
        ),
    )
    cfg = cfg.model_copy(update={"krx_id": "tester", "krx_password": "secret"})
    summary = run(cfg, cal, state, snapshots, series, alerter, toss=FakeToss())
    assert summary.steps["vkospi"] == "1/1 days d1535=0.24"
