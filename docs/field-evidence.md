# Selected fields and response presence evidence

Integrated with `main` after PR #33 (GitLab E2E), #34 (run comparison) and
#36 (GraphQL Fragments), base `5e862e7`. Named, nested and inline fragments now
produce selected coordinates and presence evidence through the same analyzer.
The former Fragments dependency is resolved; no second fragment expander exists.

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

## Shared AST traversal and response plan

`AnalysisResult.fields` remains the unique selected schema coordinates.
The additive `selection_plan: SelectionScope | None` contains:

- `SelectionScope.possible_types`: all possible concrete object types at this level;
- `SelectionScope.fields`: flattened field occurrences, with repeated named spreads deduplicated within a response object;
- `PlannedField.coordinate`: static schema coordinate (`None` only for `__typename`);
- `response_key`: actual JSON key, including alias;
- `applicable_types`: intersection of fragment type conditions at this object level;
- `child`: nested scope, with its own possible return types;
- `list_depth`: number of list wrappers in the field return type.

The bounded fragment traversal from PR #36 now builds both outputs in one walk,
after validation and variable coercion. Each fragment intersects its type
condition with the possible types inherited from every outer fragment, and this
intersection becomes `applicable_types` on its fields. The static fragment type
still determines the schema coordinate (e.g. `Node.id`, without inventing hits
for concrete implementations).

Named-spread deduplication is local to a response object and keyed by fragment
name plus applicable concrete types. Repeated spreads in that object do not
multiply fields; the same spread at another alias or nested object retains its
own plan. Unknown/custom directives and complexity errors discard both outputs,
including any coordinates collected earlier. PR #36 limits are preserved:
10,000 query tokens, selection depth 64, selection visits 20,000. Every visited
field/spread in the unified walk consumes the shared visit budget.

`app/field_evidence.py` parses neither AST nor SDL. It uses the already decoded
envelope in a worker thread. Matching follows GraphQL merging of selection sets
for the same response key: an explicitly selected unconditional `__typename`
can discriminate sibling selections of that same object, including nested lists.
Only proven applicable parent selections contribute that context; a conditional
alias cannot identify its own runtime branch. Missing/contradictory runtime type
information stays indeterminate for conditional coordinates. A common coordinate
and response key selected across every possible branch can prove presence without
inventing a concrete runtime type; an explicit typename key covering all branches
can likewise discriminate those branches. No response values, variables or plans are
persisted.

Real-document unit and Proxy → Capture → PostgreSQL → Field Evidence API tests
cover named/nested/inline fragments, repeated spreads across aliases and child
objects, intersections of type conditions, unions/interfaces, directives/default
variables, mixed arrays, null, missing runtime type and partial errors. Isolated
plan fixtures remain only as low-level matcher tests.

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
counts as successful resolvers. Historical fields use unavailable. Real named/nested/inline fragment documents
are supported; invalid documents and exceeded analysis limits have no partial
selected fields or plan and are exposed through the unavailable request counters.

## Validation

Final validation totals for the integrated branch are recorded in PR #35. Tests
run against isolated PostgreSQL 18 locally and PostgreSQL 16 in GitHub CI. This includes 60 new unit cases and 31 new PostgreSQL cases.
Only the existing Starlette TestClient/httpx deprecation warning remains.
Migration upgrade/downgrade/re-upgrade, `alembic check`, `pip check`, compileall
and `git diff --check` passed. GitHub CI also verifies PostgreSQL 16.

New tests cover classification, aliases,
directives/default variables, arrays, repeated coordinates, real fragment documents and abstract-type plan
contracts, invalid/large responses, actual proxy byte preservation, privacy,
historical fallback, endpoint pagination/filters, grants across services, atomic
rollback on evidence insert failure, and migration upgrade/downgrade/check.
The full existing suite verifies Schema Coverage and API compatibility.

## Integration acceptance example

For this real document (single object `search: Search`, union `User | Order`):

```graphql
query Search {
  search {
    __typename
    ... on User { name }
    ... on Order { total }
  }
}
```

With `{"data":{"search":{"__typename":"User","name":"Alex"}}}` the API
returns `User.name` with `selectedHits: 1, observedHits: 1`, and `Order.total`
with `selectedHits: 1, observedHits: 0, notObservedCount: 1`. Neither contributes
an extra hit for `__typename`. Replacing inline fragments with named fragments
produces the same persisted evidence and API counters.

PR #34 comparison continues to use the original selected hits, so two runs
selecting these same coordinates retain equal Schema Coverage even when their
presence evidence differs. GitLab E2E lifecycle/routes from PR #33 are unchanged.
No Path / Depth / RelationHits metrics or API have been added.
