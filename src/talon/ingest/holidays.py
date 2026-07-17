import logging
from collections.abc import Callable
from datetime import date

from talon.config import TalonSettings
from talon.data.state import StateDB
from talon.errors import SourceError
from talon.markets.kr import (
    closures_path,
    krx_calendar,
    load_stored_closures,
    save_stored_closures,
)
from talon.models import HolidaySyncSummary
from talon.notify.telegram import Alerter
from talon.sources.krx_holiday import fetch_holidays

log = logging.getLogger(__name__)


def sync_holidays(
    cfg: TalonSettings,
    *,
    state: StateDB,
    alerter: Alerter,
    today: date,
    fetch: Callable[[int], dict[date, str]] = fetch_holidays,
) -> HolidaySyncSummary:
    run_id = state.start_job("holiday-sync")
    years = [today.year, today.year + 1]
    path = closures_path(cfg.data_dir)
    stored = load_stored_closures(path)

    fetched: dict[date, str] = {}
    errors: list[str] = []
    for year in years:
        try:
            holidays = fetch(year)
        except SourceError as exc:
            errors.append(f"{year}: {exc}")
            continue
        if year == today.year and not holidays:
            errors.append(f"{year}: 휴장일 응답이 비어 있습니다")
            continue
        fetched |= holidays

    added = sorted(day for day in fetched if day not in stored)
    merged = stored | fetched
    if merged != stored:
        save_stored_closures(path, merged)
        krx_calendar.cache_clear()
    if added:
        listed = ", ".join(f"{day} {merged[day]}" for day in added)
        alerter.info("holiday-sync-added", f"휴장일 캘린더에 반영했습니다: {listed}")
    if errors:
        alerter.error("holiday-sync-error", f"휴장일 동기화 실패: {errors[0]}")

    status = "error" if errors and not fetched else ("partial" if errors else "ok")
    summary = HolidaySyncSummary(
        status=status,
        years=years,
        known=len(merged),
        added=[day.isoformat() for day in added],
        errors=errors,
    )
    detail = summary.model_dump(mode="json")
    state.heartbeat("holiday-sync", status != "error", detail)
    state.finish_job(run_id, status != "error", detail)
    return summary
