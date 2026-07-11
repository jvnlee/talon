import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from types import SimpleNamespace

import click

from talon import __version__
from talon import launchd as launchd_mod
from talon.config import TalonSettings, load_settings
from talon.data.state import StateDB
from talon.data.store import (
    DAILY_CANDLES,
    DELISTING,
    INDICATOR_MINUTE,
    MINUTE_CANDLES,
    DatePartitionedStore,
    ParquetStore,
)
from talon.ingest.collect import bootstrap_universe, run_collect
from talon.ingest.eod import run_eod
from talon.ingest.history import backfill_daily
from talon.ingest.watchdog import run_watchdog
from talon.locks import job_lock
from talon.markets.kr import krx_calendar
from talon.notify.telegram import Alerter, TelegramNotifier
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


@main.command()
def watchdog() -> None:
    cfg = load_settings()
    with runtime(cfg, toss="skip") as rt:
        summary = run_watchdog(
            cfg,
            cal=rt.cal,
            state=rt.state,
            snapshots=rt.snapshots,
            alerter=rt.alerter,
        )
    click.echo(summary.model_dump_json())


@main.group()
def universe() -> None:
    pass


@universe.command("rebuild")
def universe_rebuild() -> None:
    cfg = load_settings()
    with runtime(cfg, toss="optional") as rt:
        symbols = bootstrap_universe(cfg, rt.state, rt.cal, _today_kst(), rt.client)
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
        for job in ("collect", "eod", "watchdog", "backfill-daily", "adjust-build"):
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
        click.echo(f"분봉 적재 종목: {len(minute_symbols)} · 지표: {len(indicator_symbols)}")
        if daily_dates:
            click.echo(f"일봉 스냅샷: {len(daily_dates)}일 ({daily_dates[0]} ~ {daily_dates[-1]})")
        else:
            click.echo("일봉 스냅샷: 없음")
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
                symbols=list(symbols) or None,
                force=force,
                throttle=throttle,
                progress=report,
            )
    click.echo(summary.model_dump_json())


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
        try:
            from talon.sources.krx_daily import fetch_daily_ohlcv

            day = cal.previous_trading_day(today)
            frame = fetch_daily_ohlcv(day)
            report("pykrx", not frame.is_empty(), f"{day} {frame.height} rows")
        except Exception as exc:
            report("pykrx", False, str(exc))
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
