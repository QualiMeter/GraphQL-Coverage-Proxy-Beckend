import asyncio
from app.contracts import AnalysisResult, GraphQLQueryInput
from app.query_analyzer import analyze_query
from app.schema_cache import schema_from_sdl as _schema_from_sdl


def _analyze(request: GraphQLQueryInput, schema_sdl: str) -> AnalysisResult:
    return analyze_query(_schema_from_sdl(schema_sdl), request)


async def analyze_e2e_query(request: GraphQLQueryInput, schema_sdl: str) -> AnalysisResult:
    return await asyncio.to_thread(_analyze, request, schema_sdl)
