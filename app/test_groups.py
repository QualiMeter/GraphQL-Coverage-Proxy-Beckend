from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy import and_, case, distinct, func, select

from app.api_models import ApiModel
from app.models import ObservedRequest, RequestFieldEvidence, RequestFieldUsage, SchemaField, SchemaType, TestExecution
from app.services import COVERAGE_KINDS, counted_observation


class GroupFilters(ApiModel):
    team_key: str | None = Field(default=None, min_length=1, max_length=128)
    suite_key: str | None = Field(default=None, min_length=1, max_length=128)
    unassigned_team: bool = False
    unassigned_suite: bool = False

    @model_validator(mode="after")
    def valid_group_filters(self):
        if self.team_key is not None and self.unassigned_team:
            raise ValueError("Choose teamKey or unassignedTeam")
        if self.suite_key is not None and self.unassigned_suite:
            raise ValueError("Choose suiteKey or unassignedSuite")
        return self


class GroupPageFilters(GroupFilters):
    group_by: Literal["team", "suite"] = "suite"
    limit: int = Field(default=100, ge=1, le=500)
    offset: int = Field(default=0, ge=0, le=2**63 - 1)


class GroupCoverage(ApiModel):
    key: str | None
    total_tests: int
    total_executions: int
    request_count: int
    selected_field_count: int
    covered_field_count: int
    observed_field_count: int
    unavailable_evidence_request_count: int
    total_fields: int
    coverage_percent: float


def group_conditions(filters: GroupFilters):
    conditions = []
    for name in ("team", "suite"):
        column = getattr(TestExecution, name + "_key")
        key = getattr(filters, name + "_key")
        if key is not None:
            conditions.append(column == key)
        elif getattr(filters, "unassigned_" + name):
            conditions.append(column.is_(None))
    return conditions


async def group_coverage(session, run, filters: GroupPageFilters):
    group_key = TestExecution.team_key if filters.group_by == "team" else TestExecution.suite_key
    conditions = group_conditions(filters)
    # Count attempts before joining requests, so a busy test still counts once.
    attempts = select(
        group_key.label("key"),
        func.count().label("total_executions"),
        func.count(distinct(func.row(TestExecution.framework, TestExecution.test_key))).label("total_tests"),
    ).where(TestExecution.run_id == run.id, *conditions).group_by(group_key).cte("group_attempts")
    unavailable_evidence = select(1).select_from(RequestFieldUsage).outerjoin(
        RequestFieldEvidence, and_(
            RequestFieldEvidence.request_id == RequestFieldUsage.request_id,
            RequestFieldEvidence.field_id == RequestFieldUsage.field_id,
        ),
    ).where(RequestFieldUsage.request_id == ObservedRequest.id,
            func.coalesce(RequestFieldEvidence.state, "unavailable") == "unavailable").exists()
    requests = select(
        group_key.label("key"),
        func.count().label("request_count"),
        func.count().filter(ObservedRequest.field_evidence_version.is_(None) | unavailable_evidence
                            ).label("unavailable_evidence_request_count"),
    ).select_from(ObservedRequest).outerjoin(TestExecution, and_(
        TestExecution.id == ObservedRequest.test_execution_id, TestExecution.run_id == run.id,
    )).where(ObservedRequest.run_id == run.id, *conditions).group_by(group_key).cte("group_requests")
    covered_field = case((
        and_(counted_observation(), SchemaType.kind.in_(COVERAGE_KINDS)), SchemaField.id,
    ))
    observed_field = case((RequestFieldEvidence.state == "observed", SchemaField.id))
    fields = select(
        group_key.label("key"),
        func.count(distinct(SchemaField.id)).label("selected_field_count"),
        func.count(distinct(covered_field)).label("covered_field_count"),
        func.count(distinct(observed_field)).label("observed_field_count"),
    ).select_from(ObservedRequest).outerjoin(TestExecution, TestExecution.id == ObservedRequest.test_execution_id
    ).join(RequestFieldUsage, RequestFieldUsage.request_id == ObservedRequest.id
    ).join(SchemaField, SchemaField.id == RequestFieldUsage.field_id
    ).join(SchemaType, SchemaType.id == SchemaField.type_id
    ).outerjoin(RequestFieldEvidence, and_(
        RequestFieldEvidence.request_id == ObservedRequest.id, RequestFieldEvidence.field_id == SchemaField.id,
    )).where(ObservedRequest.run_id == run.id, SchemaType.snapshot_id == run.snapshot_id,
             ObservedRequest.analysis_status == "success", *conditions).group_by(group_key).cte("group_fields")
    keys = select(attempts.c.key).union(select(requests.c.key)).cte("group_keys")
    total = await session.scalar(select(func.count()).select_from(keys))
    total_fields = await session.scalar(select(func.count()).select_from(SchemaField).join(
        SchemaType, SchemaType.id == SchemaField.type_id
    ).where(SchemaType.snapshot_id == run.snapshot_id, SchemaType.kind.in_(COVERAGE_KINDS)))
    columns = [func.coalesce(column, 0).label(column.name)
               for table in (attempts, requests, fields) for column in table.c if column.name != "key"]
    statement = select(keys.c.key, *columns).select_from(keys)
    for table in (attempts, requests, fields):
        statement = statement.outerjoin(table, keys.c.key.is_not_distinct_from(table.c.key))
    rows = (await session.execute(statement.order_by(keys.c.key.asc().nullsfirst())
                                  .limit(filters.limit).offset(filters.offset))).mappings()
    return [
        GroupCoverage(**row, total_fields=total_fields,
                      coverage_percent=round(row["covered_field_count"] * 100 / total_fields, 2) if total_fields else 0)
        for row in rows
    ], total
