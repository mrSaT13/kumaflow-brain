"""Колокол уведомлений: единый вход для воркеров и API.

Воркер пишет итог (ночной импорт, открытия, дрейф) — веб показывает
бейдж с числом непрочитанных. Тихо глотает ошибки: уведомления
никогда не должны валить задачу.
"""
from __future__ import annotations

from app.core.logging import get_logger

logger = get_logger("notify")

KINDS = ("info", "success", "warn", "error")


def notify(db, kind: str, title: str, body: str | None = None,
           user_id: str | None = None, link: str | None = None) -> str | None:
    from app.db.models import Notification

    try:
        if kind not in KINDS:
            kind = "info"
        n = Notification(kind=kind, title=title[:512], body=body,
                         user_id=user_id, link=(link or '')[:512] or None)
        db.add(n)
        db.flush()
        return str(n.id)
    except Exception as e:  # noqa: BLE001
        logger.warning("notify failed: {}", e)
        try:
            db.rollback()
        except Exception:
            pass
        return None


def prune(db, keep: int = 200) -> int:
    """Режем старые (оставляем свежие keep), чтобы таблица не росла."""
    from app.db.models import Notification

    try:
        ids = [r[0] for r in db.query(Notification.id)
               .order_by(Notification.created_at.desc())
               .offset(max(keep, 50)).limit(5000).all()]
        if not ids:
            return 0
        n = db.query(Notification).filter(Notification.id.in_(ids)).delete(
            synchronize_session=False)
        db.commit()
        return n
    except Exception as e:  # noqa: BLE001
        logger.warning("notify prune failed: {}", e)
        try:
            db.rollback()
        except Exception:
            pass
        return 0
