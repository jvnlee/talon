import logging
from datetime import date

import polars as pl

from talon.config import TalonSettings
from talon.data.state import StateDB
from talon.data.store import (
    DAILY_CANDLES,
    INDICATOR_DAILY,
    INVESTOR_TRADING,
    MARKET_CAP,
    DatePartitionedStore,
    ParquetStore,
    candles_to_frame,
    investor_records_to_frame,
)
from talon.errors import SourceError
from talon.ingest.universe import candidate_symbols, rebuild_universe
from talon.markets.kr import KrxCalendar
from talon.models import EodSummary
from talon.notify.telegram import Alerter
from talon.sources.crosscheck import crosscheck_daily
from talon.sources.krx_daily import fetch_daily_ohlcv, fetch_market_cap
from talon.sources.toss import TossClient
from talon.timeutil import KST, now_utc

log = logging.getLogger(__name__)

INVESTOR_SYMBOLS = ("KOSPI", "KOSDAQ")


def run_eod(
    cfg: TalonSettings,
    *,
    cal: KrxCalendar,
    state: StateDB,
    snapshots: DatePartitionedStore,
    series: ParquetStore,
    toss: TossClient | None,
    alerter: Alerter,
    today: date | None = None,
    force: bool = False,
) -> EodSummary:
    day = today or now_utc().astimezone(KST).date()
    if not force and not cal.is_trading_day(day):
        return EodSummary(status="skipped-holiday", day=day)
    if not force and snapshots.has_date(DAILY_CANDLES, day):
        return EodSummary(status="already-done", day=day)

    run_id = state.start_job("eod")
    steps: dict[str, str] = {}
    try:
        ohlcv = fetch_daily_ohlcv(day)
    except SourceError as exc:
        state.heartbeat("eod", False, {"error": str(exc)})
        state.finish_job(run_id, False, {"error": str(exc)})
        alerter.alert("eod-error", f"{day} 일봉 수집 실패 (pykrx): {exc}")
        return EodSummary(status="error", day=day, steps={"daily": str(exc)})

    if ohlcv.is_empty():
        steps["daily"] = "data-not-ready"
        state.heartbeat("eod", False, {"steps": steps})
        state.finish_job(run_id, False, {"steps": steps})
        alerter.alert("eod-empty", f"{day} 일봉 데이터가 아직 비어 있습니다 (pykrx)")
        return EodSummary(status="data-not-ready", day=day, steps=steps)

    snapshots.write_date(DAILY_CANDLES, day, ohlcv)
    steps["daily"] = f"{ohlcv.height} rows"

    liquidity = _load_liquidity(cfg, snapshots, day, ohlcv, steps, alerter)
    _load_indicators(cfg, series, toss, steps)
    _load_investor_trading(cfg, series, toss, steps)
    _run_crosscheck(cfg, ohlcv, liquidity, day, steps, alerter)

    universe_size = 0
    try:
        build = rebuild_universe(cfg, state, day, liquidity, toss=toss)
        universe_size = len(build.symbols)
        steps["universe"] = f"{universe_size} symbols"
    except SourceError as exc:
        steps["universe"] = f"error: {exc}"
        alerter.alert("universe-error", f"{day} 유니버스 갱신 실패: {exc}")

    ok = universe_size > 0
    status = "ok" if ok else "degraded"
    detail = {"day": day.isoformat(), "steps": steps}
    state.heartbeat("eod", ok, detail)
    state.finish_job(run_id, ok, detail)
    return EodSummary(status=status, day=day, steps=steps, universe_size=universe_size)


def _load_liquidity(
    cfg: TalonSettings,
    snapshots: DatePartitionedStore,
    day: date,
    ohlcv: pl.DataFrame,
    steps: dict[str, str],
    alerter: Alerter,
) -> pl.DataFrame:
    fallback = ohlcv.select("symbol", "value", "volume")
    try:
        caps = fetch_market_cap(day)
    except SourceError as exc:
        steps["marketcap"] = f"error: {exc}"
        alerter.alert("marketcap-error", f"{day} 시가총액 수집 실패: {exc}")
        return fallback
    if caps.is_empty():
        steps["marketcap"] = "empty"
        return fallback
    snapshots.write_date(MARKET_CAP, day, caps)
    steps["marketcap"] = f"{caps.height} rows"
    return caps.select("symbol", "value", "volume")


def _load_indicators(
    cfg: TalonSettings,
    series: ParquetStore,
    toss: TossClient | None,
    steps: dict[str, str],
) -> None:
    if toss is None:
        steps["indicators"] = "skipped-no-toss"
        return
    loaded = 0
    errors: list[str] = []
    for symbol in cfg.indicator_daily_symbols:
        since = series.last_value(INDICATOR_DAILY, symbol)
        try:
            candles = toss.candles_since(symbol, "1d", since, max_pages=3, indicator=True)
        except SourceError as exc:
            errors.append(f"{symbol}: {exc}")
            continue
        if candles:
            loaded += series.upsert(INDICATOR_DAILY, symbol, candles_to_frame(candles))
    steps["indicators"] = f"{loaded} rows" + (f", errors: {len(errors)}" if errors else "")
    if errors:
        log.warning("indicator daily errors: %s", errors)


def _load_investor_trading(
    cfg: TalonSettings,
    series: ParquetStore,
    toss: TossClient | None,
    steps: dict[str, str],
) -> None:
    if toss is None:
        steps["investor"] = "skipped-no-toss"
        return
    loaded = 0
    errors: list[str] = []
    for symbol in INVESTOR_SYMBOLS:
        try:
            records = toss.investor_trading(symbol, count=cfg.eod_investor_days)
        except SourceError as exc:
            errors.append(f"{symbol}: {exc}")
            continue
        if records:
            frame = investor_records_to_frame(records)
            loaded += series.upsert(INVESTOR_TRADING, symbol, frame, key="day")
    steps["investor"] = f"{loaded} rows" + (f", errors: {len(errors)}" if errors else "")
    if errors:
        log.warning("investor trading errors: %s", errors)


def _run_crosscheck(
    cfg: TalonSettings,
    ohlcv: pl.DataFrame,
    liquidity: pl.DataFrame,
    day: date,
    steps: dict[str, str],
    alerter: Alerter,
) -> None:
    sample = candidate_symbols(liquidity, cfg.crosscheck_sample_size)
    result = crosscheck_daily(ohlcv, day, sample, tolerance_pct=cfg.crosscheck_tolerance_pct)
    steps["crosscheck"] = (
        f"checked {result.checked}, "
        f"mismatch {len(result.discrepancies)}, errors {len(result.errors)}"
    )
    if result.discrepancies:
        lines = ", ".join(
            f"{d.symbol}.{d.field} {d.ours:g}≠{d.theirs:g}" for d in result.discrepancies[:5]
        )
        alerter.alert("crosscheck-mismatch", f"{day} pykrx/FDR 정합성 불일치: {lines}")
    if result.checked == 0 and result.errors:
        alerter.alert("crosscheck-degraded", f"{day} FDR 크로스체크 불가: {result.errors[0]}")
