from datetime import UTC, date, datetime, timedelta

import polars as pl
from click.testing import CliRunner

from talon.cli import main
from talon.data.store import (
    MARKET_ALERTS,
    MARKET_ALERTS_SCHEMA,
    SHORT_OVERHEAT,
    SHORT_OVERHEAT_SCHEMA,
    TRADING_HALTS,
    TRADING_HALTS_SCHEMA,
    VI_EVENTS,
    VI_EVENTS_SCHEMA,
)
from talon.errors import SourceError
from talon.ingest import eod as eod_mod
from talon.ingest.actions import (
    ActionsFetchers,
    backfill_actions,
    daily_actions,
    verify_actions,
)
from talon.models import ActionsDailySummary

FETCHED = datetime(2026, 7, 22, tzinfo=UTC)
TODAY = date(2026, 7, 16)


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, 16, hour - 9, minute, tzinfo=UTC)


def vi_events(pairs: list[tuple[date, str]]) -> pl.DataFrame:
    n = len(pairs)
    return pl.DataFrame(
        {
            "day": [p[0] for p in pairs],
            "symbol": [p[1] for p in pairs],
            "name": ["종목"] * n,
            "market": ["KOSPI"] * n,
            "vi_kind": ["static"] * n,
            "trigger_time": [f"09:{i // 60:02d}:{i % 60:02d}" for i in range(n)],
            "release_time": [None] * n,
            "reference_price": [100.0] * n,
            "trigger_price": [110.0] * n,
            "divergence_pct": [10.0] * n,
            "fetched_at": [FETCHED] * n,
        },
        schema=VI_EVENTS_SCHEMA,
    )


def overheat_events(pairs: list[tuple[date, str]]) -> pl.DataFrame:
    n = len(pairs)
    return pl.DataFrame(
        {
            "day": [p[0] for p in pairs],
            "symbol": [p[1] for p in pairs],
            "isin": [f"KR7{p[1]}0" for p in pairs],
            "name": ["종목"] * n,
            "market": ["KOSDAQ"] * n,
            "mkt_id": ["KSQ"] * n,
            "restrict_apply_dd": [p[0] + timedelta(days=1) for p in pairs],
            "release_dd": [None] * n,
            "valu_pd_tr_dys": [40.0] * n,
            "tdd_srtsell_wt": [12.0] * n,
            "prc_yd": [-5.0] * n,
            "tdd_srtsell_trdval_incdec_rt": [None] * n,
            "valu_pd_avg_srtsell_wt": [5.0] * n,
            "dtec_type": ["유형2"] * n,
            "fetched_at": [FETCHED] * n,
        },
        schema=SHORT_OVERHEAT_SCHEMA,
    )


def alerts_frame(day: date, pairs: list[tuple[str, str]]) -> pl.DataFrame:
    n = len(pairs)
    return pl.DataFrame(
        {
            "day": [day] * n,
            "level": [p[0] for p in pairs],
            "symbol": [p[1] for p in pairs],
            "isin": [f"KR7{p[1]}0" for p in pairs],
            "name": ["종목"] * n,
            "market": ["KOSDAQ"] * n,
            "design_dd": [day] * n,
            "release_dd": [None] * n,
            "fetched_at": [FETCHED] * n,
        },
        schema=MARKET_ALERTS_SCHEMA,
    )


def halts_frame(rows: list[tuple[date, str, str, date | None]]) -> pl.DataFrame:
    n = len(rows)
    return pl.DataFrame(
        {
            "day": [r[0] for r in rows],
            "symbol": [r[1] for r in rows],
            "isin": [r[2] for r in rows],
            "name": ["종목"] * n,
            "market": ["KOSDAQ"] * n,
            "reason": ["정지사유"] * n,
            "last_trade_day": [r[0] - timedelta(days=1) for r in rows],
            "resume_day": [r[3] for r in rows],
            "fetched_at": [FETCHED] * n,
        },
        schema=TRADING_HALTS_SCHEMA,
    )


