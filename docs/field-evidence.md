# Selected fields and response presence evidence

Status: evidence implementation on top of `main` after PR #33 (`6697fd3`).
**Depends on the parallel Fragments task for end-to-end named/inline fragments.**
At implementation time (17 November 2026), that task was local to its developer,
with no published branch or PR. This change deliberately does not implement a
second fragment expander. Existing fragment requests still return `unsupported`
until the shared analyzer traversal is integrated. Do not treat them as zero coverage.

## Semantics

Presence is evidence that a selected response key existed, including when its
value was `null`. It does not prove successful resolver execution.

| State | Classification |
|---|---|
| `observed` | An applicable selected key was present in at least one occurrence. |
| `not_observed` | Key missing in a known applicable object in a clean response, or a type condition proven inapplicable. |
| `indeterminate` | Null/missing/malformed parent, empty list, null list item, unknown/contradictory runtime type, or missing key in partial/error data. |
| `unavailable` | Missing/invalid/oversized response, no `data`, exhausted matching budget, or no stored evidence. |

A `data: null` root gives `indeterminate`; missing `data` gives `unavailable`.
Malformed `data` gives `unavailable`. Any errors make missing keys indeterminate;
we do not attribute errors to individual resolver calls. A key present alongside
errors still proves presence. Invalid error shapes also prevent absence claims.

Arrays and aliases collapse to one state per schema coordinate and request.
Presence anywhere wins; otherwise uncertainty wins over absence. An empty array
proves the array field was present, but says nothing about its child fields.

Runtime discrimination uses only explicitly selected, unconditional `__typename`
keys (including aliases), or a single possible concrete type. An unknown branch
stays indeterminate even when its alias matches a returned user key. Conflicting
or invalid runtime names stay indeterminate. Interface fields applicable to every
possible type can be observed without a runtime discriminator.
`__typename` is not persisted as a schema field or included in coverage.
No query rewriting or automatic `__typename` insertion occurs.

## Shared analyzer contract and Fragments handoff

`AnalysisResult.fields` remains the unique selected schema coordinates.
The additive `selection_plan: SelectionScope | None` contains:

- `SelectionScope.possible_types`: all possible concrete object types at this level;
- `SelectionScope.fields`: one entry per AST field occurrence;
- `PlannedField.coordinate`: static schema coordinate (`None` only for `__typename`);
- `response_key`: actual JSON key, including alias;
- `applicable_types`: intersection of fragment type conditions at this object level;
- `child`: nested scope, with its own possible return types;
- `list_depth`: number of list wrappers in the field return type.

The existing analyzer populates the plan in its existing `_collect_fields` walk,
after the existing validation and variable/directive coercion. No AST or schema
parsing happens in `app/field_evidence.py`. The decoded envelope is reused from
the existing capture path and matched in a worker thread. The plan, variables,
aliases and response values are never written to the evidence table.

**Integration required from Fragments developer:** extend this same walk to expand
named/inline fragments once, evaluate directives on fields/spreads/fragments
using the same coerced variables, carry the intersection of possible concrete
types into `applicable_types`, keep static coordinate attribution consistent with
`fields`, and retain explicit `__typename` entries. Do not expand fragments again
in the matcher. Shared fragment traversal must populate both `fields` and the
plan; changing just one produces unavailable evidence instead of guessed results.

Unit tests and PostgreSQL adapter tests cover equivalent expanded plans for
named/inline fragments, unions, interfaces and mixed object arrays. They are
contract tests, **not end-to-end AST fragment tests**. The existing analyzer tests
continue to assert `unsupported` for actual fragment documents. Before declaring
this task fully ready, integrate that developer's traversal and run the same
scenarios through real named/inline fragment documents and the real proxy.

## Persistence and migration

Run `alembic upgrade head`. New revision `20261122_field_evidence` follows
`20261121_gitlab_pipelines`, the head introduced by merged PR #33. Downgrade:
`alembic downgrade 20261121_gitlab_pipelines` removes only evidence data/marker.

`request_field_evidence` stores only:

| Column | Meaning |
|---|---|
| `request_id` | ObservedRequest, part of the composite primary key |
| `field_id` | SchemaField of that request run's immutable snapshot, part of primary key |
| `state` | One of the four states; checked by PostgreSQL |

Composite FK `(request_id, field_id)` references `request_field_usage`, so evidence
cannot claim an unselected field. `(field_id, state)` is indexed. Snapshot IDs are
validated by the same existing observation writer before any writes. The request,
selected usage and evidence commit together under the existing run row lock;
finish and capture remain serialized. A failed evidence INSERT rolls back all
three. Repeated coordinates cannot multiply rows or counts.

`observed_requests.field_evidence_version` is nullable; value `1` means the matcher
supplied a complete per-coordinate result. Historical rows remain null, without
backfill. Missing evidence rows use `unavailable` through a left join. Version `1`
can include unavailable states (e.g. invalid response); it is a format marker,
not a claim that all observations are known.

