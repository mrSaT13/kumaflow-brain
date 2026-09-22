"""Дрейф вкуса: недельные снапшоты профиля + сравнение с текущим.

Снапшот (понедельник недели): preferredGenres/Artists + счётчики.
Сравнение: топ подъёмы/спады + человеческая фраза («ушёл из рока в эмбиент»).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any


def week_monday(d: date | None = None) -> date:
    d = d or date.today()
    return d - timedelta(days=d.weekday())


def take_snapshot(db, user_id: str, week: date | None = None) -> dict:
    """Снять слепок (идемпотентно: повтор в ту же неделю — перезапись)."""
    from app.db.models import TasteSnapshot
    from app.services import taste as _taste

    week = week or week_monday()
    prof = _taste.user_profile(db, user_id, top_n=0)
    if not prof.get("ok"):
        return {"ok": False, "error": "profile failed"}
    row = db.get(TasteSnapshot, {"user_id": user_id, "week": week})
    payload = {"genres": dict(profs_genres(prof)), "artists": dict(profs_artists(prof)),
               "counts": dict(prof.get("counts") or {})}
    if row is None:
        db.add(TasteSnapshot(user_id=user_id, week=week,
                             genres=payload["genres"], artists=payload["artists"],
                             counts=payload["counts"]))
    else:
        row.genres, row.artists, row.counts = \
            payload["genres"], payload["artists"], payload["counts"]
    db.commit()
    return {"ok": True, "week": week.isoformat()}


def profs_genres(prof: dict) -> dict:
    return {str(k): float(v) for k, v in (prof.get("preferredGenres") or {}).items()}


def profs_artists(prof: dict) -> dict:
    return {str(k): float(v) for k, v in (prof.get("preferredArtists") or {}).items()}


def _movers(old: dict[str, float], new: dict[str, float],
            limit: int = 5) -> tuple[list[dict], list[dict]]:
    keys = (set(old) | set(new)) - {"—"}
    rows = [{"name": k, "old": round(old.get(k, 0.0), 2),
             "new": round(new.get(k, 0.0), 2),
             "delta": round(new.get(k, 0.0) - old.get(k, 0.0), 2)}
            for k in keys]
    rows = [r for r in rows if r["delta"] != 0]
    up = sorted([r for r in rows if r["delta"] > 0],
                key=lambda r: r["delta"], reverse=True)[:limit]
    down = sorted([r for r in rows if r["delta"] < 0],
                  key=lambda r: r["delta"])[:limit]
    return up, down


def compare(db, user_id: str, weeks_ago: int = 4) -> dict[str, Any]:
    """Текущий профиль vs снапшот weeks_ago недель назад (ближайший имеющийся)."""
    from app.db.models import TasteSnapshot
    from app.services import taste as _taste

    snaps = db.query(TasteSnapshot).filter(
        TasteSnapshot.user_id == user_id).order_by(
        TasteSnapshot.week.desc()).all()
    if not snaps:
        return {"ok": True, "weeks": [], "hint": "Снапшотов пока нет — первый появится в понедельник ночью"}
    target = week_monday() - timedelta(weeks=max(1, weeks_ago))
    snap = min(snaps, key=lambda s: abs((s.week - target).days))
    prof = _taste.user_profile(db, user_id, top_n=0)
    if not prof.get("ok"):
        return {"ok": False, "error": "profile failed"}
    g_up, g_down = _movers(dict(snap.genres or {}), profs_genres(prof))
    a_up, a_down = _movers(dict(snap.artists or {}), profs_artists(prof))
    # фраза: топ подъём vs топ спад по жанрам
    summary = None
    if g_up and g_down and g_up[0]["name"] != g_down[0]["name"]:
        summary = f"Ушёл из «{g_down[0]['name']}» в «{g_up[0]['name']}»"
    elif g_up:
        summary = f"Новое увлечение — «{g_up[0]['name']}»"
    elif g_down:
        summary = f"Остыл к «{g_down[0]['name']}»"
    return {
        "ok": True, "summary": summary,
        "snapshot_week": snap.week.isoformat(),
        "weeks": [s.week.isoformat() for s in snaps],
        "genres_up": g_up, "genres_down": g_down,
        "artists_up": a_up, "artists_down": a_down,
    }
