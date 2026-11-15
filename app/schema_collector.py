"""Service introspection through the shared pinned, bounded outbound transport."""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any

import anyio
import httpx
from fastapi import HTTPException
from graphql import GraphQLError, build_client_schema, get_introspection_query, print_schema, validate_schema
from graphql.type.schema import GraphQLSchema

from app.schema_parser import ParsedSchema, parse_sdl
from app.upstream import (
    EndpointResolutionError, EndpointResolutionTimeout, MAX_RESPONSE_BYTES,
    PinnedTransport, UnsafeEndpoint, outbound_slot, parse_endpoint, validate_endpoint,
)


class SchemaCollectionError(Exception):
    status_code = 502
    code = "INTROSPECTION_FAILED"
    public_detail = "GraphQL schema introspection failed; import SDL manually if necessary"


class SchemaConnectionError(SchemaCollectionError):
    code = "UPSTREAM_UNAVAILABLE"
    public_detail = "GraphQL upstream is unavailable"


class InvalidSchemaTargetURLError(SchemaConnectionError):
    status_code = 409
    code = "UPSTREAM_NOT_ALLOWED"
    public_detail = "Service endpoint is not permitted by the outbound security policy"


class SchemaDNSUnavailableError(SchemaConnectionError):
    code = "DNS_UNAVAILABLE"
    public_detail = "GraphQL upstream DNS is unavailable"


class SchemaTimeoutError(SchemaConnectionError):
    status_code = 504
    code = "UPSTREAM_TIMEOUT"
    public_detail = "GraphQL introspection timed out"


class SchemaCapacityError(SchemaCollectionError):
    status_code = 503
    code = "UPSTREAM_CAPACITY_EXHAUSTED"
    public_detail = "Upstream capacity is temporarily exhausted"


class SchemaHTTPError(SchemaConnectionError):
    code = "UPSTREAM_HTTP_ERROR"
    public_detail = "GraphQL upstream returned an unsuccessful HTTP status"


class SchemaRedirectError(SchemaHTTPError):
    code = "UPSTREAM_REDIRECT"
    public_detail = "GraphQL introspection redirects are not permitted"


class SchemaResponseTooLargeError(SchemaCollectionError):
    code = "INTROSPECTION_LIMIT_EXCEEDED"
    public_detail = "GraphQL introspection exceeds the supported size or complexity limit; import SDL manually"


class InvalidIntrospectionResponseError(SchemaCollectionError):
    code = "INVALID_INTROSPECTION"
    public_detail = "GraphQL upstream returned an invalid introspection schema"


class InvalidIntrospectionJSONError(InvalidIntrospectionResponseError):
    code = "INVALID_JSON"
    public_detail = "GraphQL upstream returned invalid JSON"


class GraphQLIntrospectionError(InvalidIntrospectionResponseError):
    code = "UPSTREAM_GRAPHQL_ERRORS"
    public_detail = "GraphQL upstream rejected the introspection query; import SDL manually if necessary"


class IntrospectionDisabledError(GraphQLIntrospectionError):
    status_code = 409
    code = "INTROSPECTION_DISABLED"
    public_detail = "Upstream introspection is disabled; import SDL manually using POST /api/schemas/import"


@dataclass(frozen=True)
class CollectedSchema:
    schema: GraphQLSchema
    parsed: ParsedSchema
    # Only schema data, never unrelated response extensions or GraphQL errors.
    introspection_json: dict[str, Any]


