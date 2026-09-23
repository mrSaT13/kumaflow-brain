"""Now Playing для виджета «Слушает сейчас» на главной.

GET /api/now-playing?user_id=...&n=5
- тянет getNowPlaying из Navidrome под глобальной учёткой (как скан);
- матчит external_id -> наш Track;
- подбирает next-5 через wave-скоринг (наша алхимия) + человеческое «почему похоже».

Polling из UI раз в 10-15 сек. Никаких вебсокетов в MVP.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db

router = APIRouter()


def _explain(cur_feat, cur_track, cand_track, cand_feat, collab: float = 0.0) -> str:
    bits: list[str] = []
    try:
        if cand_track.artist_name and cur_track.artist_name \
                and cand_track.artist_name == cur_track.artist_name:
            bits.append("тот же артист")
        if cand_track.genre and cur_track.genre \
                and str(cand_track.genre).lower() == str(cur_track.genre).lower():
            bits.append(f"жанр {cand_track.genre}")
        if cur_feat is not None and cand_feat is not None:
            try:
                cb = float(cand_feat.tempo_bpm or 0)
                tb = float(cur_feat.tempo_bpm or 0)
                if cb and tb and abs(cb - tb) <= 8:
                    bits.append(f"темп {int(tb)}→{int(cb)}")
            except (TypeError, ValueError):
                pass
            try:
                ce = float(cand_feat.energy) if cand_feat.energy is not None else None
                te = float(cur_feat.energy) if cur_feat.energy is not None else None
                if ce is not None and te is not None and abs(ce - te) <= 0.15:
                    bits.append("похожая энергия")
            except (TypeError, ValueError):
                pass
            try:
                cm = set(str(m).lower() for m in (cand_feat.mood_labels or []))
                tm = set(str(m).lower() for m in (cur_feat.mood_labels or []))
                common = cm & tm
                if common:
                    bits.append(f"вайб {sorted(common)[0]}")
            except Exception:
                pass
            try:
                if cand_feat.key_name and cur_feat.key_name \
                        and cand_feat.key_name == cur_feat.key_name:
                    bits.append(f"тональность {cand_feat.key_name}")
            except Exception:
                pass
        if collab and collab > 0.4:
            bits.append("слушают те же люди")
    except Exception:
        pass
    if not bits:
        bits.append("похоже по аудио-фичам")
    return " · ".join(bits[:3])


def _next_for_track(db: Session, track_id: str, user_id: str | None, n: int = 5) -> list[dict]:
    from app.db.models import ArtistBan, Track, TrackDislike, TrackFeatures

    cur = db.get(Track, track_id)
    if not cur:
        return []
    dis: set[str] = set()
    bans: set[str] = set()
    if user_id:
        try:
            dis = {str(r.track_id) for r in
                   db.query(TrackDislike).filter_by(user_id=user_id).all()}
            bans = {str(r.artist_name) for r in
                    db.query(ArtistBan).filter_by(user_id=user_id).all()}
        except Exception:
            pass
    dis.add(str(cur.id))

    q = db.query(Track).filter(Track.id != str(cur.id))
    # лёгкий приоритет: тот же жанр/артист выше, остальное доберём
    pool: list[Track] = []
    try:
        same = q.filter(
            (Track.genre == cur.genre) | (Track.artist_name == cur.artist_name)
        ).limit(300).all()
        pool.extend(same)
    except Exception:
        pass
    if len(pool) < 400:
        try:
            rest = q.order_by(Track.play_count.desc()).limit(600).all()
            seen = {str(t.id) for t in pool}
            for t in rest:
                if str(t.id) not in seen:
                    pool.append(t)
                if len(pool) >= 600:
                    break
        except Exception:
            pass
    pool = [t for t in pool if str(t.id) not in dis
            and not (t.artist_name and t.artist_name in bans)]
    if not pool:
        return []

    cand_ids = [str(t.id) for t in pool[:600]]
    ranked: list[dict] = []
    collab_map: dict[str, float] = {}
    if user_id:
        try:
            from app.services import collab as _cb
            from app.services import wave as _wave

            rec = _cb.recommend_for_user(db, user_id, n=200)
            collab_map = {str(it["track_id"]): float(it.get("score", 0) or 0)
                          for it in rec.get("items", [])}
            seeds = _wave.select_seeds(db, user_id, limit=4)
            seeds = [str(cur.id)] + [s for s in seeds if s != str(cur.id)][:4]
            ranked = _wave.score_candidates(db, user_id, cand_ids[:400], seeds,
                                            settings={}, recent_events=None,
                                            collab_scores=collab_map)
        except Exception:
            ranked = []
    if not ranked:
        # без пользователя: чистая аудио-похожесть на текущий трек
        try:
            from app.services import wave as _wave

            ranked = _wave.score_candidates(db, user_id or str(cur.server_id),
                                            cand_ids[:400], [str(cur.id)],
                                            settings={}, recent_events=None,
                                            collab_scores={})
        except Exception:
            # совсем fallback: как есть из пула
            ranked = [{"track_id": tid, "score": 0.0} for tid in cand_ids[:n]]

    meta = {str(t.id): t for t in pool}
    feats = {str(f.track_id): f for f in
             db.query(TrackFeatures).filter(
                 TrackFeatures.track_id.in_([r["track_id"] for r in ranked[:n]] + [str(cur.id)])
             ).all()}
    cur_feat = feats.get(str(cur.id))
    out: list[dict] = []
    for r in ranked[:n]:
        tid = str(r.get("track_id") or "")
        t = meta.get(tid)
        if not t:
            continue
        out.append({
            "track_id": tid,
            "title": t.title,
            "artist_name": t.artist_name,
            "album_name": t.album_name,
            "genre": t.genre,
            "score": round(float(r.get("score", 0) or 0), 3),
            "reason": _explain(cur_feat, cur, t, feats.get(tid),
                               collab_map.get(tid, 0.0)),
        })
    return out


@router.get("", include_in_schema=False)
@router.get("/")
def now_playing(user_id: str | None = None, n: int = 5, db: Session = Depends(get_db)):
    """Что играет сейчас в Navidrome + next-N от нашей алхимии."""
    from app.db.models import MediaUser, Track
    from app.services.media_server import get_media_server_config, is_real_config

    n = max(1, min(int(n or 5), 10))
    cfg = get_media_server_config(db)
    if not is_real_config(cfg) or not cfg.get("user"):
        return {"playing": None, "next": [], "source": "no-server"}

    want_username: str | None = None
    if user_id:
        try:
            u = db.get(MediaUser, user_id)
            if u is not None:
                want_username = u.external_id
        except Exception:
            want_username = None

    async def _fetch() -> list[dict]:
        from app.services.navidrome.client import SubsonicAuth, SubsonicClient

        async with SubsonicClient(cfg["url"], SubsonicAuth(
                user=cfg["user"], password=cfg.get("password") or ""), timeout=15.0) as client:
            try:
                return await client.get_now_playing() or []
            except Exception:
                return []

    try:
        entries = asyncio.run(_fetch())
    except Exception:
        entries = []
    if not entries:
        return {"playing": None, "next": [], "source": "idle"}

    # предпочитаем запись нашего пользователя, иначе первую
    picked: dict | None = None
    if want_username:
        for e in entries:
            eu = str(e.get("username") or e.get("userName") or e.get("user") or "")
            if eu == want_username:
                picked = e
                break
    if picked is None:
        picked = entries[0] if isinstance(entries[0], dict) else None
    if not picked:
        return {"playing": None, "next": [], "source": "idle"}

    ext_id = str(picked.get("id") or "")
    local: Track | None = None
    if ext_id:
        try:
            local = db.query(Track).filter(Track.external_id == ext_id).first()
        except Exception:
            local = None
    playing = {
        "external_id": ext_id or None,
        "track_id": str(local.id) if local else None,
        "title": (local.title if local else picked.get("title")) or "—",
        "artist_name": (local.artist_name if local else picked.get("artist")) or "—",
        "album_name": (local.album_name if local else picked.get("album")) or None,
        "username": picked.get("username") or picked.get("userName"),
        "minutes_ago": picked.get("minutesAgo"),
        "player": picked.get("playerName") or picked.get("player"),
    }
    nxt: list[dict] = []
    if local is not None:
        try:
            nxt = _next_for_track(db, str(local.id), user_id, n=n)
        except Exception:
            nxt = []
    return {"playing": playing, "next": nxt, "source": "navidrome"}
