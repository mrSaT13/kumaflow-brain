from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.demo import ensure_demo_server
from app.services.queue import enqueue
from app.workers.tasks import collab_build

router = APIRouter()


@router.post("/rebuild")
def rebuild():
    return {"queued": True, "job_id": enqueue(collab_build)}


@router.get("/similar-users/{user_id}")
def similar_users(user_id: str, limit: int = 5, db: Session = Depends(get_db)):
    """Похожие по вкусу пользователи (косинус по лайкам). Реально считается."""
    from app.db.models import MediaUser
    from app.services import collab as _cb

    try:
        uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(400, "invalid id")
    if not db.get(MediaUser, user_id):
        raise HTTPException(404, "not found")
    return {"users": _cb.similar_users(db, user_id, limit=max(1, min(10, limit)))}


@router.get("/recommend/{user_id}")
def recommend(user_id: str, n: int = 30, db: Session = Depends(get_db)):
    """Коллаборативные рекомендации: лайкнули похожие — нет у тебя.

    Работает уже на 2+ пользователях с пересекающимися лайками.
    """
    from app.db.models import MediaUser
    from app.services import collab as _cb

    try:
        uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(400, "invalid id")
    if not db.get(MediaUser, user_id):
        raise HTTPException(404, "not found")
    return _cb.recommend_for_user(db, user_id, n=max(1, min(100, n)))


@router.post("/compare")
def compare_users(payload: dict, db: Session = Depends(get_db)):
    """Сравнение вкусов N пользователей: общие жанры/артисты/треки + попарные связи.

    Body: {user_ids: [uuid...2-10], top_n=12}.
    Мозг считает пересечения весов (preferredGenres/Artists из вкусовых
    профилей) — веб только рисует облака одним цветом, общее видно сразу.
    """
    from app.db.models import MediaUser, Track
    from app.services import collab as _cb
    from app.services import taste as _taste

    raw = (payload or {}).get('user_ids') or []
    top_n = max(5, min(30, int((payload or {}).get('top_n') or 12)))
    uids: list[str] = []
    for x in raw:
        try:
            uuid.UUID(str(x))
            uids.append(str(x))
        except ValueError:
            raise HTTPException(400, f'invalid id: {x}')
    uids = list(dict.fromkeys(uids))
    if len(uids) < 2:
        raise HTTPException(400, 'need 2+ user_ids')
    if len(uids) > 10:
        raise HTTPException(400, 'max 10 user_ids')
    users = [db.get(MediaUser, u) for u in uids]
    if any(u is None for u in users):
        raise HTTPException(404, 'user not found')
    names = {u: db.get(MediaUser, u).username for u in uids}

    profs: dict[str, dict] = {}
    for u in uids:
        p = _taste.user_profile(db, u, top_n=0)
        if not p.get('ok'):
            raise HTTPException(404, 'profile failed')
        profs[u] = p

    def _shared(key: str) -> list[dict]:
        # имена с весом >0 у ВСЕХ выбранных, сортировка по среднему
        common = None
        for u in uids:
            ws = {n for n, w in (profs[u].get(key) or {}).items()
                  if n != '—' and float(w or 0) > 0}
            common = ws if common is None else (common & ws)
        out = []
        for name in (common or set()):
            ws = {u: round(float(profs[u][key][name]), 2) for u in uids}
            out.append({'name': name, 'weights': ws,
                        'avg': round(sum(ws.values()) / len(ws), 2)})
        out.sort(key=lambda x: x['avg'], reverse=True)
        return out

    shared_genres = _shared('preferredGenres')
    shared_artists = _shared('preferredArtists')

    # Общие лайкнутые треки (2+ из выбранных) с названиями
    like_sets = _cb._like_sets(db)
    sel_sets = {u: set(like_sets.get(u, set())) for u in uids}
    track_ids: set[str] = set()
    for s in sel_sets.values():
        track_ids |= s
    meta = {str(t.id): t for t in
            db.query(Track).filter(Track.id.in_(list(track_ids))).all()} \
        if track_ids else {}
    shared_tracks: list[dict] = []
    by_count: dict[int, list] = {}
    for tid in track_ids:
        likers = [u for u in uids if tid in sel_sets[u]]
        if len(likers) >= 2:
            t = meta.get(tid)
            by_count.setdefault(len(likers), []).append({
                'track_id': tid, 'title': t.title if t else '—',
                'artist_name': t.artist_name if t else None,
                'liked_by': [names[u] for u in likers]})
    for n in sorted(by_count, reverse=True):
        shared_tracks.extend(by_count[n])
    shared_tracks = shared_tracks[:20]

    # Попарные связи: косинус по лайкам + общих
    pairwise = []
    for i in range(len(uids)):
        for j in range(i + 1, len(uids)):
            a, b = uids[i], uids[j]
            sim = _cb._cosine(sel_sets[a], sel_sets[b])
            pairwise.append({'a': a, 'b': b, 'a_name': names[a],
                             'b_name': names[b], 'similarity': round(sim, 4),
                             'shared_likes': len(sel_sets[a] & sel_sets[b])})
    pairwise.sort(key=lambda x: x['similarity'], reverse=True)

    per_user = [{'user_id': u, 'username': names[u],
                 'likes': int((profs[u].get('counts') or {}).get('likes', 0) or 0),
                 'genres_top': [{'name': n, 'weight': round(float(w), 2)}
                                for n, w in sorted(
                                    ((k, v) for k, v in
                                     (profs[u].get('preferredGenres') or {}).items()
                                     if k != '—' and float(v or 0) > 0),
                                    key=lambda kv: kv[1], reverse=True)[:top_n]],
                 'artists_top': [{'name': n, 'weight': round(float(w), 2)}
                                 for n, w in sorted(
                                     ((k, v) for k, v in
                                      (profs[u].get('preferredArtists') or {}).items()
                                      if k != '—' and float(v or 0) > 0),
                                     key=lambda kv: kv[1], reverse=True)[:top_n]]}
                for u in uids]
    return {'ok': True, 'users': per_user, 'shared_genres': shared_genres,
            'shared_artists': shared_artists, 'shared_tracks': shared_tracks,
            'pairwise': pairwise}
