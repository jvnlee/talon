from datetime import date

import pytest

import talon.sources.krx_actions as ka
from talon.errors import SchemaDriftError

VI_ROW = {
    "TRD_DD": "2026/07/21",
    "ISU_CD": "056730",
    "ISU_NM": "CNT85",
    "MKT_NM": "KOSDAQ",
    "VI_TG_BAS_PRC": "1,431",
    "VI_TG_PRC": "1,575",
    "VI_TG_PRC_DIVRG_RT": "10.10",
    "VI_KIND_NM": "정적VI",
    "VI_TG_TM": "09:42:22",
    "VI_RELEAS_TM": "09:44:39",
    "TDD_CLSPRC": "1,470",
}

ALERT_ROW = {
    "ISU_CD": "066910",
    "ISU_CD_FULL": "KR7066910001",
    "MKT_ID": "KSQ",
    "ISU_NM": "손오공",
    "MKT_NM": "KOSDAQ",
    "DESIGN_DD": "2026/07/22",
    "RELEASE_DD": "-",
}

OVERHEAT_ROW = {
    "BAS_DD": "2026/07/21",
    "MKTACT_APPL_DD": "2026/07/22",
    "RELEAS_DD": "2026/07/22",
    "ISU_CD": "060720",
    "ISU_CD_FULL": "KR7060720002",
    "MKT_ID": "KSQ",
    "ISU_ABBRV": "KH바텍",
    "MKT_NM": "KOSDAQ",
    "VALU_PD_TR_DYS": "40",
    "TDD_SRTSELL_WT": "12.3",
    "PRC_YD": "-16.25",
    "TDD_SRTSELL_TRDVAL_INCDEC_RT": "",
    "VALU_PD_AVG_SRTSELL_WT": "5.1",
    "SRTSELL_IMPSBL_DTEC_TP_NM": "유형3",
}

HALT_ROW = {
    "ISU_CD": "083660",
    "ISU_CD_FULL": "KR7083660001",
    "MKT_NM": "KOSDAQ",
    "ISU_NM": "CSA코스믹",
    "HALT_DESNRELS_DDTM": "2026/07/13",
    "HALT_RSN_NM": "주식의 병합, 분할 등 전자등록 변경, 말소",
    "LST_TRD_DD": "2026/07/10",
}


def capture(monkeypatch, rows):
    calls: list[tuple[str, dict[str, str], str]] = []

    def fake(bld, params, *, credentials, sleep, data_key="output"):
        calls.append((bld, params, data_key))
        return rows(bld) if callable(rows) else rows

    monkeypatch.setattr(ka, "_fetch_rows", fake)
    return calls


def test_vi_fetch_assembles_params_and_maps_fields(monkeypatch):
    calls = capture(monkeypatch, [VI_ROW])
    frame = ka.fetch_vi_events(date(2026, 7, 20), date(2026, 7, 21))
    bld, params, data_key = calls[0]
    assert bld == ka.VI_BLD
    assert data_key == "output"
    assert params["strtDd"] == "20260720"
    assert params["endDd"] == "20260721"
    assert params["param1isuCd_finder_stkisu1"] == "ALL"
    assert params["isuCd"] == "ALL"
    row = frame.row(0, named=True)
    assert row["day"] == date(2026, 7, 21)
    assert row["symbol"] == "056730"
    assert row["vi_kind"] == "static"
    assert row["trigger_time"] == "09:42:22"
    assert row["release_time"] == "09:44:39"
    assert row["reference_price"] == 1431.0
    assert row["trigger_price"] == 1575.0
    assert row["divergence_pct"] == 10.10


def test_vi_dynamic_kind_and_open_vi_release(monkeypatch):
    row = dict(VI_ROW, VI_KIND_NM="동적VI", VI_RELEAS_TM="")
    capture(monkeypatch, [row])
    frame = ka.fetch_vi_events(date(2026, 7, 21), date(2026, 7, 21))
    assert frame.row(0, named=True)["vi_kind"] == "dynamic"
    assert frame.row(0, named=True)["release_time"] is None


def test_vi_schema_drift_on_missing_column(monkeypatch):
    row = {k: v for k, v in VI_ROW.items() if k != "VI_KIND_NM"}
    capture(monkeypatch, [row])
    with pytest.raises(SchemaDriftError, match="VI columns missing"):
        ka.fetch_vi_events(date(2026, 7, 21), date(2026, 7, 21))


def test_vi_empty_response_is_empty_frame(monkeypatch):
    capture(monkeypatch, [])
    frame = ka.fetch_vi_events(date(2026, 7, 17), date(2026, 7, 17))
    assert frame.is_empty()
    assert frame.columns == list(ka.VI_EVENTS_SCHEMA)


def test_alerts_fetch_all_levels_and_map(monkeypatch):
    def rows(bld):
        if bld == ka.ALERT_LEVEL_BLDS["warning"]:
            return [ALERT_ROW]
        return []

    calls = capture(monkeypatch, rows)
    frame = ka.fetch_market_alerts(date(2026, 7, 22), sleep=lambda _: None)
    assert {bld for bld, _, _ in calls} == set(ka.ALERT_LEVEL_BLDS.values())
    assert frame.height == 1
    row = frame.row(0, named=True)
    assert row["level"] == "warning"
    assert row["day"] == date(2026, 7, 22)
    assert row["symbol"] == "066910"
    assert row["isin"] == "KR7066910001"
    assert row["design_dd"] == date(2026, 7, 22)
    assert row["release_dd"] is None


def test_overheat_uses_outblock_and_signed_change(monkeypatch):
    calls = capture(monkeypatch, [OVERHEAT_ROW])
    frame = ka.fetch_short_overheat(date(2025, 1, 1), date(2025, 12, 31))
    bld, params, data_key = calls[0]
    assert bld == ka.OVERHEAT_BLD
    assert data_key == ka.OVERHEAT_DATA_KEY
    assert params["searchType"] == "1"
    assert params["mktTpCd"] == "0"
    row = frame.row(0, named=True)
    assert row["day"] == date(2026, 7, 21)
    assert row["restrict_apply_dd"] == date(2026, 7, 22)
    assert row["prc_yd"] == -16.25
    assert row["tdd_srtsell_trdval_incdec_rt"] is None
    assert row["dtec_type"] == "유형3"


def test_halts_snapshot_maps_reason_and_start(monkeypatch):
    calls = capture(monkeypatch, [HALT_ROW])
    frame = ka.fetch_trading_halts()
    bld, _, _ = calls[0]
    assert bld == ka.HALTS_SNAPSHOT_BLD
    row = frame.row(0, named=True)
    assert row["day"] == date(2026, 7, 13)
    assert row["symbol"] == "083660"
    assert row["reason"].startswith("주식의 병합")
    assert row["last_trade_day"] == date(2026, 7, 10)
    assert row["resume_day"] is None


def test_halt_history_resume_map(monkeypatch):
    history = [
        {"TRD_HALT_DD": "2022/03/07", "RESUMP_DD": "2022/03/21"},
        {"TRD_HALT_DD": "2026/07/13", "RESUMP_DD": ""},
    ]
    capture(monkeypatch, history)
    resume = ka.fetch_halt_history("KR7000000001", date(2022, 1, 1), date(2026, 7, 22))
    assert resume == {date(2022, 3, 7): date(2022, 3, 21)}
