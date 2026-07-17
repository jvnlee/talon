import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta

import polars as pl

from talon.config import TalonSettings
from talon.data.state import StateDB
from talon.data.store import US_DAILY, US_MACRO_DAILY, ParquetStore
from talon.markets.us import UsCalendar
from talon.models import UsEodSummary
from talon.notify.telegram import Alerter
from talon.sources import fred
from talon.sources.yahoo import fetch_daily_bars
from talon.timeutil import now_utc

log = logging.getLogger(__name__)

JOB = "us-eod"
RESTATEMENT_TOLERANCE = 0.001

MACRO_SPECS: tuple[tuple[str, str, int], ...] = (
    ("VIX", "cboe", 1),
    ("DGS2", "fred", 3),
    ("DGS10", "fred", 3),
    ("T10Y2Y", "fred", 3),
    ("DEXKOUS", "fred", 10),
    ("DTWEXBGS", "fred", 10),
)
VIX_FALLBACK_SERIES = "VIXCLS"

DailyFetcher = Callable[..., pl.DataFrame]


def _restated(existing: pl.DataFrame, fetched: pl.DataFrame) -> bool:
    overlap = existing.join(fetched, on="day", how="inner", suffix="_new")
    if overlap.is_empty():
        return False
    drift = (
        overlap.select(
            ((pl.col("close_new") - pl.col("close")).abs() / pl.col("close").abs()).max()
        ).item()
    )
    return drift is not None and drift > RESTATEMENT_TOLERANCE


def _collect_bars(
    cfg: TalonSettings,
    series: ParquetStore,
    expected: date,
    summary: UsEodSummary,
    fetch: DailyFetcher,
    full: bool,
) -> None:
    for symbol in cfg.us_eod_symbols:
        try:
            stored_last = series.last_value(US_DAILY, symbol, "day")
            if full or stored_last is None:
                frame = fetch(symbol, start=cfg.us_backfill_start)
                if frame.is_empty():
                    summary.failed.append(symbol)
                    continue
                series.replace(US_DAILY, symbol, frame)
                summary.seeded += 1
            else:
                window_start = stored_last - timedelta(days=cfg.us_eod_overlap_days)
                frame = fetch(symbol, start=window_start)
                if frame.is_empty():
                    summary.failed.append(symbol)
                    continue
                existing = series.read(US_DAILY, symbol)
                if existing is not None and _restated(existing, frame):
                    reseed = fetch(symbol, start=cfg.us_backfill_start)
                    if reseed.is_empty():
                        summary.failed.append(symbol)
                        continue
                    series.replace(US_DAILY, symbol, reseed)
                    summary.reseeded += 1
                else:
                    series.upsert(US_DAILY, symbol, frame, key="day")
                    summary.updated += 1
            last = series.last_value(US_DAILY, symbol, "day")
            if last is None or last < expected:
                summary.stale.append(symbol)
        except Exception as exc:
            log.warning("us-eod bars failed: %s (%s)", symbol, exc)
            summary.failed.append(symbol)


def _collect_macro(
    series: ParquetStore,
    uscal: UsCalendar,
    expected: date,
    summary: UsEodSummary,
    captured_at: datetime,
    fetch_series: Callable[..., pl.DataFrame],
    fetch_vix: Callable[..., pl.DataFrame],
) -> None:
    for name, kind, allowed_lag in MACRO_SPECS:
        try:
            if kind == "cboe":
                try:
                    frame = fetch_vix(captured_at)
                except Exception as exc:
                    log.warning("CBOE VIX 실패, FRED 폴백: %s", exc)
                    frame = fetch_series(VIX_FALLBACK_SERIES, captured_at)
            else:
                frame = fetch_series(name, captured_at)
            series.replace(US_MACRO_DAILY, name, frame)
            last = frame["day"].max()
            if not isinstance(last, date):
                summary.macro[name] = "error: 빈 계열"
                continue
            behind = uscal.sessions_behind(last, expected)
            summary.macro[name] = "ok" if behind <= allowed_lag else f"stale: {last.isoformat()}"
        except Exception as exc:
            log.warning("us-eod macro failed: %s (%s)", name, exc)
            summary.macro[name] = f"error: {exc}"


def run_us_eod(
    cfg: TalonSettings,
    *,
    uscal: UsCalendar,
    state: StateDB,
    series: ParquetStore,
    alerter: Alerter,
    full: bool = False,
    now: Callable[[], datetime] = now_utc,
    fetch_daily: DailyFetcher = fetch_daily_bars,
    fetch_macro_series: Callable[..., pl.DataFrame] = fred.fetch_fred_series,
    fetch_vix: Callable[..., pl.DataFrame] = fred.fetch_vix_history,
) -> UsEodSummary:
    run_id = state.start_job(JOB)
    captured_at = now()
    expected = uscal.latest_completed_session(captured_at)
    summary = UsEodSummary(status="ok", symbols=len(cfg.us_eod_symbols))

    _collect_bars(cfg, series, expected, summary, fetch_daily, full)
    _collect_macro(series, uscal, expected, summary, captured_at, fetch_macro_series, fetch_vix)

    macro_bad = [name for name, status in summary.macro.items() if status.startswith("error")]
    macro_stale = [name for name, status in summary.macro.items() if status.startswith("stale")]
    if len(summary.failed) >= summary.symbols and (
        not summary.macro or len(macro_bad) == len(summary.macro)
    ):
        summary.status = "error"
    elif summary.failed or summary.stale or macro_bad or macro_stale:
        summary.status = "partial"

    if summary.status == "error":
        alerter.error("us-eod-error", f"미국 EOD 적재가 전부 실패했습니다 ({summary.symbols}종목)")
    elif summary.status == "partial":
        pieces = []
        if summary.failed:
            pieces.append(f"실패 {', '.join(summary.failed[:5])}")
        if summary.stale:
            pieces.append(f"기대 세션({expected}) 누락 {', '.join(summary.stale[:5])}")
        if macro_bad or macro_stale:
            pieces.append(f"매크로 {', '.join((macro_bad + macro_stale)[:5])}")
        alerter.warning("us-eod-partial", f"미국 EOD 적재 일부 문제: {'; '.join(pieces)}")

    ok = summary.status != "error"
    detail = summary.model_dump(mode="json") | {"expected_session": expected.isoformat()}
    state.heartbeat(JOB, ok, detail)
    state.finish_job(run_id, ok, detail)
    return summary
