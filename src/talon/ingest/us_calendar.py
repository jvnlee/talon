import logging
import time as time_module
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta

import polars as pl

from talon.config import TalonSettings
from talon.data.state import StateDB
from talon.data.store import (
    US_EARNINGS,
    US_EARNINGS_SCHEMA,
    US_EVENTS,
    US_EVENTS_HISTORY,
    US_EVENTS_HISTORY_NAME,
    US_EVENTS_HISTORY_SCHEMA,
    US_EVENTS_SCHEMA,
    DatePartitionedStore,
    ParquetStore,
)
from talon.data.usirdates import override_rows
from talon.markets.kr import KrxCalendar
from talon.markets.us import ET, UsCalendar
from talon.models import UsCalendarSummary
from talon.notify.telegram import Alerter
from talon.sources import fed, fred, nasdaq
from talon.timeutil import KST, now_utc

log = logging.getLogger(__name__)

JOB = "us-calendar"
BACKFILL_START = date(2016, 1, 1)
FOMC_HISTORY_YEARS = range(2016, 2021)

RELEASE_IDS: dict[str, int] = {
    "cpi": 10,
    "nfp": 50,
    "pce": 54,
    "gdp": 53,
    "ppi": 46,
    "claims": 180,
    "retail": 9,
}

TIERS: dict[str, str] = {
    "fomc": "skip",
    "cpi": "skip",
    "nfp": "skip",
    "pce": "shrink",
    "ism_mfg": "shrink",
    "ism_svc": "shrink",
    "retail": "shrink",
    "gdp": "note",
    "claims": "note",
    "ppi": "note",
    "witching": "note",
    "holiday": "note",
    "half_day": "note",
}

ET_TIMES: dict[str, time] = {
    "fomc": time(14, 0),
    "cpi": time(8, 30),
    "nfp": time(8, 30),
    "pce": time(8, 30),
    "gdp": time(8, 30),
    "ppi": time(8, 30),
    "claims": time(8, 30),
    "retail": time(8, 30),
    "ism_mfg": time(10, 0),
    "ism_svc": time(10, 0),
    "witching": time(16, 0),
    "holiday": time(9, 30),
    "half_day": time(13, 0),
}

EARNINGS_ET_TIMES: dict[str, time] = {
    "bmo": time(7, 30),
    "amc": time(16, 5),
    "unknown": time(12, 0),
}


def kst_at(event_day: date, category: str) -> datetime:
    return datetime.combine(event_day, ET_TIMES[category], tzinfo=ET).astimezone(UTC)


def next_trading_day(cal: KrxCalendar, day: date) -> date:
    probe = day + timedelta(days=1)
    for _ in range(30):
        if cal.is_trading_day(probe):
            return probe
        probe += timedelta(days=1)
    raise ValueError(f"{day} 이후 30일 안에 KR 거래일이 없습니다")


def hold_window(cal: KrxCalendar, today: date) -> tuple[datetime, datetime, date]:
    decision = today
    for _ in range(30):
        if cal.is_trading_day(decision):
            break
        decision += timedelta(days=1)
    start = cal.session_close(decision)
    end = cal.session_open(next_trading_day(cal, decision))
    return start, end, decision


def ism_days(uscal: UsCalendar, start: date, end: date) -> list[tuple[date, str]]:
    days: list[tuple[date, str]] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        month_start = date(year, month, 1)
        month_next = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        in_month = uscal.sessions_between(month_start, month_next - timedelta(days=1))
        if in_month:
            days.append((in_month[0], "ism_mfg"))
        if len(in_month) >= 3:
            days.append((in_month[2], "ism_svc"))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return [(day, category) for day, category in days if start <= day <= end]


def _market_structure_events(
    uscal: UsCalendar, start: date, end: date
) -> list[tuple[date, str]]:
    events = [(day, "witching") for day in uscal.witching_days(start, end)]
    events += [(day, "holiday") for day in uscal.holidays_between(start, end)]
    events += [(day, "half_day") for day in uscal.early_closes_between(start, end)]
    return events


def _event_row(
    capture_day: date,
    event_day: date,
    category: str,
    source: str,
    window: tuple[datetime, datetime],
    captured_at: datetime,
) -> dict[str, object]:
    at = kst_at(event_day, category)
    return {
        "day": capture_day,
        "event_day": event_day,
        "kst_at": at,
        "category": category,
        "tier": TIERS[category],
        "source": source,
        "in_hold_window": window[0] <= at < window[1],
        "captured_at": captured_at,
    }


