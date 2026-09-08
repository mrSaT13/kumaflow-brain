"""Cross-dialect helpers: Postgres JSONB/UUID/BYTEA ↔ SQLite-friendly types.

Used in models so the same schema works both locally (SQLite, no Docker)
and in production (PostgreSQL via docker-compose).
"""
from __future__ import annotations

from sqlalchemy import JSON, LargeBinary, String, TypeDecorator
from sqlalchemy.dialects.postgresql import UUID as _PG_UUID


class _UUIDStr(TypeDecorator):
    """Stores UUID as 36-char text. Works on every dialect."""
    impl = String(36)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return str(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return str(value)


class _CompatUUID(TypeDecorator):
    """On Postgres: native UUID. On SQLite (or other): text 36-char."""
    impl = String(36)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(_PG_UUID(as_uuid=True))
        return dialect.type_descriptor(String(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return str(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return str(value)


def UUIDCol() -> _CompatUUID:
    return _CompatUUID()


def JSONCol():
    return JSON


def BlobCol():
    return LargeBinary


__all__ = ["UUIDCol", "JSONCol", "BlobCol"]