def _disabled(payload: Any) -> bool:
    """Conservative recognition; arbitrary GraphQL/HTTP errors are not proof.

    There is no universal disabled-introspection response. Recognize explicit
    vendor codes and customary GraphQL.js/Apollo messages without exposing them.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("errors"), list):
        return False
    for error in payload["errors"][:100]:
        if not isinstance(error, dict):
            continue
        extensions = error.get("extensions")
        if isinstance(extensions, dict) and extensions.get("code") == "INTROSPECTION_DISABLED":
            return True
        message = error.get("message")
        if isinstance(message, str):
            message = message[:2048].lower()
            if re.search(r"\bintrospection(?:\s+queries)?\s+(?:(?:is|are|has been|have been|was|were)\s+)?"
                         r"(?:disabled|disallowed|forbidden|not allowed|not permitted|not enabled)\b", message):
                return True
    return False


def _json(content: bytes):
    def reject_constant(_value):
        raise ValueError
    try:
        return json.loads(content, parse_constant=reject_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise InvalidIntrospectionJSONError("GraphQL endpoint returned invalid JSON") from None


def _build_schema(payload: Any) -> CollectedSchema:
    if not isinstance(payload, dict):
        raise InvalidIntrospectionResponseError("GraphQL response must be an object")
    if payload.get("errors"):
        if _disabled(payload):
            raise IntrospectionDisabledError(IntrospectionDisabledError.public_detail)
        raise GraphQLIntrospectionError(GraphQLIntrospectionError.public_detail)
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("__schema"), dict):
        raise InvalidIntrospectionResponseError("GraphQL response does not contain data.__schema")
    schema_data = data["__schema"]
    # Bound CPU/stack/ORM work as well as wire bytes. Walk iteratively before
    # recursive graphql-core normalization. These are limits, not fake schemas.
    stack = [(schema_data, 0)]
    nodes = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if depth > 64 or nodes > 200_000:
            raise SchemaResponseTooLargeError(SchemaResponseTooLargeError.public_detail)
        if isinstance(value, (dict, list)) and nodes + len(value) > 200_000:
            raise SchemaResponseTooLargeError(SchemaResponseTooLargeError.public_detail)
        if isinstance(value, dict):
            stack.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            stack.extend((item, depth + 1) for item in value)
    try:
        schema = build_client_schema({"__schema": schema_data})
        if validate_schema(schema):
            raise InvalidIntrospectionResponseError("Introspected schema is invalid")
        parsed = parse_sdl(print_schema(schema))
        if (len(parsed.types) > 5000 or len(parsed.fields) > 50_000
                or any(len(item.name) > 255 for item in parsed.types)
                or any(len(item.name) > 255 for item in parsed.fields)):
            raise SchemaResponseTooLargeError(SchemaResponseTooLargeError.public_detail)
    except (GraphQLError, KeyError, TypeError, ValueError, AttributeError, IndexError, RecursionError):
        raise InvalidIntrospectionResponseError("Could not build a GraphQL schema from introspection") from None
    return CollectedSchema(schema, parsed, {"data": {"__schema": schema_data}})


async def collect_schema(target_url: str, *, timeout: float = 15.0,
                         transport: httpx.AsyncBaseTransport | None = None) -> CollectedSchema:
    """The caller selects a saved service endpoint, not a URL from API input.

    Optional transport is an internal test seam; production always validates DNS
    and creates PinnedTransport. No incoming headers/cookies/credentials are used.
    """
    try:
        url = parse_endpoint(target_url)
    except UnsafeEndpoint:
        raise InvalidSchemaTargetURLError("Configured GraphQL endpoint URL is invalid") from None
    try:
        # Shared with proxy: at most 16 fetch/normalization jobs per worker.
        async with outbound_slot():
            async with asyncio.timeout(timeout):
                if transport is None:
                    try:
                        destination = await validate_endpoint(target_url)
                    except EndpointResolutionTimeout:
                        raise SchemaTimeoutError(SchemaTimeoutError.public_detail) from None
                    except EndpointResolutionError:
                        raise SchemaDNSUnavailableError(SchemaDNSUnavailableError.public_detail) from None
                    except UnsafeEndpoint:
                        raise InvalidSchemaTargetURLError(InvalidSchemaTargetURLError.public_detail) from None
                    transport = PinnedTransport(destination)
                async with httpx.AsyncClient(timeout=timeout, transport=transport, trust_env=False,
                                             follow_redirects=False) as client:
                    async with client.stream("POST", url, json={"query": get_introspection_query()},
                                             headers={"Accept-Encoding": "identity"}) as response:
                        if 300 <= response.status_code < 400:
                            raise SchemaRedirectError(SchemaRedirectError.public_detail)
                        if response.headers.get("content-encoding", "identity").lower() != "identity":
                            raise InvalidIntrospectionResponseError("Encoded introspection responses are not supported")
                        content = bytearray()
                        chunks = response.aiter_bytes() if response.is_stream_consumed else response.aiter_raw()
                        async for chunk in chunks:
                            if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                                raise SchemaResponseTooLargeError(SchemaResponseTooLargeError.public_detail)
                            content.extend(chunk)
                        status = response.status_code
            # Parsing is kept off the event loop; the shared capacity slot stays
            # held until it finishes. Structural limits bound recursive work.
            if status >= 400:
                if status in {400, 403}:
                    try:
                        if _disabled(await anyio.to_thread.run_sync(_json, bytes(content))):
                            raise IntrospectionDisabledError(IntrospectionDisabledError.public_detail)
                    except InvalidIntrospectionJSONError:
                        pass
                raise SchemaHTTPError(f"GraphQL endpoint returned HTTP {status}")
            payload = await anyio.to_thread.run_sync(_json, bytes(content))
            return await anyio.to_thread.run_sync(_build_schema, payload)
    except HTTPException:
        raise SchemaCapacityError(SchemaCapacityError.public_detail) from None
    except (TimeoutError, httpx.TimeoutException):
        raise SchemaTimeoutError(SchemaTimeoutError.public_detail) from None
    except httpx.HTTPError:
        raise SchemaConnectionError(SchemaConnectionError.public_detail) from None
