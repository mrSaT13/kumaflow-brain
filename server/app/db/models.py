from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Optional, Callable

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base
from app.db.dialect_compat import UUIDCol, JSONCol, BlobCol


def _uuid() -> str:
    return str(uuid.uuid4())


class MediaServer(Base):
    __tablename__ = "media_servers"

    id: Mapped[str] = mapped_column(UUIDCol(), primary_key=True, default=_uuid)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MediaUser(Base):
    __tablename__ = "media_users"

    id: Mapped[str] = mapped_column(UUIDCol(), primary_key=True, default=_uuid)
    server_id: Mapped[str] = mapped_column(ForeignKey("media_servers.id", ondelete="CASCADE"))
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    username: Mapped[str] = mapped_column(String(128), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (UniqueConstraint("server_id", "external_id", name="uq_media_user"),)


class Artist(Base):
    __tablename__ = "artists"

    id: Mapped[str] = mapped_column(UUIDCol(), primary_key=True, default=_uuid)
    server_id: Mapped[str] = mapped_column(ForeignKey("media_servers.id", ondelete="CASCADE"))
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    starred_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (UniqueConstraint("server_id", "external_id", name="uq_artist"),)


class Album(Base):
    __tablename__ = "albums"

    id: Mapped[str] = mapped_column(UUIDCol(), primary_key=True, default=_uuid)
    server_id: Mapped[str] = mapped_column(ForeignKey("media_servers.id", ondelete="CASCADE"))
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    artist_external_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    artist_name: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    year: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    genre: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    cover_art_id: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)

    __table_args__ = (UniqueConstraint("server_id", "external_id", name="uq_album"),)


class Track(Base):
    __tablename__ = "tracks"

    id: Mapped[str] = mapped_column(UUIDCol(), primary_key=True, default=_uuid)
    server_id: Mapped[str] = mapped_column(ForeignKey("media_servers.id", ondelete="CASCADE"))
    external_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(1024), nullable=False)
    artist_external_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    artist_name: Mapped[Optional[str]] = mapped_column(String(512), nullable=True, index=True)
    album_external_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    album_name: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    genre: Mapped[Optional[str]] = mapped_column(String(256), nullable=True, index=True)
    duration_sec: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    year: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    track_no: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    disc_no: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    bitrate: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    suffix: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    content_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    path: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)
    starred: Mapped[bool] = mapped_column(Boolean, default=False)
    play_count: Mapped[int] = mapped_column(Integer, default=0)
    last_played_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    rating: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cover_art_id: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("server_id", "external_id", name="uq_track"),
        Index("ix_track_artist_album", "artist_external_id", "album_external_id"),
    )