def fetchers(*, vi=None, alerts=None, overheat=None, halts=None, halt_history=None):
    return ActionsFetchers(
        vi=vi or (lambda s, e: vi_events([])),
        alerts=alerts or (lambda d: alerts_frame(d, [])),
        overheat=overheat or (lambda s, e: overheat_events([])),
        halts=halts or (lambda: halts_frame([])),
        halt_history=halt_history or (lambda i, s, e: {}),
    )


def test_backfill_vi_writes_event_and_empty_partitions(cfg, cal, state, snapshots):
    events = [(date(2026, 7, 3), "005930"), (date(2026, 7, 8), "000660")]

    def fake_vi(start: date, end: date) -> pl.DataFrame:
        return vi_events([e for e in events if start <= e[0] <= end])

    summary = backfill_actions(
        cfg,
        cal=cal,
        state=state,
        snapshots=snapshots,
        start=date(2026, 7, 1),
        end=date(2026, 7, 10),
        parts=("vi",),
        fetchers=fetchers(vi=fake_vi),
    )
    part = summary.parts["vi"]
    assert part.status == "ok"
    assert part.loaded == 2
    assert snapshots.read_date(VI_EVENTS, date(2026, 7, 3)).height == 1
    assert snapshots.has_date(VI_EVENTS, date(2026, 7, 1))
    assert snapshots.read_date(VI_EVENTS, date(2026, 7, 1)).height == 0


def test_backfill_vi_resumes_second_run(cfg, cal, state, snapshots):
    calls: list[tuple[date, date]] = []

    def fake_vi(start: date, end: date) -> pl.DataFrame:
        calls.append((start, end))
        return vi_events([(date(2026, 7, 3), "005930")])

    kwargs = dict(
        cal=cal,
        state=state,
        snapshots=snapshots,
        start=date(2026, 7, 1),
        end=date(2026, 7, 10),
        parts=("vi",),
        fetchers=fetchers(vi=fake_vi),
    )
    backfill_actions(cfg, **kwargs)
    assert len(calls) == 1
    summary = backfill_actions(cfg, **kwargs)
    assert len(calls) == 1
    assert summary.parts["vi"].skipped == 1


def test_backfill_aborts_after_three_failures(cfg, cal, state, snapshots):
    def boom(start: date, end: date) -> pl.DataFrame:
        raise SourceError("boom")

    summary = backfill_actions(
        cfg,
        cal=cal,
        state=state,
        snapshots=snapshots,
        start=date(2026, 4, 1),
        end=date(2026, 7, 10),
        parts=("vi",),
        fetchers=fetchers(vi=boom),
    )
    assert summary.status == "aborted"
    assert summary.parts["vi"].status == "aborted"
    assert len(summary.parts["vi"].failed) == 3


def test_backfill_forward_only_parts(cfg, cal, state, snapshots):
    summary = backfill_actions(
        cfg,
        cal=cal,
        state=state,
        snapshots=snapshots,
        start=date(2026, 7, 1),
        end=date(2026, 7, 10),
        parts=("alerts", "halts"),
        fetchers=fetchers(),
    )
    assert summary.status == "ok"
    assert summary.parts["alerts"].status == "forward-only"
    assert summary.parts["halts"].status == "forward-only"


def test_backfill_overheat_skips_pre_institution(cfg, cal, state, snapshots):
    calls: list[tuple[date, date]] = []

    def fake_overheat(start: date, end: date) -> pl.DataFrame:
        calls.append((start, end))
        return overheat_events([])

    summary = backfill_actions(
        cfg,
        cal=cal,
        state=state,
        snapshots=snapshots,
        start=date(2016, 1, 1),
        end=date(2016, 12, 31),
        parts=("overheat",),
        fetchers=fetchers(overheat=fake_overheat),
    )
    assert calls == []
    assert summary.parts["overheat"].status == "ok"


