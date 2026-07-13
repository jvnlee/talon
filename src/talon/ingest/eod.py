import logging
from datetime import date

import polars as pl

from talon.config import TalonSettings
from talon.data.state import StateDB
from talon.data.store import (
    DAILY_CANDLES,
    DAILY_SNAPSHOT_SCHEMA,
    INDICATOR_DAILY,
    INVESTOR_TRADING,
    MARKET_CAP,
    MINUTE_CANDLES,
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
from talon.sources.fdr_daily import fetch_krx_listing
from talon.sources.krx_daily import KrxCredentials, fetch_daily_ohlcv, fetch_market_cap
from talon.sources.toss import TossClient
from talon.timeutil import KST, now_utc

log = logging.getLogger(__name__)

INVESTOR_SYMBOLS = ("KOSPI", "KOSDAQ")
SNAPSHOT_SAMPLE = 3
SNAPSHOT_TOLERANCE = 0.005
MINUTE_COVERAGE_RATIO = 0.9

# 마감 직후에는 시간외 거래분이 아직 집계 중이라 어느 소스든 거래량이 덜 찬다.
# 가격만 대조하고, 거래량 정합성은 익일 KRX 공식 확정본을 쓰는 reconcile 잡이 맡는다.
CROSSCHECK_FIELDS = ("close",)


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
        summary = _run_eod_steps(cfg, cal, state, snapshots, series, toss, alerter, day, steps)
    except Exception as exc:
        log.exception("eod failed")
        state.heartbeat("eod", False, {"error": str(exc), "steps": steps})
        state.finish_job(run_id, False, {"error": str(exc), "steps": steps})
        alerter.alert("eod-error", f"{day} EOD 잡 실패: {exc}")
        return EodSummary(status="error", day=day, steps=steps)

    ok = summary.status == "ok"
    detail = {"day": day.isoformat(), "steps": summary.steps}
    state.heartbeat("eod", ok, detail)
    state.finish_job(run_id, ok, detail)
    if summary.status == "data-not-ready":
        alerter.alert("eod-empty", f"{day} 일봉 데이터를 어느 소스에서도 확보하지 못했습니다")
    return summary


def _run_eod_steps(
    cfg: TalonSettings,
    cal: KrxCalendar,
    state: StateDB,
    snapshots: DatePartitionedStore,
    series: ParquetStore,
    toss: TossClient | None,
    alerter: Alerter,
    day: date,
    steps: dict[str, str],
) -> EodSummary:
    ohlcv, caps, source = _load_daily_snapshots(cfg, day, series, cal, steps, alerter)
    if ohlcv.is_empty():
        steps["daily"] = "data-not-ready"
        return EodSummary(status="data-not-ready", day=day, steps=steps)

    snapshots.write_date(DAILY_CANDLES, day, ohlcv)
    steps["daily"] = f"{ohlcv.height} rows ({source})"
    if caps is not None and not caps.is_empty():
        snapshots.write_date(MARKET_CAP, day, caps)
        steps["marketcap"] = f"{caps.height} rows ({source})"
        liquidity = caps.select("symbol", "value", "volume")
    else:
        liquidity = ohlcv.select("symbol", "value", "volume")

    _load_indicators(cfg, series, toss, steps)
    _load_investor_trading(cfg, series, toss, steps)
    if source == "pykrx":
        _run_crosscheck(cfg, ohlcv, liquidity, day, steps, alerter)
    else:
        steps["crosscheck"] = f"skipped ({source})"

    universe_size = 0
    try:
        build = rebuild_universe(cfg, state, day, liquidity, snapshots=snapshots)
        universe_size = len(build.symbols)
        steps["universe"] = f"{universe_size} symbols"
        if not build.criteria["admin_excluded"]:
            alerter.alert(
                "admin-list-unavailable",
                f"{day} 관리종목 목록을 받지 못해 KOSPI 관리종목을 거르지 못했습니다 "
                "(코스닥 관리종목은 KRX 공식 분류로 계속 걸립니다)",
            )
    except SourceError as exc:
        steps["universe"] = f"error: {exc}"
        alerter.alert("universe-error", f"{day} 유니버스 갱신 실패: {exc}")

    status = "ok" if universe_size > 0 else "degraded"
    return EodSummary(status=status, day=day, steps=steps, universe_size=universe_size)


def _load_daily_snapshots(
    cfg: TalonSettings,
    day: date,
    series: ParquetStore,
    cal: KrxCalendar,
    steps: dict[str, str],
    alerter: Alerter,
) -> tuple[pl.DataFrame, pl.DataFrame | None, str]:
    empty = pl.DataFrame(schema=DAILY_SNAPSHOT_SCHEMA)
    ohlcv = empty
    credentials = KrxCredentials(cfg.krx_id, cfg.krx_password) if cfg.krx_login_configured else None
    try:
        ohlcv = fetch_daily_ohlcv(day, credentials=credentials)
    except SourceError as exc:
        steps["pykrx"] = f"error: {exc}"
    if not ohlcv.is_empty():
        caps: pl.DataFrame | None = None
        try:
            caps_frame = fetch_market_cap(day, credentials=credentials)
            caps = caps_frame if not caps_frame.is_empty() else None
        except SourceError as exc:
            steps["marketcap"] = f"error: {exc}"
            alerter.alert("marketcap-error", f"{day} 시가총액 수집 실패: {exc}")
        return ohlcv, caps, "pykrx"

    try:
        listing_daily, listing_caps = fetch_krx_listing(day)
    except SourceError as exc:
        steps["fdr_listing"] = f"error: {exc}"
        return empty, None, "none"
    if listing_daily.is_empty():
        steps["fdr_listing"] = "empty"
        return empty, None, "none"
    verdict = _matches_minute_bars(listing_daily, day, series, cal)
    if verdict is False:
        steps["fdr_listing"] = "stale-or-mismatch"
        return empty, None, "none"
    suffix = "" if verdict else " (분봉 대조 검증 불가)"
    alerter.alert(
        "eod-fallback",
        f"{day} 일봉을 pykrx 대신 FDR 전종목 스냅샷으로 적재했습니다{suffix}",
    )
    return listing_daily, listing_caps, "fdr-listing"


def _matches_minute_bars(
    frame: pl.DataFrame,
    day: date,
    series: ParquetStore,
    cal: KrxCalendar,
    *,
    sample: int = SNAPSHOT_SAMPLE,
    tolerance: float = SNAPSHOT_TOLERANCE,
) -> bool | None:
    checked = 0
    for row in frame.sort("value", descending=True).head(sample).iter_rows(named=True):
        extremes = _session_extremes(series, cal, row["symbol"], day)
        if extremes is None:
            continue
        checked += 1
        high, low = extremes
        if _deviates(float(row["high"]), high, tolerance) or _deviates(
            float(row["low"]), low, tolerance
        ):
            log.warning(
                "daily snapshot disagrees with minute bars for %s on %s: "
                "snapshot high=%s low=%s, minutes high=%s low=%s",
                row["symbol"],
                day,
                row["high"],
                row["low"],
                high,
                low,
            )
            return False
    return True if checked else None


def _session_extremes(
    series: ParquetStore,
    cal: KrxCalendar,
    symbol: str,
    day: date,
) -> tuple[float, float] | None:
    frame = series.read(MINUTE_CANDLES, symbol)
    if frame is None or frame.is_empty():
        return None
    opened = cal.session_open(day)
    closed = cal.session_close(day)
    bars = frame.filter(pl.col("ts").is_between(opened, closed))
    expected = round((closed - opened).total_seconds() / 60)
    if bars.height < expected * MINUTE_COVERAGE_RATIO:
        return None
    high, low = bars.select(pl.col("high").max(), pl.col("low").min()).row(0)
    return float(high), float(low)


def _deviates(ours: float, theirs: float, tolerance: float) -> bool:
    return abs(ours - theirs) / max(ours, theirs, 1.0) > tolerance


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
    result = crosscheck_daily(
        ohlcv,
        day,
        sample,
        tolerance_pct=cfg.crosscheck_tolerance_pct,
        fields=CROSSCHECK_FIELDS,
    )
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
