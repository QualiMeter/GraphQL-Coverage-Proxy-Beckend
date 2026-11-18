import hashlib
import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator
from sqlalchemy import and_, case, distinct, exists, func, select, true, update
from sqlalchemy.exc import IntegrityError

from app.api_models import ApiModel
from app.contracts import RunStatus
from app.models import ObservedRequest, RequestFieldEvidence, RequestFieldUsage, SchemaField, SchemaType, TestExecution, TestRun
from app.services import COVERAGE_KINDS, ConflictError, NotFoundError, counted_observation
from app.test_groups import GroupFilters, group_conditions

ExecutionStatus = Literal["running", "passed", "failed", "skipped", "interrupted"]
FinalStatus = Literal["passed", "failed", "skipped", "interrupted"]
FailureCode = Literal["test_failed", "assertion_failed", "setup_failed", "teardown_failed", "timeout", "runner_interrupted"]


class ExecutionCreate(ApiModel):
    model_config = ConfigDict(alias_generator=ApiModel.model_config["alias_generator"], populate_by_name=True, extra="forbid")
    id: uuid.UUID
    test_id: str = Field(min_length=1, max_length=2048)
    name: str = Field(min_length=1, max_length=512)
    framework: Literal["pytest", "playwright"]
    attempt: int | None = Field(default=1, ge=1, le=10000, strict=True)
    team_key: str | None = Field(default=None, min_length=1, max_length=128)
    suite_key: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("test_id", "name", "team_key", "suite_key")
    @classmethod
    def valid_text(cls, value):
        if value is None:
            return value
        if value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Use nonempty text without surrounding whitespace or control characters")
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise ValueError("Use valid Unicode text") from None
        return value


class ExecutionFinish(ApiModel):
    model_config = ConfigDict(alias_generator=ApiModel.model_config["alias_generator"], populate_by_name=True, extra="forbid")
    status: FinalStatus
    duration_ms: int | None = Field(default=None, ge=0, le=31536000000, strict=True)
    failure_code: FailureCode | None = None

    @model_validator(mode="after")
    def normalize_failure(self):
        if self.status in {"passed", "skipped"} and self.failure_code is not None:
            raise ValueError("Passed or skipped tests cannot have a failure code")
        if self.failure_code is None and self.status in {"failed", "interrupted"}:
            self.failure_code = "runner_interrupted" if self.status == "interrupted" else "test_failed"
        return self


class ExecutionFilters(GroupFilters):
    status: ExecutionStatus | None = None
    framework: Literal["pytest", "playwright"] | None = None
    test_id: str | None = Field(default=None, min_length=1, max_length=2048)
    type_name: str | None = Field(default=None, min_length=1, max_length=255)
    field_name: str | None = Field(default=None, min_length=1, max_length=255)
    field_mode: Literal["covered", "selected", "observed"] = "covered"
    limit: int = Field(default=100, ge=1, le=500)
    offset: int = Field(default=0, ge=0, le=2**63 - 1)

    @model_validator(mode="after")
    def field_scope(self):
        if self.field_name is not None and self.type_name is None:
            raise ValueError("fieldName requires typeName")
        return self


class ExecutionOut(ApiModel):
    id: uuid.UUID
    run_id: uuid.UUID
    test_id: str
    name: str
    framework: str
    team_key: str | None = None
    suite_key: str | None = None
    attempt: int
    status: ExecutionStatus
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    failure_code: FailureCode | None
    in_flight_requests: int
    collection_status: RunStatus
    result_missing: bool
    request_count: int = 0
    counted_request_count: int = 0
    selected_field_count: int = 0
    covered_field_count: int = 0
    observed_field_count: int = 0
    total_fields: int = 0
    coverage_percent: float = 0


class ExecutionFieldOut(ApiModel):
    type_name: str
    field_name: str
    kind: str
    selected_request_count: int
    hits: int
    observed_count: int
    not_observed_count: int
    indeterminate_count: int
    unavailable_count: int


class TestCoverageSummary(ApiModel):
    run_id: uuid.UUID
    collection_status: RunStatus
    total_executions: int
    total_tests: int
    status_counts: dict[str, int]
    missing_results: int
    in_flight_requests: int
    total_requests: int
    attributed_requests: int
    unattributed_requests: int


