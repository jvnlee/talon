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
    report = tmp_path / "tearsheet.html"
    result = runner.invoke(main, ["backtest", "--out", str(out_dir), "--report", str(report)])

    assert result.exit_code == 0, result.output
    stats = json.loads(result.output.splitlines()[0])
    assert stats["initial_cash"] == 10_000_000.0
    assert stats["trades"] == 0
    gate_line = json.loads(result.output.splitlines()[1])
    assert gate_line["trial"] == 1
    for name in ("equity", "trades", "rejections", "interventions", "strategy_trades"):
        assert (out_dir / f"{name}.parquet").exists()
    assert report.exists()

    from talon.data.state import StateDB

    with StateDB(cfg.state_path) as state:
        assert state.trial_count() == 1
        assert state.trial_sharpes() == []


def test_backtest_strategy_filter(tmp_path, monkeypatch, cfg, snapshots, series):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TALON_DATA_DIR", str(cfg.data_dir))
    _write_flat_daily(snapshots, series)

    runner = CliRunner()
    ok = runner.invoke(main, ["backtest", "--strategy", "meanrev"])
    assert ok.exit_code == 0, ok.output

    bad = runner.invoke(main, ["backtest", "--strategy", "nope"])
    assert bad.exit_code != 0
    assert "알 수 없는 전략" in bad.output


def test_evaluate_smoke_on_flat_data(tmp_path, monkeypatch, cfg, snapshots, series):
    import json
    from datetime import date, timedelta

    import polars as pl

    from talon.data.store import INDEX_DAILY
    from talon.sources.fdr_daily import HISTORY_SCHEMA

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TALON_DATA_DIR", str(cfg.data_dir))
    _write_flat_daily(snapshots, series)
    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(6)]
    series.replace(
        INDEX_DAILY,
        "KOSPI",
        pl.DataFrame(
            {
                "day": days,
                "open": [100.0] * 6,
                "high": [100.0] * 6,
                "low": [100.0] * 6,
                "close": [100.0] * 6,
                "volume": [1.0] * 6,
            },
            schema=HISTORY_SCHEMA,
        ),
    )

    runner = CliRunner()
    out_dir = tmp_path / "gate1"
    result = runner.invoke(main, ["evaluate", "--oos-start", "2026-01-08", "--out", str(out_dir)])

    assert result.exit_code == 1, result.output
    report = json.loads(result.output.splitlines()[0])
    assert report["oos_start"] == "2026-01-08"
    assert report["passed"] is False
    assert {check["name"] for check in report["checks"]} == {
        "coverage",
        "oos-vs-kospi",
        "mdd",
        "trades",
        "profit-factor",
        "deflated-sharpe",
    }
    assert "관문 1: 미통과" in result.output
    for name in ("report.json", "is_equity.parquet", "oos_trades.parquet"):
        assert (out_dir / name).exists()


def test_evaluate_requires_index_data(tmp_path, monkeypatch, cfg, snapshots, series):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TALON_DATA_DIR", str(cfg.data_dir))
    _write_flat_daily(snapshots, series)

    runner = CliRunner()
    result = runner.invoke(main, ["evaluate", "--oos-start", "2026-01-08"])

    assert result.exit_code == 1
    assert "index backfill" in result.output


def test_index_backfill_smoke(tmp_path, monkeypatch, cfg):
    import json
    from datetime import date

    import polars as pl

    from talon.sources.fdr_daily import HISTORY_SCHEMA

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TALON_DATA_DIR", str(cfg.data_dir))

    def fake_history(code, start, end):
        return pl.DataFrame(
            {
                "day": [date(2026, 7, 10)],
                "open": [100.0],
                "high": [100.0],
                "low": [100.0],
                "close": [100.0],
                "volume": [1.0],
            },
            schema=HISTORY_SCHEMA,
        )

    monkeypatch.setattr("talon.ingest.index.fetch_symbol_history", fake_history)

    runner = CliRunner()
    result = runner.invoke(main, ["index", "backfill", "--symbol", "KOSPI"])

    assert result.exit_code == 0, result.output
    summary = json.loads(result.output.splitlines()[0])
    assert summary["status"] == "ok"
    assert summary["rows"] == {"KOSPI": 1}


def test_sensitivity_smoke_on_flat_data(tmp_path, monkeypatch, cfg, snapshots, series):
    import json

    from talon.data.state import StateDB

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TALON_DATA_DIR", str(cfg.data_dir))
    _write_flat_daily(snapshots, series)

    runner = CliRunner()
    out_path = tmp_path / "sensitivity.json"
    result = runner.invoke(main, ["sensitivity", "--strategy", "meanrev", "--out", str(out_path)])

    assert result.exit_code == 1, result.output
    json_line = next(
        line for line in result.output.splitlines() if line.startswith('{"base_sharpe"')
    )
    report = json.loads(json_line)
    assert report["robust"] is False
    swept = {(item["strategy"], item["param"]) for item in report["params"]}
    assert ("meanrev", "band_days") in swept
    assert all(strategy == "meanrev" for strategy, _ in swept)
    assert all(item["active"] is False for item in report["params"])
    assert "비활성" in result.output
    assert "민감도: 미통과" in result.output
    assert out_path.exists()

    with StateDB(cfg.state_path) as state:
        assert state.trial_count() == 1 + 2 * len(swept)


def test_sensitivity_rejects_unknown_strategy(tmp_path, monkeypatch, cfg):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TALON_DATA_DIR", str(cfg.data_dir))

    runner = CliRunner()
    result = runner.invoke(main, ["sensitivity", "--strategy", "nope"])

    assert result.exit_code == 1
    assert "알 수 없는 전략" in result.output


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