def test_daily_vi_excludes_today_before_ready(cfg, cal, snapshots):
    def fake_vi(start: date, end: date) -> pl.DataFrame:
        return vi_events([(date(2026, 7, 15), "005930")])

    daily_actions(
        cfg,
        cal=cal,
        snapshots=snapshots,
        now=at(15, 0),
        parts=("vi",),
        fetchers=fetchers(vi=fake_vi),
    )
    assert snapshots.has_date(VI_EVENTS, date(2026, 7, 15))
    assert snapshots.read_date(VI_EVENTS, date(2026, 7, 15)).height == 1
    assert not snapshots.has_date(VI_EVENTS, TODAY)


def test_daily_vi_includes_today_after_ready(cfg, cal, snapshots):
    def fake_vi(start: date, end: date) -> pl.DataFrame:
        return vi_events([(TODAY, "005930")])

    daily_actions(
        cfg,
        cal=cal,
        snapshots=snapshots,
        now=at(16, 30),
        parts=("vi",),
        fetchers=fetchers(vi=fake_vi),
    )
    assert snapshots.read_date(VI_EVENTS, TODAY).height == 1


def test_daily_vi_up_to_date(cfg, cal, snapshots):
    end = cal.latest_trading_day(TODAY)
    for day in cal.sessions_between(end - timedelta(days=21), end)[-7:]:
        snapshots.write_date(VI_EVENTS, day, vi_events([]))
    summary = daily_actions(
        cfg, cal=cal, snapshots=snapshots, now=at(16, 30), parts=("vi",), fetchers=fetchers()
    )
    assert summary.parts["vi"] == "up-to-date"


def test_daily_alerts_writes_snapshot(cfg, cal, snapshots):
    end = cal.latest_trading_day(TODAY)

    def fake_alerts(day: date) -> pl.DataFrame:
        return alerts_frame(day, [("warning", "066910")])

    daily_actions(
        cfg,
        cal=cal,
        snapshots=snapshots,
        now=at(16, 30),
        parts=("alerts",),
        fetchers=fetchers(alerts=fake_alerts),
    )
    stored = snapshots.read_date(MARKET_ALERTS, end)
    assert stored.height == 1
    assert stored.row(0, named=True)["level"] == "warning"


def test_daily_halts_captures_and_fills_resume(cfg, cal, snapshots):
    d1 = date(2026, 7, 6)
    snapshots.write_date(TRADING_HALTS, d1, halts_frame([(d1, "083660", "KR7083660001", None)]))

    def fake_halts() -> pl.DataFrame:
        return halts_frame([(date(2026, 7, 13), "900110", "KR9900110001", None)])

    history_calls: list[str] = []

    def fake_history(isin: str, start: date, end: date) -> dict[date, date]:
        history_calls.append(isin)
        return {d1: date(2026, 7, 10)}

    summary = daily_actions(
        cfg,
        cal=cal,
        snapshots=snapshots,
        now=at(16, 30),
        parts=("halts",),
        fetchers=fetchers(halts=fake_halts, halt_history=fake_history),
        sleep=lambda _: None,
    )
    assert summary.parts["halts"] == "1 halted, 1 resumed"
    assert history_calls == ["KR7083660001"]
    assert snapshots.has_date(TRADING_HALTS, date(2026, 7, 13))
    resumed = snapshots.read_date(TRADING_HALTS, d1).filter(pl.col("symbol") == "083660")
    assert resumed.row(0, named=True)["resume_day"] == date(2026, 7, 10)


def test_daily_isolates_part_failure(cfg, cal, snapshots):
    def boom_vi(start: date, end: date) -> pl.DataFrame:
        raise SourceError("vi down")

    def fake_alerts(day: date) -> pl.DataFrame:
        return alerts_frame(day, [("caution", "005930")])

    summary = daily_actions(
        cfg,
        cal=cal,
        snapshots=snapshots,
        now=at(16, 30),
        parts=("vi", "alerts"),
        fetchers=fetchers(vi=boom_vi, alerts=fake_alerts),
    )
    assert summary.status == "partial"
    assert summary.parts["vi"].startswith("error:")
    assert not summary.parts["alerts"].startswith("error:")


