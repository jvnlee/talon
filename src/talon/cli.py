import inspect
import json
import logging
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import click
import polars as pl

if TYPE_CHECKING:
    from talon.backtest.cohort import CohortReport

from talon import __version__
from talon import launchd as launchd_mod
from talon.backtest.benchmark import load_index_daily
from talon.backtest.crosscheck import run_crosscheck
from talon.backtest.data import PANEL_COLUMNS, load_panel
from talon.backtest.engine import EngineConfig, run_backtest
from talon.backtest.evaluate import evaluate_gate1
from talon.backtest.lookahead import (
    pick_cuts,
    verify_factors,
    verify_intraday,
    verify_replay,
)
from talon.backtest.metrics import TRADING_DAYS_PER_YEAR, BacktestStats
from talon.backtest.report import write_tearsheet
from talon.backtest.sensitivity import SweepRun, neighbors, run_sweep
from talon.config import TalonSettings, load_settings
from talon.data.state import StateDB
from talon.data.store import (
    DAILY_CANDLES,
    DART_FILINGS,
    DELISTING,
    INDICATOR_MINUTE,
    MINUTE_CANDLES,
    STOCK_INFO,
    US_KR_MAP,
    US_KR_MAP_NAME,
    DatePartitionedStore,
    ParquetStore,
)
from talon.data.uskrmap import build_us_kr_map
from talon.errors import TalonError
from talon.factors.engine import warmup_periods
from talon.ingest.actions import (
    ALL_PARTS,
    BACKFILL_PARTS,
    backfill_actions,
    daily_actions,
    verify_actions,
)
from talon.ingest.briefing import run_briefing_snapshot
from talon.ingest.close_auction import run_close_auction
from talon.ingest.collect import bootstrap_universe, run_collect
from talon.ingest.credit import backfill_credit, daily_credit, verify_credit
from talon.ingest.dart_times import (
    BACKFILL_START as DART_TIMES_START,
)
from talon.ingest.dart_times import (
    DART_WEB_HORIZON,
    backfill_dart_times,
    daily_dart_times,
    verify_dart_times,
)
from talon.ingest.eod import run_eod
from talon.ingest.flows import backfill_flows, daily_flows
from talon.ingest.history import backfill_daily
from talon.ingest.intraday import SLOTS, run_intraday
from talon.ingest.kis_minutes import (
    backfill_kis_minutes,
    daily_kis_minutes,
    probe_kis_minutes,
    verify_kis_minutes,
)
from talon.ingest.kr_events import (
    backfill_kr_events,
    daily_kr_events,
    verify_kr_events,
)
from talon.ingest.minutes import DEFAULT_MAX_PAGES, backfill_minutes
from talon.ingest.overtime import run_overtime
from talon.ingest.shorting import (
    DATASET_NAMES,
    backfill_shorting,
    daily_shorting,
    verify_shorting,
)
from talon.ingest.us_calendar import run_us_calendar
from talon.ingest.us_eod import run_us_eod
from talon.ingest.usfut import backfill_usfut, daily_usfut, verify_usfut
from talon.ingest.vkospi import backfill_vkospi, vkospi_status
from talon.ingest.watchdog import run_watchdog
from talon.locks import job_lock
from talon.markets.kr import krx_calendar
from talon.markets.us import us_calendar
from talon.models import UsMapSummary
from talon.notify.telegram import Alerter, TelegramNotifier
from talon.quant.core import QuantCore, RegimeAssessor, closed_trades_frame
from talon.quant.regime import BreadthRegimeFilter, FullExposureRegime
from talon.quant.risk import RiskConfig, RiskGate, interventions_frame
from talon.quant.signals import StrategySpec
from talon.quant.strategies import default_strategies
from talon.quant.universe import LiquidityUniverse
from talon.sources.toss import TossClient
from talon.timeutil import KST, now_utc

log = logging.getLogger(__name__)


@click.group()
@click.option("-v", "--verbose", is_flag=True)
@click.version_option(__version__)
def main(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("talon").setLevel(logging.DEBUG if verbose else logging.INFO)


def _make_toss(cfg: TalonSettings) -> TossClient:
    return TossClient(
        cfg.toss_client_id,
        cfg.toss_client_secret,
        base_url=cfg.toss_base_url,
        rps=cfg.toss_rps,
        timeout=cfg.request_timeout,
    )


@contextmanager
def runtime(cfg: TalonSettings, *, toss: str = "require") -> Iterator[SimpleNamespace]:
    state = StateDB(cfg.state_path)
    notifier = TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id)
    client: TossClient | None = None
    try:
        if toss == "require":
            if not cfg.toss_configured:
                raise click.ClickException(
                    "TALON_TOSS_CLIENT_ID / TALON_TOSS_CLIENT_SECRET 설정이 필요합니다"
                )
            client = _make_toss(cfg)
        elif toss == "optional" and cfg.toss_configured:
            client = _make_toss(cfg)
        yield SimpleNamespace(
            cfg=cfg,
            state=state,
            notifier=notifier,
            client=client,
            alerter=Alerter(notifier, state, timedelta(minutes=cfg.alert_cooldown_minutes)),
            series=ParquetStore(cfg.parquet_dir),
            snapshots=DatePartitionedStore(cfg.parquet_dir),
            cal=krx_calendar(),
        )
    finally:
        if client is not None:
            client.close()
        notifier.close()
        state.close()


def _today_kst() -> date:
    return now_utc().astimezone(KST).date()


@main.command()
@click.option("--force", is_flag=True)
def collect(force: bool) -> None:
    cfg = load_settings()
    with job_lock(cfg.locks_dir / "collect.lock") as acquired:
        if not acquired:
            click.echo("collect가 이미 실행 중입니다")
            return
        with runtime(cfg) as rt:
            assert rt.client is not None
            summary = run_collect(
                cfg,
                cal=rt.cal,
                state=rt.state,
                store=rt.series,
                snapshots=rt.snapshots,
                client=rt.client,
                alerter=rt.alerter,
                force=force,
            )
    click.echo(summary.model_dump_json())
    if summary.status == "error":
        sys.exit(1)


@main.command()
@click.option("--date", "day_text", default=None, help="YYYY-MM-DD")
@click.option("--force", is_flag=True)
def eod(day_text: str | None, force: bool) -> None:
    cfg = load_settings()
    day = date.fromisoformat(day_text) if day_text else None
    with job_lock(cfg.locks_dir / "eod.lock") as acquired:
        if not acquired:
            click.echo("eod가 이미 실행 중입니다")
            return
        with runtime(cfg, toss="optional") as rt:
            summary = run_eod(
                cfg,
                cal=rt.cal,
                state=rt.state,
                snapshots=rt.snapshots,
                series=rt.series,
                toss=rt.client,
                alerter=rt.alerter,
                today=day,
                force=force,
            )
    click.echo(summary.model_dump_json())
    if summary.status in {"error", "data-not-ready"}:
        sys.exit(1)


@main.command()
@click.option("--slot", type=click.Choice(SLOTS), required=True)
@click.option("--date", "day_text", default=None, help="YYYY-MM-DD")
@click.option("--force", is_flag=True)
def intraday(slot: str, day_text: str | None, force: bool) -> None:
    cfg = load_settings()
    day = date.fromisoformat(day_text) if day_text else None
    with job_lock(cfg.locks_dir / "intraday.lock") as acquired:
        if not acquired:
            click.echo("intraday가 이미 실행 중입니다")
            return
        with runtime(cfg, toss="skip") as rt:
            summary = run_intraday(
                cfg,
                cal=rt.cal,
                state=rt.state,
                snapshots=rt.snapshots,
                alerter=rt.alerter,
                slot=slot,
                today=day,
                force=force,
            )
    click.echo(summary.model_dump_json())
    if summary.status in {"error", "data-not-ready", "no-credentials"}:
        sys.exit(1)


@main.command("close-auction")
@click.option("--force", is_flag=True)
def close_auction(force: bool) -> None:
    cfg = load_settings()
    with job_lock(cfg.locks_dir / "close-auction.lock") as acquired:
        if not acquired:
            click.echo("close-auction이 이미 실행 중입니다")
            return
        with runtime(cfg, toss="skip") as rt:
            summary = run_close_auction(
                cfg,
                cal=rt.cal,
                state=rt.state,
                snapshots=rt.snapshots,
                alerter=rt.alerter,
                force=force,
            )
    click.echo(summary.model_dump_json())
    if summary.status not in {"ok", "skipped-holiday"}:
        sys.exit(1)


@main.command("overtime")
@click.option("--force", is_flag=True)
def overtime(force: bool) -> None:
    cfg = load_settings()
    with job_lock(cfg.locks_dir / "overtime.lock") as acquired:
        if not acquired:
            click.echo("overtime이 이미 실행 중입니다")
            return
        with runtime(cfg, toss="skip") as rt:
            summary = run_overtime(
                cfg,
                cal=rt.cal,
                state=rt.state,
                snapshots=rt.snapshots,
                alerter=rt.alerter,
                force=force,
            )
    click.echo(summary.model_dump_json())
    if summary.status not in {"ok", "skipped-holiday"}:
        sys.exit(1)


@main.command("us-eod")
@click.option("--full", is_flag=True)
def us_eod(full: bool) -> None:
    cfg = load_settings()
    with job_lock(cfg.locks_dir / "us-eod.lock") as acquired:
        if not acquired:
            click.echo("us-eod가 이미 실행 중입니다")
            return
        with runtime(cfg, toss="skip") as rt:
            summary = run_us_eod(
                cfg,
                uscal=us_calendar(),
                state=rt.state,
                series=rt.series,
                alerter=rt.alerter,
                full=full,
            )
    click.echo(summary.model_dump_json())
    if summary.status == "error":
        sys.exit(1)


@main.command("us-calendar")
@click.option("--backfill", is_flag=True)
@click.option("--date", "day_text", default=None, help="YYYY-MM-DD")
def us_calendar_command(backfill: bool, day_text: str | None) -> None:
    cfg = load_settings()
    day = date.fromisoformat(day_text) if day_text else None
    with job_lock(cfg.locks_dir / "us-calendar.lock") as acquired:
        if not acquired:
            click.echo("us-calendar가 이미 실행 중입니다")
            return
        with runtime(cfg, toss="skip") as rt:
            summary = run_us_calendar(
                cfg,
                cal=rt.cal,
                uscal=us_calendar(),
                state=rt.state,
                snapshots=rt.snapshots,
                series=rt.series,
                alerter=rt.alerter,
                today=day,
                backfill=backfill,
            )
    click.echo(summary.model_dump_json())
    if summary.status == "error":
        sys.exit(1)


@main.command("briefing-snapshot")
@click.option("--force", is_flag=True)
def briefing_snapshot(force: bool) -> None:
    cfg = load_settings()
    with job_lock(cfg.locks_dir / "briefing-snapshot.lock") as acquired:
        if not acquired:
            click.echo("briefing-snapshot이 이미 실행 중입니다")
            return
        with runtime(cfg, toss="skip") as rt:
            summary = run_briefing_snapshot(
                cfg,
                cal=rt.cal,
                state=rt.state,
                snapshots=rt.snapshots,
                alerter=rt.alerter,
                force=force,
            )
    click.echo(summary.model_dump_json())
    if summary.status == "error":
        sys.exit(1)


