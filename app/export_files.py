import csv
import io
import json
import re
from tempfile import SpooledTemporaryFile

from anyio import to_thread
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

FORMAT_VERSION = 1
MAX_EXPORT_ROWS = 100_000
MAX_EXPORT_BYTES = 32 * 1024 * 1024
BATCH_SIZE = 500
CSV_METADATA_COLUMNS = (
    "recordType", "formatVersion", "dataset", "generatedAt", "serviceId", "serviceKey",
    "serviceName", "snapshotId", "schemaHash", "runs", "filters", "summary", "rowCount",
)


class ExportLimitError(Exception):
    pass


def json_text(value):
    return json.dumps(jsonable_encoder(value), ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def csv_cell(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (list, dict)):
        value = json_text(value)
    elif not isinstance(value, str):
        return value
    formula = re.match(r"^[\x00-\x20\s\ufeff]*[=+@＝＋＠－-]", value)
    control_prefix = value and (ord(value[0]) < 32 or value[0] == "\ufeff")
    if formula or control_prefix:
        return "\t" + value
    return value


class ExportFile:
    def __init__(self, format):
        self.format = format
        self.file = SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
        self.written = 0

    async def start(self, *args):
        await to_thread.run_sync(self.begin, *args)

    async def add_batch(self, rows):
        await to_thread.run_sync(self.add, rows)

    def write(self, value):
        content = value.encode("utf-8")
        if self.file.tell() + len(content) > MAX_EXPORT_BYTES:
            raise ExportLimitError("Export exceeds the 32 MiB file limit; narrow the filters")
        self.file.write(content)

    def csv_rows(self, rows):
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, lineterminator="\r\n", quoting=csv.QUOTE_ALL)
        for row in rows:
            writer.writerow([csv_cell(value) for value in row])
        self.write(buffer.getvalue())

    def begin(self, dataset, metadata, filters, summary, columns, total):
        if total > MAX_EXPORT_ROWS:
            raise ExportLimitError("Export exceeds 100000 rows; narrow the filters")
        self.dataset, self.columns, self.total = dataset, list(columns), total
        envelope = {
            "formatVersion": FORMAT_VERSION, "dataset": dataset, "metadata": metadata,
            "filters": filters, "summary": summary, "rowCount": total,
        }
        if self.format == "json":
            self.write(json_text(envelope)[:-1] + ',"rows":[')
        else:
            self.write("\ufeff")
            self.csv_rows([[*CSV_METADATA_COLUMNS, *self.columns]])
            service, snapshot = metadata["service"], metadata["snapshot"]
            self.csv_rows([[
                "metadata", FORMAT_VERSION, dataset, metadata["generatedAt"], service["id"],
                service["key"], service["name"], snapshot["id"], snapshot["schemaHash"],
                metadata["runs"], filters, summary, total, *([None] * len(self.columns)),
            ]])

    def add(self, rows):
        for row in rows:
            if self.written >= self.total:
                raise ValueError("Export row count changed")
            row = jsonable_encoder(row)
            if self.format == "json":
                self.write(("," if self.written else "") + json_text(row))
            else:
                self.csv_rows([["data", *([None] * (len(CSV_METADATA_COLUMNS) - 1)),
                                *[row.get(key) for key in self.columns]]])
            self.written += 1

    def finish(self):
        if self.written != self.total:
            raise ValueError("Export row count changed")
        if self.format == "json":
            self.write("]}")
        self.file.seek(0)

    def response(self, identifier):
        size = self.file.seek(0, 2)
        self.file.seek(0)

        async def chunks():
            try:
                while chunk := await to_thread.run_sync(self.file.read, 64 * 1024):
                    yield chunk
            finally:
                self.close()

        return StreamingResponse(
            chunks(), media_type="application/json" if self.format == "json" else "text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{self.dataset}-{identifier}.{self.format}"',
                "Content-Length": str(size),
                "X-Export-Format-Version": str(FORMAT_VERSION),
                "X-Total-Count": str(self.total), "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
            background=BackgroundTask(self.close),
        )

    def close(self):
        self.file.close()
