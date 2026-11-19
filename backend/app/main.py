from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routers import characters, evidence, exports, graph, health, jobs, novels, reviews
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging
from app.infrastructure.db import models  # noqa: F401
from app.infrastructure.db.session import Base, engine


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    # Auto-create tables in sqlite dev/test mode; PostgreSQL uses Alembic migrations
    if settings.database_url.startswith("sqlite"):
        Base.metadata.create_all(bind=engine)
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Novel Character Knowledge Engine (NCKE) API",
        version="1.0.0",
        description="Evidence-backed novel character knowledge base, graph, and deep analysis API.",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(AppError)
    async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.code, "message": exc.message, "details": exc.details},
        )

    prefix = "/api/v1"
    app.include_router(health.router, prefix=prefix)
    app.include_router(novels.router, prefix=prefix)
    app.include_router(jobs.router, prefix=prefix)
    app.include_router(characters.router, prefix=prefix)
    app.include_router(graph.router, prefix=prefix)
    app.include_router(evidence.router, prefix=prefix)
    app.include_router(reviews.router, prefix=prefix)
    app.include_router(exports.router, prefix=prefix)

    return app


app = create_app()