@main.command("us-map")
def us_map() -> None:
    cfg = load_settings()
    with runtime(cfg, toss="skip") as rt:
        frame = build_us_kr_map()
        known: set[str] = set()
        latest_info = rt.snapshots.latest(STOCK_INFO)
        if latest_info is not None:
            known = set(latest_info[1]["symbol"].to_list())
        mapped = {symbol for symbols in frame["kr_symbols"].to_list() for symbol in symbols}
        unknown = sorted(mapped - known) if known else []
        rt.series.replace(US_KR_MAP, US_KR_MAP_NAME, frame)
        summary = UsMapSummary(status="ok", rows=frame.height, unknown=unknown)
    click.echo(summary.model_dump_json())


@main.group()
def trials() -> None:
    pass


@trials.command("show")
def trials_show() -> None:
    cfg = load_settings()
    with runtime(cfg, toss="skip") as rt:
        payload = {
            "active_cycle": rt.state.active_cycle(),
            "active_count": rt.state.trial_count(),
            "by_cycle": rt.state.cycle_counts(),
        }
    click.echo(json.dumps(payload, ensure_ascii=False))


@trials.command("open")
@click.option("--name", required=True)
@click.option("--note", default="")
def trials_open(name: str, note: str) -> None:
    cfg = load_settings()
    with runtime(cfg, toss="skip") as rt:
        try:
            rt.state.open_cycle(name, note=note)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        payload = {
            "active_cycle": rt.state.active_cycle(),
            "active_count": rt.state.trial_count(),
            "by_cycle": rt.state.cycle_counts(),
        }
    click.echo(json.dumps(payload, ensure_ascii=False))


@main.command("backfill-minutes")
@click.option("--pages", type=int, default=DEFAULT_MAX_PAGES, show_default=True)
@click.option("--symbol", "symbols", multiple=True)
def backfill_minutes_command(pages: int, symbols: tuple[str, ...]) -> None:
    cfg = load_settings()
    with job_lock(cfg.locks_dir / "backfill-minutes.lock") as acquired:
        if not acquired:
            click.echo("backfill-minutes가 이미 실행 중입니다")
            return
        with runtime(cfg) as rt:
            assert rt.client is not None
            targets = list(symbols)
            if not targets:
                snapshot = rt.state.latest_universe()
                targets = snapshot.symbols if snapshot is not None else []
            if not targets:
                raise click.ClickException(
                    "유니버스 스냅샷이 없습니다 — talon collect 를 먼저 돌리십시오"
                )
            summary = backfill_minutes(rt.series, rt.client, targets, max_pages=pages)
    click.echo(summary.model_dump_json())
    if summary.status != "ok":
        sys.exit(1)


@main.command("backfill-daily")
@click.option("--years", type=int, default=None)
@click.option("--start", "start_text", default=None, help="YYYY-MM-DD")
@click.option("--end", "end_text", default=None, help="YYYY-MM-DD")
def backfill_daily_command(years: int | None, start_text: str | None, end_text: str | None) -> None:
    cfg = load_settings()
    with job_lock(cfg.locks_dir / "backfill.lock") as acquired:
        if not acquired:
            click.echo("backfill이 이미 실행 중입니다")
            return
        with runtime(cfg, toss="skip") as rt:
            end = (
                date.fromisoformat(end_text)
                if end_text
                else rt.cal.previous_trading_day(_today_kst())
            )
            if start_text:
                start = date.fromisoformat(start_text)
            else:
                span_years = years if years is not None else cfg.backfill_years
                start = end - timedelta(days=round(365.25 * span_years))

            def report(index: int, total: int, day: date) -> None:
                if index % 25 == 0 or index == total:
                    click.echo(f"{index}/{total} {day}")

            summary = backfill_daily(
                cfg,
                cal=rt.cal,
                state=rt.state,
                snapshots=rt.snapshots,
                start=start,
                end=end,
                progress=report,
            )
    click.echo(summary.model_dump_json())


@main.command(
    "repair-daily",
    help="저장된 일봉에서 무거래일(거래정지·기세) 행 결손을 marcap 원천으로 복원하고 "
    "영향 종목의 수정계수를 재산출한다.",
)
@click.option("--start", "start_text", default=None, help="YYYY-MM-DD")
@click.option("--end", "end_text", default=None, help="YYYY-MM-DD")
@click.option("--skip-adjust", is_flag=True)
@click.option("--throttle", type=float, default=0.2, show_default=True)
def repair_daily_command(
    start_text: str | None,
    end_text: str | None,
    skip_adjust: bool,
    throttle: float,
) -> None:
    from talon.ingest.repair import repair_daily_gaps

    cfg = load_settings()
    with (
        job_lock(cfg.locks_dir / "backfill.lock") as backfill_acquired,
        job_lock(cfg.locks_dir / "adjust.lock") as adjust_acquired,
    ):
        if not backfill_acquired or not adjust_acquired:
            click.echo("backfill 또는 adjust 잡이 이미 실행 중입니다")
            return
        with runtime(cfg, toss="skip") as rt:

            def report_day(index: int, total: int, day: date) -> None:
                if index % 250 == 0 or index == total:
                    click.echo(f"days {index}/{total} {day}")

            def report_symbol(index: int, total: int, symbol: str) -> None:
                if index % 100 == 0 or index == total:
                    click.echo(f"factors {index}/{total} {symbol}")

            summary = repair_daily_gaps(
                cfg,
                state=rt.state,
                snapshots=rt.snapshots,
                series=rt.series,
                alerter=rt.alerter,
                start=date.fromisoformat(start_text) if start_text else None,
                end=date.fromisoformat(end_text) if end_text else None,
                rebuild_factors=not skip_adjust,
                throttle=throttle,
                progress=report_day,
                factor_progress=report_symbol,
            )
    click.echo(summary.model_dump_json())


@main.command()
@click.option("--days", type=int, default=None, help="되돌아볼 거래일 수")
@click.option("--start", "start_text", default=None, help="YYYY-MM-DD")
@click.option("--end", "end_text", default=None, help="YYYY-MM-DD")
def reconcile(days: int | None, start_text: str | None, end_text: str | None) -> None:
    """저장된 일봉을 KRX 공식 확정본(Open API)과 대조해 교정한다."""
    from talon.ingest.holidays import sync_holidays
    from talon.ingest.reconcile import reconcile_daily

    cfg = load_settings()
    if not cfg.krx_openapi_configured:
        raise click.ClickException("TALON_KRX_API_KEY 설정이 필요합니다")
    with job_lock(cfg.locks_dir / "reconcile.lock") as acquired:
        if not acquired:
            click.echo("reconcile이 이미 실행 중입니다")
            return
        with runtime(cfg, toss="skip") as rt:
            sync_holidays(cfg, state=rt.state, alerter=rt.alerter, today=_today_kst())
            end = (
                date.fromisoformat(end_text)
                if end_text
                else rt.cal.previous_trading_day(_today_kst())
            )
            if start_text:
                start = date.fromisoformat(start_text)
            else:
                lookback = days if days is not None else cfg.reconcile_lookback_days
                start = end - timedelta(days=round(lookback * 1.6) + 7)
                sessions = rt.cal.sessions_between(start, end)
                start = sessions[-lookback] if len(sessions) > lookback else sessions[0]
            summary = reconcile_daily(
                cfg,
                cal=rt.cal,
                state=rt.state,
                snapshots=rt.snapshots,
                alerter=rt.alerter,
                start=start,
                end=end,
            )
    click.echo(summary.model_dump_json(exclude={"days"}))
    if summary.status == "error":
        sys.exit(1)


@main.group()
def flows() -> None:
    """KRX 확정 투자자별 수급 (11분류, 일별)."""


@flows.command("backfill")
@click.option("--start", "start_text", default="2016-07-01", show_default=True, help="YYYY-MM-DD")
@click.option("--end", "end_text", default=None, help="YYYY-MM-DD")
def flows_backfill(start_text: str, end_text: str | None) -> None:
    cfg = load_settings()
    if not cfg.krx_login_configured:
        raise click.ClickException("TALON_KRX_ID / TALON_KRX_PASSWORD 설정이 필요합니다")
    with job_lock(cfg.locks_dir / "flows-backfill.lock") as acquired:
        if not acquired:
            click.echo("flows backfill이 이미 실행 중입니다")
            return
        with runtime(cfg, toss="skip") as rt:
            start = date.fromisoformat(start_text)
            end = (
                date.fromisoformat(end_text)
                if end_text
                else rt.cal.previous_trading_day(_today_kst())
            )
            if end < start:
                raise click.ClickException("종료일이 시작일보다 빠릅니다")

            def report(index: int, total: int, day: date) -> None:
                if index % 25 == 0 or index == total:
                    click.echo(f"{index}/{total} {day}")

            summary = backfill_flows(
                cfg,
                cal=rt.cal,
                state=rt.state,
                snapshots=rt.snapshots,
                start=start,
                end=end,
                progress=report,
            )
    click.echo(summary.model_dump_json())


@flows.command("daily")
def flows_daily() -> None:
    cfg = load_settings()
    if not cfg.krx_login_configured:
        raise click.ClickException("TALON_KRX_ID / TALON_KRX_PASSWORD 설정이 필요합니다")
    with runtime(cfg, toss="skip") as rt:
        result = daily_flows(cfg, cal=rt.cal, snapshots=rt.snapshots)
    click.echo(result)


@main.group()
def vkospi() -> None:
    pass


@vkospi.command("backfill")
@click.option("--start", "start_text", default="2015-07-01", show_default=True, help="YYYY-MM-DD")
@click.option("--end", "end_text", default=None, help="YYYY-MM-DD")
@click.option("--force", is_flag=True)
def vkospi_backfill(start_text: str, end_text: str | None, force: bool) -> None:
    cfg = load_settings()
    if not cfg.krx_login_configured:
        raise click.ClickException("TALON_KRX_ID / TALON_KRX_PASSWORD 설정이 필요합니다")
    with job_lock(cfg.locks_dir / "vkospi-backfill.lock") as acquired:
        if not acquired:
            click.echo("vkospi backfill이 이미 실행 중입니다")
            return
        with runtime(cfg, toss="skip") as rt:
            start = date.fromisoformat(start_text)
            end = (
                date.fromisoformat(end_text)
                if end_text
                else rt.cal.previous_trading_day(_today_kst())
            )
            if end < start:
                raise click.ClickException("종료일이 시작일보다 빠릅니다")
            summary = backfill_vkospi(cfg, rt.series, rt.cal, start=start, end=end, force=force)
    click.echo(summary.model_dump_json())
    if summary.status != "ok":
        sys.exit(1)


@vkospi.command("status")
def vkospi_status_cmd() -> None:
    cfg = load_settings()
    with runtime(cfg, toss="skip") as rt:
        report = vkospi_status(rt.series, rt.cal)
    click.echo(report.model_dump_json(indent=2))
    if report.status != "ok":
        sys.exit(1)


@main.group()
def usfut() -> None:
    pass


@usfut.command("backfill")
@click.option("--start", "start_text", default="2011-09-19", show_default=True, help="YYYY-MM-DD")
@click.option("--end", "end_text", default=None, help="YYYY-MM-DD")
def usfut_backfill(start_text: str, end_text: str | None) -> None:
    cfg = load_settings()
    with job_lock(cfg.locks_dir / "usfut-backfill.lock") as acquired:
        if not acquired:
            click.echo("usfut backfill이 이미 실행 중입니다")
            return
        with runtime(cfg, toss="skip") as rt:
            start = date.fromisoformat(start_text)
            end = (
                date.fromisoformat(end_text)
                if end_text
                else rt.cal.previous_trading_day(_today_kst())
            )
            if end < start:
                raise click.ClickException("종료일이 시작일보다 빠릅니다")

            def report(index: int, total: int, day: date) -> None:
                if index % 100 == 0 or index == total:
                    click.echo(f"{index}/{total} {day}")

            summary = backfill_usfut(
                snapshots=rt.snapshots,
                start=start,
                end=end,
                pause=cfg.usfut_pause_seconds,
                progress=report,
            )
    click.echo(summary.model_dump_json())
    if summary.status not in {"ok", "partial"}:
        sys.exit(1)


