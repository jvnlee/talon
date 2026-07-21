import pytest

from talon.errors import SourceError
from talon.sources.krx_index import parse_vkospi_rows

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