async def get_run(session, run_id, *, lock=False):
    statement = select(TestRun).where(TestRun.id == run_id).execution_options(populate_existing=True)
    run = await session.scalar(statement.with_for_update() if lock else statement)
    if run is None:
        raise NotFoundError("Test run not found")
    return run


async def get_execution(session, run_id, execution_id, *, lock=False):
    statement = select(TestExecution).where(TestExecution.id == execution_id, TestExecution.run_id == run_id)
    statement = statement.execution_options(populate_existing=True)
    row = await session.scalar(statement.with_for_update() if lock else statement)
    if row is None:
        raise NotFoundError("Test execution not found")
    return row


async def create_execution(session, run_id, payload):
    run = await get_run(session, run_id, lock=True)
    existing = await session.get(TestExecution, payload.id, populate_existing=True)
    if existing is not None:
        if existing.run_id != run_id:
            raise NotFoundError("Test execution not found")
        if any(getattr(existing, name) != getattr(payload, name)
               for name in ("test_id", "name", "framework", "attempt", "team_key", "suite_key")
               if name != "attempt" or payload.attempt is not None):
            raise ConflictError("Execution ID already has different metadata")
        await session.commit()
        return await execution_detail(session, run_id, existing.id)
    if run.status != "running":
        raise ConflictError("Cannot start a test in a finished run")
    test_key = hashlib.sha256(payload.test_id.encode("utf-8")).hexdigest()
    attempt = payload.attempt
    if attempt is None:
        # The run row lock serializes allocation with every other registration.
        latest = await session.scalar(select(func.max(TestExecution.attempt)).where(
            TestExecution.run_id == run_id, TestExecution.framework == payload.framework,
            TestExecution.test_key == test_key))
        attempt = (latest or 0) + 1
        if attempt > 10000:
            raise ConflictError("Test attempt limit reached")
    row = TestExecution(id=payload.id, run_id=run_id, test_id=payload.test_id,
                        test_key=test_key,
                        name=payload.name, framework=payload.framework, attempt=attempt,
                        team_key=payload.team_key, suite_key=payload.suite_key)
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise ConflictError("This test attempt is already registered") from None
    return await execution_detail(session, run_id, row.id)


async def finish_execution(session, run_id, execution_id, payload):
    await get_run(session, run_id, lock=True)
    row = await get_execution(session, run_id, execution_id, lock=True)
    if row.status != "running":
        if (row.status != payload.status or row.failure_code != payload.failure_code
                or (payload.duration_ms is not None and row.duration_ms != payload.duration_ms)):
            raise ConflictError("Execution already has a different final result")
    else:
        if row.in_flight_requests and payload.status != "interrupted":
            raise ExecutionBusy("Wait for all test requests before publishing the result")
        finished = datetime.now(timezone.utc)
        elapsed = max(0, int((finished - row.started_at).total_seconds() * 1000))
        row.status = payload.status
        row.finished_at = finished
        row.duration_ms = payload.duration_ms if payload.duration_ms is not None else elapsed
        row.failure_code = payload.failure_code
    await session.commit()
    return await execution_detail(session, run_id, execution_id)


class ExecutionBusy(ConflictError):
    pass


async def admit_request(session, run_id, execution_id=None):
    run = await get_run(session, run_id, lock=True)
    if run.status != "running":
        raise ConflictError("Coverage run is not running")
    if execution_id is not None:
        execution = await get_execution(session, run_id, execution_id, lock=True)
        if execution.status != "running":
            raise ConflictError("Test execution has already finished")
        execution.in_flight_requests += 1
    run.in_flight_requests += 1
    await session.commit()
    await session.close()


async def release_request(sessions, execution_id=None, *, run_id=None):
    async with sessions() as session:
        # Lock order matches admission and recording: run, then execution.
        if run_id is not None:
            await session.execute(update(TestRun).where(
                TestRun.id == run_id, TestRun.in_flight_requests > 0,
            ).values(in_flight_requests=TestRun.in_flight_requests - 1))
        if execution_id is not None:
            await session.execute(update(TestExecution).where(
                TestExecution.id == execution_id, TestExecution.in_flight_requests > 0,
            ).values(in_flight_requests=TestExecution.in_flight_requests - 1))
        await session.commit()


