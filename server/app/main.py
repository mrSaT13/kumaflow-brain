from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import (
    tracks,
    scan,
    library,
    playlists,
    users,
    analysis,
    clusters,
    lyrics,
    yandex,
    collab,
    cron,
    settings as settings_api,
    status,
    covers,
    bridge,
)
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db import init_db
from app.services.queue import init_redis, shutdown_redis


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    init_db()
    init_redis()
    try:
        yield
    finally:
        shutdown_redis()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(status.router, prefix="/api", tags=["status"])
    app.include_router(settings_api.router, prefix="/api/settings", tags=["settings"])
    app.include_router(library.router, prefix="/api/library", tags=["library"])
    app.include_router(tracks.router, prefix="/api/tracks", tags=["tracks"])
    app.include_router(scan.router, prefix="/api/scan", tags=["scan"])
    app.include_router(analysis.router, prefix="/api/analysis", tags=["analysis"])
    app.include_router(clusters.router, prefix="/api/clusters", tags=["clusters"])
    app.include_router(playlists.router, prefix="/api/playlists", tags=["playlists"])
    app.include_router(users.router, prefix="/api/users", tags=["users"])
    app.include_router(collab.router, prefix="/api/collab", tags=["collab"])
    app.include_router(lyrics.router, prefix="/api/lyrics", tags=["lyrics"])
    app.include_router(yandex.router, prefix="/api/yandex", tags=["yandex"])
    app.include_router(cron.router, prefix="/api/cron", tags=["cron"])
    app.include_router(covers.router, prefix="/api/covers", tags=["covers"])
    app.include_router(bridge.router, prefix="/api/bridge", tags=["bridge"])

    return app


app = create_app()