@usfut.command("daily")
def usfut_daily() -> None:
    cfg = load_settings()
    with job_lock(cfg.locks_dir / "usfut-daily.lock") as acquired:
        if not acquired:
            click.echo("usfut daily가 이미 실행 중입니다")
            return
        with runtime(cfg, toss="skip") as rt:
            result = daily_usfut(snapshots=rt.snapshots, pause=cfg.usfut_pause_seconds)
    click.echo(result)


@usfut.command("verify")
@click.option("--start", "start_text", default=None, help="YYYY-MM-DD")
@click.option("--end", "end_text", default=None, help="YYYY-MM-DD")
def usfut_verify(start_text: str | None, end_text: str | None) -> None:
    cfg = load_settings()
    start = date.fromisoformat(start_text) if start_text else None
    end = date.fromisoformat(end_text) if end_text else None
    with runtime(cfg, toss="skip") as rt:
        report = verify_usfut(
            snapshots=rt.snapshots, series=rt.series, cal=rt.cal, start=start, end=end
        )
    click.echo(report.model_dump_json(indent=2))
    if report.status not in {"ok", "empty"}:
        sys.exit(1)


@main.group()
def shorting() -> None:
    """KRX 공매도 (거래·잔고·투자자별, 일별)."""


@shorting.command("backfill")
@click.option(
    "--dataset",
    type=click.Choice(["trade", "balance", "investor", "all"]),
    default="all",
    show_default=True,
)
@click.option("--start", "start_text", default="2016-01-01", show_default=True, help="YYYY-MM-DD")
@click.option("--end", "end_text", default=None, help="YYYY-MM-DD")
def shorting_backfill(dataset: str, start_text: str, end_text: str | None) -> None:
    cfg = load_settings()
    if not cfg.krx_login_configured:
        raise click.ClickException("TALON_KRX_ID / TALON_KRX_PASSWORD 설정이 필요합니다")
    selected = list(DATASET_NAMES) if dataset == "all" else [dataset]
    with job_lock(cfg.locks_dir / "shorting-backfill.lock") as acquired:
        if not acquired:
            click.echo("shorting backfill이 이미 실행 중입니다")
            return
        with runtime(cfg, toss="skip") as rt:
            start = date.fromisoformat(start_text)
            end = (
                date.fromisoformat(end_text)
                if end_text
                else rt.cal.previous_trading_day(_today_kst())
            )
            if end < start:
                raise click.ClickException("종료일이 시작일보다 빠릅니다")

            def report(index: int, total: int, day: date) -> None:
                if index % 25 == 0 or index == total:
                    click.echo(f"{index}/{total} {day}")

            summaries = {}
            for name in selected:
                summary = backfill_shorting(
                    cfg,
                    cal=rt.cal,
                    state=rt.state,
                    snapshots=rt.snapshots,
                    dataset=DATASET_NAMES[name],
                    start=start,
                    end=end,
                    progress=report,
                )
                summaries[name] = summary.model_dump(mode="json")
    click.echo(json.dumps(summaries, ensure_ascii=False))


@shorting.command("daily")
def shorting_daily() -> None:
    cfg = load_settings()
    if not cfg.krx_login_configured:
        raise click.ClickException("TALON_KRX_ID / TALON_KRX_PASSWORD 설정이 필요합니다")
    with runtime(cfg, toss="skip") as rt:
        result = daily_shorting(cfg, cal=rt.cal, snapshots=rt.snapshots)
    click.echo(result)


@shorting.command("verify")
@click.option("--start", "start_text", default=None, help="YYYY-MM-DD")
@click.option("--end", "end_text", default=None, help="YYYY-MM-DD")
def shorting_verify(start_text: str | None, end_text: str | None) -> None:
    cfg = load_settings()
    start = date.fromisoformat(start_text) if start_text else None
    end = date.fromisoformat(end_text) if end_text else None
    with runtime(cfg, toss="skip") as rt:
        report = verify_shorting(cfg, snapshots=rt.snapshots, start=start, end=end)
    click.echo(report.model_dump_json(indent=2))
    if report.status != "ok":
        sys.exit(1)


_ACTIONS_PART_CHOICE = click.Choice(list(ALL_PARTS))


@main.group()
def actions() -> None:
    """KRX 시장조치 이력 (VI·시장경보·공매도과열·거래정지)."""


@actions.command("backfill")
@click.option("--start", "start_text", default="2016-01-01", show_default=True, help="YYYY-MM-DD")
@click.option("--end", "end_text", default=None, help="YYYY-MM-DD")
@click.option("--part", "parts", multiple=True, type=_ACTIONS_PART_CHOICE)
def actions_backfill(start_text: str, end_text: str | None, parts: tuple[str, ...]) -> None:
    cfg = load_settings()
    if not cfg.krx_login_configured:
        raise click.ClickException("TALON_KRX_ID / TALON_KRX_PASSWORD 설정이 필요합니다")
    with job_lock(cfg.locks_dir / "actions-backfill.lock") as acquired:
        if not acquired:
            click.echo("actions backfill이 이미 실행 중입니다")
            return
        with runtime(cfg, toss="skip") as rt:
            start = date.fromisoformat(start_text)
            end = (
                date.fromisoformat(end_text)
                if end_text
                else rt.cal.previous_trading_day(_today_kst())
            )
            if end < start:
                raise click.ClickException("종료일이 시작일보다 빠릅니다")
            selected = parts or BACKFILL_PARTS

            def report(index: int, total: int, day: date) -> None:
                if index % 12 == 0 or index == total:
                    click.echo(f"{index}/{total} {day}")

            summary = backfill_actions(
                cfg,
                cal=rt.cal,
                state=rt.state,
                snapshots=rt.snapshots,
                start=start,
                end=end,
                parts=selected,
                progress=report,
            )
    click.echo(summary.model_dump_json())


@actions.command("daily")
@click.option("--part", "parts", multiple=True, type=_ACTIONS_PART_CHOICE)
def actions_daily(parts: tuple[str, ...]) -> None:
    cfg = load_settings()
    if not cfg.krx_login_configured:
        raise click.ClickException("TALON_KRX_ID / TALON_KRX_PASSWORD 설정이 필요합니다")
    with job_lock(cfg.locks_dir / "actions-daily.lock") as acquired:
        if not acquired:
            click.echo("actions daily가 이미 실행 중입니다")
            return
        with runtime(cfg, toss="skip") as rt:
            summary = daily_actions(
                cfg, cal=rt.cal, snapshots=rt.snapshots, parts=parts or ALL_PARTS
            )
    click.echo(summary.model_dump_json())


@actions.command("verify")
@click.option("--part", "parts", multiple=True, type=_ACTIONS_PART_CHOICE)
def actions_verify(parts: tuple[str, ...]) -> None:
    cfg = load_settings()
    with runtime(cfg, toss="skip") as rt:
        report = verify_actions(cfg, snapshots=rt.snapshots, parts=parts or ALL_PARTS)
    click.echo(report.model_dump_json(indent=2))


@main.group()
def events() -> None:
    """KR 결정론적 이벤트 캘린더 (만기·리밸런싱·배당락·연말연시)."""


@events.command("backfill")
def events_backfill() -> None:
    cfg = load_settings()
    with job_lock(cfg.locks_dir / "events-backfill.lock") as acquired:
        if not acquired:
            click.echo("events backfill이 이미 실행 중입니다")
            return
        with runtime(cfg, toss="skip") as rt:
            summary = backfill_kr_events(
                cfg, cal=rt.cal, state=rt.state, snapshots=rt.snapshots, series=rt.series
            )
    click.echo(summary.model_dump_json())


@events.command("daily")
@click.option("--day", "day_text", default=None, help="YYYY-MM-DD (재처리용)")
def events_daily(day_text: str | None) -> None:
    cfg = load_settings()
    day = date.fromisoformat(day_text) if day_text else None
    with job_lock(cfg.locks_dir / "events-daily.lock") as acquired:
        if not acquired:
            click.echo("events daily가 이미 실행 중입니다")
            return
        with runtime(cfg, toss="skip") as rt:
            summary = daily_kr_events(
                cfg,
                cal=rt.cal,
                snapshots=rt.snapshots,
                series=rt.series,
                today=day,
            )
    click.echo(summary.model_dump_json())


@events.command("verify")
def events_verify() -> None:
    cfg = load_settings()
    with runtime(cfg, toss="skip") as rt:
        report = verify_kr_events(cfg, cal=rt.cal, snapshots=rt.snapshots, series=rt.series)
    click.echo(report.model_dump_json(indent=2))


@main.group("kis-minutes")
def kis_minutes() -> None:
    """KIS 확정 분봉 (KRX단독 1분봉, 일 파티션)."""


@kis_minutes.command("backfill")
@click.option("--start", "start_text", default=None, help="YYYY-MM-DD")
@click.option("--end", "end_text", default=None, help="YYYY-MM-DD")
@click.option("--rps", type=float, default=None, help="KIS 초당 호출 수 상한")
@click.option("--force", is_flag=True)
def kis_minutes_backfill(
    start_text: str | None, end_text: str | None, rps: float | None, force: bool
) -> None:
    cfg = load_settings()
    if not cfg.kis_configured:
        raise click.ClickException("TALON_KIS_APP_KEY / TALON_KIS_APP_SECRET 설정이 필요합니다")
    if rps is not None:
        cfg = cfg.model_copy(update={"kis_rps": rps})
    with job_lock(cfg.locks_dir / "kis-minutes-backfill.lock") as acquired:
        if not acquired:
            click.echo("kis-minutes backfill이 이미 실행 중입니다")
            return
        with runtime(cfg, toss="skip") as rt:
            start = date.fromisoformat(start_text) if start_text else None
            end = (
                date.fromisoformat(end_text)
                if end_text
                else rt.cal.previous_trading_day(_today_kst())
            )
            if start is not None and end < start:
                raise click.ClickException("종료일이 시작일보다 빠릅니다")

            def report(index: int, total: int, day: date) -> None:
                if index % 5 == 0 or index == total:
                    click.echo(f"{index}/{total} {day}")

            summary = backfill_kis_minutes(
                cfg,
                cal=rt.cal,
                state=rt.state,
                snapshots=rt.snapshots,
                start=start,
                end=end,
                progress=report,
                force=force,
            )
    click.echo(summary.model_dump_json())


@kis_minutes.command("daily")
def kis_minutes_daily() -> None:
    cfg = load_settings()
    if not cfg.kis_configured:
        raise click.ClickException("TALON_KIS_APP_KEY / TALON_KIS_APP_SECRET 설정이 필요합니다")
    with job_lock(cfg.locks_dir / "kis-minutes-daily.lock") as acquired:
        if not acquired:
            click.echo("kis-minutes daily가 이미 실행 중입니다")
            return
        with runtime(cfg, toss="skip") as rt:
            result = daily_kis_minutes(cfg, cal=rt.cal, snapshots=rt.snapshots)
    click.echo(result)


