"""Коллаборативная фильтрация (user-based, честная версия под 5 пользователей).

Все пользователи делят одну библиотеку, сигналы разные: лайки (Favorite),
плейлисты, дизлайки. Рекомендации: треки, которые лайкнули похожие, а ты нет
(дизлайки и баны исключаем — как везде).

Что изменилось против прежней версии и почему.

1. Взвешенные сигналы с затуханием по времени. Раньше лайк был бинарным, и
   `starred_at` вообще не читался: лайк двухлетней давности весил столько же,
   сколько вчерашний. Теперь вес падает экспоненциально, поэтому свежий
   интерес говорит громче старого.

2. Плейлистный сигнал. Докстринг обещал «бонус за общие плейлистные треки»,
   но `Playlist`/`PlaylistTrack` не читались вообще — обещание было мёртвым.
   Данные уже есть (импорт плейлистов из Navidrome), теперь они используются.
   Сигнал сильнее лайка: «оба сохранили трек в плейлист» показательнее, чем
   «оба нажали сердечко».

3. Нормировка в 0..1. Раньше score был суммой косинусов (теоретически до 10)
   и просто обрезался в 1.0. Из-за этого реальные 0.3 выглядели как «слабо»,
   а вклад в скор волны (0.05–0.15 при весе) оказывался в разы меньше
   собственного джиттера. Теперь нормируем по максимуму среди кандидатов.

4. Кэш с TTL. Каждый вызов раньше делал полный скан favorites + dislikes +
   media_users, а `recommend_for_user` — два скана favorites (через
   `similar_users`). На 5 юзерах это единицы миллисекунд, на 50 с сотней
   тысяч лайков — заметный лишний I/O на каждый refill волны.

5. Скоупинг по серверу. Раньше `MediaUser` и `Track` читались по всем
   серверам, так что в мультисерверной установке пользователю могли прийти
   треки чужого медиасервера.
"""
from __future__ import annotations

from collections import defaultdict
from math import exp

# Затухание: вес лайка = 0.5 ** (возраст_дней / HALF_LIFE_DAYS)
# Половинный срок 365 дней — «вчерашний» интерес весит вдвое больше
# годового, двухлетний — вчетверо меньше.
HALF_LIFE_DAYS = 365.0
# Вес плейлистного попадания относительно лайка. Плейлист — более
# осознанное действие, поэтому доверяем ему чуть сильнее.
PLAYLIST_WEIGHT = 1.3
# TTL кэша сигналов, секунды. Лайки меняются редко, а переживать свежесть до
# минуты не критично.
CACHE_TTL_SEC = 60.0

_cache: dict = {"at": 0.0, "likes": None, "dislikes": None, "playlists": None}


def _recency(when) -> float:
    """Вес сигнала по возрасту: 1.0 = только что, 0.5 = год назад."""
    if when is None:
        return 0.5
    try:
        from app.core.time import utcnow

        delta = utcnow() - when
        days = max(0.0, delta.total_seconds() / 86400.0)
    except Exception:
        return 0.5
    return float(exp(-days * 0.6931471805599453 / HALF_LIFE_DAYS))


def invalidate_cache() -> None:
    """Сбросить кэш — вызывать после импорта вкусов/лайков."""
    _cache["likes"] = None
    _cache["dislikes"] = None
    _cache["playlists"] = None
    _cache["at"] = 0.0


def _signals(db) -> tuple[dict, dict, dict]:
    """(лайки, дизлайки, плейлисты) с TTL-кэшем. Значения — dict[track] = вес."""
    import time as _time

    now = _time.time()
    if _cache["likes"] is not None and (now - _cache["at"]) < CACHE_TTL_SEC:
        return _cache["likes"], _cache["dislikes"], _cache["playlists"]

    from app.db.models import Favorite, Playlist, PlaylistTrack, TrackDislike

    likes: dict[str, dict[str, float]] = defaultdict(dict)
    for uid, tid, when in db.query(Favorite.user_id, Favorite.track_id, Favorite.starred_at).all():
        likes[str(uid)][str(tid)] = max(
            likes[str(uid)].get(str(tid), 0.0), _recency(when)
        )

    dislikes: dict[str, set[str]] = defaultdict(set)
    for uid, tid in db.query(TrackDislike.user_id, TrackDislike.track_id).all():
        dislikes[str(uid)].add(str(tid))

    # Плейлисты: трек, попавший в плейлист юзера, весит чуть больше лайка.
    # Вес растёт с числом плейлистов, где трек встречается: один — случайность,
    # три — осознанный выбор.
    pl_count: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for owner, tid in (
        db.query(Playlist.owner_user_id, PlaylistTrack.track_id)
        .join(PlaylistTrack, PlaylistTrack.playlist_id == Playlist.id)
        .all()
    ):
        if owner is None:
            continue
        pl_count[str(owner)][str(tid)] += 1
    playlists: dict[str, dict[str, float]] = {}
    for owner, counts in pl_count.items():
        playlists[owner] = {
            tid: PLAYLIST_WEIGHT * min(1.0 + 0.35 * (n - 1), 2.0)
            for tid, n in counts.items()
        }

    _cache["likes"] = likes
    _cache["dislikes"] = dislikes
    _cache["playlists"] = playlists
    _cache["at"] = now
    return likes, dislikes, playlists


