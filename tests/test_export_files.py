import csv
import io
import json

import pytest

from app import export_files
from app.export_files import ExportFile, ExportLimitError, csv_cell

METADATA = {
    "generatedAt": "2026-11-18T12:00:00+00:00",
    "service": {"id": "service", "key": "shop", "name": "Магазин"},
    "snapshot": {"id": "snapshot", "schemaHash": "a" * 64},
    "runs": [{"id": "run", "name": "QA"}],
}


@pytest.mark.parametrize("text", ["=1+1", "+cmd", "-cmd", "@SUM(1)", "  =1", "\t=1", "\r=1", "\n=1", " \ufeff=1", "\ufeff+1", "＝1", "＋1", "＠SUM(1)", "－1", "\x00=1"])
def test_csv_blocks_formula_and_control_prefixes(text):
    assert csv_cell(text) == "\t" + text


@pytest.mark.parametrize("value", [0, -12, 12.5, "Проверка", "a,b\n\"c\"", "safe@example.com"])
def test_csv_preserves_numbers_and_plain_text(value):
    assert csv_cell(value) == value


@pytest.mark.parametrize("format", ["json", "csv"])
@pytest.mark.parametrize("rows", [[], [{"name": "Привет, мир\n\"QA\"", "hits": 3}]])
def test_empty_and_unicode_exports_keep_metadata(format, rows):
    writer = ExportFile(format)
    try:
        writer.begin("coverage", METADATA, {}, {}, ["name", "hits"], len(rows))
        writer.add(rows)
        writer.finish()
        content = writer.file.read()
        if format == "json":
            result = json.loads(content)
            assert result["rows"] == rows and result["rowCount"] == len(rows)
            assert result["metadata"]["service"]["name"] == "Магазин"
        else:
            assert content.startswith(b"\xef\xbb\xbf")
            result = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
            assert result[0]["recordType"] == "metadata" and result[0]["serviceName"] == "Магазин"
            assert int(result[0]["rowCount"]) == len(rows)
            assert len(result) == len(rows) + 1
            if rows:
                assert result[1]["name"] == rows[0]["name"]
    finally:
        writer.close()


def test_size_limits_fail_before_a_download_is_returned(monkeypatch):
    writer = ExportFile("json")
    try:
        monkeypatch.setattr(export_files, "MAX_EXPORT_ROWS", 2)
        with pytest.raises(ExportLimitError):
            writer.begin("coverage", METADATA, {}, {}, ["name"], 3)
        monkeypatch.setattr(export_files, "MAX_EXPORT_BYTES", 10)
        with pytest.raises(ExportLimitError):
            writer.begin("coverage", METADATA, {}, {}, ["name"], 1)
    finally:
        writer.close()


async def test_download_rewinds_and_closes_the_spooled_file():
    writer = ExportFile("json")
    writer.begin("coverage", METADATA, {}, {}, ["hits"], 1)
    writer.add([{"hits": 2}])
    writer.finish()
    response = writer.response("run")
    content = b"".join([chunk async for chunk in response.body_iterator])
    assert json.loads(content)["rows"] == [{"hits": 2}]
    assert response.headers["Content-Length"] == str(len(content))
    assert writer.file.closed


async def test_disconnect_closes_the_spooled_file():
    writer = ExportFile("json")
    writer.begin("coverage", METADATA, {}, {}, ["value"], 1)
    writer.add([{"value": "x" * 100_000}])
    writer.finish()
    response = writer.response("run")
    await anext(response.body_iterator)
    await response.body_iterator.aclose()
    assert writer.file.closed


async def test_large_export_spills_to_disk_and_is_removed_after_reading():
    writer = ExportFile("json")
    writer.begin("coverage", METADATA, {}, {}, ["value"], 1)
    writer.add([{"value": "x" * (1024 * 1024 + 1)}])
    writer.finish()
    assert writer.file._rolled
    response = writer.response("run")
    content = b"".join([chunk async for chunk in response.body_iterator])
    assert len(json.loads(content)["rows"][0]["value"]) == 1024 * 1024 + 1
    assert writer.file.closed
