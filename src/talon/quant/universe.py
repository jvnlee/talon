import polars as pl


class LiquidityUniverse:
    def __init__(self, size: int = 300, min_value: float = 1_000_000_000.0) -> None:
        if size < 1:
            raise ValueError(f"유니버스 크기는 1 이상이어야 합니다: {size}")
        self.size = size
        self.min_value = min_value

    def filter(self, day_frame: pl.DataFrame) -> pl.DataFrame:
        return (
            day_frame.filter(
                (pl.col("volume") > 0)
                & pl.col("symbol").str.ends_with("0")
                & (pl.col("value") >= self.min_value)
            )
            .sort("value", descending=True)
            .head(self.size)
        )