@kis_minutes.command("probe")
@click.option("--day", "day_text", default=None, help="YYYY-MM-DD")
@click.option("--anchor", default=None, help="HHMMSS")
def kis_minutes_probe(day_text: str | None, anchor: str | None) -> None:
    cfg = load_settings()
    if not cfg.kis_configured:
        raise click.ClickException("TALON_KIS_APP_KEY / TALON_KIS_APP_SECRET 설정이 필요합니다")
    with runtime(cfg, toss="skip") as rt:
        day = date.fromisoformat(day_text) if day_text else None
        report = probe_kis_minutes(cfg, cal=rt.cal, day=day, anchor=anchor)
    click.echo(report.model_dump_json(indent=2))


@kis_minutes.command("verify")
@click.option("--start", "start_text", default=None, help="YYYY-MM-DD")
@click.option("--end", "end_text", default=None, help="YYYY-MM-DD")
@click.option("--symbols-sample", type=int, default=30, show_default=True)
def kis_minutes_verify(start_text: str | None, end_text: str | None, symbols_sample: int) -> None:
    cfg = load_settings()
    start = date.fromisoformat(start_text) if start_text else None
    end = date.fromisoformat(end_text) if end_text else None
    with runtime(cfg, toss="skip") as rt:
        report = verify_kis_minutes(
            cfg,
            cal=rt.cal,
            snapshots=rt.snapshots,
            start=start,
            end=end,
            symbols_sample=symbols_sample,
        )
    click.echo(report.model_dump_json(indent=2))


@main.group()
def credit() -> None:
    """KIS 종목별 신용융자·대주 잔고 (일별, 관측 T+3)."""


@credit.command("backfill")
@click.option("--start", "start_text", default="2016-01-04", show_default=True, help="YYYY-MM-DD")
@click.option("--end", "end_text", default=None, help="YYYY-MM-DD")
@click.option("--symbol", "symbols", multiple=True, help="재개용 종목 부분집합")
@click.option("--rps", type=float, default=None, help="KIS 초당 호출 수 상한")
def credit_backfill(
    start_text: str, end_text: str | None, symbols: tuple[str, ...], rps: float | None
) -> None:
    cfg = load_settings()
    if not cfg.kis_configured:
        raise click.ClickException("TALON_KIS_APP_KEY / TALON_KIS_APP_SECRET 설정이 필요합니다")
    if rps is not None:
        cfg = cfg.model_copy(update={"kis_rps": rps})
    with job_lock(cfg.locks_dir / "credit-backfill.lock") as acquired:
        if not acquired:
            click.echo("credit backfill이 이미 실행 중입니다")
            return
        with runtime(cfg, toss="skip") as rt:
            start = date.fromisoformat(start_text)
            end = (
                date.fromisoformat(end_text)
                if end_text
                else rt.cal.previous_trading_day(_today_kst())
            )
            if end < start:
                raise click.ClickException("종료일이 시작일보다 빠릅니다")

            def report(index: int, total: int, symbol: str) -> None:
                if index % 100 == 0 or index == total:
                    click.echo(f"{index}/{total} {symbol}")

            summary = backfill_credit(
                cfg,
                state=rt.state,
                snapshots=rt.snapshots,
                start=start,
                end=end,
                symbols=list(symbols) or None,
                delisting=rt.series.read(DELISTING, "registry"),
                progress=report,
            )
    click.echo(summary.model_dump_json())


@credit.command("daily")
def credit_daily() -> None:
    cfg = load_settings()
    if not cfg.kis_configured:
        raise click.ClickException("TALON_KIS_APP_KEY / TALON_KIS_APP_SECRET 설정이 필요합니다")
    with job_lock(cfg.locks_dir / "credit-daily.lock") as acquired:
        if not acquired:
            click.echo("credit daily가 이미 실행 중입니다")
            return
        with runtime(cfg, toss="skip") as rt:
            result = daily_credit(cfg, snapshots=rt.snapshots)
    click.echo(result)


@credit.command("verify")
@click.option("--start", "start_text", default=None, help="YYYY-MM-DD")
@click.option("--end", "end_text", default=None, help="YYYY-MM-DD")
def credit_verify(start_text: str | None, end_text: str | None) -> None:
    cfg = load_settings()
    start = date.fromisoformat(start_text) if start_text else None
    end = date.fromisoformat(end_text) if end_text else None
    with runtime(cfg, toss="skip") as rt:
        report = verify_credit(cfg, snapshots=rt.snapshots, start=start, end=end)
    click.echo(report.model_dump_json(indent=2))
    if report.status not in {"ok", "empty"}:
        sys.exit(1)


@main.group()
def dart() -> None:
    """DART 전자공시 수집."""


@dart.command("backfill")
@click.option("--start", "start_text", required=True, help="YYYY-MM-DD")
@click.option("--end", "end_text", default=None, help="YYYY-MM-DD")
@click.option("--types", "types_text", default="A,B,D", show_default=True)
@click.option("--force", is_flag=True)
def dart_backfill(
    start_text: str,
    end_text: str | None,
    types_text: str,
    force: bool,
) -> None:
    from talon.sources.dart import fetch_filings

    cfg = load_settings()
    if not cfg.dart_api_key:
        raise click.ClickException(
            "DART API 키가 없습니다. https://opendart.fss.or.kr 에서 발급 후 "
            "~/.talon/env 에 TALON_DART_API_KEY=<키> 를 추가하세요"
        )
    start = date.fromisoformat(start_text)
    end = date.fromisoformat(end_text) if end_text else _today_kst()
    if end < start:
        raise click.ClickException("종료일이 시작일보다 빠릅니다")
    types = tuple(part.strip() for part in types_text.split(",") if part.strip())
    with job_lock(cfg.locks_dir / "dart.lock") as acquired:
        if not acquired:
            click.echo("dart backfill이 이미 실행 중입니다")
            return
        snapshots = DatePartitionedStore(cfg.parquet_dir)
        total = (end - start).days + 1
        written = skipped = failed = 0
        day = start
        index = 0
        while day <= end:
            index += 1
            if not force and snapshots.has_date(DART_FILINGS, day):
                skipped += 1
                day += timedelta(days=1)
                continue
            try:
                frame = fetch_filings(cfg.dart_api_key, day, types=types)
            except TalonError as exc:
                failed += 1
                log.warning("dart %s 실패: %s", day, exc)
                if failed >= 5:
                    raise click.ClickException(f"연속 실패 누적으로 중단: {exc}") from exc
                day += timedelta(days=1)
                continue
            snapshots.write_date(DART_FILINGS, day, frame)
            written += 1
            if index % 25 == 0 or day == end:
                click.echo(f"{index}/{total} {day} ({frame.height}건)")
            time.sleep(cfg.dart_throttle_seconds)
            day += timedelta(days=1)
    click.echo(
        json.dumps({"written": written, "skipped": skipped, "failed": failed, "total": total})
    )


@main.group("dart-times")
def dart_times() -> None:
    """DART 접수시각 (dsac001 최근공시 목록 스크레이프, day 파티션, rcept_no 조인)."""


@dart_times.command("backfill")
@click.option("--start", "start_text", default=None, help="YYYY-MM-DD")
@click.option("--end", "end_text", default=None, help="YYYY-MM-DD")
def dart_times_backfill(start_text: str | None, end_text: str | None) -> None:
    cfg = load_settings()
    with job_lock(cfg.locks_dir / "dart-times-backfill.lock") as acquired:
        if not acquired:
            click.echo("dart-times backfill이 이미 실행 중입니다")
            return
        with runtime(cfg, toss="skip") as rt:
            start = (
                date.fromisoformat(start_text)
                if start_text
                else max(DART_TIMES_START, DART_WEB_HORIZON)
            )
            end = date.fromisoformat(end_text) if end_text else _today_kst()
            if end < start:
                raise click.ClickException("종료일이 시작일보다 빠릅니다")

            def report(index: int, total: int, day: date) -> None:
                if index % 25 == 0 or index == total:
                    click.echo(f"{index}/{total} {day}")

            summary = backfill_dart_times(
                cfg,
                state=rt.state,
                snapshots=rt.snapshots,
                start=start,
                end=end,
                progress=report,
            )
    click.echo(summary.model_dump_json())
    if summary.status == "aborted":
        sys.exit(1)


@dart_times.command("daily")
def dart_times_daily() -> None:
    cfg = load_settings()
    with job_lock(cfg.locks_dir / "dart-times-daily.lock") as acquired:
        if not acquired:
            click.echo("dart-times daily가 이미 실행 중입니다")
            return
        with runtime(cfg, toss="skip") as rt:
            summary = daily_dart_times(cfg, cal=rt.cal, snapshots=rt.snapshots)
    click.echo(summary.model_dump_json())


@dart_times.command("verify")
def dart_times_verify() -> None:
    cfg = load_settings()
    with runtime(cfg, toss="skip") as rt:
        report = verify_dart_times(cfg, snapshots=rt.snapshots)
    click.echo(report.model_dump_json(indent=2))
    if not (report.status == "ok" or report.status == "empty"):
        sys.exit(1)


def _columns_warmup(strategies: list[StrategySpec], regime_filter: RegimeAssessor) -> int:
    columns: dict[str, str] = {}
    for spec in strategies:
        columns.update(spec.columns())
    columns.update(regime_filter.columns())
    feature_columns = set(PANEL_COLUMNS) - {"day", "symbol"}
    return max(warmup_periods(columns, feature_columns).values(), default=0)


def _warmup_start(start: date | None, warmup: int) -> date | None:
    if start is None:
        return None
    return start - timedelta(days=int(warmup * 1.7) + 10)


def _trading_universe(cfg: TalonSettings) -> LiquidityUniverse:
    return LiquidityUniverse(size=cfg.universe_size, min_value=cfg.universe_min_trading_value)


def _gate_for(max_positions: int | None) -> RiskGate | None:
    if max_positions is None:
        return None
    return RiskGate(RiskConfig(max_positions=max_positions))


def _selected_strategies(strategy_filter: tuple[str, ...]) -> list[StrategySpec]:
    from talon.quant.strategies import STRATEGY_FACTORIES

    if not strategy_filter:
        specs = default_strategies()
        if not specs:
            raise click.ClickException(
                "활성 전략이 없습니다 — Phase 2 재설계 대기 중입니다 (ADR 0013). "
                f"--strategy 로 명시하십시오 (지원: {sorted(STRATEGY_FACTORIES)})"
            )
        return specs
    unknown = sorted(set(strategy_filter) - set(STRATEGY_FACTORIES))
    if unknown:
        raise click.ClickException(
            f"알 수 없는 전략: {unknown} (지원: {sorted(STRATEGY_FACTORIES)})"
        )
    return [factory() for name, factory in STRATEGY_FACTORIES.items() if name in strategy_filter]


def _record_trial(
    cfg: TalonSettings,
    stats: BacktestStats,
    symbols: tuple[str, ...],
    strategies_desc: list[str],
) -> int:
    daily = stats.sharpe / TRADING_DAYS_PER_YEAR**0.5 if stats.sharpe is not None else None
    with StateDB(cfg.state_path) as state:
        return state.record_trial(
            start=stats.start,
            end=stats.end,
            symbols=sorted(symbols),
            strategies=strategies_desc,
            sharpe_daily=daily,
            trades=stats.trades,
            total_return_pct=stats.total_return_pct,
        )


