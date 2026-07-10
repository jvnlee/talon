from pathlib import Path
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

GLOBAL_ENV_FILE = Path.home() / ".talon" / "env"

INDICATOR_SYMBOLS = (
    "KOSPI",
    "KOSDAQ",
    "KR_BOND_2Y",
    "KR_BOND_3Y",
    "KR_BOND_5Y",
    "KR_BOND_10Y",
    "KR_BOND_20Y",
    "KR_BOND_30Y",
)

CsvList = Annotated[list[str], NoDecode]


class TalonSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TALON_",
        env_file=(".env", str(GLOBAL_ENV_FILE)),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    data_dir: Path = Path.home() / ".talon"

    toss_client_id: str = ""
    toss_client_secret: str = ""
    toss_base_url: str = "https://openapi.tossinvest.com"
    toss_rps: float = 5.0
    request_timeout: float = 10.0

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    universe_size: int = 300
    universe_min_trading_value: float = 1_000_000_000.0
    pinned_symbols: CsvList = []

    indicator_minute_symbols: CsvList = ["KOSPI", "KOSDAQ"]
    indicator_daily_symbols: CsvList = list(INDICATOR_SYMBOLS)

    collect_max_pages: int = 30
    collect_pre_open_minutes: int = 5
    collect_post_close_minutes: int = 20
    collect_failure_ratio: float = 0.2

    heartbeat_stale_minutes: int = 15
    alert_cooldown_minutes: int = 60

    crosscheck_sample_size: int = 20
    crosscheck_tolerance_pct: float = 0.1

    eod_investor_days: int = 30
    backfill_sleep_seconds: float = 0.2
    backfill_years: int = 10

    @field_validator(
        "pinned_symbols",
        "indicator_minute_symbols",
        "indicator_daily_symbols",
        mode="before",
    )
    @classmethod
    def _parse_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("data_dir", mode="before")
    @classmethod
    def _expand_path(cls, value: object) -> object:
        if isinstance(value, str):
            return Path(value).expanduser()
        if isinstance(value, Path):
            return value.expanduser()
        return value

    @property
    def parquet_dir(self) -> Path:
        return self.data_dir / "parquet" / "kr"

    @property
    def state_path(self) -> Path:
        return self.data_dir / "state.sqlite3"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def locks_dir(self) -> Path:
        return self.data_dir / "locks"

    @property
    def toss_configured(self) -> bool:
        return bool(self.toss_client_id and self.toss_client_secret)

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.parquet_dir, self.logs_dir, self.locks_dir):
            path.mkdir(parents=True, exist_ok=True)


def load_settings(**overrides: object) -> TalonSettings:
    settings = TalonSettings(**overrides)  # type: ignore[arg-type]
    settings.ensure_dirs()
    return settings
