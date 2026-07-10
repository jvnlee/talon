from talon.config import TalonSettings


def test_defaults(tmp_path):
    cfg = TalonSettings(_env_file=None, data_dir=tmp_path)
    assert cfg.universe_size == 300
    assert cfg.indicator_minute_symbols == ["KOSPI", "KOSDAQ"]
    assert not cfg.toss_configured
    assert not cfg.telegram_configured
    assert cfg.state_path == tmp_path / "state.sqlite3"
    assert cfg.parquet_dir == tmp_path / "parquet" / "kr"


def test_csv_lists_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TALON_PINNED_SYMBOLS", "005930, 000660,")
    monkeypatch.setenv("TALON_INDICATOR_MINUTE_SYMBOLS", "KOSPI")
    monkeypatch.setenv("TALON_DATA_DIR", str(tmp_path))
    cfg = TalonSettings(_env_file=None)
    assert cfg.pinned_symbols == ["005930", "000660"]
    assert cfg.indicator_minute_symbols == ["KOSPI"]
    assert cfg.data_dir == tmp_path


def test_ensure_dirs(tmp_path):
    cfg = TalonSettings(_env_file=None, data_dir=tmp_path / "nested" / "data")
    cfg.ensure_dirs()
    assert cfg.parquet_dir.is_dir()
    assert cfg.logs_dir.is_dir()
    assert cfg.locks_dir.is_dir()


def test_toss_configured(tmp_path):
    cfg = TalonSettings(
        _env_file=None,
        data_dir=tmp_path,
        toss_client_id="id",
        toss_client_secret="secret",
    )
    assert cfg.toss_configured
