# Query paths review fixes

Path limits apply only to optional analytics. Once the unique-path budget is
exceeded, the analyzer discards all paths and returns `max_depth=None`, while
continuing to collect selected coordinates and the response observation plan.
The base analysis remains `success`. The existing paths API reports
`unavailable` (or `partial` when mixed with requests with complete paths).
No new API status or migration is required; `selectedHits` and evidence remain
available.

Path dictionary insertion uses sorted batches of at most 1000 rows with
`ON CONFLICT DO NOTHING RETURNING path_hash, id`. Only newly inserted paths
receive segments; all dictionary IDs are then read in one query. The run lock,
unique snapshot/hash constraint and atomic transaction remain in place.
Sorting also gives concurrent transactions a consistent unique-index order.

Regression measurement on PostgreSQL 18, 5000 distinct one-segment paths:
original HEAD performs 5000 dictionary INSERT calls; the fix performs 5.
The regression also checks fewer than 100 total SQL calls on first capture,
repeat capture and two concurrent writers, with exactly 5000 dictionary paths
and 15000 request/path links after three captures.

Merge order: merge PR #37 first. Before merging PR #39, update its branch from
main and change `20261124_test_executions.down_revision` from
`20261123_incomplete_collection` to `20261122_query_paths`. Do not change PR #39's
parent revision before #37 lands, as that would break standalone migrations.
See PR #39's `docs/pr37-pr39-compatibility.md` for the integration procedure.
