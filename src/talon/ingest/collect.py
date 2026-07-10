import logging
from datetime import date, timedelta

from talon.config import TalonSettings
from talon.data.state import StateDB
from talon.data.store import (
    INDICATOR_MINUTE,
    MINUTE_CANDLES,
    ParquetStore,
    candles_to_frame,
)
from talon.errors import SourceError
from talon.ingest.universe import rebuild_universe
from talon.markets.kr import KrxCalendar, within_session
from talon.models import CollectSummary
from talon.notify.telegram import Alerter
from talon.sources.fdr_daily import fetch_krx_listing
from talon.sources.krx_daily import fetch_market_cap
from talon.sources.toss import TossClient, TossError
from talon.timeutil import KST, minute_floor, now_utc

log = logging.getLogger(__name__)

PROGRESS_HEARTBEAT_EVERY = 50
AUTH_ERROR_CODES = {"invalid_client", "unauthorized_client", "access_denied"}


def bootstrap_universe(
    cfg: TalonSettings,
    state: StateDB,
    cal: KrxCalendar,
    day: date,
    toss: TossClient | None,
) -> list[str]:
    latest = cal.latest_trading_day(day)
    probe = latest
    for _ in range(7):
        try:
            caps = fetch_market_cap(probe)
        except SourceError as exc:
            log.warning("pykrx market cap failed for %s: %s", probe, exc)
            break
        if not caps.is_empty():
            build = rebuild_universe(cfg, state, probe, caps, toss=toss)
            return build.symbols
        probe = cal.previous_trading_day(probe)
    try:
        _, caps = fetch_krx_listing(latest)
    except SourceError as exc:
        raise SourceError(f"universe bootstrap failed on all sources: {exc}") from exc
    if caps.is_empty():
        raise SourceError("universe bootstrap failed: FDR listing snapshot empty")
    build = rebuild_universe(cfg, state, latest, caps, toss=toss)
    return build.symbols


def _is_auth_error(error: TossError) -> bool:
    return error.status == 401 or error.code in AUTH_ERROR_CODES


def run_collect(
    cfg: TalonSettings,
    *,
    cal: KrxCalendar,
    state: StateDB,
    store: ParquetStore,
    client: TossClient,
    alerter: Alerter,
    now=None,
    force: bool = False,
) -> CollectSummary:
    now = now or now_utc()
    pre = timedelta(minutes=cfg.collect_pre_open_minutes)
    post = timedelta(minutes=cfg.collect_post_close_minutes)
    if not force and not within_session(cal, now, pre=pre, post=post):
        state.heartbeat("collect", True, {"status": "skipped-closed"})
        return CollectSummary(status="skipped-closed")

    run_id = state.start_job("collect")
    try:
        summary = _collect(cfg, cal, state, store, client, now)
    except Exception as exc:
        log.exception("collect failed")
        state.heartbeat("collect", False, {"error": str(exc)})
        state.finish_job(run_id, False, {"error": str(exc)})
        alerter.alert("collect-error", f"분봉 수집 실패: {exc}")
        return CollectSummary(status="error")

    ok = summary.status == "ok"
    state.heartbeat("collect", ok, summary.model_dump(mode="json"))
    state.finish_job(run_id, ok, summary.model_dump(mode="json"))
    if not ok:
        alerter.alert(
            "collect-degraded",
            f"분봉 수집 실패 종목 {len(summary.failed)}/{summary.symbols}: "
            f"{', '.join(summary.failed[:10])}",
        )
    return summary


def _collect(
    cfg: TalonSettings,
    cal: KrxCalendar,
    state: StateDB,
    store: ParquetStore,
    client: TossClient,
    now,
) -> CollectSummary:
    snapshot = state.latest_universe()
    if snapshot is not None:
        universe = snapshot.symbols
    else:
        log.info("no universe snapshot, bootstrapping")
        universe = bootstrap_universe(cfg, state, cal, now.astimezone(KST).date(), client)
    symbols = list(dict.fromkeys([*universe, *cfg.pinned_symbols]))
    cutoff = minute_floor(now)

    rows = 0
    failed: list[str] = []
    for index, symbol in enumerate(symbols, start=1):
        try:
            rows += _pull_minutes(store, client, cfg, MINUTE_CANDLES, symbol, cutoff)
        except TossError as exc:
            if _is_auth_error(exc):
                raise
            failed.append(symbol)
            log.warning("minute pull failed for %s: %s", symbol, exc)
        if index % PROGRESS_HEARTBEAT_EVERY == 0:
            state.heartbeat(
                "collect", True, {"status": "collecting", "progress": f"{index}/{len(symbols)}"}
            )

    indicator_rows = 0
    for symbol in cfg.indicator_minute_symbols:
        try:
            indicator_rows += _pull_minutes(
                store, client, cfg, INDICATOR_MINUTE, symbol, cutoff, indicator=True
            )
        except TossError as exc:
            if _is_auth_error(exc):
                raise
            failed.append(symbol)
            log.warning("indicator pull failed for %s: %s", symbol, exc)

    total = len(symbols) + len(cfg.indicator_minute_symbols)
    ratio = len(failed) / max(total, 1)
    status = "ok" if ratio <= cfg.collect_failure_ratio else "degraded"
    return CollectSummary(
        status=status,
        symbols=len(symbols),
        failed=failed,
        rows=rows,
        indicator_rows=indicator_rows,
    )


def _pull_minutes(
    store: ParquetStore,
    client: TossClient,
    cfg: TalonSettings,
    dataset: str,
    symbol: str,
    cutoff,
    *,
    indicator: bool = False,
) -> int:
    since = store.last_value(dataset, symbol)
    candles = client.candles_since(
        symbol,
        "1m",
        since,
        max_pages=cfg.collect_max_pages,
        indicator=indicator,
    )
    closed = [candle for candle in candles if candle.ts < cutoff]
    if not closed:
        return 0
    return store.upsert(dataset, symbol, candles_to_frame(closed))
