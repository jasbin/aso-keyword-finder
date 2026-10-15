"""FastAPI application: REST API for research runs plus the single-page web UI.

Use :func:`create_app` (tests, embedding) or ``uvicorn --factory keyfinder.api:get_app``.
Nothing is instantiated at import time.
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import annotated_types
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from keyfinder import __version__
from keyfinder.cache import ResponseCache
from keyfinder.config import Settings, get_settings
from keyfinder.export import results_to_csv, run_to_json
from keyfinder.http import HttpClient
from keyfinder.jobs import JobManager, JobManagerClosedError, QueueFullError
from keyfinder.logging_setup import configure_logging
from keyfinder.models import ResearchParams, ResearchRun, RunStatus, RunSummary, Store
from keyfinder.storage import RunRepository
from keyfinder.stores import STOREFRONTS, StoreClientFactory, create_store_client

__all__ = ["SECURITY_HEADERS", "STATIC_DIR", "create_app", "export_filename", "get_app"]

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"

SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}

_AUTH_ERROR = "invalid or missing API key"
_EXPORT_MEDIA_TYPES = {"csv": "text/csv; charset=utf-8", "json": "application/json"}
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SLUG_MAX = 40


# ---------------------------------------------------------------------- response models


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    version: str


class MetaResponse(BaseModel):
    version: str
    stores: list[str]
    countries: list[str]
    limits: dict[str, list[int]]
    auth_required: bool


class CancelResponse(BaseModel):
    cancelled: bool


# ---------------------------------------------------------------------- helpers


def _configured_key(settings: Settings) -> bytes | None:
    """The API key as bytes, or None when auth is off (an empty key counts as unset)."""
    if settings.api_key is None:
        return None
    key = settings.api_key.get_secret_value()
    return key.encode("utf-8") if key else None


def _presented_key(request: Request) -> str | None:
    header = request.headers.get("x-api-key")
    if header:
        return header
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return None


def _seed_slug(seed: str) -> str:
    slug = _SLUG_RE.sub("-", seed.lower()).strip("-")[:_SLUG_MAX].rstrip("-")
    return slug or "run"


def export_filename(run: ResearchRun, fmt: str) -> str:
    """``keywords-<seed-slug>-<id8>.<fmt>`` (ASCII only, safe in a Content-Disposition header)."""
    return f"keywords-{_seed_slug(run.params.seed)}-{run.id[:8]}.{fmt}"


def _jobs(request: Request) -> JobManager:
    return cast(JobManager, request.app.state.jobs)


def _repo(request: Request) -> RunRepository:
    return cast(RunRepository, request.app.state.repo)


JobsDep = Annotated[JobManager, Depends(_jobs)]
RepoDep = Annotated[RunRepository, Depends(_repo)]


# ---------------------------------------------------------------------- middleware


class _SecurityAndLoggingMiddleware:
    """Adds security headers to every HTTP response and logs one line per request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status_code = 500
        path: str = scope.get("path", "")

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                for name, value in SECURITY_HEADERS.items():
                    encoded = name.lower().encode("latin-1")
                    if encoded not in present:
                        headers.append((encoded, value.encode("latin-1")))
                if path.startswith("/api/") and b"cache-control" not in present:
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            method = scope.get("method", "")
            logger.info(
                "%s %s %d %.1fms",
                method,
                path,
                status_code,
                elapsed_ms,
                extra={"method": method, "path": path, "status": status_code, "ms": elapsed_ms},
            )


# ---------------------------------------------------------------------- app factory


