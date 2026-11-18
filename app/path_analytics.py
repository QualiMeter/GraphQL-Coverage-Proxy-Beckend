"""Bounded path and relation analytics for a single recorded run."""

import uuid

from sqlalchemy import and_, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.api_models import (
    QueryPathOut,
    QueryPathPageOut,
    RelationHitOut,
    RelationHitPageOut,
    RunPathsOut,
    SchemaCoordinateOut,
)
from app.models import (
    ObservedRequest,
    QueryPath,
    QueryPathField,
    RequestFieldUsage,
    RequestPathUsage,
    SchemaField,
    SchemaType,
    TestRun,
)
from app.services import NotFoundError


async def get_run_paths(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    limit: int = 100,
    offset: int = 0,
    type_name: str | None = None,
    field_name: str | None = None,
) -> RunPathsOut:
    if (type_name is None) != (field_name is None):
        raise ValueError("typeName and fieldName must be supplied together")
    run = await session.get(TestRun, run_id)
    if run is None:
        raise NotFoundError("Test run not found")

    successful_count, available_count = (await session.execute(
        select(
            func.count().filter(ObservedRequest.analysis_status == "success"),
            func.count().filter(ObservedRequest.max_depth.is_not(None)),
        ).where(ObservedRequest.run_id == run_id)
    )).one()
    if successful_count == 0 or available_count == 0:
        availability = "unavailable"
    elif successful_count == available_count:
        availability = "available"
    else:
        availability = "partial"
    max_depth = await session.scalar(
        select(func.max(ObservedRequest.max_depth)).where(
            ObservedRequest.run_id == run_id, ObservedRequest.max_depth.is_not(None),
        )
    )

    path_filter = None
    if type_name is not None:
        terminal_field = aliased(SchemaField)
        terminal_type = aliased(SchemaType)
        path_filter = exists(
            select(1)
            .select_from(QueryPathField)
            .join(terminal_field, terminal_field.id == QueryPathField.field_id)
            .join(terminal_type, terminal_type.id == terminal_field.type_id)
            .where(
                QueryPathField.path_id == QueryPath.id,
                QueryPathField.position == QueryPath.max_depth - 1,
                terminal_type.name == type_name,
                terminal_field.name == field_name,
            )
        )
    path_statement = (
        select(QueryPath.id.label("path_id"), QueryPath.max_depth,
               func.count().label("path_hits"))
        .select_from(QueryPath)
        .join(RequestPathUsage, RequestPathUsage.path_id == QueryPath.id)
        .join(ObservedRequest, ObservedRequest.id == RequestPathUsage.request_id)
        .where(ObservedRequest.run_id == run_id, ObservedRequest.analysis_status == "success")
        .group_by(QueryPath.id, QueryPath.max_depth)
    )
    if path_filter is not None:
        path_statement = path_statement.where(path_filter)
    path_total_query = select(func.count()).select_from(path_statement.subquery())
    path_total = await session.scalar(path_total_query) or 0
    path_rows = (await session.execute(
        path_statement.order_by(QueryPath.max_depth, QueryPath.id).limit(limit).offset(offset)
    )).mappings().all()
    page_path_ids = [row["path_id"] for row in path_rows]
    segments: dict[uuid.UUID, list[SchemaCoordinateOut]] = {path_id: [] for path_id in page_path_ids}
    if page_path_ids:
        segment_rows = (await session.execute(
            select(QueryPathField.path_id, SchemaType.name, SchemaField.name)
            .join(SchemaField, SchemaField.id == QueryPathField.field_id)
            .join(SchemaType, SchemaType.id == SchemaField.type_id)
            .where(QueryPathField.path_id.in_(page_path_ids))
            .order_by(QueryPathField.path_id, QueryPathField.position)
        )).all()
        for path_id, type_name_value, field_name_value in segment_rows:
            segments[path_id].append(SchemaCoordinateOut(
                type_name=type_name_value, field_name=field_name_value,
            ))
    paths = QueryPathPageOut(
        total=path_total,
        items=[QueryPathOut(
            path=segments[row["path_id"]], path_hits=row["path_hits"],
        ) for row in path_rows],
    )

    target_type = aliased(SchemaType)
    relation_statement = (
        select(
            SchemaType.name.label("from_type"),
            SchemaField.name.label("field_name"),
            target_type.name.label("to_type"),
            func.count().label("relation_hits"),
        )
        .select_from(RequestFieldUsage)
        .join(ObservedRequest, ObservedRequest.id == RequestFieldUsage.request_id)
        .join(SchemaField, SchemaField.id == RequestFieldUsage.field_id)
        .join(SchemaType, SchemaType.id == SchemaField.type_id)
        .join(target_type, target_type.id == SchemaField.target_type_id)
        .where(ObservedRequest.run_id == run_id, ObservedRequest.analysis_status == "success")
        .group_by(SchemaType.name, SchemaField.name, target_type.name)
    )
    relation_total_query = select(func.count()).select_from(relation_statement.subquery())
    relation_total = await session.scalar(relation_total_query) or 0
    relation_rows = (await session.execute(
        relation_statement.order_by(SchemaType.name, SchemaField.name, target_type.name)
        .limit(limit).offset(offset)
    )).mappings().all()
    relations = RelationHitPageOut(
        total=relation_total,
        items=[RelationHitOut(
            from_type=row["from_type"], field_name=row["field_name"],
            to_type=row["to_type"], relation_hits=row["relation_hits"],
        ) for row in relation_rows],
    )
    return RunPathsOut(
        run_id=run.id, snapshot_id=run.snapshot_id,
        path_data_availability=availability, max_depth=max_depth,
        paths=paths, relations=relations,
    )
