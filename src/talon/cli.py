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

import click
import polars as pl

from talon import __version__
from talon import launchd as launchd_mod
from talon.backtest.benchmark import load_index_daily
from talon.backtest.crosscheck import run_crosscheck
from talon.backtest.data import PANEL_COLUMNS, load_panel
from talon.backtest.engine import EngineConfig, run_backtest
from talon.backtest.evaluate import evaluate_gate1
from talon.backtest.lookahead import pick_cuts, verify_factors, verify_replay
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
    DatePartitionedStore,
    ParquetStore,
)
from talon.errors import TalonError
from talon.factors.engine import warmup_periods
from talon.ingest.collect import bootstrap_universe, run_collect
from talon.ingest.eod import run_eod
from talon.ingest.history import backfill_daily
from talon.ingest.watchdog import run_watchdog
from talon.locks import job_lock
from talon.markets.kr import krx_calendar
from talon.notify.telegram import Alerter, TelegramNotifier
from talon.quant.core import QuantCore, closed_trades_frame
from talon.quant.regime import BreadthRegimeFilter
from talon.quant.risk import interventions_frame
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
@click.option("--days", type=int, default=None, help="되돌아볼 거래일 수")
@click.option("--start", "start_text", default=None, help="YYYY-MM-DD")
@click.option("--end", "end_text", default=None, help="YYYY-MM-DD")
def reconcile(days: int | None, start_text: str | None, end_text: str | None) -> None:
    """저장된 일봉을 KRX 공식 확정본(Open API)과 대조해 교정한다."""
    from talon.ingest.reconcile import reconcile_daily

    cfg = load_settings()
    if not cfg.krx_openapi_configured:
        raise click.ClickException("TALON_KRX_API_KEY 설정이 필요합니다")
    with job_lock(cfg.locks_dir / "reconcile.lock") as acquired:
        if not acquired:
            click.echo("reconcile이 이미 실행 중입니다")
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


def _columns_warmup(strategies: list[StrategySpec], regime_filter: BreadthRegimeFilter) -> int:
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


def _selected_strategies(strategy_filter: tuple[str, ...]) -> list[StrategySpec]:
    from talon.quant.strategies import STRATEGY_FACTORIES

    if not strategy_filter:
        return default_strategies()
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


@main.command()
@click.option("--start", "start_text", default=None, help="YYYY-MM-DD")
@click.option("--end", "end_text", default=None, help="YYYY-MM-DD")
@click.option("--symbol", "symbols", multiple=True)
@click.option("--cash", type=float, default=10_000_000.0, show_default=True)
@click.option("--strategy", "strategy_filter", multiple=True)
@click.option("--out", "out_dir", type=click.Path(path_type=Path), default=None)
@click.option("--report", "report_path", type=click.Path(path_type=Path), default=None)
def backtest(
    start_text: str | None,
    end_text: str | None,
    symbols: tuple[str, ...],
    cash: float,
    strategy_filter: tuple[str, ...],
    out_dir: Path | None,
    report_path: Path | None,
) -> None:
    cfg = load_settings()
    start = date.fromisoformat(start_text) if start_text else None
    end = date.fromisoformat(end_text) if end_text else None
    strategies = _selected_strategies(strategy_filter)
    regime_filter = BreadthRegimeFilter()
    load_start = _warmup_start(start, _columns_warmup(strategies, regime_filter))
    with runtime(cfg, toss="skip") as rt:
        panel = load_panel(
            rt.snapshots,
            rt.series,
            start=load_start,
            end=end,
            symbols=list(symbols) or None,
        )
    core = QuantCore(
        panel,
        strategies=strategies,
        regime_filter=regime_filter,
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
@click.option("--cash", type=float, default=10_000_000.0, show_default=True)
@click.option("--out", "out_dir", type=click.Path(path_type=Path), default=None)
def evaluate(
    oos_start_text: str,
    start_text: str | None,
    end_text: str | None,
    symbols: tuple[str, ...],
    cash: float,
    out_dir: Path | None,
) -> None:
    cfg = load_settings()
    oos_start = date.fromisoformat(oos_start_text)
    start = date.fromisoformat(start_text) if start_text else None
    end = date.fromisoformat(end_text) if end_text else None
    strategies = default_strategies()
    regime_filter = BreadthRegimeFilter()
    load_start = _warmup_start(start, _columns_warmup(strategies, regime_filter))
    with runtime(cfg, toss="skip") as rt:
        panel = load_panel(
            rt.snapshots,
            rt.series,
            start=load_start,
            end=end,
            symbols=list(symbols) or None,
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
@click.option("--cuts", type=int, default=3, show_default=True)
def lookahead(
    start_text: str | None,
    end_text: str | None,
    symbols: tuple[str, ...],
    cuts: int,
) -> None:
    cfg = load_settings()
    start = date.fromisoformat(start_text) if start_text else None
    end = date.fromisoformat(end_text) if end_text else None
    columns: dict[str, str] = {}
    for spec in default_strategies():
        columns.update(spec.columns())
    columns.update(BreadthRegimeFilter().columns())
    with runtime(cfg, toss="skip") as rt:
        panel = load_panel(
            rt.snapshots,
            rt.series,
            start=start,
            end=end,
            symbols=list(symbols) or None,
        )
    cut_days = pick_cuts(panel["day"].unique().to_list(), cuts)
    if not cut_days:
        raise click.ClickException("컷을 고를 거래일이 부족합니다")
    factor_violations = verify_factors(panel, columns, cut_days)
    replay_violations = verify_replay(
        panel,
        lambda p: QuantCore(p, strategies=default_strategies()),
        cut_days,
    )
    payload: dict[str, object] = {
        "status": "ok" if not factor_violations and not replay_violations else "lookahead",
        "cuts": [day.isoformat() for day in cut_days],
        "factor_violations": len(factor_violations),
        "replay_violations": len(replay_violations),
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
