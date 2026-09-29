from __future__ import annotations

"""Мои устройства: живые слоты волны + человекочитаемые имена + токены.

Изоляция — конструкцией, а не проверками в каждом месте: user_id НЕ
принимается ни в каком виде, владелец всегда берётся из токена
(`request.state.brain_token.owner_user_id`). Чужие устройства нельзя
ни увидеть, ни переименовать — даже прямым запросом.
Админский env-токен и открытая LAN без владельца: владельца нет —
просим `?user_id=` явно (там авторизации всё равно нет ни на что).
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.auth import require_brain_auth
from app.db import get_db

router = APIRouter(dependencies=[Depends(require_brain_auth)])


def _me(request: Request, user_id: str | None = None) -> str:
    """owner_user_id из токена. Без владельца — только явный user_id (open LAN)."""
    info = getattr(request.state, "brain_token", None) or {}
    owner = info.get("owner_user_id")
    if owner:
        return str(owner)
    if user_id:
        return str(user_id)
    raise HTTPException(
        403,
        "токен без владельца: укажите ?user_id= (открытая LAN) "
        "или войдите пользовательским токеном",
    )


@router.get("/")
def my_devices(request: Request, user_id: str | None = None, db: Session = Depends(get_db)):
    """Все устройства владельца токена: живые слоты + сохранённые имена + токены.

    На каждое: display_name (своё имя или сырая метка плеера или слот),
    live (возраст слота, длина очереди, пауза), now_playing (что играет
    сейчас — сверить перед переименованием), токены пользователя
    (имя, префикс, вкл/выкл, был ли) и подсветка дублей имён токенов
    (один токен на два устройства — так быть не должно).
    Только чтение.
    """
    from app.api.wave import _live_slots
    from app.db.models import DeviceName
    from app.services import api_tokens as _tokens
    from app.services.track_resolve import get_track as _gt

    uid = _me(request, user_id)
    names = {
        d.device_slot: d.display_name
        for d in db.query(DeviceName).filter(DeviceName.owner_user_id == uid).all()
    }

    live: dict[str, dict] = {}
    for slot, entry, age in _live_slots(uid):
        cur = entry.get("current_track_id")
        now = None
        if cur:
            try:
                t = _gt(db, str(cur))
                if t is not None:
                    now = {
                        "title": getattr(t, "title", None),
                        "artist_name": getattr(t, "artist_name", None),
                        "position_sec": entry.get("position_sec"),
                        "duration_sec": entry.get("duration_sec"),
                    }
            except Exception:
                now = None
        q = [x for x in (entry.get("queue") or []) if str(x)]
        raw_label = entry.get("device") or slot
        live[slot] = {
            "device_id": slot,
            "raw_label": raw_label,
            "display_name": names.get(slot) or raw_label,
            "renamed": slot in names,
            "age_sec": int(age),
            "queue_len": len(q),
            "paused": bool(entry.get("paused")),
            "now_playing": now,
        }

    # Переименованные, но молчащие слоты — тоже показываем (иначе имя,
    # данное вчера, исчезнет из списка и его нельзя поправить).
    for slot, disp in names.items():
        if slot not in live:
            live[slot] = {
                "device_id": slot,
                "raw_label": slot,
                "display_name": disp,
                "renamed": True,
                "age_sec": None,
                "queue_len": 0,
                "paused": False,
                "now_playing": None,
            }

    try:
        toks = _tokens.list_tokens(db, uid) or []
    except Exception:
        toks = []
    norm: dict[str, int] = {}
    for t in toks:
        k = str((t.get("name") if isinstance(t, dict) else getattr(t, "name", "")) or "").strip().lower()
        norm[k] = norm.get(k, 0) + 1
    dup_names = sorted(k for k, n in norm.items() if k and n > 1)
    tokens = [
        {
            "id": t.get("id") if isinstance(t, dict) else getattr(t, "id", None),
            "name": t.get("name") if isinstance(t, dict) else getattr(t, "name", ""),
            "prefix": t.get("prefix") if isinstance(t, dict) else getattr(t, "prefix", ""),
            "scopes": t.get("scopes") if isinstance(t, dict) else getattr(t, "scopes", []),
            "enabled": t.get("enabled") if isinstance(t, dict) else getattr(t, "enabled", True),
            "last_used_at": str(t.get("last_used_at") or "") if isinstance(t, dict) else str(getattr(t, "last_used_at", "") or ""),
            "name_duplicate": (
                str((t.get("name") if isinstance(t, dict) else getattr(t, "name", "")) or "").strip().lower()
                in dup_names
            ),
        }
        for t in toks
    ]

    devs = sorted(
        live.values(),
        key=lambda d: (d["age_sec"] is None, d["age_sec"] if d["age_sec"] is not None else 0),
    )
    return {
        "ok": True,
        "user_id": uid,
        "devices": devs,
        "tokens": tokens,
        "duplicate_token_names": dup_names,
        "note": (
            "Имя видно в селекторе волны и в «Продолжить с …» вместо сырого UUID. "
            "Перед переименованием сверьте now_playing — что играет прямо сейчас."
        ),
    }


@router.patch("/{slot}")
def rename_device(slot: str, payload: dict | None = None, request: Request = None,
                  db: Session = Depends(get_db)):
    """Переименовать СВОЁ устройство. Пустое имя — сброс к сырой метке."""
    from app.api.wave import _norm_device
    from app.db.models import DeviceName

    uid = _me(request)
    norm = _norm_device(slot)
    name = str(((payload or {}).get("display_name")) or "").strip()[:64]
    if not name:
        db.query(DeviceName).filter(
            DeviceName.owner_user_id == uid, DeviceName.device_slot == norm
        ).delete()
        db.commit()
        return {"ok": True, "device_id": norm, "display_name": None, "reset": True}
    row = db.get(DeviceName, (uid, norm))
    if row is None:
        row = DeviceName(owner_user_id=uid, device_slot=norm, display_name=name)
        db.add(row)
    else:
        row.display_name = name
    db.commit()
    return {"ok": True, "device_id": norm, "display_name": name}
