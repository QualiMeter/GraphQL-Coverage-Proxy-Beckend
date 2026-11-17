"""Match an analyzer plan to decoded data in memory; no schema/AST parsing here."""

from app.contracts import AnalysisResult, FieldEvidence, SelectionScope

# Bound repeated aliases, list entries and depth independently of wire size.
MAX_MATCH_STEPS = 100_000
MAX_MATCH_DEPTH = 100
STATES = frozenset({"observed", "not_observed", "indeterminate", "unavailable"})
_PRIORITY = {"not_observed": 0, "unavailable": 1, "indeterminate": 2, "observed": 3}


class _Limit(Exception):
    pass


class _Matcher:
    def __init__(self, partial):
        self.partial = partial
        self.states = {}
        self.steps = 0

    def step(self, depth):
        self.steps += 1
        if self.steps > MAX_MATCH_STEPS or depth > MAX_MATCH_DEPTH:
            raise _Limit

    def record(self, field, state):
        if field is not None:
            previous = self.states.get(field)
            if previous is None or _PRIORITY[state] > _PRIORITY[previous]:
                self.states[field] = state

    def mark(self, scope, state, depth):
        self.step(depth)
        for field in scope.fields:
            self.step(depth)
            self.record(field.coordinate, state)
            if field.child is not None:
                self.mark(field.child, state, depth + 1)

    def children(self, scope, value, list_depth, depth):
        self.step(depth)
        if list_depth:
            if not isinstance(value, list) or not value:
                self.mark(scope, "indeterminate", depth + 1)
            else:
                for item in value:
                    self.children(scope, item, list_depth - 1, depth + 1)
        elif isinstance(value, dict):
            self.match(scope, value, depth + 1)
        else:
            # A null (including error bubbling), malformed value or a missing
            # parent cannot provide trustworthy evidence about its children.
            self.mark(scope, "indeterminate", depth + 1)

    def match(self, scope: SelectionScope, obj: dict, depth=0):
        self.step(depth)
        possible = scope.possible_types
        # Only unconditional __typename selections can discriminate. A branch's
        # alias could name a user field in another branch. Never inspect an
        # unsolicited JSON __typename, nor infer types from user field names.
        names = [obj[f.response_key] for f in scope.fields
                 if f.coordinate is None and f.applicable_types == possible and f.response_key in obj]
        invalid = any(not isinstance(name, str) or name not in possible for name in names)
        invalid |= len(set(name for name in names if isinstance(name, str))) > 1
        if invalid:
            self.mark(scope, "indeterminate", depth + 1)
            return
        runtime = names[0] if names else (next(iter(possible)) if len(possible) == 1 else None)
        for field in scope.fields:
            self.step(depth)
            if field.coordinate is None:
                continue
            if runtime is not None and runtime not in field.applicable_types:
                self.record(field.coordinate, "not_observed")
                if field.child is not None:
                    self.mark(field.child, "not_observed", depth + 1)
                continue
            if runtime is None and field.applicable_types != possible:
                self.record(field.coordinate, "indeterminate")
                if field.child is not None:
                    self.mark(field.child, "indeterminate", depth + 1)
                continue
            present = field.response_key in obj
            self.record(field.coordinate, "observed" if present else (
                "indeterminate" if self.partial else "not_observed"))
            if field.child is not None:
                self.children(field.child, obj.get(field.response_key), field.list_depth, depth + 1)


def match_field_evidence(analysis: AnalysisResult, payload: dict | None) -> tuple[FieldEvidence, ...] | None:
    """One state per unique selected coordinate, independent of coverage hits.

    None means a legacy/failed analyzer supplied no complete plan. Malformed,
    missing or oversized envelopes supply unavailable evidence. A null root
    data object supplies indeterminate evidence. Existing keys with null values
    are observed. Any uncertain occurrence prevents an absence claim, while a
    proven presence anywhere is sufficient to establish presence for a request.
    """
    if analysis.status != "success" or analysis.selection_plan is None:
        return None
    fields = set(analysis.fields)
    try:
        errors = payload.get("errors", []) if payload is not None else []
        matcher = _Matcher(partial=not isinstance(errors, list) or bool(errors))
        matcher.mark(analysis.selection_plan, "unavailable", 0)
        if set(matcher.states) != fields:
            return None  # Incomplete adapter/fragment plan: never guess.
        matcher.states.clear()
        matcher.steps = 0
        if payload is None or "data" not in payload or not isinstance(payload["data"], (dict, type(None))):
            matcher.mark(analysis.selection_plan, "unavailable", 0)
        elif payload["data"] is None:
            matcher.mark(analysis.selection_plan, "indeterminate", 0)
        else:
            matcher.match(analysis.selection_plan, payload["data"])
        return tuple(FieldEvidence(f, matcher.states[f]) for f in sorted(fields, key=lambda f: (f.type_name, f.field_name)))
    except Exception:
        # Optional matching must not break transparent proxying. In particular,
        # discard incomplete results if a complexity budget was exhausted.
        return tuple(FieldEvidence(f, "unavailable") for f in sorted(fields, key=lambda f: (f.type_name, f.field_name)))