def _record_cohort_trials(cfg: TalonSettings, report: "CohortReport") -> list[int]:
    ids: list[int] = []
    with StateDB(cfg.state_path) as state:
        for row in report.rows:
            description = (
                f"{row.label} n={row.stats.n} 평균={row.stats.mean_pct:+.3f}% "
                f"Δ={row.delta_mean_pct:+.3f}% t={row.welch_t:.2f} {row.verdict}"
            )
            ids.append(
                state.record_trial(
                    start=report.start,
                    end=report.end,
                    symbols=[],
                    strategies=[description],
                    sharpe_daily=None,
                    trades=row.stats.n,
                    total_return_pct=row.stats.mean_pct,
                )
            )
    return ids


@main.command()
@click.option("--start", "start_text", default=None, help="YYYY-MM-DD")
@click.option("--end", "end_text", default=None, help="YYYY-MM-DD")
@click.option("--symbol", "symbols", multiple=True)
@click.option("--cash", type=float, default=10_000_000.0, show_default=True)
@click.option("--strategy", "strategy_filter", multiple=True)
@click.option("--max-positions", type=int, default=None, help="동시 보유 상한 (기본: RiskConfig)")
@click.option("--regime/--no-regime", "use_regime", default=True, show_default=True)
@click.option("--out", "out_dir", type=click.Path(path_type=Path), default=None)
@click.option("--report", "report_path", type=click.Path(path_type=Path), default=None)
def backtest(
    start_text: str | None,
    end_text: str | None,
    symbols: tuple[str, ...],
    cash: float,
    strategy_filter: tuple[str, ...],
    max_positions: int | None,
    use_regime: bool,
    out_dir: Path | None,
    report_path: Path | None,
) -> None:
    cfg = load_settings()
    start = date.fromisoformat(start_text) if start_text else None
    end = date.fromisoformat(end_text) if end_text else None
    strategies = _selected_strategies(strategy_filter)
    regime_filter: RegimeAssessor = BreadthRegimeFilter() if use_regime else FullExposureRegime()
    load_start = _warmup_start(start, _columns_warmup(strategies, regime_filter))
    with runtime(cfg, toss="skip") as rt:
        panel = load_panel(
            rt.snapshots,
            rt.series,
            start=load_start,
            end=end,
            symbols=list(symbols) or None,
            max_info_stale_days=cfg.universe_info_max_stale_days,
        )
    core = QuantCore(
        panel,
        strategies=strategies,
        regime_filter=regime_filter,
        gate=_gate_for(max_positions),
        universe=_trading_universe(cfg),
    )
    trading_panel = panel if start is None else panel.filter(pl.col("day") >= start)
    try:
        result = run_backtest(trading_panel, core, config=EngineConfig(initial_cash=cash))
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    stats = result.stats
    trial_id = _record_trial(cfg, stats, symbols, [spec.name for spec in strategies])
    click.echo(stats.model_dump_json())
    actions = Counter(intervention.action for intervention in core.interventions)
    click.echo(
        json.dumps({"halted": core.gate.halted, "interventions": dict(actions), "trial": trial_id})
    )
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        result.equity.write_parquet(out_dir / "equity.parquet")
        result.trades.write_parquet(out_dir / "trades.parquet")
        result.rejections.write_parquet(out_dir / "rejections.parquet")
        interventions_frame(core.interventions).write_parquet(out_dir / "interventions.parquet")
        closed_trades_frame(core.closed_trades).write_parquet(out_dir / "strategy_trades.parquet")
        click.echo(str(out_dir))
    if report_path is not None:
        try:
            title = f"talon {result.stats.start} ~ {result.stats.end}"
            write_tearsheet(result, report_path, title=title)
        except TalonError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(str(report_path))


@main.command()
@click.option("--oos-start", "oos_start_text", required=True, help="YYYY-MM-DD")
@click.option("--start", "start_text", default=None, help="YYYY-MM-DD")
@click.option("--end", "end_text", default=None, help="YYYY-MM-DD")
@click.option("--symbol", "symbols", multiple=True)
@click.option("--strategy", "strategy_filter", multiple=True)
@click.option("--cash", type=float, default=10_000_000.0, show_default=True)
@click.option("--max-positions", type=int, default=None, help="동시 보유 상한 (기본: RiskConfig)")
@click.option("--regime/--no-regime", "use_regime", default=True, show_default=True)
@click.option("--out", "out_dir", type=click.Path(path_type=Path), default=None)
def evaluate(
    oos_start_text: str,
    start_text: str | None,
    end_text: str | None,
    symbols: tuple[str, ...],
    strategy_filter: tuple[str, ...],
    cash: float,
    max_positions: int | None,
    use_regime: bool,
    out_dir: Path | None,
) -> None:
    cfg = load_settings()
    oos_start = date.fromisoformat(oos_start_text)
    start = date.fromisoformat(start_text) if start_text else None
    end = date.fromisoformat(end_text) if end_text else None
    strategies = _selected_strategies(strategy_filter)
    regime_filter: RegimeAssessor = BreadthRegimeFilter() if use_regime else FullExposureRegime()
    load_start = _warmup_start(start, _columns_warmup(strategies, regime_filter))
    with runtime(cfg, toss="skip") as rt:
        panel = load_panel(
            rt.snapshots,
            rt.series,
            start=load_start,
            end=end,
            symbols=list(symbols) or None,
            max_info_stale_days=cfg.universe_info_max_stale_days,
        )
        try:
            kospi = load_index_daily(rt.series, "KOSPI")
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        trial_sharpes = rt.state.trial_sharpes()
    try:
        evaluation = evaluate_gate1(
            panel,
            make_core=lambda p: QuantCore(
                p,
                strategies=strategies,
                regime_filter=regime_filter,
                gate=_gate_for(max_positions),
                universe=_trading_universe(cfg),
            ),
            benchmark_daily=kospi,
            oos_start=oos_start,
            trading_start=start,
            config=EngineConfig(initial_cash=cash),
            trial_sharpes=trial_sharpes,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    report = evaluation.report
    click.echo(report.model_dump_json())
    for check in report.checks:
        click.echo(f"{'✓' if check.passed else '✗'} {check.name}: {check.detail}")
    click.echo(f"관문 1: {'통과' if report.passed else '미통과'}")
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "report.json").write_text(report.model_dump_json(indent=2))
        for label, result in (("is", evaluation.in_sample), ("oos", evaluation.out_of_sample)):
            result.equity.write_parquet(out_dir / f"{label}_equity.parquet")
            result.trades.write_parquet(out_dir / f"{label}_trades.parquet")
            result.rejections.write_parquet(out_dir / f"{label}_rejections.parquet")
        click.echo(str(out_dir))
    if not report.passed:
        sys.exit(1)


@main.command()
@click.option("--strategy", "strategy_name", default="close_bet_v1", show_default=True)
@click.option("--start", "start_text", default=None, help="YYYY-MM-DD")
@click.option("--end", "end_text", default=None, help="YYYY-MM-DD (OOS 시작일 이전만 허용)")
@click.option("--oos-start", "oos_start_text", default="2024-01-01", show_default=True)
@click.option("--cash", type=float, default=10_000_000.0, show_default=True)
@click.option("--max-positions", type=int, default=3, show_default=True)
@click.option("--out", "out_path", type=click.Path(path_type=Path), default=None)
def grid(
    strategy_name: str,
    start_text: str | None,
    end_text: str | None,
    oos_start_text: str,
    cash: float,
    max_positions: int,
    out_path: Path | None,
) -> None:
    """선언된 격자 전체를 IS 구간에서 실행하고 시행마다 trials에 기록한다."""
    from talon.backtest.grid import GridRun, approx_pct, clamp_is_end, describe, run_grid
    from talon.quant.strategies import STRATEGY_FACTORIES, STRATEGY_GRIDS

    cfg = load_settings()
    grid_params = STRATEGY_GRIDS.get(strategy_name)
    if grid_params is None:
        raise click.ClickException(
            f"{strategy_name}에는 선언된 격자가 없습니다 (지원: {sorted(STRATEGY_GRIDS)})"
        )
    factory = STRATEGY_FACTORIES[strategy_name]
    oos_start = date.fromisoformat(oos_start_text)
    start = date.fromisoformat(start_text) if start_text else None
    try:
        end = clamp_is_end(date.fromisoformat(end_text) if end_text else None, oos_start)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    regime_filter = FullExposureRegime()
    warmup = max(_columns_warmup([factory(**params)], regime_filter) for params in grid_params)
    load_start = _warmup_start(start, warmup)
    with runtime(cfg, toss="skip") as rt:
        panel = load_panel(
            rt.snapshots,
            rt.series,
            start=load_start,
            end=end,
            symbols=None,
            max_info_stale_days=cfg.universe_info_max_stale_days,
        )
    panel = panel.filter(pl.col("day") < oos_start)
    trading_panel = panel if start is None else panel.filter(pl.col("day") >= start)
    if trading_panel.is_empty():
        raise click.ClickException("IS 구간에 거래일이 없습니다")

    def runner(params: dict[str, float]) -> tuple[BacktestStats, pl.Series, int]:
        spec = factory(**params)
        core = QuantCore(
            panel,
            strategies=[spec],
            regime_filter=regime_filter,
            gate=_gate_for(max_positions),
            universe=_trading_universe(cfg),
        )
        result = run_backtest(trading_panel, core, config=EngineConfig(initial_cash=cash))
        trial = _record_trial(cfg, result.stats, (), [describe(strategy_name, params)])
        return result.stats, result.equity["equity"], trial

    def trial_sharpes() -> list[float]:
        with StateDB(cfg.state_path) as state:
            return state.trial_sharpes()

    def fmt(value: float | None) -> str:
        return f"{value:.2f}" if value is not None else "N/A"

    def show(run: GridRun) -> None:
        stats = run.stats
        pf = f"{stats.profit_factor:.2f}" if stats.profit_factor is not None else "N/A"
        click.echo(
            f"[{run.trial}] {run.description} sharpe={fmt(stats.sharpe)} PF={pf} "
            f"수익률={stats.total_return_pct:.1f}% MDD={stats.mdd_pct:.1f}% "
            f"트레이드={stats.trades}건"
        )

    report = run_grid(
        strategy=strategy_name,
        grid=grid_params,
        runner=runner,
        initial_cash=cash,
        oos_start=oos_start,
        panel_approx_pct=approx_pct(trading_panel),
        trial_sharpes=trial_sharpes,
        progress=show,
    )
    click.echo(report.model_dump_json())
    click.echo(f"일봉 근사 비율: {report.approx_pct:.1f}% (정확한 15:10 상태는 나머지)")
    if report.best is not None:
        click.echo(f"최고 sharpe: {report.best}")
    if report.deflated is not None:
        margin = report.deflated.margin
        click.echo(
            f"DSR(시도 {report.deflated.trials}회 반영): 일간 sharpe "
            f"{report.deflated.sharpe_daily:.4f} vs 우연 기대 최대 "
            f"{report.deflated.expected_max_daily:.4f} → 마진 {margin:+.4f}"
        )
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report.model_dump_json(indent=2))
        click.echo(str(out_path))


@main.command("limits")
@click.option("--start", "start_text", default=None, help="YYYY-MM-DD")
@click.option("--end", "end_text", default=None, help="YYYY-MM-DD")
def limits_audit(start_text: str | None, end_text: str | None) -> None:
    """계산한 상하한가를 실제 일봉과 대조한다 — 위반 0건이 규칙 검증."""
    from talon.backtest.limits import audit_price_limits

    cfg = load_settings()
    start = date.fromisoformat(start_text) if start_text else None
    end = date.fromisoformat(end_text) if end_text else None
    with runtime(cfg, toss="skip") as rt:
        panel = load_panel(
            rt.snapshots,
            rt.series,
            start=start,
            end=end,
            symbols=None,
            max_info_stale_days=cfg.universe_info_max_stale_days,
        )
        delisting = rt.series.read(DELISTING, "registry")
    report = audit_price_limits(panel, delisting=delisting)
    click.echo(report.model_dump_json(indent=2))


