from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db import models
from app.db.database import session_scope
from app.services.queue import enqueue

logger = get_logger("demo")

_DEMO_TRACKS: list[dict[str, Any]] = [
    {
        "title": "Cosmic Drift",
        "artist_name": "Aurora Pulse",
        "album_name": "Stellar Lines",
        "genre": "Ambient",
        "duration_sec": 312,
        "year": 2023,
        "track_no": 1,
        "play_count": 42,
        "rating": 5,
    },
    {
        "title": "Midnight Drive",
        "artist_name": "Neon Echo",
        "album_name": "Night Roads",
        "genre": "Synthwave",
        "duration_sec": 246,
        "year": 2022,
        "track_no": 3,
        "play_count": 18,
        "rating": 4,
    },
    {
        "title": "Forest Whisper",
        "artist_name": "Tundra",
        "album_name": "Earth Tones",
        "genre": "Folk",
        "duration_sec": 198,
        "year": 2020,
        "track_no": 2,
        "play_count": 7,
    },
    {
        "title": "Iron Pulse",
        "artist_name": "Heavy Static",
        "album_name": "Forge",
        "genre": "Metal",
        "duration_sec": 274,
        "year": 2024,
        "track_no": 5,
        "play_count": 33,
        "rating": 5,
    },
    {
        "title": "Glass City",
        "artist_name": "Aurora Pulse",
        "album_name": "Stellar Lines",
        "genre": "Ambient",
        "duration_sec": 401,
        "year": 2023,
        "track_no": 4,
        "play_count": 12,
    },
    {
        "title": "Sunny Detour",
        "artist_name": "Coastline",
        "album_name": "Daylight",
        "genre": "Indie",
        "duration_sec": 211,
        "year": 2021,
        "track_no": 1,
        "play_count": 9,
    },
]


def seed_demo_library(server_id: str) -> dict[str, int]:
    """Insert a handful of demo tracks if DB is empty. Returns counts."""
    with session_scope() as db:
        if db.query(models.Track).count() > 0:
            return {"tracks": 0, "seeded": False}

        for t in _DEMO_TRACKS:
            db.add(
                models.Track(
                    id=str(uuid.uuid4()),
                    server_id=server_id,
                    external_id=f"demo-{uuid.uuid4().hex[:8]}",
                    **t,
                    starred=t.get("rating", 0) >= 5,
                    last_played_at=datetime.utcnow(),
                )
            )
        db.flush()
        n = db.query(models.Track).count()
        logger.info("seeded {} demo tracks", n)
        return {"tracks": n, "seeded": True}


def ensure_demo_server(db: Session) -> models.MediaServer:
    s = db.query(models.MediaServer).filter_by(type="demo").first()
    if s is None:
        s = models.MediaServer(
            id=str(uuid.uuid4()),
            type="demo",
            name="Local Demo",
            url="http://localhost",
            enabled=True,
        )
        db.add(s)
        db.flush()
    return s
