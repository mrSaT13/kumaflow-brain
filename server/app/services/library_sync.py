"""Library Sync — связка с внешним сервисом-поставщиком треков.

Мозг — хаб: игрок присылает подписку на артиста, мы ретранслируем её
наружу (POST /api/subs), а когда поставщик сообщает о скачанном
(POST /api/library-sync/events) — добираем треки в библиотеку
и уведомляем подписавшихся.

Нейминг намеренно нейтральный: ни в UI, ни в API, ни в логах нет
упоминания конкретного провайдера.
"""
from __future__ import annotations

import re
import time
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models import AppSetting, ArtistSubscription

logger = get_logger("library_sync")

KEY = "library_sync"
SEEN_KEY = "library_sync_seen"  # {job_id: ts} — дедуп входящих событий
SEEN_KEEP = 200
SEEN_TTL_SEC = 7 * 24 * 3600


def _norm_url(u: str | None) -> str:
    if not u:
        return ""
    u = u.strip().rstrip("/")
    if not u.startswith("http"):
        u = "http://" + u
    return u


def norm_name(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def canonical_key(s: str | None) -> str:
    """Канонический ключ имени — только для скопа Library Sync.

    Строже norm_name: регистр/пунктуация/ё-пробелы схлопываются
    (AC/DC == AC-DC == ac dc). Библиотечные нормализаторы
    (artist_names, dedup) не трогаем — у них другой контракт
    и SQL-предикаты, завязанные на точное lower(trim).
    """
    import unicodedata

    t = unicodedata.normalize("NFKC", s or "").lower()
    t = re.sub(r"[^a-zа-я0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def get_config(db: Session) -> dict[str, Any]:
    """Сначала AppSetting.library_sync (сохранено из веб-UI), иначе env."""
    row = db.get(AppSetting, KEY)
    if row and isinstance(row.value, dict):
        v = row.value
        return {
            "url": _norm_url(v.get("url")),
            "token": (v.get("token") or "").strip(),
            "enabled": bool(v.get("enabled", False)),
        }
    s = get_settings()
    return {
        "url": _norm_url(s.library_sync_url),
        "token": (s.library_sync_token or "").strip(),
        "enabled": False,
    }


def save_config(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    # Токен меняем только если ключ прислан явно; пустая форма = оставить прежний.
    prev_token = ""
    prev = db.get(AppSetting, KEY)
    if prev is not None and isinstance(prev.value, dict):
        prev_token = (prev.value.get("token") or "").strip()
    if "token" in payload:
        token = (payload.get("token") or "").strip()
    else:
        token = prev_token
    value = {
        "url": _norm_url(payload.get("url")),
        "token": token,
        "enabled": bool(payload.get("enabled", False)),
    }
    row = db.get(AppSetting, KEY)
    if row is None:
        row = AppSetting(key=KEY, value=value)
        db.add(row)
    else:
        row.value = value
    db.commit()
    return value


def masked_config(db: Session) -> dict[str, Any]:
    """Конфиг для UI: токен не отдаём, только факт наличия."""
    cfg = get_config(db)
    return {"url": cfg["url"], "enabled": cfg["enabled"],
            "token_configured": bool(cfg["token"])}


async def test_connection(url: str, token: str = "") -> dict[str, Any]:
    """Пинг сервиса: GET {url}/api/health. Бросает исключение — ловит вызывающий."""
    target = f"{_norm_url(url)}/api/health"
    headers: dict[str, str] = {}
    if (token or "").strip():
        headers["Authorization"] = f"Bearer {token.strip()}"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(target, headers=headers)
        r.raise_for_status()
        try:
            body = r.json()
        except Exception:
            body = {}
        return {"ok": True, "status": r.status_code,
                "version": (body or {}).get("version")}


def _auth_headers(cfg: dict[str, Any]) -> dict[str, str]:
    token = (cfg.get("token") or "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def push_subscription(cfg: dict[str, Any], artist_name: str,
                      mode: str = "tracks") -> dict[str, Any]:
    """Ретрансляция подписки наружу. Синхронно (httpx), вызывается из API/воркера."""
    base = (cfg.get("url") or "").rstrip("/")
    if not base:
        raise RuntimeError("Library Sync не настроен (нет URL)")
    with httpx.Client(timeout=20) as client:
        r = client.post(f"{base}/api/subs",
                        json={"artistName": artist_name, "mode": mode},
                        headers=_auth_headers(cfg))
    if r.status_code >= 400:
        try:
            err = (r.json() or {}).get("error") or r.text[:300]
        except Exception:
            err = r.text[:300]
        raise RuntimeError(f"сервис ответил {r.status_code}: {err}")
    return r.json()


def remove_subscription(cfg: dict[str, Any], provider_artist_id: str) -> dict[str, Any]:
    """Отписка наружу по id артиста на стороне поставщика."""
    base = (cfg.get("url") or "").rstrip("/")
    if not base or not provider_artist_id:
        return {"ok": False, "error": "не настроено"}
    with httpx.Client(timeout=20) as client:
        r = client.post(f"{base}/api/subs/cancel/{provider_artist_id}",
                        headers=_auth_headers(cfg))
    return {"ok": r.status_code < 400}


def record_event(db: Session, job_id: str) -> bool:
    """Дедуп событий по jobId. True = новое (обработать), False = уже видели."""
    if not job_id:
        return True
    row = db.get(AppSetting, SEEN_KEY)
    seen = dict(row.value) if row and isinstance(row.value, dict) else {}
    if job_id in seen:
        return False
    seen[job_id] = time.time()
    # чистим старые и лишние
    cutoff = time.time() - SEEN_TTL_SEC
    fresh = {k: v for k, v in seen.items() if v and v > cutoff}
    if len(fresh) > SEEN_KEEP:
        for k in sorted(fresh, key=lambda k: fresh[k])[: len(fresh) - SEEN_KEEP]:
            del fresh[k]
    if row is None:
        db.add(AppSetting(key=SEEN_KEY, value=fresh))
    else:
        row.value = fresh
    db.commit()
    return True


def find_subscribers(db: Session, artist_name: str,
                     provider_artist_id: str | None = None) -> list[ArtistSubscription]:
    """Все подписки на артиста: сначала по id поставщика, затем по имени.

    Имя сравниваем по canonical_key, со старым norm_name как фолбэком —
    строки, сохранённые до ужесточения ключа, продолжают находиться.
    """
    if provider_artist_id:
        rows = db.query(ArtistSubscription).filter(
            ArtistSubscription.provider_artist_id == str(provider_artist_id)).all()
        if rows:
            return rows
    key = canonical_key(artist_name)
    legacy = norm_name(artist_name)
    if not key and not legacy:
        return []
    rows = db.query(ArtistSubscription).all()
    out = []
    for r in rows:
        if key and canonical_key(r.artist_name) == key:
            out.append(r)
        elif legacy and norm_name(r.artist_name) == legacy:
            out.append(r)
    return out


def find_existing_subscription(db: Session, user_id: str | None,
                               artist_name: str) -> ArtistSubscription | None:
    """Дубль подписки того же юзера: сверка по каноническому ключу имени."""
    key = canonical_key(artist_name)
    legacy = norm_name(artist_name)
    q = db.query(ArtistSubscription).filter(ArtistSubscription.user_id == user_id)
    for r in q.all():
        if key and canonical_key(r.artist_name) == key:
            return r
        if legacy and norm_name(r.artist_name) == legacy:
            return r
    return None
