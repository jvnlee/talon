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
