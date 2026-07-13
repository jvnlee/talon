import polars as pl

TRADABLE_STOCK = "tradable_stock"

SECURITY_GROUP = "주권"
SHARE_KIND = "보통주"
EXCLUDED_SECTIONS = ("관리종목", "투자주의환기종목", "SPAC")


def tradable_stock() -> pl.Expr:
    return (
        (pl.col("security_group") == SECURITY_GROUP)
        & (pl.col("share_kind") == SHARE_KIND)
        & ~pl.col("section").fill_null("").str.contains_any(list(EXCLUDED_SECTIONS))
    )


def tradable_symbols(info: pl.DataFrame) -> list[str]:
    return info.filter(tradable_stock()).get_column("symbol").to_list()


class LiquidityUniverse:
    def __init__(self, size: int = 300, min_value: float = 1_000_000_000.0) -> None:
        if size < 1:
            raise ValueError(f"유니버스 크기는 1 이상이어야 합니다: {size}")
        self.size = size
        self.min_value = min_value

    def filter(self, day_frame: pl.DataFrame) -> pl.DataFrame:
        if TRADABLE_STOCK not in day_frame.columns:
            raise ValueError(
                f"패널에 {TRADABLE_STOCK} 컬럼이 없습니다 (talon stock-info backfill 먼저 실행)"
            )
        return (
            day_frame.filter(
                (pl.col("volume") > 0)
                & pl.col(TRADABLE_STOCK)
                & (pl.col("value") >= self.min_value)
            )
            .sort("value", descending=True)
            .head(self.size)
        )