class TrackFeatures(Base):
    __tablename__ = "track_features"

    track_id: Mapped[str] = mapped_column(
        ForeignKey("tracks.id", ondelete="CASCADE"), primary_key=True
    )
    tempo_bpm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    key_name: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    scale: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    energy: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    danceability: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    valence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    arousal: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    loudness_db: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    spectral_centroid: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    spectral_rolloff: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    zero_crossing_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mfcc_summary: Mapped[Optional[dict]] = mapped_column(JSONCol(), nullable=True)
    chroma_summary: Mapped[Optional[dict]] = mapped_column(JSONCol(), nullable=True)
    mood_vector: Mapped[Optional[dict]] = mapped_column(JSONCol(), nullable=True)
    mood_labels: Mapped[Optional[list]] = mapped_column(JSONCol(), nullable=True)
    analyzed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TrackEmbedding(Base):
    __tablename__ = "track_embeddings"

    track_id: Mapped[str] = mapped_column(
        ForeignKey("tracks.id", ondelete="CASCADE"), primary_key=True
    )
    model: Mapped[str] = mapped_column(String(64), primary_key=True)
    dim: Mapped[int] = mapped_column(Integer, nullable=False)
    vector: Mapped[bytes] = mapped_column(BlobCol(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TrackCluster(Base):
    __tablename__ = "track_clusters"

    track_id: Mapped[str] = mapped_column(
        ForeignKey("tracks.id", ondelete="CASCADE"), primary_key=True
    )
    algorithm: Mapped[str] = mapped_column(String(32), primary_key=True)
    cluster_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    distance_to_center: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Lyrics(Base):
    __tablename__ = "lyrics"

    track_id: Mapped[str] = mapped_column(
        ForeignKey("tracks.id", ondelete="CASCADE"), primary_key=True
    )
    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    language: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    synced: Mapped[Optional[dict]] = mapped_column(JSONCol(), nullable=True)
    source_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TrackMetadataEnrich(Base):
    __tablename__ = "track_metadata_enrich"

    track_id: Mapped[str] = mapped_column(
        ForeignKey("tracks.id", ondelete="CASCADE"), primary_key=True
    )
    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    data: Mapped[dict] = mapped_column(JSONCol(), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Playlist(Base):
    __tablename__ = "playlists"

    id: Mapped[str] = mapped_column(UUIDCol(), primary_key=True, default=_uuid)
    server_id: Mapped[str] = mapped_column(ForeignKey("media_servers.id", ondelete="CASCADE"))
    external_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    owner_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("media_users.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)
    is_auto_generated: Mapped[bool] = mapped_column(Boolean, default=False)
    generated_for_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PlaylistTrack(Base):
    __tablename__ = "playlist_tracks"

    playlist_id: Mapped[str] = mapped_column(
        ForeignKey("playlists.id", ondelete="CASCADE"), primary_key=True
    )
    track_id: Mapped[str] = mapped_column(
        ForeignKey("tracks.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PlayHistory(Base):
    __tablename__ = "play_history"

    id: Mapped[str] = mapped_column(UUIDCol(), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("media_users.id", ondelete="CASCADE"))
    track_id: Mapped[str] = mapped_column(ForeignKey("tracks.id", ondelete="CASCADE"))
    played_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class Favorite(Base):
    __tablename__ = "favorites"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("media_users.id", ondelete="CASCADE"), primary_key=True
    )
    track_id: Mapped[str] = mapped_column(
        ForeignKey("tracks.id", ondelete="CASCADE"), primary_key=True
    )
    starred_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TrackDislike(Base):
    """Дизлайк трека (порт mobile TrackRating.like=false).

    source: manual (кнопка), rating (оценка <=2 из Navidrome), skip3 (3 скипа).
    """

    __tablename__ = "track_dislikes"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("media_users.id", ondelete="CASCADE"), primary_key=True
    )
    track_id: Mapped[str] = mapped_column(
        ForeignKey("tracks.id", ondelete="CASCADE"), primary_key=True
    )
    source: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ArtistBan(Base):
    """Бан артиста (порт mobile bannedArtists).

    reason: manual, auto3 (3 дизлайка треков артиста — правило mobile).
    """

    __tablename__ = "artist_bans"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("media_users.id", ondelete="CASCADE"), primary_key=True
    )
    artist_name: Mapped[str] = mapped_column(String(512), primary_key=True)
    reason: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PlayEvent(Base):
    """Богатое событие прослушивания (порт mobile PlayEvent + TrackRating счётчики).

    action: play, complete, skip, replay, seek_back, abandon.
    hour/day_of_week денормализованы для паттернов «когда слушает».
    """

    __tablename__ = "play_events"

    id: Mapped[str] = mapped_column(UUIDCol(), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("media_users.id", ondelete="CASCADE"), index=True
    )
    track_id: Mapped[str] = mapped_column(
        ForeignKey("tracks.id", ondelete="CASCADE"), index=True
    )
    action: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    position_sec: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    hour: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    day_of_week: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (Index("ix_play_event_user_track", "user_id", "track_id"),)


class UserCredential(Base):
    """Шифрованный пароль Navidrome пользователя (opt-in автообновление вкусов).

    Хранится ТОЛЬКО шифротекст Fernet; ключ — в env TASTE_VAULT_KEY (секрет
    compose, не в базе). Без ключа расшифровка невозможна.
    """

    __tablename__ = "user_credentials"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("media_users.id", ondelete="CASCADE"), primary_key=True
    )
    enc_password: Mapped[bytes] = mapped_column(BlobCol(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class TrackStat(Base):
    """Агрегат прослушиваний (user, track) — одна строка на пару.

    Для bulk-синка с мобилы (TrackRating целиком) и обмена обратно:
    upsert без раздувания PlayHistory тысячами строк.
    """

    __tablename__ = "track_stats"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("media_users.id", ondelete="CASCADE"), primary_key=True
    )
    track_id: Mapped[str] = mapped_column(
        ForeignKey("tracks.id", ondelete="CASCADE"), primary_key=True
    )
    plays: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skips: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    early_skips: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    replays: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    seek_backs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    abandons: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    mobile_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_played: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class TasteProfile(Base):
    """Слепок вкусов (user_id PK): свой + прилетевший с мобилы.

    mobile: точная копия MLProfile (preferredGenres/Artists, liked/disliked
    external ids, banned, artistDislikeCounts) — для обмена и подмешивания
    в серверный скоринг (точнее волна и плейлисты).
    """

    __tablename__ = "taste_profiles"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("media_users.id", ondelete="CASCADE"), primary_key=True
    )
    genres: Mapped[dict] = mapped_column(JSONCol(), default=dict, nullable=False)
    artists: Mapped[dict] = mapped_column(JSONCol(), default=dict, nullable=False)
    mobile_genres: Mapped[dict] = mapped_column(JSONCol(), default=dict, nullable=False)
    mobile_artists: Mapped[dict] = mapped_column(JSONCol(), default=dict, nullable=False)
    mobile_liked: Mapped[list] = mapped_column(JSONCol(), default=list, nullable=False)
    mobile_disliked: Mapped[list] = mapped_column(JSONCol(), default=list, nullable=False)
    mobile_banned: Mapped[list] = mapped_column(JSONCol(), default=list, nullable=False)
    mobile_counts: Mapped[dict] = mapped_column(JSONCol(), default=dict, nullable=False)
    mobile_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class ScanRun(Base):
    __tablename__ = "scan_runs"

    id: Mapped[str] = mapped_column(UUIDCol(), primary_key=True, default=_uuid)
    server_id: Mapped[str] = mapped_column(ForeignKey("media_servers.id", ondelete="CASCADE"))
    phase: Mapped[str] = mapped_column(String(64), nullable=False, default="library")
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    total_items: Mapped[int] = mapped_column(Integer, default=0)
    processed_items: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metadata_extra: Mapped[Optional[dict]] = mapped_column(JSONCol(), nullable=True)


class ScanLog(Base):
    __tablename__ = "scan_logs"

    id: Mapped[str] = mapped_column(UUIDCol(), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("scan_runs.id", ondelete="CASCADE"), index=True
    )
    level: Mapped[str] = mapped_column(String(16), default="info")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class CronJob(Base):
    __tablename__ = "cron_jobs"

    id: Mapped[str] = mapped_column(UUIDCol(), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    cron_expr: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    payload: Mapped[Optional[dict]] = mapped_column(JSONCol(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONCol(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Notification(Base):
    """Колокол: воркеры пишут сюда итоги (ночной импорт, открытия, дрейф).

    user_id NULL = глобальное (видно всем). Прочитано = read_at NOT NULL.
    """

    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(UUIDCol(), primary_key=True, default=_uuid)
    user_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("media_users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), default="info", nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    link: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class TasteSnapshot(Base):
    """Недельный слепок вкуса (понедельник): веса жанров/артистов + счётчики.

    Для дрейфа («ушёл из рока в эмбиент»). Снимает крон по понедельникам.
    """

    __tablename__ = "taste_snapshots"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("media_users.id", ondelete="CASCADE"), primary_key=True
    )
    week: Mapped[date] = mapped_column(Date, primary_key=True)
    genres: Mapped[dict] = mapped_column(JSONCol(), default=dict, nullable=False)
    artists: Mapped[dict] = mapped_column(JSONCol(), default=dict, nullable=False)
    counts: Mapped[dict] = mapped_column(JSONCol(), default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ApiToken(Base):
    """API-токен для связки плеер <-> мозг (создаётся в веб-UI, без compose).

    Храним только sha256-хэш; plaintext показывается один раз при создании.
    scopes: wave (волна/now-playing), sync (синк вкусов/событий/оценок),
    covers (обложки), playlists (weekly/discovery/export), admin (всё).
    """

    __tablename__ = "api_tokens"

    id: Mapped[str] = mapped_column(UUIDCol(), primary_key=True, default=_uuid)
    owner_user_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("media_users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="mobile")
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    prefix: Mapped[str] = mapped_column(String(12), nullable=False, default="")
    scopes: Mapped[list] = mapped_column(JSONCol(), default=list, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