@main.command("gap-stats")
@click.option("--start", "start_text", default=None, help="YYYY-MM-DD")
@click.option("--end", "end_text", default=None, help="YYYY-MM-DD (OOS 시작일 이전만 허용)")
@click.option("--oos-start", "oos_start_text", default="2024-01-01", show_default=True)
def gap_stats(start_text: str | None, end_text: str | None, oos_start_text: str) -> None:
    """IS 구간 익일 갭(오늘 종가 → 내일 시가) 하위 분포를 잰다 — 사이징 가정 갭의 근거."""
    from talon.backtest.gaps import overnight_gap_stats
    from talon.backtest.grid import clamp_is_end

    cfg = load_settings()
    oos_start = date.fromisoformat(oos_start_text)
    start = date.fromisoformat(start_text) if start_text else None
    try:
        end = clamp_is_end(date.fromisoformat(end_text) if end_text else None, oos_start)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    with runtime(cfg, toss="skip") as rt:
        panel = load_panel(
            rt.snapshots,
            rt.series,
            start=start,
            end=end,
            symbols=None,
            max_info_stale_days=cfg.universe_info_max_stale_days,
        )
    panel = panel.filter(pl.col("day") < oos_start)
    results = [
        overnight_gap_stats(
            panel,
            universe_size=cfg.universe_size,
            min_value=cfg.universe_min_trading_value,
            strength_floor_pct=floor,
        )
        for floor in (None, 2.0, 3.0, 4.0)
    ]
    for stats in results:
        label = (
            "전체"
            if stats.strength_floor_pct is None
            else f"당일 +{stats.strength_floor_pct:g}% 이상"
        )
        quantiles = " ".join(f"{name}={value:+.2f}%" for name, value in stats.quantiles_pct.items())
        click.echo(f"{label} (표본 {stats.count:,}건): 평균 {stats.mean_pct:+.2f}% {quantiles}")
    click.echo(json.dumps([stats.model_dump() for stats in results], ensure_ascii=False))


@main.command()
@click.option("--start", "start_text", default=None, help="YYYY-MM-DD")
@click.option("--end", "end_text", default=None, help="YYYY-MM-DD (OOS 시작일 이전만 허용)")
@click.option("--oos-start", "oos_start_text", default="2024-01-01", show_default=True)
@click.option("--record", is_flag=True, help="집계 11건을 활성 사이클 trial로 기록")
@click.option("--out", "out_path", type=click.Path(path_type=Path), default=None)
def cohort(
    start_text: str | None,
    end_text: str | None,
    oos_start_text: str,
    record: bool,
    out_path: Path | None,
) -> None:
    """H1·H2 코호트의 익일 갭 분포를 무신호 기준선과 비교한다 (블로그 0단계 진단)."""
    from talon.backtest.cohort import diagnose_cohorts, signal_warmup
    from talon.backtest.grid import clamp_is_end
    from talon.data.store import MARKET_CAP

    cfg = load_settings()
    oos_start = date.fromisoformat(oos_start_text)
    start = date.fromisoformat(start_text) if start_text else None
    try:
        end = clamp_is_end(date.fromisoformat(end_text) if end_text else None, oos_start)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    load_start = _warmup_start(start, signal_warmup())
    with runtime(cfg, toss="skip") as rt:
        panel = load_panel(
            rt.snapshots,
            rt.series,
            start=load_start,
            end=end,
            symbols=None,
            max_info_stale_days=cfg.universe_info_max_stale_days,
        )
        cap_scan = rt.snapshots.scan(MARKET_CAP)
        if cap_scan is None:
            raise click.ClickException(
                "시가총액 스토어가 없습니다 (talon eod / backfill-daily 로 marketcap 적재 필요)"
            )
        caps = cap_scan.select("day", "symbol", "cap").collect()
    panel = panel.filter(pl.col("day") < oos_start).join(caps, on=["day", "symbol"], how="left")
    try:
        report = diagnose_cohorts(
            panel,
            start=start,
            end=end,
            universe_size=cfg.universe_size,
            min_value=cfg.universe_min_trading_value,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(report.model_dump_json())
    click.echo(
        f"유니버스 {report.universe_pairs:,}쌍 · 기준선 {report.baseline.n:,}건 "
        f"(평균 {report.baseline.mean_pct:+.3f}%) · 정지 제외 {report.halt_excluded}건 · "
        f"상한가 제외 H1 {report.limit_up_excluded['h1']}건 / H2 {report.limit_up_excluded['h2']}건"
    )
    for row in report.rows:
        base = "" if row.baseline_label == "baseline" else f" vs {row.baseline_label}"
        click.echo(
            f"{row.label:14} n={row.stats.n:>6} 평균={row.stats.mean_pct:+.3f}% "
            f"중앙값={row.stats.median_pct:+.3f}% 승률={row.stats.win_rate_pct:.1f}% "
            f"Δ={row.delta_mean_pct:+.3f}% t={row.welch_t:+.2f}{base} → {row.verdict}"
        )
    if record:
        trial_ids = _record_cohort_trials(cfg, report)
        click.echo(json.dumps({"recorded_trials": trial_ids}, ensure_ascii=False))
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report.model_dump_json(indent=2))
        click.echo(str(out_path))


@main.command()
@click.option("--strategy", "strategy_name", default="close_bet_v1", show_default=True)
@click.option("--start", "start_text", default=None, help="YYYY-MM-DD")
@click.option("--end", "end_text", default=None, help="YYYY-MM-DD")
@click.option("--out", "out_path", type=click.Path(path_type=Path), default=None)
def fidelity(
    strategy_name: str,
    start_text: str | None,
    end_text: str | None,
    out_path: Path | None,
) -> None:
    """정확한 15:10 패널로 일봉 근사의 오차를 잰다 — 선택 겹침·가격 오차만, 수익률은 안 본다."""
    from talon.backtest.fidelity import measure_fidelity
    from talon.backtest.grid import describe
    from talon.quant.strategies import STRATEGY_FACTORIES, STRATEGY_GRIDS

    cfg = load_settings()
    grid_params = STRATEGY_GRIDS.get(strategy_name)
    if grid_params is None:
        raise click.ClickException(
            f"{strategy_name}에는 선언된 격자가 없습니다 (지원: {sorted(STRATEGY_GRIDS)})"
        )
    factory = STRATEGY_FACTORIES[strategy_name]
    start = date.fromisoformat(start_text) if start_text else None
    end = date.fromisoformat(end_text) if end_text else None
    with runtime(cfg, toss="skip") as rt:
        panel = load_panel(
            rt.snapshots,
            rt.series,
            start=start,
            end=end,
            symbols=None,
            max_info_stale_days=cfg.universe_info_max_stale_days,
        )
    specs = {describe(strategy_name, params): factory(**params) for params in grid_params}
    try:
        report = measure_fidelity(
            panel,
            specs,
            universe_size=cfg.universe_size,
            min_value=cfg.universe_min_trading_value,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"정확한 날 {report.exact_days}일 (조건2 창이 다 정확해지는 날은 {report.settled_days}일), "
        f"유니버스 행 기준 정확 비율 {report.universe_exact_row_pct:.1f}%"
    )
    click.echo(
        "15:10가→종가 오차(절대값): "
        + " ".join(f"{k}={v:.2f}%" for k, v in report.price_error_abs_pct.items())
    )
    click.echo(
        "15:10까지 거래량/하루치 비율: "
        + " ".join(f"{k}={v:.2f}" for k, v in report.volume_ratio.items())
    )
    for name, overlap in report.overlaps.items():
        settled = report.settled_overlaps[name]
        jaccard = f"{overlap.mean_jaccard:.2f}" if overlap.mean_jaccard is not None else "N/A"
        settled_jaccard = (
            f"{settled.mean_jaccard:.2f}" if settled.mean_jaccard is not None else "N/A"
        )
        click.echo(
            f"{name}: 선택 겹침 {jaccard} (정확 {overlap.exact_picks}·근사 "
            f"{overlap.approx_picks}·공통 {overlap.common_picks}) | 안정 구간 {settled_jaccard}"
        )
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report.model_dump_json(indent=2))
        click.echo(str(out_path))


def _numeric_defaults(factory: Callable[..., StrategySpec]) -> dict[str, int | float]:
    return {
        name: parameter.default
        for name, parameter in inspect.signature(factory).parameters.items()
        if isinstance(parameter.default, int | float) and not isinstance(parameter.default, bool)
    }


@main.command()
@click.option("--start", "start_text", default=None, help="YYYY-MM-DD")
@click.option("--end", "end_text", default=None, help="YYYY-MM-DD")
@click.option("--symbol", "symbols", multiple=True)
@click.option("--cash", type=float, default=10_000_000.0, show_default=True)
@click.option("--strategy", "strategy_filter", multiple=True)
@click.option("--out", "out_path", type=click.Path(path_type=Path), default=None)
def sensitivity(
    start_text: str | None,
    end_text: str | None,
    symbols: tuple[str, ...],
    cash: float,
    strategy_filter: tuple[str, ...],
    out_path: Path | None,
) -> None:
    from talon.quant.strategies import ACTIVE_STRATEGIES, STRATEGY_FACTORIES

    cfg = load_settings()
    start = date.fromisoformat(start_text) if start_text else None
    end = date.fromisoformat(end_text) if end_text else None
    if strategy_filter:
        unknown = sorted(set(strategy_filter) - set(STRATEGY_FACTORIES))
        if unknown:
            raise click.ClickException(
                f"알 수 없는 전략: {unknown} (지원: {sorted(STRATEGY_FACTORIES)})"
            )
        selected = tuple(name for name in STRATEGY_FACTORIES if name in strategy_filter)
    else:
        selected = ACTIVE_STRATEGIES
    factories = {name: STRATEGY_FACTORIES[name] for name in selected}
    params = {name: _numeric_defaults(factory) for name, factory in factories.items()}

    def variant(target: str, param: str, value: int | float) -> list[StrategySpec]:
        return [
            factory(**{param: value}) if name == target else factory()
            for name, factory in factories.items()
        ]

    def variant_desc(target: str, param: str, value: int | float) -> list[str]:
        return [f"{name}({param}={value:g})" if name == target else name for name in factories]

    variants: dict[tuple[str, str, float], list[StrategySpec]] = {}
    for name, strategy_params in params.items():
        for param, base_value in strategy_params.items():
            for value in neighbors(base_value):
                variants[(name, param, float(value))] = variant(name, param, value)

    regime_filter = BreadthRegimeFilter()
    base_strategies = [factory() for factory in factories.values()]
    warmup = _columns_warmup(base_strategies, regime_filter)
    for specs in variants.values():
        warmup = max(warmup, _columns_warmup(specs, regime_filter))
    load_start = _warmup_start(start, warmup)
    with runtime(cfg, toss="skip") as rt:
        panel = load_panel(
            rt.snapshots,
            rt.series,
            start=load_start,
            end=end,
            symbols=list(symbols) or None,
            max_info_stale_days=cfg.universe_info_max_stale_days,
        )
    trading_panel = panel if start is None else panel.filter(pl.col("day") >= start)

    def execute(specs: list[StrategySpec], desc: list[str]) -> tuple[BacktestStats, QuantCore]:
        core = QuantCore(
            panel,
            strategies=specs,
            regime_filter=regime_filter,
            universe=_trading_universe(cfg),
        )
        result = run_backtest(trading_panel, core, config=EngineConfig(initial_cash=cash))
        _record_trial(cfg, result.stats, symbols, desc)
        return result.stats, core

    def fmt(value: float | None) -> str:
        return f"{value:.2f}" if value is not None else "N/A"

    try:
        base_stats, _ = execute(base_strategies, [spec.name for spec in base_strategies])
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"기준 실행: sharpe={fmt(base_stats.sharpe)} "
        f"수익률={base_stats.total_return_pct:.1f}% 트레이드={base_stats.trades}건"
    )

    def runner(name: str, param: str, value: int | float) -> tuple[BacktestStats, int | None]:
        specs = variants[(name, param, float(value))]
        stats, core = execute(specs, variant_desc(name, param, value))
        return stats, core.trades_by(name)

    def show(run: SweepRun) -> None:
        click.echo(
            f"{'✓' if run.ok else '✗'} {run.strategy}.{run.param}={run.value:g} "
            f"sharpe={fmt(run.sharpe)} 수익률={run.total_return_pct:.1f}% "
            f"트레이드={run.trades}건 (전략 {run.strategy_trades}건)"
        )

    report = run_sweep(base_stats=base_stats, params=params, runner=runner, progress=show)
    click.echo(report.model_dump_json())
    for verdict in report.params:
        status = "✓" if verdict.robust else "✗"
        note = "" if verdict.active else " [비활성 — 스윕 구간에서 해당 전략 무거래]"
        click.echo(
            f"{status} {verdict.strategy}.{verdict.param} (기준값 {verdict.base_value:g}){note}"
        )
    click.echo(f"민감도: {'통과' if report.robust else '미통과'}")
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report.model_dump_json(indent=2))
        click.echo(str(out_path))
    if not report.robust:
        sys.exit(1)


