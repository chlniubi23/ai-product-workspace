from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from . import db as database
from .common import (
    _redact_validation_details,
    _request_id,
)
from .config import settings
from .db import get_db, init_db
from .routers import (
    ai,
    analysis,
    auth,
    copilot,
    datasets,
    decisions,
    documents,
    feedback,
    insights,
    interview,
    jobs,
    problems,
    projects,
    workspaces,
)
from .services.job_handlers import _register_job_handlers, job_executor


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Reading the property creates DATA_ROOT and its uploads/processed/exports
    # subdirectories, so a fresh clone can accept an upload before any request.
    _ = settings.data_path
    init_db()
    # A failed configured database must remain diagnosable through the health
    # endpoint.  Do not let job recovery mask the real connection error.
    if database.database_ready():
        await job_executor.recover_pending()
    yield


app = FastAPI(title="AI Product Workspace API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth.router)
app.include_router(workspaces.router)
app.include_router(projects.router)
app.include_router(datasets.router)
app.include_router(analysis.router)
app.include_router(insights.router)
app.include_router(interview.router)
app.include_router(feedback.router)
app.include_router(problems.router)
app.include_router(decisions.router)
app.include_router(documents.router)
app.include_router(jobs.router)
app.include_router(copilot.router)
app.include_router(ai.router)

_V11_LEGACY_API_PREFIXES = (
    "/api/v1/workspaces",
    "/api/v1/tasks",
    "/api/v1/approval-requests",
    "/api/v1/decision-proposals",
    "/api/v1/jobs",
    "/api/v1/copilot/sessions",
    "/api/v1/feedback-items",
    "/api/v1/feedback-clusters",
)


@app.middleware("http")
async def mark_legacy_api_surfaces(request: Request, call_next):
    response = await call_next(request)
    if any(request.url.path.startswith(prefix) for prefix in _V11_LEGACY_API_PREFIXES):
        response.headers["Deprecation"] = "true"
        response.headers["Sunset"] = "2027-01-01"
        response.headers["Link"] = '</api/v1/feedback-notes>; rel="successor-version"'
    return response



@app.exception_handler(HTTPException)
async def http_error_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, dict) else {"code": "HTTP_ERROR", "message": str(exc.detail)}
    # Keep the canonical ``error`` envelope while exposing the legacy
    # ``detail`` alias for readiness probes and older API clients.
    return JSONResponse(status_code=exc.status_code, content={"error": detail, "detail": detail, "meta": {"request_id": _request_id()}})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": {"code": "VALIDATION_ERROR", "message": "Request validation failed", "details": _redact_validation_details(exc.errors())}, "meta": {"request_id": _request_id()}})


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "ai-product-workspace-api", "version": app.version}


@app.get("/health/ready")
def health_ready(db: Session = Depends(get_db)) -> dict[str, Any]:
    checks: dict[str, Any] = {"database": False, "data_root": False, "database_backend": database.engine.url.get_backend_name(), "database_fallback": database.USING_FALLBACK_SQLITE}
    try:
        db.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception:
        checks["database"] = False
    try:
        path = settings.data_path
        checks["data_root"] = path.is_dir() and (path / "uploads").is_dir()
    except Exception:
        checks["data_root"] = False
    # A fallback is intentionally *not* ready.  Returning 200 here previously
    # made an empty SQLite store look like the configured MySQL database.
    ready = bool(checks["database"]) and bool(checks["data_root"]) and not bool(checks["database_fallback"])
    payload = {"status": "ready" if ready else "not_ready", "checks": checks}
    if not ready:
        raise HTTPException(status_code=503, detail=payload)
    return payload


@app.get("/health/ai")
def health_ai() -> dict[str, Any]:
    return {"status": "configured" if settings.deepseek_api_key else "not_configured", "provider": "deepseek", "model": settings.deepseek_model, "base_url": settings.deepseek_base_url}


# Register after all handler dependencies have been defined. Startup recovery can
# then safely replay jobs committed by an earlier process.
_register_job_handlers()
