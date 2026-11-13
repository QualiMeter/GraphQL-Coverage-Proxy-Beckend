"""Pure GraphQL schema import logic, without database access."""

import hashlib
from dataclasses import dataclass

from graphql import (
    GraphQLEnumType,
    GraphQLInputObjectType,
    GraphQLInterfaceType,
    GraphQLObjectType,
    GraphQLScalarType,
    GraphQLUnionType,
    build_schema,
    get_named_type,
    print_schema,
    validate_schema,
)
from graphql.type.schema import GraphQLSchema


@dataclass(frozen=True)
class ParsedType:
    name: str
    kind: str


@dataclass(frozen=True)
class ParsedField:
    type_name: str
    name: str
    return_type: str
    target_type_name: str | None
    is_deprecated: bool


@dataclass(frozen=True)
class ParsedSchema:
    canonical_sdl: str
    sha256: str
    types: tuple[ParsedType, ...]
    fields: tuple[ParsedField, ...]


def parse_sdl(sdl: str) -> ParsedSchema:
    schema: GraphQLSchema = build_schema(sdl)
    errors = validate_schema(schema)
    if errors:
        raise ValueError("; ".join(error.message for error in errors))

    normalized = print_schema(schema)
    sha = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    type_classes = {
        GraphQLObjectType: "OBJECT",
        GraphQLInterfaceType: "INTERFACE",
        GraphQLUnionType: "UNION",
        GraphQLInputObjectType: "INPUT_OBJECT",
        GraphQLEnumType: "ENUM",
        GraphQLScalarType: "SCALAR",
    }
    types: list[ParsedType] = []
    fields: list[ParsedField] = []

    for name, gql_type in sorted(schema.type_map.items()):
        if name.startswith("__"):
            continue
        kind = type_classes.get(type(gql_type))
        if kind is None:
            continue
        types.append(ParsedType(name=name, kind=kind))

        if isinstance(gql_type, (GraphQLObjectType, GraphQLInterfaceType)):
            for field_name, gql_field in sorted(gql_type.fields.items()):
                if field_name.startswith("__"):
                    continue
                named_type = get_named_type(gql_field.type)
                target = (
                    named_type.name
                    if isinstance(named_type, (GraphQLObjectType, GraphQLInterfaceType, GraphQLUnionType))
                    else None
                )
                fields.append(
                    ParsedField(
                        type_name=name,
                        name=field_name,
                        return_type=str(gql_field.type),
                        target_type_name=target,
                        is_deprecated=gql_field.deprecation_reason is not None,
                    )
                )

    return ParsedSchema(normalized, sha, tuple(types), tuple(fields))