def run_us_calendar(
    cfg: TalonSettings,
    *,
    cal: KrxCalendar,
    uscal: UsCalendar,
    state: StateDB,
    snapshots: DatePartitionedStore,
    series: ParquetStore,
    alerter: Alerter,
    today: date | None = None,
    backfill: bool = False,
    now: Callable[[], datetime] = now_utc,
    fetch_release_dates: Callable[..., list[date]] = fred.fetch_release_dates,
    fetch_fomc_calendar: Callable[..., set[date]] = fed.fetch_fomc_calendar,
    fetch_fomc_history: Callable[..., set[date]] = fed.fetch_fomc_history,
    fetch_earnings: Callable[..., list[dict[str, str]]] = nasdaq.fetch_earnings_calendar,
    sleep: Callable[[float], None] = time_module.sleep,
) -> UsCalendarSummary:
    run_id = state.start_job(JOB)
    captured_at = now()
    day = today or captured_at.astimezone(KST).date()
    horizon = day + timedelta(days=cfg.us_events_forward_days)
    window_start, window_end, decision = hold_window(cal, day)
    window = (window_start, window_end)
    summary = UsCalendarSummary(status="ok", day=day)
    rows: list[dict[str, object]] = []
    history: list[tuple[date, str, str]] = []

    if not cfg.fred_api_key:
        summary.parts["fred"] = "skipped-no-key"
    else:
        try:
            count = 0
            for category, release_id in RELEASE_IDS.items():
                fetch_start = BACKFILL_START if backfill else day
                for event_day in fetch_release_dates(
                    release_id, cfg.fred_api_key, start=fetch_start, end=horizon
                ):
                    count += 1
                    if event_day >= day:
                        rows.append(
                            _event_row(day, event_day, category, "fred", window, captured_at)
                        )
                    if backfill and event_day <= day:
                        history.append((event_day, category, "fred"))
            summary.parts["fred"] = f"ok: {count}"
        except Exception as exc:
            log.warning("us-calendar fred failed: %s", exc)
            summary.parts["fred"] = f"error: {exc}"

    try:
        fomc_days = set(fetch_fomc_calendar())
        if backfill:
            for year in FOMC_HISTORY_YEARS:
                fomc_days |= set(fetch_fomc_history(year))
        for event_day in sorted(fomc_days):
            if day <= event_day <= horizon:
                rows.append(_event_row(day, event_day, "fomc", "fed", window, captured_at))
            if backfill and event_day <= day:
                history.append((event_day, "fomc", "fed"))
        summary.parts["fomc"] = f"ok: {len(fomc_days)}"
    except Exception as exc:
        log.warning("us-calendar fomc failed: %s", exc)
        summary.parts["fomc"] = f"error: {exc}"

    try:
        structural: list[tuple[date, str, str]] = []
        for event_day, category in ism_days(uscal, day, horizon):
            structural.append((event_day, category, "rule"))
        for event_day, category in _market_structure_events(uscal, day, horizon):
            structural.append((event_day, category, "xnys"))
        for event_day, category, source in structural:
            rows.append(_event_row(day, event_day, category, source, window, captured_at))
        if backfill:
            for event_day, category in ism_days(uscal, BACKFILL_START, day):
                history.append((event_day, category, "rule"))
            for event_day, category in _market_structure_events(uscal, BACKFILL_START, day):
                history.append((event_day, category, "xnys"))
        summary.parts["market"] = f"ok: {len(structural)}"
    except Exception as exc:
        log.warning("us-calendar market structure failed: %s", exc)
        summary.parts["market"] = f"error: {exc}"

    if rows:
        frame = pl.DataFrame(rows, schema=US_EVENTS_SCHEMA)
        summary.events = snapshots.upsert_date(US_EVENTS, day, frame, ("category", "event_day"))

    if history:
        records = [
            {
                "event_key": f"{category}:{event_day.isoformat()}",
                "event_day": event_day,
                "kst_at": kst_at(event_day, category),
                "category": category,
                "tier": TIERS[category],
                "source": source,
            }
            for event_day, category, source in sorted(set(history))
        ]
        frame = pl.DataFrame(records, schema=US_EVENTS_HISTORY_SCHEMA)
        summary.history_rows = series.upsert(
            US_EVENTS_HISTORY, US_EVENTS_HISTORY_NAME, frame, key="event_key"
        )

    summary.earnings = _collect_earnings(
        cfg, uscal, snapshots, summary, day, window, captured_at, fetch_earnings, sleep
    )

    errors = [name for name, status in summary.parts.items() if status.startswith("error")]
    if errors and len(errors) == len(summary.parts):
        summary.status = "error"
    elif errors or "skipped-no-key" in summary.parts.values():
        summary.status = "partial"

    if summary.status == "error":
        alerter.error("us-calendar-error", f"{day} 미국 캘린더 수집이 전부 실패했습니다")
    elif errors:
        alerter.warning("us-calendar-partial", f"{day} 미국 캘린더 일부 실패: {errors}")
    elif summary.parts.get("fred") == "skipped-no-key":
        alerter.warning(
            "us-calendar-no-fred-key",
            "FRED API 키가 없어 경제지표 일정을 못 받습니다 — "
            "https://fred.stlouisfed.org/docs/api/api_key.html 에서 무료 발급 후 "
            "TALON_FRED_API_KEY 설정",
        )

    ok = summary.status != "error"
    detail = summary.model_dump(mode="json") | {"decision_day": decision.isoformat()}
    state.heartbeat(JOB, ok, detail)
    state.finish_job(run_id, ok, detail)
    return summary


