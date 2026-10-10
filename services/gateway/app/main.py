from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import admin, extract, health, search
from app.api.deps import current_api_key
from app.config import get_settings
from app.db import close_engine, get_session_factory, init_engine
from app.errors import AppError, error_body, error_headers
from app.logging import get_logger, new_request_id, set_request_id, setup_logging

_CODE_BY_STATUS = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
    502: "provider_error",
    503: "service_unavailable",
}


async def _sample_queue_depth() -> None:
    """Keep searchos_queue_depth fresh on a timer.

    The gauge used to be set only inside the /stats handler, which nothing
    scrapes — so the QueueBacklog alert could never fire. A dead crawler was
    completely invisible.
    """
    from sqlalchemy import func, select

    from app.metrics import get_metrics
    from app.models import IngestJob, IngestJobStatus

    while True:
        try:
            async with get_session_factory()() as session:
                pending = (
                    await session.scalar(
                        select(func.count()).select_from(IngestJob).where(IngestJob.status == IngestJobStatus.pending)
                    )
                    or 0
                )
                running = (
                    await session.scalar(
                        select(func.count()).select_from(IngestJob).where(IngestJob.status == IngestJobStatus.running)
                    )
                    or 0
                )
                failed = (
                    await session.scalar(
                        select(func.count()).select_from(IngestJob).where(IngestJob.status == IngestJobStatus.failed)
                    )
                    or 0
                )
            metrics = get_metrics()
            metrics.queue_depth.set(pending)
            metrics.ingest_jobs_running.set(running)
            metrics.ingest_jobs_failed.set(failed)
        except Exception:
            pass
        await asyncio.sleep(15)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level)
    init_engine()
    logger = get_logger("app")

    # Load the embedding and rerank models before serving. Previously the first
    # query paid for both, blocking for ~10s and timing the client out — which
    # surfaced as a dropped connection rather than a slow response.
    def _preload_embeddings() -> None:
        from app.embed.provider import get_embedding_provider

        get_embedding_provider().warm()

    async def _warm_models() -> None:
        """Load embedding and rerank models off the request path.

        Both are loaded lazily otherwise, so the first search blocked on a model
        download and timed clients out — which read as a dropped connection
        rather than a slow response.
        """
        if settings.embedding_model:
            try:
                await asyncio.to_thread(_preload_embeddings)
                logger.info("embedding model warm: %s", settings.embedding_model)
            except Exception as exc:
                logger.warning("embedding model warm failed: %s: %s", exc.__class__.__name__, exc)
        if settings.rerank_enabled and settings.rerank_model:
            try:
                from app.search.rerank import CrossEncoderReranker

                ce = CrossEncoderReranker(settings.rerank_model, batch_size=settings.rerank_batch_size)
                if await asyncio.to_thread(ce.warm):
                    logger.info("rerank model warm: %s", settings.rerank_model)
                else:
                    logger.warning(
                        "rerank model %s unavailable; falling back to heuristic scores, "
                        "which cannot support a relevance threshold",
                        settings.rerank_model,
                    )
            except Exception as exc:
                logger.warning("rerank model warm failed: %s: %s", exc.__class__.__name__, exc)

    warm = asyncio.create_task(_warm_models())

    sampler = asyncio.create_task(_sample_queue_depth())
    try:
        for attempt in range(5):
            try:
                async with get_session_factory()() as session:
                    from app.security import get_security_service

                    security = get_security_service()
                    admin_key = await security.bootstrap_admin(session)
                    if admin_key:
                        logger.info("bootstrap admin key active")
                break
            except Exception as exc:
                # A slow-to-start Postgres must not crash-loop the gateway.
                if attempt == 4:
                    raise
                delay = 2 * (2**attempt)
                logger.warning(
                    "startup DB attempt %d/5 failed (%s); retrying in %ds",
                    attempt + 1,
                    exc.__class__.__name__,
                    delay,
                )
                await asyncio.sleep(delay)
    except Exception:
        sampler.cancel()
        await close_engine()
        raise

    from app.cache import close_cache
    from app.http import close_http_client

    logger.info("%s v%s started", settings.app_name, settings.app_version)
    try:
        yield
    finally:
        warm.cancel()
        sampler.cancel()
        await close_http_client()
        await close_cache()
        await close_engine()
        logger.info("shutdown complete")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=f"{settings.app_name} Search API",
        version=settings.app_version,
        docs_url="/docs" if settings.app_debug else None,
        redoc_url=None,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        # Starlette reflects the request Origin when this is on, so a wildcard
        # origin list plus credentials lets any website drive the API.
        allow_credentials=settings.cors_allows_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def body_size_limit_middleware(request: Request, call_next):
        """Reject oversized bodies before they are buffered.

        FastAPI calls ``await request.body()`` before resolving dependencies, so
        an unauthenticated client could previously make the worker buffer an
        arbitrarily large payload in RAM before its API key was ever checked.
        """
        settings = get_settings()
        limit = settings.max_request_body_bytes
        if limit > 0 and request.method in ("POST", "PUT", "PATCH"):
            declared = request.headers.get("content-length")
            if declared is not None:
                try:
                    if int(declared) > limit:
                        get_logger("app").warning(
                            "refused oversized body: %s bytes (limit %s)",
                            declared,
                            limit,
                        )
                        from app.metrics import get_metrics

                        get_metrics().blocked_requests.labels(reason="body_size").inc()
                        return JSONResponse(
                            status_code=413,
                            content={
                                "code": "payload_too_large",
                                "message": f"Request body exceeds {limit} bytes.",
                                "details": None,
                            },
                        )
                except ValueError:
                    pass
        return await call_next(request)

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request.state.request_id = new_request_id()
        set_request_id(request.state.request_id)
        try:
            response = await call_next(request)
        except Exception:
            # ServerErrorMiddleware re-raises above us; still attach the id to
            # whatever response we can, so errors remain correlatable.
            raise
        response.headers["X-Request-Id"] = request.state.request_id
        return response

    @app.middleware("http")
    async def metrics_middleware(request: Request, call_next):
        from app.metrics import get_metrics

        endpoint = "unmatched"
        response = None
        try:
            response = await call_next(request)
            return response
        finally:
            # try/finally so 500s from unhandled exceptions are still counted —
            # without it the HighErrorRate alert could never fire.
            route = request.scope.get("route")
            endpoint = getattr(route, "path", None) or endpoint
            status_code = response.status_code if response is not None else 500
            try:
                get_metrics().requests_total.labels(endpoint=endpoint, status=str(status_code)).inc()
            except Exception:
                pass

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        # Headers matter: Retry-After on 429, WWW-Authenticate on 401. Building a
        # bare JSONResponse from .detail silently dropped both.
        return JSONResponse(
            status_code=exc.http_status,
            content=error_body(exc),
            headers=error_headers(exc),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        """Normalise FastAPI's HTTPException onto the same envelope.

        Handlers that did ``raise to_http_error(e)`` used to emit
        ``{"detail": {...}}`` while the AppError handler emitted the flat shape.
        """
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail:
            body = detail
        else:
            body = {"code": _CODE_BY_STATUS.get(exc.status_code, "http_error"), "message": str(detail), "details": None}
        return JSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "code": "validation_error",
                "message": "Request validation failed.",
                "details": exc.errors(),
            },
        )

    @app.exception_handler(PydanticValidationError)
    async def pydantic_validation_handler(request: Request, exc: PydanticValidationError):
        """Models bound with ``Depends()`` raise a bare pydantic ValidationError.

        FastAPI only converts *request-body/query* failures into
        RequestValidationError. A model validator on a Depends()-bound model
        escapes as a raw pydantic error, which the catch-all handler turned into
        a 500 — so a blank query or a self-contradicting filter surfaced as an
        internal server error instead of a client error.
        """
        return JSONResponse(
            status_code=422,
            content={
                "code": "validation_error",
                "message": "Request validation failed.",
                "details": [
                    {"loc": ["query", *err.get("loc", ())], "msg": err.get("msg"), "type": err.get("type")}
                    for err in exc.errors()
                ],
            },
        )

    @app.exception_handler(SQLAlchemyError)
    async def db_exception_handler(request: Request, exc: SQLAlchemyError):
        logger = get_logger("app")
        logger.error("database error: %s: %s", exc.__class__.__name__, exc, exc_info=True)
        return JSONResponse(
            status_code=503,
            content={
                "code": "database_unavailable",
                "message": "A database error occurred.",
                "details": None,
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logger = get_logger("app")
        request_id = getattr(request.state, "request_id", None)
        logger.error(
            "unhandled error request_id=%s: %s: %s",
            request_id,
            exc.__class__.__name__,
            exc,
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={
                "code": "internal_error",
                "message": "Internal server error.",
                "details": {"request_id": request_id},
            },
            headers={"X-Request-Id": request_id} if request_id else None,
        )

    # Health/ops routes are unauthenticated by design (probes, Prometheus), but
    # /stats and /metrics are not cheap and must not be open to the world.
    app.include_router(health.router, prefix="")

    api_deps = [] if settings.auth_enabled else [Depends(current_api_key)]
    app.include_router(search.router, prefix="", dependencies=api_deps)
    app.include_router(extract.router, prefix="", dependencies=api_deps)
    app.include_router(admin.router, prefix="", dependencies=api_deps)

    @app.get("/")
    async def root() -> dict:
        return {"service": settings.app_name, "version": settings.app_version}

    return app


app = create_app()