def create_app(
    settings: Settings | None = None, *, client_factory: StoreClientFactory | None = None
) -> FastAPI:
    """Build the application. Resources are created in the lifespan, not here."""
    cfg = settings if settings is not None else get_settings()
    factory = client_factory if client_factory is not None else create_store_client
    api_key = _configured_key(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(cfg.log_level, cfg.log_format)
        cfg.ensure_dirs()
        async with AsyncExitStack() as stack:
            http = HttpClient(cfg)
            stack.push_async_callback(http.aclose)
            cache: ResponseCache | None = None
            if cfg.cache_ttl_seconds > 0:
                cache = ResponseCache(cfg.cache_path, cfg.cache_ttl_seconds)
                stack.callback(cache.close)
                await cache.purge_expired()
            repo = RunRepository(cfg.db_path)
            stack.callback(repo.close)
            await repo.init()
            await repo.mark_interrupted()
            jobs = JobManager(cfg, repo, http, cache, client_factory=factory)
            stack.push_async_callback(jobs.shutdown)
            app.state.settings = cfg
            app.state.repo = repo
            app.state.jobs = jobs
            logger.info(
                "keyfinder %s API ready (data dir %s, auth %s)",
                __version__,
                cfg.data_dir,
                "on" if api_key else "off",
            )
            yield
            logger.info("shutting down: %d active run(s)", jobs.active_count)

    async def require_api_key(request: Request) -> None:
        if api_key is None:
            return
        presented = _presented_key(request)
        if presented is None or not secrets.compare_digest(presented.encode("utf-8"), api_key):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=_AUTH_ERROR,
                headers={"WWW-Authenticate": "Bearer"},
            )

    app = FastAPI(
        title="ASO Keyword Finder",
        version=__version__,
        lifespan=lifespan,
        # The interactive docs load assets from a CDN, which the CSP forbids.
        docs_url=None,
        redoc_url=None,
    )

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
        return JSONResponse({"detail": "internal server error"}, status_code=500)

    open_router = APIRouter()
    api = APIRouter(prefix="/api", dependencies=[Depends(require_api_key)])

    @open_router.get("/healthz", response_model=HealthResponse)
    async def healthz() -> HealthResponse:
        return HealthResponse(version=__version__)

    @open_router.get("/api/meta", response_model=MetaResponse)
    async def meta() -> MetaResponse:
        limit_field = ResearchParams.model_fields["limit"]
        depth_field = ResearchParams.model_fields["depth"]
        return MetaResponse(
            version=__version__,
            stores=[store.value for store in Store],
            countries=sorted(STOREFRONTS),
            limits={"limit": _bounds(limit_field.metadata), "depth": _bounds(depth_field.metadata)},
            auth_required=api_key is not None,
        )

    @api.post("/runs", status_code=status.HTTP_202_ACCEPTED, response_model=ResearchRun)
    async def submit_run(params: ResearchParams, jobs: JobsDep, response: Response) -> ResearchRun:
        if params.store is Store.APPSTORE and params.country not in STOREFRONTS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unsupported App Store country {params.country!r}; see /api/meta for the list",
            )
        try:
            run = await jobs.submit(params)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except QueueFullError as exc:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)) from exc
        except JobManagerClosedError as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
        response.headers["Location"] = f"/api/runs/{run.id}"
        return run

    @api.get("/runs", response_model=list[RunSummary])
    async def list_runs(
        repo: RepoDep,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[RunSummary]:
        return await repo.list(limit=limit, offset=offset)

    @api.get("/runs/{run_id}", response_model=ResearchRun)
    async def get_run(run_id: str, jobs: JobsDep) -> ResearchRun:
        return await _require_run(jobs, run_id)

    @api.post("/runs/{run_id}/cancel", response_model=CancelResponse)
    async def cancel_run(run_id: str, jobs: JobsDep) -> CancelResponse:
        if await jobs.cancel(run_id):
            return CancelResponse(cancelled=True)
        await _require_run(jobs, run_id)
        return CancelResponse(cancelled=False)

    @api.delete("/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
    async def delete_run(run_id: str, jobs: JobsDep, repo: RepoDep) -> Response:
        if jobs.is_active(run_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="run is still active; cancel it first"
            )
        if not await repo.delete(run_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @api.get("/runs/{run_id}/export", response_class=Response)
    async def export_run(run_id: str, jobs: JobsDep, format: Literal["csv", "json"] = "csv") -> Response:
        run = await _require_run(jobs, run_id)
        if run.status is not RunStatus.SUCCEEDED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"run is {run.status.value}; only succeeded runs can be exported",
            )
        body = results_to_csv(run.results) if format == "csv" else run_to_json(run)
        filename = export_filename(run, format)
        return Response(
            content=body,
            media_type=_EXPORT_MEDIA_TYPES[format],
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @open_router.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.include_router(open_router)
    app.include_router(api)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    if cfg.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cfg.cors_origins,
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["Content-Type", "X-API-Key", "Authorization"],
            expose_headers=["Content-Disposition", "Location"],
        )
    app.add_middleware(_SecurityAndLoggingMiddleware)
    return app


def get_app() -> FastAPI:
    """Factory for ``uvicorn --factory keyfinder.api:get_app``: settings from the environment."""
    return create_app()


async def _require_run(jobs: JobManager, run_id: str) -> ResearchRun:
    run = await jobs.get(run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    return run


def _bounds(metadata: list[Any]) -> list[int]:
    """``[ge, le]`` from a pydantic field's constraint metadata (kept in sync with the model)."""
    low = next(cast(int, m.ge) for m in metadata if isinstance(m, annotated_types.Ge))
    high = next(cast(int, m.le) for m in metadata if isinstance(m, annotated_types.Le))
    return [low, high]
