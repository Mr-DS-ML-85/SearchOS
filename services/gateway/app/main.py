from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import admin, extract, health, search
from app.config import get_settings
from app.db import close_engine, get_session_factory, init_engine
from app.errors import AppError, to_http_error
from app.logging import get_logger, new_request_id, set_request_id, setup_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level)
    init_engine()
    logger = get_logger("app")
    async with get_session_factory()() as session:
        from app.security import get_security_service

        security = get_security_service()
        admin_key = await security.bootstrap_admin(session)
        if admin_key:
            logger.info("bootstrap admin key active")
    logger.info("%s v%s started", settings.app_name, settings.app_version)
    yield
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
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request.state.request_id = new_request_id()
        set_request_id(request.state.request_id)
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        return response

    @app.middleware("http")
    async def metrics_middleware(request: Request, call_next):
        from app.metrics import get_metrics

        response = await call_next(request)
        route = request.scope.get("route")
        endpoint = getattr(route, "path", None) or "unmatched"
        get_metrics().requests_total.labels(endpoint=endpoint, status=str(response.status_code)).inc()
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        return JSONResponse(status_code=exc.http_status, content=to_http_error(exc).detail)

    app.include_router(health.router, prefix="")
    app.include_router(search.router, prefix="")
    app.include_router(extract.router, prefix="")
    app.include_router(admin.router, prefix="")

    @app.get("/")
    async def root() -> dict:
        return {"service": settings.app_name, "version": settings.app_version}

    return app


app = create_app()
