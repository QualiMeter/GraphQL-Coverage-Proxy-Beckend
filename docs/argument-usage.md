# Argument Usage — Backend task 4

Argument Usage describes the argument syntax and effective variable nullness of a
selected schema field in each captured HTTP request. It does not assert resolver
execution, a successful test, or coverage of all possible argument values. Schema
Coverage and its denominator/eligibility formula are unchanged.

## Base and delivery order

Final implementation base: `main` `8304f7c` (18 November 2026), including merged
PR [#40](https://github.com/QualiMeter/GraphQL-Coverage-Proxy-Beckend/pull/40).
At task start #40 was open, so development and the first regression run used its
head `1ab1fe1`; the merged main has an identical tree. The feature branch was then
rebased onto the new main so its PR contains only Argument Usage changes. #40
supplies run-wide admission/cleanup, including requests without TestExecution.
No merge or deployment was performed by this task.

Tasks 1–3, exports, branch/CI history, grouping, GitLab integrations and frontend
code are outside this change. The only new endpoint is `/arguments`; the only
new migration follows #40's revision. If another task adds a migration in
parallel, reconcile the Alembic head before merging; do not rename/rewrite
already applied revisions.

After merging PR #41, this branch was reconciled with `main`. The migration
`20261126_argument_usage` now follows `20261126_merge_ci_history`, which joins
`20261125_gitlab_history` and `20261125_run_in_flight`. There is one Alembic head.

## One mutually exclusive category per request and argument

Canonical coordinate: `Type.field.argumentName`. Type/field use the analyzer's
static schema coordinate, including interface coordinates. Aliases and response
paths are not persisted in this metric. The same coordinate appears once per
ObservedRequest, however many AST occurrences, fragments or list elements exist.
Separate HTTP requests, including real retries, each count once.

| Category | Meaning |
| --- | --- |
| `provided` | Non-null literal, list/input object, or successfully coerced non-null variable. Contents and nested nulls are not analyzed as separate arguments. |
| `explicit_null` | Literal `null`, variable `null`, or a variable default resolving to null. |
| `omitted` | No argument expression, or an optional variable absent with no variable default. A schema default may still apply. |
| `unknown` | A variable value is unavailable or runtime variable coercion failed. |
| `mixed` | Aliases/occurrences have different **known** categories for the same coordinate in this HTTP request. |

An unknown occurrence dominates all other occurrences of that coordinate;
otherwise differing known categories merge into `mixed`. These operations are
order-independent. `mixed` contributes to its own counter only: it is not added
to provided/null/omitted. Two different non-null values remain one `provided`
hit. No value diversity or value percentage is collected.

Variable defaults are handled by the existing GraphQL coercer. An explicit
variable null overrides a variable default. An omitted argument stays `omitted`
even if the field's schema default is non-null. If any runtime variable fails
coercion, this implementation conservatively marks **all variable-based argument
occurrences** unknown; literal and absent argument categories remain known.
Invalid variables continue to produce no selected-field coverage. Internally,
`GraphQLQueryInput.variables=None` explicitly denotes unavailable variable
information; an absent/null HTTP `variables` envelope means the standard known
empty map instead.

The existing AST traversal handles operationName, Query/Mutation, named/nested
and inline fragments, aliases and skip/include. Statically invalid documents
(including nonexistent arguments), unsupported operations/directives, parse
failures, unresolved directive conditions or traversal limits yield unavailable
argument collection, without fabricated rows. GraphQL errors, partial or missing
responses do not erase known request categories: this metric describes selection
and supplied arguments, independently of response field evidence.

## Storage and limits

Migration `20261126_argument_usage`, parent `20261125_run_in_flight`, adds:

- Nullable `observed_requests.argument_usage_version` (1 when collection is
  complete, NULL when unavailable), with a check constraint and no default or
  historical backfill. An analyzed empty selection has version 1 and zero rows.
- `request_argument_usage(request_id, field_id, argument_name, state)`, primary
  key `(request_id, field_id, argument_name)`, checked category and foreign keys
  to ObservedRequest (cascade) and SchemaField (restrict).
- Index `(field_id, argument_name, state)`; the primary key and existing
  `observed_requests(run_id)` index support run-scoped aggregation.

The run's snapshot is reached through ObservedRequest → TestRun → SchemaSnapshot;
field coordinates through SchemaField → SchemaType → SchemaSnapshot. The storage
service verifies every field and argument against that exact run snapshot before
writing. Names/types/default-presence come from the existing immutable, cached
SDL, so there is no duplicated schema-argument catalog. Run/finish/execution lock
order and atomic observation commit remain unchanged. There is no connection
held during upstream HTTP and no new upstream invocation or mutation retry.

The existing token/depth/selection-visit limits remain. At most 5,000 unique
argument rows are stored per observation, with at most 20,000 argument visits; over-limit collection is discarded in
full and marked unavailable while existing field/path coverage remains usable.
Argument names over 255 characters similarly make argument collection unavailable
rather than failing an already executed request. No argument values, variables,
input-object contents or new client-derived error details are stored or returned.
The existing journal's masked document mechanism continues to redact literals
and variable defaults.

## Frontend contract

`GET /api/runs/{runId}/arguments`

Uses the same protected analytics policy as the request journal: analyst or
operator with a service grant; admin or trusted CLI key. A public service does
not expose this endpoint anonymously. Private resources without a grant return
404, insufficient role returns 403, missing authentication returns 401.

Optional exact filters `typeName`, `fieldName`, `argumentName` combine with AND
and may be supplied independently. `limit` defaults to 100 (1–200), `offset`
defaults to 0. Items sort lexicographically by type, field, argument. `total` is
the filtered catalog size before pagination. Unselected schema arguments also
appear, with zero request counts. No schema default values are returned.

Example: `/api/runs/00000000-0000-0000-0000-000000000001/arguments?typeName=Query&fieldName=orders&argumentName=status`

```json
{
  "runId": "00000000-0000-0000-0000-000000000001",
  "snapshotId": "00000000-0000-0000-0000-000000000002",
  "availability": "partial",
  "totalRequests": 8,
  "analyzedRequests": 7,
  "unavailableRequests": 1,
  "total": 1,
  "limit": 100,
  "offset": 0,
  "items": [{
    "typeName": "Query",
    "fieldName": "orders",
    "argumentName": "status",
    "argumentType": "Status",
    "hasSchemaDefault": true,
    "providedCount": 2,
    "explicitNullCount": 1,
    "omittedCount": 2,
    "unknownCount": 1,
    "mixedCount": 1,
    "requestCount": 7
  }]
}
```

Counters are counts of **HTTP requests selecting this field**, never counts of
AST nodes or successful tests. `providedCount` specifically means non-null;
`explicitNullCount` is separate. `requestCount` is the sum of all five category
counters and can be smaller than `analyzedRequests` because not every request
selects that field.

`availability` is run-wide, unaffected by filters or pagination:

- `available`: every persisted observation has complete argument collection.
- `partial`: some observations have collection, others do not. Display counters
  as the known subset, with `unavailableRequests` prominently visible.
- `unavailable`: no observation has collection, including empty/historical runs.
  Display “analytics unavailable”, not “argument never used”. Zero counters are
  placeholders for a readable schema catalog, not proof of non-use.

Unknown is a recorded per-argument category; unavailable means no reliable
per-argument inventory for that observation. `analyzedRequests` includes requests
with unknown categories. Availability and all page counts use one PostgreSQL
statement snapshot, including while a run is collecting. Live pagination may
observe later requests on subsequent page fetches. `/openapi.json` and `/docs`
contain the typed response, query bounds and authorization schemes.

## Validation

Unit tests: `tests/test_argument_usage.py`. PostgreSQL integration tests:
`tests/test_argument_usage_postgres.py`. The latter cover actual proxy capture,
8 concurrent exchanges and a separate retry, storage uniqueness, variable
availability, GraphQL errors/partial responses, historical data, migration
round-trip/Alembic metadata parity, filters/pagination, RBAC/service grants,
privacy of DB/journal/API/logs and unchanged Schema/Test Coverage semantics.

Use a disposable local PostgreSQL with `TEST_DATABASE_URL`; never production.
The existing full backend suite also covers GitLab and admission/cleanup
regressions from #40. Deployment/production smoke tests are outside this task.

## Changed files

| File | Change |
| --- | --- |
| `app/query_analyzer.py` | Shared AST traversal classifies, deduplicates and bounds argument categories. |
| `app/contracts.py` | In-memory value-free ArgumentUsage contract; explicit unavailable-variable marker. |
| `app/models.py` | RequestArgumentUsage entity and nullable observation collection version. |
| `app/services.py` | Validate against run snapshot and atomically persist argument rows. |
| `app/argument_usage.py` | Catalog, category validation, typed API response/example and consistent aggregation. |
| `app/api.py` | Protected, filtered and paginated GET run arguments route. |
| `alembic/versions/20261126_argument_usage.py` | Additive migration, constraints, foreign keys and index. |
| `tests/test_argument_usage.py` | 32 unit/OpenAPI cases, including CI compatibility regression. |
| `tests/test_argument_usage_postgres.py` | 30 PostgreSQL capture/API/privacy/concurrency/migration cases. |
| `tests/test_auth_migrations_postgres.py` | Historical comparison allows new column and explicitly asserts NULL. |
| `tests/test_final_features_postgres.py` | Historical safe-document migration asserts nullable argument version. |
| `tests/test_graphql_error_details_postgres.py` | Historical error metadata migration asserts nullable argument version. |
| `docs/argument-usage.md` | Semantics, model, API example, limits and verification report. |
| `docs/frontend-api.md` | Link to the frontend Argument Usage contract. |
| `README.md` | Link to Argument Usage documentation. |

## Completed verification (18 November 2026)

Final feature code: `a08d708`, based on `main` `8304f7c`. PostgreSQL clusters were
created specifically for this task under `/tmp/gcp-arguments-pg` (port 55441) and
`/tmp/gcp-arguments-pg16` (port 55442), with test-only databases/credentials. No
production connection, migration, merge or deployment was used.

| Check | Result |
| --- | --- |
| Full backend suite, PostgreSQL 18 | **1106 passed**, 285.08 s |
| Full backend suite, PostgreSQL 16 | **1106 passed**, 288.41 s |
| New tests included above | **31 unit/OpenAPI + 30 PostgreSQL cases** |
| Standalone Playwright SDK unit suite, Node 22 | **3 passed** |
| Fresh Alembic upgrade to head, both PostgreSQL versions | Passed |
| Argument migration downgrade/upgrade + historical row preservation | Passed on both versions within the full suite |
| `alembic check`, both PostgreSQL versions | No new upgrade operations detected |
| `python -m pip check` | No broken requirements found |
| `python -m compileall -q app` | Passed |
| `git diff --check` | Passed |

Both full runs emitted only the existing Starlette/httpx TestClient deprecation
warning. The initial full run exposed three historical JSON-row comparisons
that needed to allow the additive column; those assertions now also explicitly
verify its historical NULL value. All final runs above are green.

Reproduction (Python 3.12, Node 22; migrate the disposable target first):

```sh
DATABASE_URL="$TEST_DATABASE_URL" alembic upgrade head
python -m pytest -q
DATABASE_URL="$TEST_DATABASE_URL" alembic check
node --test integrations/playwright/index.test.cjs
python -m pip check
python -m compileall -q app
git diff --check
```

Final local full-suite logs are `/tmp/gcp-argument-usage-final-pg18.log` and
`/tmp/gcp-argument-usage-final-pg16.log`. Production/Docker deployment smoke tests
were not invoked; the repository CI workflow is unchanged.

## CI compatibility correction

The first PR CI run resolved `graphql-core 3.3.2`, while the initial local runs
used `3.2.14`. Six unit cases failed: 3.3 returns a `VariableValues` object with
`.coerced`/`.sources`, whereas 3.2 returns a dictionary. The analyzer now reads
argument nullness from `.coerced` when present and passes the original native
object to directive coercion. Failed/unavailable variable coercion uses `None`
(supported by both versions), preserving validation errors and uncertainty.

The existing variable/default/directive tests reproduced all six failures on
3.3.2 before the fix. A further regression covers a literal directive alongside
unavailable argument variables. CI now tests both supported dependency lines
(`3.2.*` and `3.3.*`) with `fail-fast: false`, including full PostgreSQL and Docker
smoke checks. Production deployment conditions are unchanged.

Additional changed file: `.github/workflows/backend-ci.yml` (dependency matrix).
