from datetime import date
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

US_EOD_SYMBOLS = (
    "^GSPC",
    "^IXIC",
    "^NDX",
    "^SOX",
    "^DJI",
    "^RUT",
    "NVDA",
    "MU",
    "TSM",
    "TSLA",
    "AVGO",
    "AMD",
    "MRVL",
    "AAPL",
    "SKHY",
    "EWY",
    "CPNG",
    "PKX",
    "KB",
    "SHG",
    "WF",
    "SKM",
    "KT",
    "LPL",
    "KEP",
    "GRVY",
    "WBTN",
)

US_EARNINGS_SYMBOLS = (
    "NVDA",
    "MU",
    "TSM",
    "TSLA",
    "AVGO",
    "AMD",
    "MRVL",
    "AAPL",
    "SKHY",
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

    dart_api_key: str = ""
    dart_throttle_seconds: float = 0.5

    kis_app_key: str = ""
    kis_app_secret: str = ""
    kis_base_url: str = "https://openapi.koreainvestment.com:9443"
    kis_rps: float = 8.0
    kis_sweep_size: int = 300
    kis_workers: int = 8
    kis_penalty_rps: float = 2.0
    kis_penalty_seconds: float = 30.0

    krx_id: str = ""
    krx_password: str = ""
    krx_api_key: str = ""
    krx_openapi_base_url: str = "https://data-dbg.krx.co.kr/svc/apis"
    krx_openapi_throttle_seconds: float = 0.2
    krx_flows_pause_seconds: float = 0.5
    krx_vkospi_pause_seconds: float = 0.35

    universe_size: int = 300
    universe_min_trading_value: float = 1_000_000_000.0
    universe_info_max_stale_days: int = 10
    pinned_symbols: CsvList = []

    indicator_minute_symbols: CsvList = ["KOSPI", "KOSDAQ"]
    indicator_daily_symbols: CsvList = list(INDICATOR_SYMBOLS)
    us_eod_symbols: CsvList = list(US_EOD_SYMBOLS)
    us_earnings_symbols: CsvList = list(US_EARNINGS_SYMBOLS)
    us_backfill_start: date = date(2015, 1, 1)
    us_eod_overlap_days: int = 10
    us_events_forward_days: int = 40
    us_earnings_forward_days: int = 45
    kr_events_forward_days: int = 40
    us_source_throttle_seconds: float = 0.5

    usfut_enabled: bool = False
    usfut_pause_seconds: float = 0.3

    fred_api_key: str = ""
    ecos_api_key: str = ""

    collect_max_pages: int = 30
    collect_pre_open_minutes: int = 5
    collect_post_close_minutes: int = 20
    collect_failure_ratio: float = 0.2

    heartbeat_stale_minutes: int = 15
    alert_cooldown_minutes: int = 60

    crosscheck_sample_size: int = 20
    crosscheck_tolerance_pct: float = 0.1

    eod_investor_days: int = 30
    backfill_years: int = 10
    reconcile_lookback_days: int = 5

    @field_validator(
        "pinned_symbols",
        "indicator_minute_symbols",
        "indicator_daily_symbols",
        "us_eod_symbols",
        "us_earnings_symbols",
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
    def marcap_cache_dir(self) -> Path:
        return self.data_dir / "cache" / "marcap"

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
    def krx_login_configured(self) -> bool:
        return bool(self.krx_id and self.krx_password)

    @property
    def kis_configured(self) -> bool:
        return bool(self.kis_app_key and self.kis_app_secret)

    @property
    def kis_token_path(self) -> Path:
        return self.data_dir / "cache" / "kis_token.json"

    @property
    def kis_pacer_path(self) -> Path:
        return self.locks_dir / "kis-pacer.json"

    @property
    def krx_openapi_configured(self) -> bool:
        return bool(self.krx_api_key)

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
