IR_EARNINGS_OVERRIDES: tuple[dict[str, object], ...] = ()


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
