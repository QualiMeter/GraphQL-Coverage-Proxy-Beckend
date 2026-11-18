# PR #37 and PR #39 integration procedure

PR #37 (`feat/query-paths-depth`) and PR #39 (`feat/test-coverage-sdk`) remain
independently migratable from current main. Do not make #39 depend on an absent
revision while #37 is still open.

## Merge order and migration chain

1. Review and merge #37 first through the normal repository workflow.
2. Fetch main and merge it into the existing #39 branch without force push.
3. Resolve the historical-row assertions in
   `tests/test_auth_migrations_postgres.py` and
   `tests/test_final_features_postgres.py` by retaining **both** nullable
   additions: `max_depth` and `test_execution_id`. Exclude both when comparing
   old JSON shapes; assert each new column is null for historical requests.
4. Change only `20261124_test_executions.down_revision` to
   `20261122_query_paths`. The resulting chain is:
   `20261123_incomplete_collection -> 20261122_query_paths -> 20261124_test_executions`.
   Revision identifiers are identities; their numeric ordering is immaterial.
5. Require one `alembic heads` result, `alembic upgrade head` and `alembic check`
   against a clean disposable database and a database with historical requests
   at the old head, plus the complete unit/PostgreSQL/SDK suites.
6. Push the updated #39 branch, wait for CI and repeat review before merging.

No merge or deployment is performed by this procedure's local verification.
The combined branch is a local test artifact and must not be pushed.

Changing this parent is safe for the intended case where #39 has not been
applied to shared databases. A developer who already applied #39 locally must
use a fresh disposable DB or explicitly reconcile that DB's version state;
do not blindly rewrite an already-deployed migration chain or downgrade a
populated production DB. Historical requests require no data backfill and
must not be deleted.

## Transaction and SDK review decisions

The proxy closes its capture session before releasing the admitted request
through another session. Closing rolls back failures and releases all row
locks before cleanup can update TestExecution. A shielded cleanup task survives
repeated cancellation; errors remain visible. Preflight and admission still
end their transactions before upstream HTTP.

The release remains a single cleanup path after success or failure. Moving it
into capture would require a separate durable ownership protocol for ambiguous
commit outcomes, to avoid decrementing another admitted request twice. The
current fix keeps capture's existing service contract and tests real SQL
errors, application exceptions, and cancellation after locking the execution.

Playwright caches in-flight registration promises, retaining the UUID on
failure for safe metadata retry. `execution.post(request, data)` adds the API
key and disables redirects with `maxRedirects: 0`; manual examples and the
integration helper do the same. Metadata fetch already rejects redirects.

Python finish retries the structured `409/test_requests_in_flight` response
with the same execution ID and payload. It allows at most three attempts,
100/200 ms backoff and a five-second retry deadline; each request timeout is
capped by the remaining budget. Other 409 responses fail immediately. Existing
network/5xx metadata retries remain. The first known final result is retained
for sessionfinish retry; only tests without a known result become interrupted.
Read/report failures never replace the known status.

## QA repository follow-up (not changed here)

Port the Python finish busy-response retry and deadline, preserve the original
UUID/status/duration/failureCode across retries, retain a known final result
for session cleanup, and reserve `interrupted` for actual runner interruption.
Port the busy-then-success and exhausted-retry regressions for passed, assertion
failure and teardown failure, plus a real runner interruption test. If QA uses
Playwright, also port promise caching and `maxRedirects: 0`/the safe post helper.
