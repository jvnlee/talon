from datetime import date

import pytest

from talon.errors import SourceError
from talon.sources.krx_index import parse_vkospi_daily_row, parse_vkospi_rows

VKOSPI_ROW = {
    "IDX_NM": "코스피 200 변동성지수",
    "CLSPRC_IDX": "84.89",
    "FLUC_TP_CD": "2",
    "CMPPREVDD_IDX": "-1.98",
    "FLUC_RT": "-2.28",
    "OPNPRC_IDX": "87.07",
    "HGPRC_IDX": "87.07",
    "LWPRC_IDX": "84.73",
}
OTHER_ROW = {
    "IDX_NM": "코스피 200 월별 양매도 ATM",
    "CLSPRC_IDX": "1,234.56",
    "CMPPREVDD_IDX": "3.21",
}


def test_parses_price_and_prev_close():
    quote = parse_vkospi_rows([OTHER_ROW, VKOSPI_ROW])

    assert quote.price == 84.89
    assert quote.prev_close == 86.87


def test_positive_change_lowers_prev_close():
    row = dict(VKOSPI_ROW, CMPPREVDD_IDX="1.35")

    quote = parse_vkospi_rows([row])

    assert quote.prev_close == 83.54


def test_missing_index_row_is_rejected():
    with pytest.raises(SourceError, match="분류 변경 의심"):
        parse_vkospi_rows([OTHER_ROW])


def test_holiday_dashes_are_rejected():
    row = dict(VKOSPI_ROW, CLSPRC_IDX="-", CMPPREVDD_IDX="-")

    with pytest.raises(SourceError, match="휴장이거나 아직 산출 전"):
        parse_vkospi_rows([row])


def test_out_of_range_value_is_rejected():
    row = dict(VKOSPI_ROW, CLSPRC_IDX="412.00")

    with pytest.raises(SourceError, match="정상 범위"):
        parse_vkospi_rows([row])


def test_missing_change_keeps_price_without_prev_close():
    row = dict(VKOSPI_ROW, CMPPREVDD_IDX="-")

    quote = parse_vkospi_rows([row])

    assert quote.price == 84.89
    assert quote.prev_close is None


DAILY_ROW = {
    "IDX_NM": "코스피 200 변동성지수",
    "CLSPRC_IDX": "87.14",
    "FLUC_TP_CD": "1",
    "CMPPREVDD_IDX": "1.35",
    "FLUC_RT": "1.57",
    "OPNPRC_IDX": "85.15",
    "HGPRC_IDX": "89.75",
    "LWPRC_IDX": "85.15",
}
NEGATIVE_DAILY_ROW = {
    "IDX_NM": "코스피 200 변동성지수",
    "CLSPRC_IDX": "13.60",
    "FLUC_TP_CD": "2",
    "CMPPREVDD_IDX": "-0.85",
    "FLUC_RT": "-5.88",
    "OPNPRC_IDX": "14.60",
    "HGPRC_IDX": "14.65",
    "LWPRC_IDX": "13.60",
}


def test_parses_full_daily_bar():
    bar = parse_vkospi_daily_row([OTHER_ROW, DAILY_ROW], date(2026, 7, 16))

    assert bar.day == date(2026, 7, 16)
    assert bar.close == 87.14
    assert bar.change == 1.35
    assert bar.change_pct == 1.57
    assert (bar.open, bar.high, bar.low) == (85.15, 89.75, 85.15)


def test_parses_negative_change_daily_bar():
    bar = parse_vkospi_daily_row([NEGATIVE_DAILY_ROW], date(2015, 7, 1))

    assert bar.close == 13.60
    assert bar.change == -0.85
    assert bar.change_pct == -5.88
    assert (bar.open, bar.high, bar.low) == (14.60, 14.65, 13.60)


def test_daily_bar_missing_ohlc_is_none():
    row = {
        "IDX_NM": "코스피 200 변동성지수",
        "CLSPRC_IDX": "20.00",
        "CMPPREVDD_IDX": "-",
        "FLUC_RT": "-",
        "OPNPRC_IDX": "-",
        "HGPRC_IDX": "",
        "LWPRC_IDX": "-",
    }

    bar = parse_vkospi_daily_row([row], date(2026, 7, 16))

    assert bar.close == 20.00
    assert bar.open is None
    assert bar.high is None
    assert bar.low is None
    assert bar.change is None
    assert bar.change_pct is None


def test_daily_bar_out_of_range_close_is_rejected():
    row = dict(DAILY_ROW, CLSPRC_IDX="412.00")

    with pytest.raises(SourceError, match="정상 범위"):
        parse_vkospi_daily_row([row], date(2026, 7, 16))


def test_daily_bar_incoherent_ohlc_is_rejected():
    row = dict(DAILY_ROW, LWPRC_IDX="90.00")

    with pytest.raises(SourceError, match="OHLC 정합 위반"):
        parse_vkospi_daily_row([row], date(2026, 7, 16))


def test_daily_bar_missing_index_row_is_rejected():
    with pytest.raises(SourceError, match="분류 변경 의심"):
        parse_vkospi_daily_row([OTHER_ROW], date(2026, 7, 16))


def test_daily_bar_holiday_dashes_are_rejected():
    row = dict(DAILY_ROW, CLSPRC_IDX="-")

    with pytest.raises(SourceError, match="휴장이거나 아직 산출 전"):
        parse_vkospi_daily_row([row], date(2026, 7, 16))
