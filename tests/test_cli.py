from click.testing import CliRunner

from talon.cli import main


def test_help_smoke():
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    for command in ("collect", "eod", "backfill-daily", "watchdog", "status", "launchd"):
        assert command in result.output


def test_status_on_empty_data_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TALON_DATA_DIR", str(tmp_path / "data"))
    runner = CliRunner()
    result = runner.invoke(main, ["status"])
    assert result.exit_code == 0
    assert "유니버스" in result.output


def test_collect_requires_toss_credentials(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TALON_DATA_DIR", str(tmp_path / "data"))
    runner = CliRunner()
    result = runner.invoke(main, ["collect"])
    assert result.exit_code != 0
    assert "TALON_TOSS_CLIENT_ID" in result.output


def test_launchd_install_print_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TALON_DATA_DIR", str(tmp_path / "data"))
    runner = CliRunner()
    result = runner.invoke(main, ["launchd", "install", "--print-only"])
    assert result.exit_code == 0
    assert "com.talon.collect" in result.output
    assert "StartCalendarInterval" in result.output


def _write_flat_daily(snapshots, series, count=6):
    from datetime import date, timedelta

    import polars as pl

    from talon.data.adjust import FACTOR_SCHEMA
    from talon.data.store import ADJUST_FACTORS, DAILY_CANDLES, DAILY_SNAPSHOT_SCHEMA

    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(count)]
    for day in days:
        snapshots.write_date(
            DAILY_CANDLES,
            day,
            pl.DataFrame(
                {
                    "day": [day],
                    "symbol": ["AAA"],
                    "open": [100.0],
                    "high": [100.0],
                    "low": [100.0],
                    "close": [100.0],
                    "volume": [1000.0],
                    "value": [100_000.0],
                    "change_pct": [0.0],
                },
                schema=DAILY_SNAPSHOT_SCHEMA,
            ),
        )
    series.replace(
        ADJUST_FACTORS,
        "AAA",
        pl.DataFrame({"day": days, "factor": [1.0] * len(days)}, schema=FACTOR_SCHEMA),
    )


def test_backtest_smoke_on_flat_data(tmp_path, monkeypatch, cfg, snapshots, series):
    import json

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TALON_DATA_DIR", str(cfg.data_dir))
    _write_flat_daily(snapshots, series)

    runner = CliRunner()
    out_dir = tmp_path / "bt"
    result = runner.invoke(main, ["backtest", "--out", str(out_dir)])

    assert result.exit_code == 0, result.output
    stats = json.loads(result.output.splitlines()[0])
    assert stats["initial_cash"] == 10_000_000.0
    assert stats["trades"] == 0
    for name in ("equity", "trades", "rejections", "interventions"):
        assert (out_dir / f"{name}.parquet").exists()


def test_lookahead_smoke_on_flat_data(tmp_path, monkeypatch, cfg, snapshots, series):
    import json

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TALON_DATA_DIR", str(cfg.data_dir))
    _write_flat_daily(snapshots, series)

    runner = CliRunner()
    result = runner.invoke(main, ["lookahead", "--cuts", "2"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.splitlines()[0])
    assert payload["status"] == "ok"
    assert payload["factor_violations"] == 0
    assert payload["replay_violations"] == 0
    assert len(payload["cuts"]) == 2