async def execution_stats(session, run, rows):
    ids = [row.id for row in rows]
    if not ids:
        return []
    total_fields = await session.scalar(select(func.count()).select_from(SchemaField).join(
        SchemaType, SchemaType.id == SchemaField.type_id).where(
        SchemaType.snapshot_id == run.snapshot_id, SchemaType.kind.in_(COVERAGE_KINDS)))
    requests = (await session.execute(select(ObservedRequest.test_execution_id,
        func.count(), func.sum(case((counted_observation(), 1), else_=0))).where(
        ObservedRequest.run_id == run.id, ObservedRequest.test_execution_id.in_(ids),
    ).group_by(ObservedRequest.test_execution_id))).all()
    request_counts = {ident: (total, counted) for ident, total, counted in requests}
    fields = (await session.execute(select(ObservedRequest.test_execution_id,
        func.count(distinct(RequestFieldUsage.field_id)),
        func.count(distinct(case((and_(counted_observation(), SchemaType.kind.in_(COVERAGE_KINDS)), SchemaField.id)))),
        func.count(distinct(case((RequestFieldEvidence.state == "observed", SchemaField.id)))),
    ).select_from(ObservedRequest)
        .join(RequestFieldUsage, RequestFieldUsage.request_id == ObservedRequest.id)
        .join(SchemaField, SchemaField.id == RequestFieldUsage.field_id)
        .join(SchemaType, SchemaType.id == SchemaField.type_id)
        .outerjoin(RequestFieldEvidence, and_(RequestFieldEvidence.request_id == ObservedRequest.id,
                                            RequestFieldEvidence.field_id == SchemaField.id))
        .where(ObservedRequest.run_id == run.id, ObservedRequest.test_execution_id.in_(ids))
        .group_by(ObservedRequest.test_execution_id))).all()
    field_counts = {ident: (selected, covered, observed) for ident, selected, covered, observed in fields}
    result = []
    attributes = ("id", "run_id", "test_id", "name", "framework", "attempt", "status", "started_at",
                  "finished_at", "duration_ms", "failure_code", "in_flight_requests", "team_key", "suite_key")
    for row in rows:
        total, counted = request_counts.get(row.id, (0, 0))
        selected, covered, observed = field_counts.get(row.id, (0, 0, 0))
        result.append(ExecutionOut(**{name: getattr(row, name) for name in attributes},
            collection_status=run.status, result_missing=run.status != "running" and row.status == "running",
            request_count=total, counted_request_count=counted, selected_field_count=selected,
            covered_field_count=covered, observed_field_count=observed, total_fields=total_fields,
            coverage_percent=round(covered * 100 / total_fields, 2) if total_fields else 0))
    return result


async def execution_detail(session, run_id, execution_id):
    run = await get_run(session, run_id)
    row = await get_execution(session, run_id, execution_id)
    return (await execution_stats(session, run, [row]))[0]


def execution_query(run_id, snapshot_id, filters):
    conditions = [TestExecution.run_id == run_id, *group_conditions(filters)]
    for name in ("status", "framework"):
        if getattr(filters, name) is not None:
            conditions.append(getattr(TestExecution, name) == getattr(filters, name))
    if filters.test_id is not None:
        conditions.extend((TestExecution.test_key == hashlib.sha256(filters.test_id.encode("utf-8")).hexdigest(),
                           TestExecution.test_id == filters.test_id))
    if filters.type_name is not None:
        selected = select(1).select_from(ObservedRequest).join(
            RequestFieldUsage, RequestFieldUsage.request_id == ObservedRequest.id).join(
            SchemaField, SchemaField.id == RequestFieldUsage.field_id).join(SchemaType, SchemaType.id == SchemaField.type_id)
        predicates = [ObservedRequest.test_execution_id == TestExecution.id, ObservedRequest.run_id == run_id,
                      SchemaType.snapshot_id == snapshot_id, SchemaType.name == filters.type_name]
        if filters.field_name is not None:
            predicates.append(SchemaField.name == filters.field_name)
        if filters.field_mode == "covered":
            predicates.extend((counted_observation(), SchemaType.kind.in_(COVERAGE_KINDS)))
        elif filters.field_mode == "observed":
            selected = selected.join(RequestFieldEvidence, and_(RequestFieldEvidence.request_id == ObservedRequest.id,
                                                               RequestFieldEvidence.field_id == SchemaField.id))
            predicates.append(RequestFieldEvidence.state == "observed")
        conditions.append(exists(selected.where(*predicates)))
    return select(TestExecution).where(*conditions).order_by(TestExecution.started_at, TestExecution.id)


