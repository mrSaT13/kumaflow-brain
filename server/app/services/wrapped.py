"""Итоги месяца (self-hosted Wrapped): агрегатор по готовым таблицам.

Источники: PlayHistory (прослушивания), PlayEvent (скипы/реплаи...),
Favorite.starred_at (новые лайки), TrackDislike.created_at, мета треков.
Ничего нового писать не нужно — только читать.
"""
from __future__ import annotations

from calendar import monthrange
from collections import Counter
from datetime import datetime
from typing import Any


def _month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    start = datetime(year, month, 1)
    last_day = monthrange(year, month)[1]
    if month == 12:
        end = datetime(year + 1, 1, 1)
    else:
        end = datetime(year, month + 1, 1)
    return start, end


def available_months(db, user_id: str) -> list[str]:
    """Месяцы с хоть одним прослушиванием: ['2026-09', ...] новые первые."""
    from app.db.models import PlayHistory

    rows = db.query(PlayHistory.played_at).filter(
        PlayHistory.user_id == user_id,
        PlayHistory.played_at.is_not(None)).all()
    months = sorted({f"{r[0].year:04d}-{r[0].month:02d}" for r in rows if r[0]},
                    reverse=True)
    return months


def month_summary(db, user_id: str, year: int, month: int) -> dict[str, Any]:
    from app.db.models import (ArtistBan, Favorite, PlayEvent, PlayHistory,
                               Track, TrackDislike)

    start, end = _month_bounds(year, month)
    label = f"{year:04d}-{month:02d}"

    hist = db.query(PlayHistory).filter(
        PlayHistory.user_id == user_id,
        PlayHistory.played_at >= start, PlayHistory.played_at < end).all()
    plays = len(hist)
    if not plays:
        return {"ok": True, "month": label, "plays": 0}

    tids = [str(h.track_id) for h in hist]
    meta = {str(t.id): t for t in
            db.query(Track).filter(Track.id.in_(tids)).all()} if tids else {}
    cnt = Counter(tids)
    # минуты: сумма длительностей сыгранного
    minutes = sum((meta[t].duration_sec or 0) for t in tids if t in meta) // 60
    active_days = len({h.played_at.date() for h in hist if h.played_at})

    top_tracks = []
    for tid, c in cnt.most_common(10):
        t = meta.get(tid)
        top_tracks.append({"track_id": tid, "title": t.title if t else "—",
                           "artist_name": t.artist_name if t else None,
                           "plays": c})
    ac, gc = Counter(), Counter()
    for tid, c in cnt.items():
        t = meta.get(tid)
        if not t:
            continue
        if t.artist_name:
            ac[t.artist_name] += c
        if t.genre:
            gc[t.genre] += c
    # открытия: трек впервые сыгран именно в этом месяце
    before = {str(r[0]) for r in db.query(PlayHistory.track_id).filter(
        PlayHistory.user_id == user_id, PlayHistory.played_at < start).all()}
    firsts = [t for t in set(tids) if t not in before]
    firsts_meta = [{"track_id": t, "title": (meta[t].title if t in meta else "—"),
                    "artist_name": (meta[t].artist_name if t in meta else None)}
                   for t in firsts[:12]]

    likes = db.query(Favorite).filter(
        Favorite.user_id == user_id,
        Favorite.starred_at >= start, Favorite.starred_at < end).count()
    dislikes = db.query(TrackDislike).filter(
        TrackDislike.user_id == user_id,
        TrackDislike.created_at >= start, TrackDislike.created_at < end).count()
    bans = db.query(ArtistBan).filter(
        ArtistBan.user_id == user_id,
        ArtistBan.created_at >= start, ArtistBan.created_at < end).count()
    ev = db.query(PlayEvent.action).filter(
        PlayEvent.user_id == user_id,
        PlayEvent.created_at >= start, PlayEvent.created_at < end).all()
    evc = Counter(a[0] for a in ev)
    hours = [0] * 24
    for h in hist:
        if h.played_at:
            hours[h.played_at.hour] += 1
    peak_hour = max(range(24), key=lambda i: hours[i])

    return {
        "ok": True, "month": label, "plays": plays, "minutes": minutes,
        "active_days": active_days, "peak_hour": peak_hour, "hours": hours,
        "top_tracks": top_tracks,
        "top_artists": [{"name": n, "plays": c} for n, c in ac.most_common(8)],
        "top_genres": [{"name": n, "plays": c} for n, c in gc.most_common(8)],
        "discoveries": len(firsts), "discoveries_sample": firsts_meta,
        "new_likes": likes, "new_dislikes": dislikes, "new_bans": bans,
        "skips": evc.get("skip", 0), "replays": evc.get("replay", 0),
        "completes": evc.get("complete", 0),
    }
