"""Value-free argument persistence validation and run analytics."""

import uuid
from typing import Literal

from sqlalchemy import func, select, true, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_models import ApiModel
from app.models import ObservedRequest, RequestArgumentUsage, SchemaField, SchemaSnapshot, SchemaType, TestRun
from app.schema_cache import schema_from_sdl
from app.schema_work import schema_work
from app.services import InvalidReferenceError, NotFoundError


STATES = ("provided", "explicit_null", "omitted", "unknown", "mixed")


class ArgumentUsageOut(ApiModel):
    type_name: str
    field_name: str
    argument_name: str
    argument_type: str
    has_schema_default: bool
    provided_count: int
    explicit_null_count: int
    omitted_count: int
    unknown_count: int
    mixed_count: int
    request_count: int


class RunArgumentsOut(ApiModel):
    model_config = {"json_schema_extra": {"examples": [{
        "runId": "00000000-0000-0000-0000-000000000001",
        "snapshotId": "00000000-0000-0000-0000-000000000002",
        "availability": "partial", "totalRequests": 8, "analyzedRequests": 7,
        "unavailableRequests": 1, "total": 1, "limit": 100, "offset": 0,
        "items": [{"typeName": "Query", "fieldName": "orders", "argumentName": "status",
                   "argumentType": "Status", "hasSchemaDefault": True,
                   "providedCount": 2, "explicitNullCount": 1, "omittedCount": 2,
                   "unknownCount": 1, "mixedCount": 1, "requestCount": 7}]
    }]}}

    run_id: uuid.UUID
    snapshot_id: uuid.UUID
    availability: Literal["available", "partial", "unavailable"]
    total_requests: int
    analyzed_requests: int
    unavailable_requests: int
    total: int
    limit: int
    offset: int
    items: list[ArgumentUsageOut]


def validate_argument_usage(sdl, arguments):
    from app.query_analyzer import MAX_ARGUMENTS_PER_REQUEST
    if len(arguments) > MAX_ARGUMENTS_PER_REQUEST:
        raise InvalidReferenceError("Argument usage exceeds the record limit")
    schema = schema_from_sdl(sdl)
    seen = set()
    for item in arguments:
        key = (item.field, item.argument_name)
        parent = schema.get_type(item.field.type_name)
        definition = getattr(parent, "fields", {}).get(item.field.field_name)
        if (item.state not in STATES or len(item.argument_name) > 255 or key in seen or definition is None
                or item.argument_name not in definition.args):
            raise InvalidReferenceError("Invalid argument usage for run snapshot")
        seen.add(key)


def argument_catalog(sdl, type_name, field_name, argument_name):
    """Use the existing cached immutable schema; return no defaults or values."""
    from graphql import Undefined
    schema = schema_from_sdl(sdl)
    result = []
    for name, parent in schema.type_map.items():
        if name.startswith("__") or (type_name is not None and name != type_name):
            continue
        for field, definition in getattr(parent, "fields", {}).items():
            if field_name is not None and field != field_name:
                continue
            for arg, argument in getattr(definition, "args", {}).items():
                if argument_name is None or arg == argument_name:
                    has_default = (getattr(argument.ast_node, "default_value", None) is not None
                                   or argument.default_value is not Undefined)
                    result.append((name, field, arg, str(argument.type), has_default))
    return sorted(result)


async def get_run_arguments(
    session: AsyncSession, run_id: uuid.UUID, *, limit=100, offset=0,
    type_name=None, field_name=None, argument_name=None,
) -> RunArgumentsOut:
    run = await session.get(TestRun, run_id)
    if run is None:
        raise NotFoundError("Test run not found")
    snapshot_id = run.snapshot_id
    sdl = await session.scalar(select(SchemaSnapshot.schema_sdl).where(SchemaSnapshot.id == snapshot_id))
    # End read/authorization phase before cached-schema computation or worker wait.
    await session.close()
    catalog = await schema_work(argument_catalog, sdl, type_name, field_name, argument_name)
    page = catalog[offset:offset + limit]

    totals = select(
        func.count().label("total_requests"),
        func.count().filter(ObservedRequest.argument_usage_version == 1).label("analyzed_requests"),
    ).where(ObservedRequest.run_id == run_id).subquery()
    counts = select(SchemaType.name.label("type_name"), SchemaField.name.label("field_name"),
                    RequestArgumentUsage.argument_name,
                    *(func.count().filter(RequestArgumentUsage.state == state).label(state) for state in STATES))
    counts = (counts.select_from(RequestArgumentUsage)
        .join(ObservedRequest, ObservedRequest.id == RequestArgumentUsage.request_id)
        .join(SchemaField, SchemaField.id == RequestArgumentUsage.field_id)
        .join(SchemaType, SchemaType.id == SchemaField.type_id)
        .where(ObservedRequest.run_id == run_id, ObservedRequest.argument_usage_version == 1,
               SchemaType.snapshot_id == snapshot_id,
               tuple_(SchemaType.name, SchemaField.name, RequestArgumentUsage.argument_name)
               .in_([item[:3] for item in page]))
        .group_by(SchemaType.name, SchemaField.name, RequestArgumentUsage.argument_name).subquery())
    # Availability and page counts share one PostgreSQL statement snapshot even
    # while a run is collecting concurrent observations. Empty pages retain totals.
    rows = (await session.execute(select(totals, counts).select_from(totals.outerjoin(counts, true())))).mappings().all()
    total_requests, analyzed = rows[0]["total_requests"], rows[0]["analyzed_requests"]
    availability = "unavailable" if not analyzed else "available" if analyzed == total_requests else "partial"
    indexed = {(row["type_name"], row["field_name"], row["argument_name"]): row for row in rows}
    items = []
    for type_value, field, argument, arg_type, has_default in page:
        row = indexed.get((type_value, field, argument))
        values = {state + "_count": row[state] if row else 0 for state in STATES}
        items.append(ArgumentUsageOut(type_name=type_value, field_name=field, argument_name=argument,
            argument_type=arg_type, has_schema_default=has_default, request_count=sum(values.values()), **values))
    return RunArgumentsOut(run_id=run_id, snapshot_id=snapshot_id, availability=availability,
        total_requests=total_requests, analyzed_requests=analyzed, unavailable_requests=total_requests-analyzed,
        total=len(catalog), limit=limit, offset=offset, items=items)