async def execution_page(session, run_id, filters):
    run = await get_run(session, run_id)
    statement = execution_query(run_id, run.snapshot_id, filters)
    total = await session.scalar(select(func.count()).select_from(statement.order_by(None).subquery()))
    rows = (await session.scalars(statement.limit(filters.limit).offset(filters.offset))).all()
    return await execution_stats(session, run, rows), total


def execution_fields_query(run_id, snapshot_id, execution_id=None, *, groups=None, per_execution=False):
    statement = select(SchemaType.name.label("type_name"), SchemaField.name.label("field_name"),
        SchemaType.kind.label("kind"), func.count().label("selected_request_count"),
        func.sum(case((and_(counted_observation(), SchemaType.kind.in_(COVERAGE_KINDS)), 1), else_=0)).label("hits"),
        *[func.sum(case((RequestFieldEvidence.state == state, 1), else_=0)).label(state + "_count")
          for state in ("observed", "not_observed", "indeterminate")],
        func.sum(case((and_(RequestFieldEvidence.state.is_not(None), RequestFieldEvidence.state != "unavailable"), 0),
                      else_=1)).label("unavailable_count"),
    ).select_from(ObservedRequest).join(RequestFieldUsage, RequestFieldUsage.request_id == ObservedRequest.id)
    statement = statement.join(SchemaField, SchemaField.id == RequestFieldUsage.field_id).join(
        SchemaType, SchemaType.id == SchemaField.type_id).outerjoin(RequestFieldEvidence, and_(
            RequestFieldEvidence.request_id == ObservedRequest.id, RequestFieldEvidence.field_id == SchemaField.id))
    statement = statement.where(ObservedRequest.run_id == run_id, SchemaType.snapshot_id == snapshot_id)
    if execution_id is not None:
        statement = statement.where(ObservedRequest.test_execution_id == execution_id)
    if groups is not None:
        statement = statement.outerjoin(TestExecution, TestExecution.id == ObservedRequest.test_execution_id)
        statement = statement.where(*group_conditions(groups))
    if per_execution:
        statement = statement.add_columns(ObservedRequest.test_execution_id.label("execution_id"))
        statement = statement.where(ObservedRequest.test_execution_id.is_not(None))
        statement = statement.group_by(ObservedRequest.test_execution_id)
    statement = statement.group_by(SchemaType.id, SchemaType.name, SchemaType.kind, SchemaField.id, SchemaField.name)
    return statement


async def execution_fields(session, run_id, execution_id, limit=100, offset=0, *, groups=None):
    run = await get_run(session, run_id)
    if execution_id is not None:
        await get_execution(session, run_id, execution_id)
    statement = execution_fields_query(run_id, run.snapshot_id, execution_id, groups=groups)
    fields = statement.cte("execution_fields")
    total = select(func.count().label("total")).select_from(fields).subquery()
    page = select(fields).order_by(fields.c.type_name, fields.c.field_name).limit(limit).offset(offset).subquery()
    rows = (await session.execute(select(total.c.total, *page.c).select_from(total).outerjoin(
        page, true(),
    ).order_by(page.c.type_name, page.c.field_name))).mappings().all()
    return [ExecutionFieldOut(**row) for row in rows if row["field_name"] is not None], rows[0]["total"]


async def coverage_summary(session, run_id):
    run = await get_run(session, run_id)
    rows = (await session.execute(select(TestExecution.status, func.count(), func.sum(TestExecution.in_flight_requests))
        .where(TestExecution.run_id == run_id).group_by(TestExecution.status))).all()
    counts = {status: 0 for status in ("running", "passed", "failed", "skipped", "interrupted")}
    in_flight = 0
    for status, count, pending in rows:
        counts[status] = count
        in_flight += pending
    tests = select(TestExecution.framework, TestExecution.test_key).where(TestExecution.run_id == run_id).distinct().subquery()
    total_tests = await session.scalar(select(func.count()).select_from(tests))
    total, attributed = (await session.execute(select(func.count(), func.count(ObservedRequest.test_execution_id))
        .select_from(ObservedRequest).where(ObservedRequest.run_id == run_id))).one()
    return TestCoverageSummary(run_id=run_id, collection_status=run.status, total_executions=sum(counts.values()),
        total_tests=total_tests, status_counts=counts, missing_results=counts["running"] if run.status != "running" else 0,
        in_flight_requests=run.in_flight_requests, total_requests=total, attributed_requests=attributed, unattributed_requests=total-attributed)