@main.command()
@click.option("--start", "start_text", default=None, help="YYYY-MM-DD")
@click.option("--end", "end_text", default=None, help="YYYY-MM-DD")
@click.option("--symbol", "symbols", multiple=True)
@click.option("--strategy", "strategy_filter", multiple=True)
@click.option("--cuts", type=int, default=3, show_default=True)
def lookahead(
    start_text: str | None,
    end_text: str | None,
    symbols: tuple[str, ...],
    strategy_filter: tuple[str, ...],
    cuts: int,
) -> None:
    cfg = load_settings()
    start = date.fromisoformat(start_text) if start_text else None
    end = date.fromisoformat(end_text) if end_text else None
    specs = _selected_strategies(strategy_filter)
    intraday_violations = verify_intraday(specs)
    if intraday_violations:
        click.echo(
            json.dumps(
                {
                    "status": "lookahead",
                    "intraday_violations": len(intraday_violations),
                    "intraday_examples": [v.describe() for v in intraday_violations[:5]],
                },
                ensure_ascii=False,
            )
        )
        sys.exit(1)
    columns: dict[str, str] = {}
    for spec in specs:
        columns.update(spec.columns())
    columns.update(BreadthRegimeFilter().columns())
    with runtime(cfg, toss="skip") as rt:
        panel = load_panel(
            rt.snapshots,
            rt.series,
            start=start,
            end=end,
            symbols=list(symbols) or None,
            max_info_stale_days=cfg.universe_info_max_stale_days,
        )
    cut_days = pick_cuts(panel["day"].unique().to_list(), cuts)
    if not cut_days:
        raise click.ClickException("컷을 고를 거래일이 부족합니다")
    factor_violations = verify_factors(panel, columns, cut_days)
    replay_violations = verify_replay(
        panel,
        lambda p: QuantCore(p, strategies=specs),
        cut_days,
    )
    payload: dict[str, object] = {
        "status": "ok" if not factor_violations and not replay_violations else "lookahead",
        "cuts": [day.isoformat() for day in cut_days],
        "factor_violations": len(factor_violations),
        "replay_violations": len(replay_violations),
        "intraday_violations": 0,
    }
    if factor_violations:
        payload["factor_examples"] = [
            f"{v.factor} cut={v.cut} {v.day}/{v.symbol} full={v.full_value} prefix={v.prefix_value}"
            for v in factor_violations[:5]
        ]
    if replay_violations:
        payload["replay_examples"] = [
            f"{v.kind} cut={v.cut} day={v.day} {v.detail}" for v in replay_violations[:5]
        ]
    click.echo(json.dumps(payload, ensure_ascii=False))
    if factor_violations or replay_violations:
        sys.exit(1)


@main.command("crosscheck-engine")
@click.option("--scenarios", type=int, default=10, show_default=True)
@click.option("--seed", type=int, default=42, show_default=True)
@click.option("--symbols", type=int, default=3, show_default=True)
@click.option("--days", type=int, default=120, show_default=True)
def crosscheck_engine(scenarios: int, seed: int, symbols: int, days: int) -> None:
    try:
        report = run_crosscheck(seed=seed, scenarios=scenarios, symbols=symbols, days=days)
    except TalonError as exc:
        raise click.ClickException(str(exc)) from exc
    payload: dict[str, object] = {
        "status": "ok" if report.ok else "mismatch",
        "scenarios": report.scenarios,
        "symbols": report.symbols,
        "trades": report.trades,
        "mismatches": len(report.mismatches),
    }
    if report.mismatches:
        payload["examples"] = [
            f"{m.kind} s{m.scenario}/{m.symbol}: {m.detail}" for m in report.mismatches[:5]
        ]
    click.echo(json.dumps(payload, ensure_ascii=False))
    if not report.ok:
        sys.exit(1)


@main.command()
def watchdog() -> None:
    cfg = load_settings()
    with runtime(cfg, toss="skip") as rt:
        summary = run_watchdog(
            cfg,
            cal=rt.cal,
            state=rt.state,
            snapshots=rt.snapshots,
            series=rt.series,
            alerter=rt.alerter,
        )
    click.echo(summary.model_dump_json())


@main.group()
def universe() -> None:
    pass


@universe.command("rebuild")
def universe_rebuild() -> None:
    cfg = load_settings()
    with runtime(cfg, toss="skip") as rt:
        try:
            symbols = bootstrap_universe(cfg, rt.state, rt.cal, _today_kst(), rt.snapshots)
        except TalonError as exc:
            raise click.ClickException(str(exc)) from exc
    click.echo(f"유니버스 갱신 완료: {len(symbols)}종목")


@universe.command("show")
def universe_show() -> None:
    cfg = load_settings()
    with StateDB(cfg.state_path) as state:
        snapshot = state.latest_universe()
    if snapshot is None:
        click.echo("유니버스 스냅샷이 없습니다 (talon universe rebuild 또는 talon eod 실행)")
        return
    click.echo(f"기준일 {snapshot.day} · {len(snapshot.symbols)}종목")
    click.echo(", ".join(snapshot.symbols[:30]) + (" ..." if len(snapshot.symbols) > 30 else ""))


@main.command()
def status() -> None:
    cfg = load_settings()
    with runtime(cfg, toss="skip") as rt:
        jobs = (
            "collect",
            "eod",
            "reconcile",
            "watchdog",
            "backfill-daily",
            "adjust-build",
            "index-backfill",
            "stock-info-backfill",
        )
        for job in jobs:
            heartbeat = rt.state.get_heartbeat(job)
            runs = rt.state.recent_runs(job, limit=1)
            beat_text = (
                f"{heartbeat.ts.astimezone(KST):%m-%d %H:%M} ok={heartbeat.ok}"
                if heartbeat
                else "-"
            )
            run_text = (
                f"last_run ok={runs[0].ok} {runs[0].detail.get('status', '')}" if runs else ""
            )
            click.echo(f"{job:15} heartbeat {beat_text:22} {run_text}")
        minute_symbols = rt.series.names(MINUTE_CANDLES)
        indicator_symbols = rt.series.names(INDICATOR_MINUTE)
        daily_dates = rt.snapshots.dates(DAILY_CANDLES)
        info_dates = rt.snapshots.dates(STOCK_INFO)
        click.echo(f"분봉 적재 종목: {len(minute_symbols)} · 지표: {len(indicator_symbols)}")
        if daily_dates:
            click.echo(f"일봉 스냅샷: {len(daily_dates)}일 ({daily_dates[0]} ~ {daily_dates[-1]})")
        else:
            click.echo("일봉 스냅샷: 없음")
        if info_dates:
            click.echo(f"종목기본정보: {len(info_dates)}일 ({info_dates[0]} ~ {info_dates[-1]})")
        else:
            click.echo("종목기본정보: 없음 (talon stock-info backfill 필요)")
        snapshot = rt.state.latest_universe()
        if snapshot:
            click.echo(f"유니버스: {snapshot.day} 기준 {len(snapshot.symbols)}종목")
        else:
            click.echo("유니버스: 없음")


@main.group()
def adjust() -> None:
    pass


@adjust.command("build")
@click.option("--force", is_flag=True)
@click.option("--symbol", "symbols", multiple=True)
@click.option("--throttle", type=float, default=0.2, show_default=True)
def adjust_build(force: bool, symbols: tuple[str, ...], throttle: float) -> None:
    from talon.ingest.factors import build_factors

    cfg = load_settings()
    with job_lock(cfg.locks_dir / "adjust.lock") as acquired:
        if not acquired:
            click.echo("adjust build가 이미 실행 중입니다")
            return
        with runtime(cfg, toss="skip") as rt:

            def report(index: int, total: int, symbol: str) -> None:
                if index % 100 == 0 or index == total:
                    click.echo(f"{index}/{total} {symbol}")

            summary = build_factors(
                cfg,
                state=rt.state,
                snapshots=rt.snapshots,
                series=rt.series,
                alerter=rt.alerter,
                symbols=list(symbols) or None,
                force=force,
                throttle=throttle,
                progress=report,
            )
    click.echo(summary.model_dump_json())


@adjust.command("rebase")
@click.option("--symbol", "symbols", multiple=True)
def adjust_rebase(symbols: tuple[str, ...]) -> None:
    from talon.ingest.factors import rebase_factors

    cfg = load_settings()
    with job_lock(cfg.locks_dir / "adjust.lock") as acquired:
        if not acquired:
            click.echo("adjust 잡이 이미 실행 중입니다")
            return
        with runtime(cfg, toss="skip") as rt:

            def report(index: int, total: int, symbol: str) -> None:
                if index % 500 == 0 or index == total:
                    click.echo(f"{index}/{total} {symbol}")

            summary = rebase_factors(
                cfg,
                state=rt.state,
                snapshots=rt.snapshots,
                series=rt.series,
                symbols=list(symbols) or None,
                progress=report,
            )
    click.echo(summary.model_dump_json())


@main.group()
def index() -> None:
    pass


@index.command("backfill")
@click.option("--years", type=int, default=None)
@click.option("--start", "start_text", default=None, help="YYYY-MM-DD")
@click.option("--end", "end_text", default=None, help="YYYY-MM-DD")
@click.option("--symbol", "symbols", multiple=True)
def index_backfill(
    years: int | None,
    start_text: str | None,
    end_text: str | None,
    symbols: tuple[str, ...],
) -> None:
    from talon.ingest.index import backfill_index

    cfg = load_settings()
    with runtime(cfg, toss="skip") as rt:
        end = (
            date.fromisoformat(end_text) if end_text else rt.cal.previous_trading_day(_today_kst())
        )
        if start_text:
            start = date.fromisoformat(start_text)
        else:
            span_years = years if years is not None else cfg.backfill_years
            start = end - timedelta(days=round(365.25 * span_years))
        try:
            summary = backfill_index(
                state=rt.state,
                series=rt.series,
                start=start,
                end=end,
                symbols=list(symbols) or None,
            )
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
    click.echo(summary.model_dump_json())
    if summary.status == "error":
        sys.exit(1)


