from datetime import timezone

from sqlalchemy import Text, and_, case, func, select
from sqlalchemy.orm import aliased

from app.api_models import OperationOut, RequestOut
from app.export_files import BATCH_SIZE, MAX_EXPORT_BYTES, MAX_EXPORT_ROWS, ExportLimitError
from app.journal import request_query
from app.models import ObservedRequest, RequestFieldUsage, SchemaField, SchemaType, TestExecution
from app.run_analytics import _identifiable, _identity_columns, list_operations
from app.services import COVERAGE_KINDS, counted_observation, coverage_summary
from app.test_executions import (
    ExecutionFieldOut, ExecutionOut, coverage_summary as test_summary,
    execution_fields_query, execution_query, execution_stats,
)


def columns(model):
    return [field.alias or name for name, field in model.model_fields.items()]


def effective_filters(filters):
    return filters.model_dump(mode="json", by_alias=True, exclude={"format", "limit", "offset"}, exclude_none=True)


async def row_count(session, statement):
    bounded = statement.order_by(None).limit(MAX_EXPORT_ROWS + 1).subquery()
    count, size = (await session.execute(select(
        func.count(), func.coalesce(func.sum(func.octet_length(func.row_to_json(bounded.table_valued()).cast(Text))), 0),
    ).select_from(bounded))).one()
    if count > MAX_EXPORT_ROWS:
        raise ExportLimitError("Export exceeds 100000 rows; narrow the filters")
    if size > MAX_EXPORT_BYTES:
        raise ExportLimitError("Export data exceeds the 32 MiB processing limit; narrow the filters")
    return count


async def write_query(session, writer, statement, convert=dict):
    result = await session.stream(statement.execution_options(yield_per=BATCH_SIZE))
    try:
        async for batch in result.mappings().partitions(BATCH_SIZE):
            await writer.add_batch([convert(row) for row in batch])
    finally:
        await result.close()


async def coverage_export(session, run, filters, writer, metadata):
    hits = (select(RequestFieldUsage.field_id, func.count().label("hits"))
        .join(ObservedRequest, ObservedRequest.id == RequestFieldUsage.request_id)
        .where(ObservedRequest.run_id == run.id, counted_observation())
        .group_by(RequestFieldUsage.field_id).subquery())
    hit_count = func.coalesce(hits.c.hits, 0)
    target = aliased(SchemaType)
    is_list = SchemaField.return_type.startswith("[")
    return_kind = case((is_list, "list"), (target.kind == "SCALAR", "scalar"), (target.kind == "OBJECT", "object"))
    statement = (select(
        SchemaType.name.label("typeName"), SchemaType.kind.label("kind"),
        SchemaField.name.label("fieldName"), SchemaField.return_type.label("returnType"),
        hit_count.label("hits"), (hit_count > 0).label("covered"),
        SchemaType.kind.in_(COVERAGE_KINDS).label("countedInDenominator"),
        SchemaField.is_deprecated.label("deprecated"), return_kind.label("returnKind"),
    ).select_from(SchemaField).join(SchemaType, SchemaType.id == SchemaField.type_id)
        .outerjoin(hits, hits.c.field_id == SchemaField.id)
        .outerjoin(target, and_(target.snapshot_id == SchemaType.snapshot_id,
            target.name == func.regexp_replace(SchemaField.return_type, r"[\[\]!]", "", "g")))
        .where(SchemaType.snapshot_id == run.snapshot_id, SchemaType.kind.in_(("OBJECT", "INTERFACE"))))
    total_fields, covered_fields = (await session.execute(select(
        func.count(SchemaField.id), func.count().filter(hit_count > 0),
    ).select_from(SchemaField).join(SchemaType, SchemaType.id == SchemaField.type_id)
        .outerjoin(hits, hits.c.field_id == SchemaField.id)
        .where(SchemaType.snapshot_id == run.snapshot_id, SchemaType.kind.in_(COVERAGE_KINDS)))).one()
    if filters.type_name is not None:
        statement = statement.where(SchemaType.name == filters.type_name)
    if filters.field_name is not None:
        statement = statement.where(SchemaField.name == filters.field_name)
    if filters.covered is not None:
        statement = statement.where((hit_count > 0) == filters.covered)
    if filters.search is not None:
        searchable = func.lower(func.concat(SchemaType.name, " ", SchemaField.name, " ",
                                            SchemaType.name, ".", SchemaField.name, " ", SchemaField.return_type))
        statement = statement.where(searchable.contains(filters.search.strip().lower(), autoescape=True))
    if filters.min_hits is not None:
        statement = statement.where(hit_count >= filters.min_hits)
    if filters.max_hits is not None:
        statement = statement.where(hit_count <= filters.max_hits)
    if filters.deprecated is not None:
        statement = statement.where(SchemaField.is_deprecated == filters.deprecated)
    if filters.returns is not None:
        statement = statement.where(return_kind == filters.returns)
    sort = {"hits": hit_count, "coordinate": SchemaType.name, "fieldName": SchemaField.name}[filters.sort_by]
    order = sort.desc() if filters.sort_order == "desc" else sort.asc()
    if filters.sort_by == "coordinate":
        field_order = SchemaField.name.desc() if filters.sort_order == "desc" else SchemaField.name.asc()
        statement = statement.order_by(order, field_order)
    else:
        statement = statement.order_by(order, SchemaField.name, SchemaType.name)
    await writer.start("coverage", metadata, effective_filters(filters),
        coverage_summary(total_fields, covered_fields).model_dump(by_alias=True),
        ["typeName", "kind", "fieldName", "returnType", "hits", "covered", "countedInDenominator", "deprecated", "returnKind"],
        await row_count(session, statement))
    await write_query(session, writer, statement)