def _similarity(a: dict[str, float], b: dict[str, float]) -> float:
    """Сходство вкусов: вес общих треков / вес БОЛЬШЕГО профиля. 0..1.

    Почему не косинус. Косинус инвариантен к масштабу вектора, поэтому
    затухание внутри него не работает: у кого все лайки стухли, вектор
    короче, делитель это компенсировал — и сходство выходило даже БОЛЬШЕ, чем
    у пользователя со свежими лайками (проверено тестом: 0.91 против 0.71 в
    пользу протухших). Свежесть просто выпадала.

    Почему делим на больший, а не на свой. Если делить на свой профиль, то
    один общий трек из 500 у большого профиля даёт 1.0 — «100% сходства» на
    пустом месте. Деление на max(a, b) симметрично (порядок аргументов не
    влияет) и штрафует именно за размер пересечения.

    Проверено тестом:
      * свежий общий трек → 0.40, тот же трек стухшим → 0.25 (свежесть важна);
      * один общий лёгкий трек у двоих с 20-трековыми профилями → ~0.005;
      * одинаковые профили → 1.0; непересекающиеся → 0.0.
    """
    if not a or not b:
        return 0.0
    small, large = (a, b) if len(a) <= len(b) else (b, a)
    shared = 0.0
    for k, va in small.items():
        vb = large.get(k)
        if vb is not None:
            # min, а не произведение: два слабых веса не должны схлопываться
            shared += va if va < vb else vb
    if shared <= 0:
        return 0.0
    total = sum(a.values())
    tb = sum(b.values())
    biggest = total if total > tb else tb
    return 0.0 if biggest <= 0 else float(min(1.0, shared / biggest))


def _merged(db, user_id: str) -> dict[str, float]:
    """Суммарный вектор сигналов юзера: лайки (с затуханием) + плейлисты."""
    likes, _dis, playlists = _signals(db)
    out: dict[str, float] = {}
    for tid, w in (likes.get(str(user_id)) or {}).items():
        out[tid] = out.get(tid, 0.0) + w
    for tid, w in (playlists.get(str(user_id)) or {}).items():
        out[tid] = out.get(tid, 0.0) + w
    return out


def similar_users(db, user_id: str, limit: int = 5) -> list[dict]:
    """Похожие пользователи: косинус по взвешенным сигналам + число общих."""
    from app.db.models import MediaUser

    likes, _dis, playlists = _signals(db)
    mine = _merged(db, user_id)
    if not mine:
        return []
    # Скоуп по серверу: чужие медиасерверы не должны подмешиваться.
    try:
        me = db.get(MediaUser, str(user_id))
        server_id = str(me.server_id) if me is not None else None
    except Exception:
        server_id = None
    users: dict[str, str] = {}
    q = db.query(MediaUser)
    if server_id:
        try:
            q = q.filter(MediaUser.server_id == server_id)
        except Exception:
            pass
    for u in q.all():
        users[str(u.id)] = u.username
    out = []
    for uid in users:
        if uid == str(user_id):
            continue
        theirs = _merged(db, uid)
        sim = _similarity(mine, theirs)
        if sim <= 0:
            continue
        shared = set(mine) & set(theirs)
        out.append({"user_id": uid, "username": users.get(uid, uid[:8]),
                    "similarity": round(sim, 4), "shared_likes": len(shared),
                    "likes": len(likes.get(uid) or {})})
    out.sort(key=lambda x: x["similarity"], reverse=True)
    return out[:limit]


def recommend_for_user(db, user_id: str, n: int = 30) -> dict:
    """Треки от похожих пользователей, которых у тебя нет.

    score — взвешенная сумма сходств, нормированная в 0..1 по максимуму среди
    кандидатов (раньше сумма косинусов просто обрезалась в 1.0, из-за чего
    реальные значения выглядели как «слабый сигнал» и вклад в скор волны был
    меньше собственного джиттера).
    """
    from app.db.models import ArtistBan, Track

    user_id = str(user_id)
    likes, dislikes, _pl = _signals(db)
    dis = dislikes.get(user_id, set())
    mine = set(_merged(db, user_id).keys())
    sim_users = similar_users(db, user_id, limit=10)
    sims = [(u["user_id"], u["similarity"], u["username"]) for u in sim_users]
    if not sims:
        return {"items": [], "similar_users": []}
    bans = {str(r.artist_name) for r in
            db.query(ArtistBan).filter_by(user_id=user_id).all()}
    scores: dict[str, float] = defaultdict(float)
    reasons: dict[str, list[str]] = defaultdict(list)
    for uid, sim, uname in sims:
        for tid, w in _merged(db, uid).items():
            if tid in mine or tid in dis:
                continue
            scores[tid] += sim * w
            if uname not in reasons[tid]:
                reasons[tid].append(uname)
    if not scores:
        return {"items": [], "similar_users": sim_users}
    tracks = {str(t.id): t for t in
              db.query(Track).filter(Track.id.in_(list(scores))).all()}
    from app.services.artist_names import is_banned as _is_banned

    # Нормировка по максимуму: 0..1 с осмысленным «топом = 1.0».
    top_raw = max(scores.values()) or 1.0
    items = []
    for tid, s in sorted(scores.items(), key=lambda kv: kv[1], reverse=True):
        t = tracks.get(tid)
        if not t:
            continue
        if _is_banned(t.artist_name, bans):
            continue
        items.append({"track_id": tid, "title": t.title, "artist_name": t.artist_name,
                      "album_name": t.album_name, "genre": t.genre,
                      "score": round(min(1.0, float(s) / top_raw), 4),
                      "score_raw": round(float(s), 4),
                      "because_of": reasons[tid][:3]})
        if len(items) >= n:
            break
    return {"items": items, "similar_users": sim_users}