def _collect_earnings(
    cfg: TalonSettings,
    uscal: UsCalendar,
    snapshots: DatePartitionedStore,
    summary: UsCalendarSummary,
    day: date,
    window: tuple[datetime, datetime],
    captured_at: datetime,
    fetch_earnings: Callable[..., list[dict[str, str]]],
    sleep: Callable[[float], None],
) -> int:
    watchlist = {symbol.upper() for symbol in cfg.us_earnings_symbols}
    horizon = day + timedelta(days=cfg.us_earnings_forward_days)
    found: dict[tuple[str, date], dict[str, object]] = {}
    failures = 0
    scanned = 0
    for session in uscal.sessions_between(day, horizon):
        try:
            entries = fetch_earnings(session)
            scanned += 1
        except Exception as exc:
            log.warning("us-calendar earnings failed: %s (%s)", session, exc)
            failures += 1
            if failures >= 3:
                summary.parts["earnings"] = f"error: {session} 등 {failures}건 실패"
                return 0
            continue
        for entry in entries:
            symbol = entry["symbol"]
            if symbol in watchlist:
                found[(symbol, session)] = {
                    "symbol": symbol,
                    "report_day": session,
                    "when": entry["when"],
                    "confirmed": False,
                    "source": "nasdaq",
                }
        sleep(cfg.us_source_throttle_seconds)
    for override in override_rows():
        report_day = override["report_day"]
        if not isinstance(report_day, date) or not day <= report_day <= horizon:
            continue
        symbol = str(override["symbol"]).upper()
        found[(symbol, report_day)] = {
            "symbol": symbol,
            "report_day": report_day,
            "when": override["when"],
            "confirmed": override["confirmed"],
            "source": "ir",
        }
    if not found:
        summary.parts["earnings"] = f"ok: 0건 ({scanned}일 스캔)"
        return 0
    records: list[dict[str, object]] = []
    for item in found.values():
        report_day = item["report_day"]
        assert isinstance(report_day, date)
        when = str(item["when"])
        at = datetime.combine(report_day, EARNINGS_ET_TIMES[when], tzinfo=ET).astimezone(UTC)
        records.append(
            {
                "day": day,
                "symbol": item["symbol"],
                "report_day": report_day,
                "when": when,
                "confirmed": item["confirmed"],
                "in_hold_window": window[0] <= at < window[1],
                "source": item["source"],
                "captured_at": captured_at,
            }
        )
    frame = pl.DataFrame(records, schema=US_EARNINGS_SCHEMA)
    rows = snapshots.upsert_date(US_EARNINGS, day, frame, ("symbol", "report_day"))
    summary.parts["earnings"] = f"ok: {rows}건 ({scanned}일 스캔)"
    return rows
