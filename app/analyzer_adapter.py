"""Bind the synchronous GraphQL AST analyzer to the async E2E capture contract."""

import asyncio
from functools import lru_cache

from graphql import GraphQLSchema, build_schema

from app.contracts import AnalysisResult, GraphQLQueryInput
from app.query_analyzer import analyze_query


@lru_cache(maxsize=32)
def _schema_from_sdl(schema_sdl: str) -> GraphQLSchema:
    # A snapshot's canonical SDL is immutable; reuse a bounded number of schemas.
    return build_schema(schema_sdl)


def _analyze(request: GraphQLQueryInput, schema_sdl: str) -> AnalysisResult:
    return analyze_query(_schema_from_sdl(schema_sdl), request)


async def analyze_e2e_query(request: GraphQLQueryInput, schema_sdl: str) -> AnalysisResult:
    """Analyze against the run's snapshot without blocking the event loop."""
    return await asyncio.to_thread(_analyze, request, schema_sdl)
