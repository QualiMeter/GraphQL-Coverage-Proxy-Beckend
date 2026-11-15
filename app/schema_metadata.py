"""Read metadata from immutable saved SDL; never change canonical hashes or IDs."""

from graphql import GraphQLInterfaceType, GraphQLUnionType, Undefined, ast_from_value

from app.api_models import RootTypesOut, SchemaArgumentOut
from app.safe_document import mask_value_ast
from app.schema_cache import schema_from_sdl as schema_metadata


def root_types(schema) -> RootTypesOut:
    return RootTypesOut(**{
        kind: root.name if root else None for kind, root in (
            ("query", schema.query_type), ("mutation", schema.mutation_type),
            ("subscription", schema.subscription_type),
        )
    })


def argument_metadata(field) -> list[SchemaArgumentOut]:
    result = []
    for name, arg in sorted(field.args.items()):
        default = None
        availability = "absent"
        # Prefer the saved SDL literal: graphql-core 3.3 keeps new-style
        # defaults lazily, while 3.2 used default_value. This also preserves
        # the authored container shape without printing any original value.
        node = getattr(arg.ast_node, "default_value", None)
        if node is not None or arg.default_value is not Undefined:
            try:
                if node is None:
                    node = ast_from_value(arg.default_value, arg.type)
                safe = mask_value_ast(node) if node is not None else None
                default = safe.text if safe else None
                availability = safe.availability if safe else "unavailable"
            except Exception:
                availability = "unavailable"
        result.append(SchemaArgumentOut(name=name, type=str(arg.type), description=arg.description,
            default_value=default, default_value_availability=availability))
    return result


def abstract_metadata(schema, gql_type):
    interfaces = sorted(i.name for i in getattr(gql_type, "interfaces", ()))
    possible = sorted(t.name for t in schema.get_possible_types(gql_type)) if isinstance(
        gql_type, (GraphQLInterfaceType, GraphQLUnionType)
    ) else []
    return interfaces, possible
