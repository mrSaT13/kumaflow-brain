"""Общие настройки «Моей волны»: одна волна на всех устройствах.

Пилюли (mood/activity/characteristic/language), выбранные на любом плеере,
хранятся в таблице wave_settings (user_id PK) — остальные устройства
подхватывают их при старте. Сброс на одном (PUT {} / DELETE) виден всем:
version растёт, очередь перестраивается следующей докруткой.

Семантика слияния с запросом waveContinue:
  - ключ ЕСТЬ в запросе (хоть пустой) — побеждает запрос, только на этот раз;
  - ключа НЕТ — подставляется stored. Пустой выбор («авто») в запросе
    НЕ тянет stored-пилюлю: «авто» значит «авто» именно здесь.
PUT пишет stored (merge; пустая строка стирает пилюлю).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

KEYS = ("mood", "activity", "characteristic", "language")
_MAX_LEN = 64


def _norm(v: Any) -> str:
    return str(v or "").strip()[:_MAX_LEN]


def _shape(row) -> dict:
    upd = getattr(row, "updated_at", None) if row is not None else None
    try:
        upd_s = upd.isoformat() + "Z" if upd else None
    except Exception:
        upd_s = None
    return {
        "settings": dict(getattr(row, "settings", None) or {}) if row is not None else {},
        "version": int(getattr(row, "version", 1) or 1) if row is not None else 1,
        "updated_at": upd_s,
    }


def get_settings(db, user_id: str) -> dict:
    """Прочитать общие настройки (пусто + version 1, если не заданы)."""
    from app.db.models import WaveSettings as _WS

    row = db.get(_WS, str(user_id))
    return _shape(row)


def save_settings(db, user_id: str, patch: dict | None) -> dict:
    """Merge-патч stored-настроек. Пустая строка стирает пилюлю.
    Пустой патч ({} / None) — полный сброс: все пилюли сняты."""
    from app.db.models import WaveSettings as _WS

    row = db.get(_WS, str(user_id))
    if row is None:
        row = _WS(user_id=str(user_id), settings={}, version=1)
        db.add(row)
    if not patch:
        cur: dict = {}
    else:
        cur = dict(row.settings or {})
        for k in KEYS:
            if k in patch:
                v = _norm(patch.get(k))
                if v:
                    cur[k] = v
                else:
                    cur.pop(k, None)
    row.settings = cur
    row.version = int(row.version or 1) + 1
    row.updated_at = datetime.utcnow()
    db.commit()
    return _shape(row)


def reset_settings(db, user_id: str) -> dict:
    """Полный сброс (как PUT {}): все пилюли сняты, version растёт."""
    return save_settings(db, user_id, {})


def effective_settings(db, user_id: str, request_settings: dict | None) -> dict:
    """Итоговые настройки для одного waveContinue: stored + override запроса."""
    stored = get_settings(db, user_id)["settings"]
    eff = {k: v for k, v in stored.items() if k in KEYS and v}
    if isinstance(request_settings, dict):
        for k in KEYS:
            if k in request_settings:
                v = _norm(request_settings.get(k))
                if v:
                    eff[k] = v
                else:
                    eff.pop(k, None)
    return eff