@main.group("stock-info")
def stock_info() -> None:
    """KRX 공식 종목기본정보 — 유니버스 분류의 정본."""


@stock_info.command("backfill")
@click.option("--years", type=int, default=None)
@click.option("--start", "start_text", default=None, help="YYYY-MM-DD")
@click.option("--end", "end_text", default=None, help="YYYY-MM-DD")
@click.option("--force", is_flag=True, help="이미 받은 날짜도 다시 받는다")
def stock_info_backfill(
    years: int | None,
    start_text: str | None,
    end_text: str | None,
    force: bool,
) -> None:
    from talon.ingest.stockinfo import backfill_stock_info

    cfg = load_settings()
    if not cfg.krx_openapi_configured:
        raise click.ClickException("TALON_KRX_API_KEY 설정이 필요합니다")
    with job_lock(cfg.locks_dir / "stock-info.lock") as acquired:
        if not acquired:
            click.echo("stock-info backfill이 이미 실행 중입니다")
            return
        with runtime(cfg, toss="skip") as rt:
            end = (
                date.fromisoformat(end_text)
                if end_text
                else rt.cal.previous_trading_day(_today_kst())
            )
            if start_text:
                start = date.fromisoformat(start_text)
            else:
                span_years = years if years is not None else cfg.backfill_years
                start = end - timedelta(days=round(365.25 * span_years))

            def report(index: int, total: int, day: date) -> None:
                if index % 25 == 0 or index == total:
                    click.echo(f"{index}/{total} {day}")

            summary = backfill_stock_info(
                cfg,
                cal=rt.cal,
                state=rt.state,
                snapshots=rt.snapshots,
                start=start,
                end=end,
                force=force,
                progress=report,
            )
    click.echo(summary.model_dump_json())


@stock_info.command("show")
@click.option("--date", "day_text", default=None, help="YYYY-MM-DD (기본: 최신)")
def stock_info_show(day_text: str | None) -> None:
    from talon.quant.universe import SECURITY_GROUP, SHARE_KIND, tradable_stock

    cfg = load_settings()
    snapshots = DatePartitionedStore(cfg.parquet_dir)
    days = snapshots.dates(STOCK_INFO)
    if not days:
        click.echo("종목기본정보가 없습니다 (talon stock-info backfill 먼저 실행)")
        return
    day = date.fromisoformat(day_text) if day_text else days[-1]
    frame = snapshots.read_date(STOCK_INFO, day)
    if frame is None:
        raise click.ClickException(f"{day} 종목기본정보가 없습니다")
    tradable = frame.filter(tradable_stock())
    click.echo(f"적재 범위: {days[0]} ~ {days[-1]} ({len(days)}일)")
    click.echo(f"{day} 기준 {frame.height}종목 중 매매대상 보통주 {tradable.height}종목")

    is_stock = pl.col("security_group") == SECURITY_GROUP
    is_common = pl.col("share_kind") == SHARE_KIND
    dropped = frame.filter(~tradable_stock())
    reasons = (
        ("증권군", dropped.filter(~is_stock).group_by("security_group").len()),
        ("주식종류", dropped.filter(is_stock & ~is_common).group_by("share_kind").len()),
        ("소속부", dropped.filter(is_stock & is_common).group_by("section").len()),
    )
    for label, counts in reasons:
        for name, count in sorted(counts.iter_rows(), key=lambda row: -row[1]):
            click.echo(f"  제외 [{label}] {name}: {count}")


@main.group()
def delisting() -> None:
    pass


@delisting.command("refresh")
def delisting_refresh() -> None:
    from talon.sources.delisting import fetch_delisting_registry

    cfg = load_settings()
    registry = fetch_delisting_registry(_today_kst())
    ParquetStore(cfg.parquet_dir).replace(DELISTING, "registry", registry)
    counts = dict(registry.group_by("classification").len().iter_rows())
    breakdown = ", ".join(f"{key} {value}" for key, value in sorted(counts.items()))
    click.echo(f"상폐 레지스트리 {registry.height}건 적재 ({breakdown})")


@main.group()
def holidays() -> None:
    """KRX 휴장일 캘린더 동기화."""


@holidays.command("sync")
def holidays_sync() -> None:
    """KRX 시장정보의 연간 휴장일을 받아 휴장일 캘린더에 반영한다."""
    from talon.ingest.holidays import sync_holidays

    cfg = load_settings()
    with runtime(cfg, toss="skip") as rt:
        summary = sync_holidays(cfg, state=rt.state, alerter=rt.alerter, today=_today_kst())
    click.echo(summary.model_dump_json())
    if summary.status == "error":
        sys.exit(1)


@holidays.command("show")
def holidays_show() -> None:
    """저장된 휴장일 캘린더(정적 목록 + 동기화분)를 출력한다."""
    from talon.markets.kr import CLOSURES_MISSING_FROM_XKRX, closures_path, load_stored_closures

    cfg = load_settings()
    closures = dict(CLOSURES_MISSING_FROM_XKRX)
    closures |= load_stored_closures(closures_path(cfg.data_dir))
    if not closures:
        click.echo("등록된 휴장일이 없습니다")
        return
    for day, name in sorted(closures.items()):
        click.echo(f"{day}\t{name}")


@main.group()
def telegram() -> None:
    pass


@telegram.command("send")
@click.argument("text")
def telegram_send(text: str) -> None:
    cfg = load_settings()
    notifier = TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id)
    try:
        ok = notifier.send(text)
    finally:
        notifier.close()
    click.echo("발송 완료" if ok else "발송 실패 (설정/네트워크 확인)")
    if not ok:
        sys.exit(1)


@telegram.command("test")
@click.pass_context
def telegram_test(ctx: click.Context) -> None:
    ctx.invoke(telegram_send, text="talon 알림 경로 테스트")


@telegram.command("chat-id")
def telegram_chat_id() -> None:
    cfg = load_settings()
    notifier = TelegramNotifier(cfg.telegram_bot_token, "")
    try:
        chats = notifier.list_chats()
    finally:
        notifier.close()
    if not chats:
        click.echo("수신된 대화가 없습니다. 봇에게 아무 메시지나 보낸 뒤 다시 실행하세요.")
        return
    for chat_id, label in chats:
        click.echo(f"{chat_id}\t{label}")


@main.group()
def launchd() -> None:
    pass


@launchd.command("install")
@click.option("--print-only", is_flag=True)
def launchd_install(print_only: bool) -> None:
    cfg = load_settings()
    talon_bin = launchd_mod.default_talon_bin()
    if print_only:
        for job in launchd_mod.JOBS:
            click.echo(launchd_mod.render_plist(job, talon_bin, cfg.data_dir).decode())
        return
    written = launchd_mod.install(talon_bin, cfg.data_dir)
    for path in written:
        click.echo(f"설치됨: {path}")


@launchd.command("uninstall")
def launchd_uninstall() -> None:
    removed = launchd_mod.uninstall()
    if not removed:
        click.echo("설치된 launchd 잡이 없습니다")
        return
    for path in removed:
        click.echo(f"제거됨: {path}")


@main.command()
@click.option("--live", is_flag=True)
def doctor(live: bool) -> None:
    cfg = load_settings()
    failures = 0

    def report(name: str, ok: bool, detail: str) -> None:
        nonlocal failures
        if not ok:
            failures += 1
        click.echo(f"{'✓' if ok else '✗'} {name}: {detail}")

    try:
        probe = cfg.data_dir / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        report("data_dir", True, str(cfg.data_dir))
    except OSError as exc:
        report("data_dir", False, str(exc))

    report("toss 자격증명", cfg.toss_configured, "설정됨" if cfg.toss_configured else "미설정")
    report(
        "telegram 설정", cfg.telegram_configured, "설정됨" if cfg.telegram_configured else "미설정"
    )
    report(
        "KRX 로그인 자격증명",
        cfg.krx_login_configured,
        "설정됨" if cfg.krx_login_configured else "미설정 (TALON_KRX_ID / TALON_KRX_PASSWORD)",
    )
    report(
        "KRX Open API 인증키",
        cfg.krx_openapi_configured,
        "설정됨" if cfg.krx_openapi_configured else "미설정 (TALON_KRX_API_KEY)",
    )

    cal = krx_calendar()
    today = _today_kst()
    trading = cal.is_trading_day(today)
    report("KRX 캘린더", True, f"{today} {'거래일' if trading else '휴장일'}")

    if live:
        if cfg.toss_configured:
            try:
                with _make_toss(cfg) as client:
                    calendar = client.market_calendar_kr()
                report("toss API", True, f"영업일 {calendar.get('today', {}).get('date')}")
            except Exception as exc:
                report("toss API", False, str(exc))
        if cfg.telegram_bot_token:
            notifier = TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id)
            try:
                me = notifier.get_me()
                report("telegram API", me is not None, str(me.get("username")) if me else "실패")
            finally:
                notifier.close()
        day = cal.previous_trading_day(today)
        try:
            from talon.sources.krx_daily import KrxCredentials, fetch_daily_ohlcv

            credentials = (
                KrxCredentials(cfg.krx_id, cfg.krx_password) if cfg.krx_login_configured else None
            )
            frame = fetch_daily_ohlcv(day, credentials=credentials)
            report("pykrx (KRX 로그인)", not frame.is_empty(), f"{day} {frame.height} rows")
        except Exception as exc:
            report("pykrx (KRX 로그인)", False, str(exc))
        if cfg.krx_openapi_configured:
            from talon.sources.krx_openapi import STOCK_ENDPOINTS, KrxOpenApiSource

            source = KrxOpenApiSource(
                cfg.krx_api_key,
                base_url=cfg.krx_openapi_base_url,
                throttle=cfg.krx_openapi_throttle_seconds,
            )
            try:
                for endpoint in STOCK_ENDPOINTS:
                    label = f"KRX Open API {endpoint.split('/')[-1]}"
                    try:
                        rows = source.rows(endpoint, day)
                        report(label, bool(rows), f"{day} {len(rows)} rows")
                    except Exception as exc:
                        report(label, False, str(exc))
            finally:
                source.close()
        try:
            from talon.sources.fdr_daily import fetch_symbol_history

            frame = fetch_symbol_history("005930", today - timedelta(days=14), today)
            report("FinanceDataReader", not frame.is_empty(), f"{frame.height} rows")
        except Exception as exc:
            report("FinanceDataReader", False, str(exc))
        try:
            from talon.sources.fdr_daily import fetch_krx_listing

            daily, _ = fetch_krx_listing(cal.latest_trading_day(today))
            report("FDR KRX 스냅샷", not daily.is_empty(), f"{daily.height} rows")
        except Exception as exc:
            report("FDR KRX 스냅샷", False, str(exc))
        try:
            from talon.sources.marcap_daily import MarcapSource

            with MarcapSource(cfg.marcap_cache_dir) as marcap:
                latest = marcap.latest_available(today.year)
            report("marcap", latest is not None, f"최신 {latest}")
        except Exception as exc:
            report("marcap", False, str(exc))

    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
