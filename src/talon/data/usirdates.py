from datetime import date

IR_EARNINGS_OVERRIDES: tuple[dict[str, object], ...] = (
    {"symbol": "TSLA", "report_day": date(2026, 7, 22), "when": "amc", "confirmed": True},
    {"symbol": "AAPL", "report_day": date(2026, 7, 30), "when": "amc", "confirmed": True},
    {"symbol": "AMD", "report_day": date(2026, 8, 4), "when": "amc", "confirmed": True},
)


def override_rows() -> list[dict[str, object]]:
    rows = []
    for entry in IR_EARNINGS_OVERRIDES:
        rows.append(
            {
                "symbol": str(entry["symbol"]),
                "report_day": entry["report_day"],
                "when": str(entry.get("when", "unknown")),
                "confirmed": bool(entry.get("confirmed", True)),
            }
        )
    return rows