async def requests_export(session, run, filters, writer, metadata):
    statement = request_query(run, filters).with_only_columns(
        *[getattr(ObservedRequest, name) for name in RequestOut.model_fields])
    if filters.error_group == "http":
        statement = statement.where(ObservedRequest.http_status.between(400, 599))
    elif filters.error_group == "analyzer":
        statement = statement.where(ObservedRequest.analysis_status.in_(("parse_error", "validation_error", "internal_error")))
    await writer.start("requests", metadata, effective_filters(filters), {}, columns(RequestOut),
                       await row_count(session, statement))

    def convert(row):
        row = dict(row)
        row["created_at"] = row["created_at"].astimezone(timezone.utc)
        return RequestOut(**row).model_dump(mode="json", by_alias=True)

    await write_query(session, writer, statement, convert)


async def tests_export(session, run, filters, writer, metadata):
    statement = execution_query(run.id, run.snapshot_id, filters)
    await writer.start("tests", metadata, effective_filters(filters),
        (await test_summary(session, run.id)).model_dump(mode="json", by_alias=True),
        columns(ExecutionOut), await row_count(session, statement))
    result = await session.stream_scalars(statement.execution_options(yield_per=BATCH_SIZE))
    try:
        async for batch in result.partitions(BATCH_SIZE):
            rows = await execution_stats(session, run, batch)
            await writer.add_batch([row.model_dump(mode="json", by_alias=True) for row in rows])
    finally:
        await result.close()


async def matrix_export(session, run, filters, writer, metadata):
    selected = execution_query(run.id, run.snapshot_id, filters).with_only_columns(TestExecution.id).order_by(None)
    field_rows = execution_fields_query(run.id, run.snapshot_id, per_execution=True).subquery()
    execution_columns = ("test_id", "name", "framework", "attempt", "status", "team_key", "suite_key")
    statement = (select(*field_rows.c,
        *[getattr(TestExecution, name) for name in execution_columns])
        .join(TestExecution, TestExecution.id == field_rows.c.execution_id)
        .where(TestExecution.id.in_(selected))
        .order_by(TestExecution.started_at, TestExecution.id, field_rows.c.type_name, field_rows.c.field_name))
    if filters.type_name is not None:
        statement = statement.where(field_rows.c.type_name == filters.type_name)
    if filters.field_name is not None:
        statement = statement.where(field_rows.c.field_name == filters.field_name)
    await writer.start("test-matrix", metadata, effective_filters(filters), {},
        ["executionId", "testId", "name", "framework", "attempt", "status", "teamKey", "suiteKey", *columns(ExecutionFieldOut)],
        await row_count(session, statement))

    def convert(row):
        return {
            "executionId": str(row["execution_id"]), "testId": row["test_id"],
            "teamKey": row["team_key"], "suiteKey": row["suite_key"],
            **{key: row[key] for key in ("name", "framework", "attempt", "status")},
            **ExecutionFieldOut(**row).model_dump(mode="json", by_alias=True),
        }

    await write_query(session, writer, statement, convert)


async def operations_export(session, run, filters, writer, metadata):
    predicates = [ObservedRequest.run_id == run.id, _identifiable()]
    if filters.operation_name is not None:
        predicates.append(ObservedRequest.operation_name == filters.operation_name)
    if filters.operation_type is not None:
        predicates.append(ObservedRequest.operation_type == filters.operation_type)
    await row_count(session, select(*_identity_columns()).where(*predicates).distinct())
    await row_count(session, select(*_identity_columns(), RequestFieldUsage.field_id).select_from(ObservedRequest)
        .join(RequestFieldUsage, RequestFieldUsage.request_id == ObservedRequest.id)
        .join(SchemaField, SchemaField.id == RequestFieldUsage.field_id)
        .join(SchemaType, SchemaType.id == SchemaField.type_id)
        .where(*predicates, counted_observation(), SchemaType.snapshot_id == run.snapshot_id).distinct())
    rows = await list_operations(session, run.id, filters.operation_name, filters.operation_type)
    if filters.search is not None:
        search = filters.search.strip().lower()
        rows = [row for row in rows if search in (row.operation_name or "Без имени").lower()]
    if filters.has_errors is not None:
        rows = [row for row in rows if (row.http_error_count + row.graphql_error_count > 0) == filters.has_errors]
    if filters.type_name is not None:
        rows = [row for row in rows if any(field.type_name == filters.type_name
            and (filters.field_name is None or field.field_name == filters.field_name) for field in row.fields)]
    if filters.sort_by == "identity":
        if filters.sort_order == "desc":
            rows.reverse()
    else:
        def key(row):
            if filters.sort_by == "duration":
                return row.avg_duration_ms if row.avg_duration_ms is not None else (
                    float("inf") if filters.sort_order == "asc" else -float("inf"))
            return row.request_count if filters.sort_by == "count" else row.http_error_count + row.graphql_error_count
        rows = sorted(rows, key=key, reverse=filters.sort_order == "desc")
    await writer.start("operations", metadata, effective_filters(filters), {}, columns(OperationOut), len(rows))
    for offset in range(0, len(rows), BATCH_SIZE):
        await writer.add_batch([row.model_dump(mode="json", by_alias=True) for row in rows[offset:offset + BATCH_SIZE]])