def test_verify_empty_store_is_ok(cfg, snapshots):
    report = verify_actions(cfg, snapshots=snapshots)
    assert report.status == "ok"
    assert report.parts["vi"] == "empty"
    assert report.parts["halts"] == "empty"


def test_verify_vi_ok(cfg, snapshots):
    snapshots.write_date(
        VI_EVENTS, date(2020, 1, 2), vi_events([(date(2020, 1, 2), "005930")] * 10)
    )
    report = verify_actions(cfg, snapshots=snapshots, parts=("vi",))
    assert report.parts["vi"] == "ok"


def test_verify_vi_flags_institution_boundary(cfg, snapshots):
    snapshots.write_date(
        VI_EVENTS, date(2013, 1, 2), vi_events([(date(2013, 1, 2), "005930")] * 6)
    )
    report = verify_actions(cfg, snapshots=snapshots, parts=("vi",))
    assert "before-institution" in report.parts["vi"]
    assert report.status == "issues"


def test_verify_vi_flags_bad_kind_and_time(cfg, snapshots):
    frame = vi_events([(date(2020, 1, 2), "005930")] * 6).with_columns(
        pl.Series("vi_kind", ["static", "static", "static", "static", "static", "bogus"]),
        pl.Series(
            "trigger_time",
            ["09:00:00", "09:00:01", "09:00:02", "09:00:03", "09:00:04", "nope"],
        ),
    )
    snapshots.write_date(VI_EVENTS, date(2020, 1, 2), frame)
    report = verify_actions(cfg, snapshots=snapshots, parts=("vi",))
    assert "bad-kind" in report.parts["vi"]
    assert "bad-time" in report.parts["vi"]


def test_verify_overheat_flags_boundary(cfg, snapshots):
    snapshots.write_date(
        SHORT_OVERHEAT, date(2016, 5, 2), overheat_events([(date(2016, 5, 2), "005930")])
    )
    report = verify_actions(cfg, snapshots=snapshots, parts=("overheat",))
    assert "before-institution" in report.parts["overheat"]


def test_verify_alerts_flags_bad_level(cfg, snapshots):
    frame = alerts_frame(date(2026, 7, 22), [("bogus", "005930")])
    snapshots.write_date(MARKET_ALERTS, date(2026, 7, 22), frame)
    report = verify_actions(cfg, snapshots=snapshots, parts=("alerts",))
    assert "bad-level" in report.parts["alerts"]


def test_eod_step_skips_without_krx_login(cfg, cal, snapshots):
    steps: dict[str, str] = {}
    eod_mod._load_market_actions(cfg, cal, snapshots, steps)
    assert steps["actions"] == "skipped-no-krx-login"


def test_eod_step_reports_parts_with_krx_login(cfg, cal, snapshots, monkeypatch):
    configured = cfg.model_copy(update={"krx_id": "u", "krx_password": "p"})
    monkeypatch.setattr(
        eod_mod,
        "daily_actions",
        lambda *a, **k: ActionsDailySummary(
            status="ok", parts={"vi": "3 days, 10 rows", "halts": "5 halted, 0 resumed"}
        ),
    )
    steps: dict[str, str] = {}
    eod_mod._load_market_actions(configured, cal, snapshots, steps)
    assert "vi: 3 days, 10 rows" in steps["actions"]
    assert "halts: 5 halted, 0 resumed" in steps["actions"]


def test_cli_actions_group_registered():
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "actions" in result.output


def test_cli_actions_daily_requires_krx(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TALON_DATA_DIR", str(tmp_path / "data"))
    result = CliRunner().invoke(main, ["actions", "daily"])
    assert result.exit_code != 0
    assert "TALON_KRX_ID" in result.output


def test_cli_actions_verify_on_empty_store(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TALON_DATA_DIR", str(tmp_path / "data"))
    result = CliRunner().invoke(main, ["actions", "verify"])
    assert result.exit_code == 0
    assert "empty" in result.output
