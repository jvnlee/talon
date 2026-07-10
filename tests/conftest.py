import os
from datetime import UTC, datetime, timedelta

import pytest

from talon.config import TalonSettings
from talon.data.state import StateDB
from talon.data.store import DatePartitionedStore, ParquetStore
from talon.models import Candle
from talon.notify.telegram import Alerter


@pytest.fixture(autouse=True)
def _clean_talon_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("TALON_"):
            monkeypatch.delenv(key)
    monkeypatch.setitem(TalonSettings.model_config, "env_file", None)


@pytest.fixture
def cfg(tmp_path) -> TalonSettings:
    settings = TalonSettings(_env_file=None, data_dir=tmp_path / "data")
    settings.ensure_dirs()
    return settings


@pytest.fixture
def state(cfg) -> StateDB:
    with StateDB(cfg.state_path) as db:
        yield db


@pytest.fixture
def series(cfg) -> ParquetStore:
    return ParquetStore(cfg.parquet_dir)


@pytest.fixture
def snapshots(cfg) -> DatePartitionedStore:
    return DatePartitionedStore(cfg.parquet_dir)


@pytest.fixture(scope="session")
def cal():
    from talon.markets.kr import KrxCalendar

    return KrxCalendar()


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    @property
    def can_send(self) -> bool:
        return True

    def send(self, text: str) -> bool:
        self.sent.append(text)
        return True


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


@pytest.fixture
def alerter(notifier, state) -> Alerter:
    return Alerter(notifier, state, timedelta(0))


def make_candle(ts: datetime, price: float = 100.0, volume: float = 10.0) -> Candle:
    return Candle(ts=ts, open=price, high=price, low=price, close=price, volume=volume)


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)
