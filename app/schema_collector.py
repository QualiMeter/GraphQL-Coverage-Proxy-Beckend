"""Fetch and validate a GraphQL schema through the standard introspection API."""

from dataclasses import dataclass
from typing import Any

from graphql import (
    GraphQLError,
    build_client_schema,
    get_introspection_query,
    print_schema,
    validate_schema,
)
from graphql.type.schema import GraphQLSchema

from app.schema_parser import ParsedSchema, parse_sdl


class SchemaCollectionError(Exception):
    """Base error for failures while fetching or interpreting introspection."""


class SchemaConnectionError(SchemaCollectionError):
    """The configured GraphQL endpoint could not return an HTTP response."""


class InvalidIntrospectionResponseError(SchemaCollectionError):
    """The endpoint responded, but did not return a usable GraphQL schema."""


@dataclass(frozen=True)
class CollectedSchema:
    """Validated schema objects and the exact JSON response returned upstream."""

    schema: GraphQLSchema
    parsed: ParsedSchema
    introspection_json: dict[str, Any]


async def collect_schema(target_url: str, *, timeout: float = 15.0) -> CollectedSchema:
    """Fetch introspection from a trusted, application-configured endpoint.

    Callers must not pass a URL taken from an untrusted request body. Keeping
    HTTPX imported lazily lets the base API boot while the runtime dependency
    change is coordinated with the proxy owner.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                target_url,
                json={"query": get_introspection_query()},
            )
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise SchemaConnectionError(
            f"GraphQL endpoint returned HTTP {exc.response.status_code}"
        ) from exc
    except httpx.InvalidURL as exc:
        raise SchemaConnectionError("Configured GraphQL endpoint URL is invalid") from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise SchemaConnectionError("Could not connect to the configured GraphQL endpoint") from exc

    try:
        payload = response.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidIntrospectionResponseError(
            "GraphQL endpoint returned invalid JSON"
        ) from exc

    if not isinstance(payload, dict):
        raise InvalidIntrospectionResponseError(
            "GraphQL endpoint response must be a JSON object"
        )

    graphql_errors = payload.get("errors")
    if graphql_errors:
        messages = [
            item.get("message", "Unknown GraphQL error")
            for item in graphql_errors
            if isinstance(item, dict)
        ]
        detail = "; ".join(messages) if messages else "Unknown GraphQL error"
        raise InvalidIntrospectionResponseError(
            f"GraphQL introspection returned errors: {detail}"
        )

    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("__schema"), dict):
        raise InvalidIntrospectionResponseError(
            "GraphQL response does not contain data.__schema"
        )

    try:
        schema = build_client_schema(data)
        schema_errors = validate_schema(schema)
        if schema_errors:
            details = "; ".join(error.message for error in schema_errors)
            raise InvalidIntrospectionResponseError(
                f"Introspected schema is invalid: {details}"
            )
        parsed = parse_sdl(print_schema(schema))
    except InvalidIntrospectionResponseError:
        raise
    except (GraphQLError, KeyError, TypeError, ValueError) as exc:
        raise InvalidIntrospectionResponseError(
            f"Could not build a GraphQL schema from introspection: {exc}"
        ) from exc

    return CollectedSchema(
        schema=schema,
        parsed=parsed,
        introspection_json=payload,
    )
