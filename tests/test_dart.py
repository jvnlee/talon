import json
from datetime import date

import httpx
import polars as pl
import pytest

from talon.errors import SourceError
from talon.sources.dart import DART_FILINGS_SCHEMA, fetch_filings

DAY = date(2023, 3, 2)


def filing(rcept_no, stock_code="005930", report_nm="주요사항보고서(유상증자결정)"):
    return {
        "corp_code": "00126380",
        "corp_name": "삼성전자",
        "stock_code": stock_code,
        "corp_cls": "Y",
        "report_nm": report_nm,
        "rcept_no": rcept_no,
        "flr_nm": "삼성전자",
        "rcept_dt": "20230302",
        "rm": "",
    }


def page_payload(items, page_no=1, total_page=1):
    return {
        "status": "000",
        "message": "정상",
        "page_no": page_no,
        "page_count": 100,
        "total_count": len(items),
        "total_page": total_page,
        "list": items,
    }


def transport_with(pages):
    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        key = (params["pblntf_ty"], int(params["page_no"]))
        payload = pages.get(key, {"status": "013", "message": "조회된 데이타가 없습니다."})
        return httpx.Response(200, text=json.dumps(payload))

    return httpx.MockTransport(handler)


def test_fetch_filings_pages_and_dedupes():
    pages = {
        ("A", 1): page_payload([filing("1"), filing("2")], page_no=1, total_page=2),
        ("A", 2): page_payload([filing("2"), filing("3")], page_no=2, total_page=2),
        ("B", 1): page_payload([filing("4", stock_code="000660")]),
    }
    frame = fetch_filings("test-key", DAY, types=("A", "B", "D"), transport=transport_with(pages))

    assert dict(frame.schema) == DART_FILINGS_SCHEMA
    assert frame.get_column("rcept_no").to_list() == ["1", "2", "3", "4"]
    assert frame.get_column("day").unique().to_list() == [DAY]
    assert frame.filter(pl.col("rcept_no") == "4").get_column("filing_type").item() == "B"


def test_fetch_filings_drops_unlisted():
    pages = {
        ("A", 1): page_payload([filing("1"), filing("2", stock_code=" ")]),
    }
    frame = fetch_filings("test-key", DAY, types=("A",), transport=transport_with(pages))

    assert frame.get_column("rcept_no").to_list() == ["1"]


def test_fetch_filings_empty_day():
    frame = fetch_filings("test-key", DAY, types=("A", "B"), transport=transport_with({}))

    assert frame.is_empty()
    assert dict(frame.schema) == DART_FILINGS_SCHEMA


def test_fetch_filings_error_status():
    pages = {("A", 1): {"status": "020", "message": "사용한도 초과"}}
    with pytest.raises(SourceError, match="020"):
        fetch_filings("test-key", DAY, types=("A",), transport=transport_with(pages))


def test_fetch_filings_requires_key():
    with pytest.raises(SourceError, match="API 키"):
        fetch_filings("", DAY)


def test_dart_backfill_cli(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from talon.cli import main
    from talon.data.store import DART_FILINGS, DatePartitionedStore

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TALON_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TALON_DART_API_KEY", "test-key")
    monkeypatch.setenv("TALON_DART_THROTTLE_SECONDS", "0")

    calls: list[date] = []

    def fake_fetch(api_key, day, *, types):
        calls.append(day)
        return pl.DataFrame(
            [
                {
                    "day": day,
                    "symbol": "005930",
                    "corp_code": "00126380",
                    "corp_name": "삼성전자",
                    "corp_cls": "Y",
                    "filing_type": types[0],
                    "report_nm": "보고서",
                    "rcept_no": f"{day:%Y%m%d}00001",
                }
            ],
            schema=DART_FILINGS_SCHEMA,
        )

    monkeypatch.setattr("talon.sources.dart.fetch_filings", fake_fetch)

    runner = CliRunner()
    result = runner.invoke(
        main, ["dart", "backfill", "--start", "2023-03-02", "--end", "2023-03-03"]
    )
    assert result.exit_code == 0, result.output
    assert len(calls) == 2

    store = DatePartitionedStore(tmp_path / "data" / "parquet" / "kr")
    frame = store.read_date(DART_FILINGS, date(2023, 3, 2))
    assert frame is not None
    assert frame.get_column("symbol").to_list() == ["005930"]

    rerun = runner.invoke(
        main, ["dart", "backfill", "--start", "2023-03-02", "--end", "2023-03-03"]
    )
    assert rerun.exit_code == 0, rerun.output
    assert len(calls) == 2
    assert '"skipped": 2' in rerun.output


def test_dart_backfill_requires_key(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from talon.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TALON_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TALON_DART_API_KEY", "")

    runner = CliRunner()
    result = runner.invoke(main, ["dart", "backfill", "--start", "2023-03-02"])
    assert result.exit_code != 0
    assert "API 키" in result.output
