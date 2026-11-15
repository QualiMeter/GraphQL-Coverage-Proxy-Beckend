"""One bounded cache of immutable saved schemas shared by analysis and metadata."""

from functools import lru_cache

from graphql import GraphQLSchema, build_schema


@lru_cache(maxsize=32)
def schema_from_sdl(schema_sdl: str) -> GraphQLSchema:
    return build_schema(schema_sdl)
