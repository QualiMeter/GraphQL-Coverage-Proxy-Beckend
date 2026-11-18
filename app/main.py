import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from app.analyzer_adapter import analyze_e2e_query
from app.api import router
from app.auth_api import router as auth_router
from app.auth_boundary import AuthBoundaryMiddleware, route_path
from app.config import cors_origins
from app.database import SessionFactory, engine
from app.proxy import router as proxy_router
from app.gitlab_api import router as gitlab_router
from app.test_coverage_api import router as test_coverage_router
from app.gitlab_config import gitlab_enabled, gitlab_settings
from app.gitlab_history import monitor_jobs
from app.gitlab_runs import monitor


@asynccontextmanager
async def lifespan(_: FastAPI):
    tasks = []
    if gitlab_enabled():
        settings = gitlab_settings()
        tasks = [
            asyncio.create_task(monitor(SessionFactory, settings), name="gitlab-monitor"),
            asyncio.create_task(monitor_jobs(SessionFactory, settings), name="gitlab-jobs-monitor"),
        ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        await engine.dispose()


app = FastAPI(title="GraphQL Coverage API", version="0.1.0", lifespan=lifespan)
app.state.coverage_analyzer = analyze_e2e_query
app.add_middleware(CORSMiddleware, allow_origins=cors_origins(), allow_methods=["*"],
                   allow_headers=["*"], expose_headers=["X-Total-Count"], allow_credentials=False)
app.add_middleware(AuthBoundaryMiddleware)
app.include_router(router)
app.include_router(auth_router)
app.include_router(proxy_router)
app.include_router(gitlab_router)
app.include_router(test_coverage_router)


@app.get("/health", tags=["System"])
async def health():
    return {"status": "ok"}


@app.exception_handler(RequestValidationError)
async def safe_validation_error(request: Request, exc: RequestValidationError):
    if (route_path(request.scope).startswith("/api/auth/")
            or route_path(request.scope).startswith("/api/services")
            or route_path(request.scope).startswith("/api/schemas")
            or route_path(request.scope) == "/api/runs/gitlab"
            or (route_path(request.scope).startswith("/api/runs/") and "/tests" in route_path(request.scope))):
        # Pydantic's default error input can echo a submitted password or body.
        return JSONResponse(status_code=422, content={"detail": [
            {key: error[key] for key in ("type", "loc", "msg")} for error in exc.errors()
        ]})
    return await request_validation_exception_handler(request, exc)
