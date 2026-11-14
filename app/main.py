from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.analyzer_adapter import analyze_e2e_query
from app.api import router
from app.config import cors_origins
from app.database import engine
from app.proxy import router as proxy_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await engine.dispose()


app = FastAPI(title="GraphQL Coverage API", version="0.1.0", lifespan=lifespan)
# Only the actual AST analyzer is installed; no fabricated production results.
app.state.coverage_analyzer = analyze_e2e_query
app.add_middleware(CORSMiddleware, allow_origins=cors_origins(), allow_methods=["*"], allow_headers=["*"])
app.include_router(router)
app.include_router(proxy_router)


@app.get("/health", tags=["System"])
async def health():
    return {"status": "ok"}
