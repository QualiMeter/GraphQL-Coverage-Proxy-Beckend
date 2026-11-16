"""Bounded upstream error metadata. Raw error text never crosses this module."""

import json
from dataclasses import dataclass
from typing import Literal

from graphql import GraphQLError, GraphQLList, GraphQLNonNull, get_operation_ast, parse
from graphql.language.ast import FieldNode

from app.schema_cache import schema_from_sdl

ErrorDetailsAvailability = Literal["sanitized", "truncated", "unavailable"]
MAX_ERROR_DETAILS = 20
MAX_PATH_SEGMENTS = 16
MAX_NAME_LENGTH = 64
MAX_PATH_INDEX = 1_000_000
MAX_LOCATIONS = 8
MAX_COORDINATE = 1_000_000
MAX_DETAILS_BYTES = 16_384
MAX_QUERY_BYTES = 262_144
MAX_RESPONSE_BYTES = 1_048_576

# Values are fixed server text, never derived from the upstream message.
SAFE_MESSAGES = {
    "GRAPHQL_PARSE_FAILED": "The GraphQL document could not be parsed.",
    "GRAPHQL_VALIDATION_FAILED": "The GraphQL document failed validation.",
    "BAD_USER_INPUT": "The GraphQL input was rejected.",
    "UNAUTHENTICATED": "Authentication is required by the GraphQL service.",
    "FORBIDDEN": "The GraphQL service denied access.",
    "PERSISTED_QUERY_NOT_FOUND": "The persisted GraphQL query was not found.",
    "PERSISTED_QUERY_NOT_SUPPORTED": "Persisted GraphQL queries are not supported.",
    "INTERNAL_SERVER_ERROR": "The GraphQL service reported an internal error.",
}


@dataclass(frozen=True)
class ErrorSummary:
    count: int | None = None
    availability: ErrorDetailsAvailability = "unavailable"
    details: list[dict] | None = None


def response_envelope(body: bytes | None) -> tuple[dict | None, bool | None, bool | None]:
    """Preserve legacy flags, including truthiness of malformed errors."""
    if body is None or len(body) > MAX_RESPONSE_BYTES:
        return None, None, None
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        return None, None, None
    if not isinstance(payload, dict):
        return None, None, None
    return payload, isinstance(payload.get("data"), dict), bool(payload.get("errors"))


def error_count(payload: dict | None) -> int | None:
    if payload is None:
        return None
    errors = payload.get("errors", [])
    # Malformed arrays do not assert a known object count (or known absence).
    if not isinstance(errors, list) or any(not isinstance(e, dict) for e in errors):
        return None
    return len(errors)


class _PathVerifier:
    def __init__(self, query: str | None, operation_name: str | None, sdl: str):
        self.query, self.operation_name, self.sdl = query, operation_name, sdl
        self.context = None
        self.attempted = False

    def path(self, value: object) -> list[str | int] | None:
        if not isinstance(value, list) or not 1 <= len(value) <= MAX_PATH_SEGMENTS:
            return None
        for segment in value:
            if type(segment) is int:
                if not 0 <= segment <= MAX_PATH_INDEX:
                    return None
            elif type(segment) is not str or not 1 <= len(segment) <= MAX_NAME_LENGTH:
                return None
        if not self.attempted:
            self.attempted = True
            try:
                if self.query is None or len(self.query.encode("utf-8")) > MAX_QUERY_BYTES:
                    return None
                document = parse(self.query, max_tokens=10_000)
                operation = get_operation_ast(document, self.operation_name)
                if operation is None:
                    return None
                schema = schema_from_sdl(self.sdl)
                root = getattr(schema, operation.operation.value + "_type")
                if root is not None:
                    self.context = (root, operation.selection_set)
            except (GraphQLError, ValueError, UnicodeError, RecursionError):
                return None
        if self.context is None:
            return None
        current_type, selection = self.context
        result = []
        for segment in value:
            while isinstance(current_type, GraphQLNonNull):
                current_type = current_type.of_type
            if isinstance(current_type, GraphQLList):
                if type(segment) is not int:
                    return None
                current_type = current_type.of_type
            else:
                if type(segment) is not str or selection is None:
                    return None
                # Fragments/abstract branches and ambiguous duplicate selections
                # are hidden conservatively. Never infer a field from a global
                # name set, or accept an alias even if it matches a schema name.
                if any(not isinstance(node, FieldNode) for node in selection.selections):
                    return None
                matches = [node for node in selection.selections
                           if (node.alias or node.name).value == segment]
                if len(matches) != 1 or matches[0].alias is not None:
                    return None
                field = getattr(current_type, "fields", {}).get(segment)
                if field is None:
                    return None
                current_type, selection = field.type, matches[0].selection_set
            result.append(segment)
        return result


def sanitize_errors(payload: dict | None, query: str | None,
                    operation_name: str | None, sdl: str) -> ErrorSummary:
    """Extract only bounded, verified metadata from a parsed upstream envelope."""
    count = error_count(payload)
    if count is None:
        return ErrorSummary()
    if count == 0:
        return ErrorSummary(0, "sanitized", [])
    verifier = _PathVerifier(query, operation_name, sdl)
    details = []
    truncated = count > MAX_ERROR_DETAILS
    for error in payload["errors"][:MAX_ERROR_DETAILS]:
        extensions = error.get("extensions")
        code = extensions.get("code") if isinstance(extensions, dict) else None
        code = code if isinstance(code, str) and code in SAFE_MESSAGES else None
        raw_locations = error.get("locations")
        locations = []
        if isinstance(raw_locations, list):
            truncated |= len(raw_locations) > MAX_LOCATIONS
            for location in raw_locations[:MAX_LOCATIONS]:
                if not isinstance(location, dict):
                    continue
                line, column = location.get("line"), location.get("column")
                if (type(line) is int and type(column) is int
                        and 1 <= line <= MAX_COORDINATE and 1 <= column <= MAX_COORDINATE):
                    locations.append({"line": line, "column": column})
        path = error.get("path")
        if isinstance(path, list) and len(path) > MAX_PATH_SEGMENTS:
            truncated = True
        detail = {"code": code, "path": verifier.path(path), "locations": locations,
                  "safeMessage": SAFE_MESSAGES.get(code)}
        if len(json.dumps(details + [detail], ensure_ascii=True).encode("utf-8")) > MAX_DETAILS_BYTES:
            truncated = True
            break
        details.append(detail)
    return ErrorSummary(count, "truncated" if truncated else "sanitized", details)
