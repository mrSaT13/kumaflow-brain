"""Коллаборативная фильтрация (user-based, честная версия под 5 пользователей).

Все пользователи делят одну библиотеку, сигналы разные: лайки (Favorite),
плейлисты, дизлайки. Считаем косинус по лайк-векторам + бонус за общие
плейлистные треки. Рекомендации: треки, которые лайкнули похожие, а ты нет
(дизлайки и баны исключаем — как везде).
"""
from __future__ import annotations

from collections import defaultdict
from math import sqrt


def _like_sets(db) -> dict[str, set[str]]:
    from app.db.models import Favorite

    out: dict[str, set[str]] = defaultdict(set)
    for uid, tid in db.query(Favorite.user_id, Favorite.track_id).all():
        out[str(uid)].add(str(tid))
    return out


def _dislike_sets(db) -> dict[str, set[str]]:
    from app.db.models import TrackDislike

    out: dict[str, set[str]] = defaultdict(set)
    for uid, tid in db.query(TrackDislike.user_id, TrackDislike.track_id).all():
        out[str(uid)].add(str(tid))
    return out


def _cosine(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / sqrt(len(a) * len(b))


def similar_users(db, user_id: str, limit: int = 5) -> list[dict]:
    """Похожие пользователи: косинус лайков + число общих."""
    from app.db.models import MediaUser

    sets = _like_sets(db)
    mine = sets.get(str(user_id), set())
    if not mine:
        return []
    users = {str(u.id): u.username for u in db.query(MediaUser).all()}
    out = []
    for uid, likes in sets.items():
        if uid == str(user_id):
            continue
        sim = _cosine(mine, likes)
        if sim <= 0:
            continue
        out.append({"user_id": uid, "username": users.get(uid, uid[:8]),
                    "similarity": round(sim, 4), "shared_likes": len(mine & likes),
                    "likes": len(likes)})
    out.sort(key=lambda x: x["similarity"], reverse=True)
    return out[:limit]


def recommend_for_user(db, user_id: str, n: int = 30) -> dict:
    """Треки от похожих пользователей, которых у тебя нет в лайках/дизлайках.

    score = Σ similarity(v) по лайкнувшим; причина — кто именно.
    """
    from app.db.models import ArtistBan, Track

    user_id = str(user_id)
    sets = _like_sets(db)
    dis = _dislike_sets(db).get(user_id, set())
    mine = sets.get(user_id, set())
    sim_users = similar_users(db, user_id, limit=10)
    sims = [(u["user_id"], u["similarity"], u["username"]) for u in sim_users]
    if not sims:
        return {"items": [], "similar_users": []}
    bans = {str(r.artist_name) for r in
            db.query(ArtistBan).filter_by(user_id=user_id).all()}
    scores: dict[str, float] = defaultdict(float)
    reasons: dict[str, list[str]] = defaultdict(list)
    for uid, sim, uname in sims:
        for tid in sets.get(uid, set()):
            if tid in mine or tid in dis:
                continue
            scores[tid] += sim
            if uname not in reasons[tid]:
                reasons[tid].append(uname)
    if not scores:
        return {"items": [], "similar_users": sim_users}
    tracks = {str(t.id): t for t in
              db.query(Track).filter(Track.id.in_(list(scores))).all()}
    from app.services.artist_names import is_banned as _is_banned

    items = []
    for tid, s in sorted(scores.items(), key=lambda kv: kv[1], reverse=True):
        t = tracks.get(tid)
        if not t:
            continue
        if _is_banned(t.artist_name, bans):
            continue
        items.append({"track_id": tid, "title": t.title, "artist_name": t.artist_name,
                      "album_name": t.album_name, "genre": t.genre,
                      "score": round(float(s), 4), "because_of": reasons[tid][:3]})
        if len(items) >= n:
            break
    return {"items": items, "similar_users": sim_users}