No raw response, variables, user field values, tokens or plans are stored. Existing
sanitized request/error metadata keeps its established policy. Matching is bounded
to 100,000 steps and depth 100; the existing decoded response limit is 1 MiB.
Budget exhaustion discards partial results and reports unavailable evidence.

## Coverage compatibility

`RequestFieldUsage` and `counted_observation()` are unchanged:
`selectedHits = count(unique request/field where analysis_status='success' AND response_has_data=true)`.
Existing Schema Coverage denominator remains OBJECT fields and its numerator
remains distinct fields with at least one counted selected hit. Neither evidence
state nor GraphQL/HTTP error status changes those hits. Existing `/coverage`,
`/stats`, `/operations` and selectedFields keep their meanings.

`selectedRequestCount` includes all successfully analyzed selections, including
requests without data. It can therefore exceed `selectedHits`. For each field:

```text
selectedRequestCount = observedHits + notObservedCount + indeterminateCount + unavailableCount
```

`observedHits` is at most one per coordinate/request, regardless of array size or
aliases. No observed percentage is inferred from unknown states.

## API for frontend

`GET /api/runs/{runId}/field-evidence`

Journal authorization applies: authenticated analyst/operator with an explicit
service grant, or admin/trusted CLI. Public visibility alone never opens evidence.
Missing/inaccessible runs and foreign request IDs return 404, with no foreign
metadata. Viewer gets 403; anonymous gets 401. Existing Origin/CSRF rules remain.

Query parameters (AND): `typeName`, `fieldName` (exact case-sensitive matches),
`state` (at least one request with that state), `requestId` (within this run),
`limit` (default 100, range 1–500), `offset` (default 0, >=0).
Invalid parameters return 422. Fields sort by type, field, ID. Only coordinates
selected at least once are returned. `total` is the filtered count before paging;
request counts concern the whole run or `requestId` scope and do not change with
field filters. Read all pages for all selected fields. Unselected coordinates can
be displayed from the existing schema graph.

For `{ user { id name } }` with `{"data":{"user":{"id":null}}}`:

```json
{
  "runId": "10000000-0000-0000-0000-000000000001",
  "snapshotId": "10000000-0000-0000-0000-000000000002",
  "requestCount": 1,
  "selectionUnavailableRequestCount": 0,
  "evidenceUnavailableRequestCount": 0,
  "total": 3,
  "limit": 100,
  "offset": 0,
  "fields": [
    {
      "fieldId": "20000000-0000-0000-0000-000000000001",
      "typeName": "Query", "fieldName": "user",
      "selectedRequestCount": 1, "selectedHits": 1, "observedHits": 1,
      "notObservedCount": 0, "indeterminateCount": 0, "unavailableCount": 0
    },
    {
      "fieldId": "20000000-0000-0000-0000-000000000002",
      "typeName": "User", "fieldName": "id",
      "selectedRequestCount": 1, "selectedHits": 1, "observedHits": 1,
      "notObservedCount": 0, "indeterminateCount": 0, "unavailableCount": 0
    },
    {
      "fieldId": "20000000-0000-0000-0000-000000000003",
      "typeName": "User", "fieldName": "name",
      "selectedRequestCount": 1, "selectedHits": 1, "observedHits": 0,
      "notObservedCount": 1, "indeterminateCount": 0, "unavailableCount": 0
    }
  ]
}
```

`selectionUnavailableRequestCount` counts requests with unsuccessful analysis;
their unknown selections cannot be attributed to particular schema coordinates.
`evidenceUnavailableRequestCount` counts requests without the version marker or
with at least one unavailable/missing evidence row. It includes old requests,
failed analyses and invalid/unavailable responses; it is not the sum of field counts.

`GET /api/runs/{runId}/requests/{requestId}` adds `fieldEvidenceVersion: 1 | null`.
Existing `selectedFields` stays unchanged. Use
`/field-evidence?requestId={requestId}&limit=100&offset=0` for paginated per-request
states (each counter then is 0 or 1).

Frontend handoff: display Selected Coverage and Observed Evidence separately;
show four explicit state counters/badges and the unavailable selection count.
Never turn indeterminate/unavailable into missing/zero coverage. A zero
`observedHits` alone is insufficient to assert absence. Do not label observed
counts as successful resolvers. Historical fields use unavailable, and actual
fragment documents remain unsupported until the dependency above is integrated.

## Validation

Final local validation: **887 passed**, no skips, on isolated PostgreSQL 18
(142.97 seconds). This includes 60 new unit cases and 31 new PostgreSQL cases.
Only the existing Starlette TestClient/httpx deprecation warning remains.
Migration upgrade/downgrade/re-upgrade, `alembic check`, `pip check`, compileall
and `git diff --check` passed. GitHub CI also verifies PostgreSQL 16.

New tests cover classification, aliases,
directives/default variables, arrays, repeated coordinates, abstract-type plan
contracts, invalid/large responses, actual proxy byte preservation, privacy,
historical fallback, endpoint pagination/filters, grants across services, atomic
rollback on evidence insert failure, and migration upgrade/downgrade/check.
The full existing suite verifies Schema Coverage and API compatibility.
