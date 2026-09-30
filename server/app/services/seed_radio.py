from __future__ import annotations

"""Радио по сиду: «волна по Eminem», «волна по треку».

Идея: Navidrome умеет похожих (`getSimilarSongs2` — узко, только свой каталог
и Last.fm), мозг — широко (жанры, кластеры, CLAP/аудио-косинус, коллаборативка,
скип-риск, UCB). Объединяем: серверные похожие идут ДОБОРОМ в пул кандидатов,
а ранжирует общий скоринг волны (`score_candidates`) от центроида сида.
Сид записывается в `wave_seeds` — главная показывает капсулу, мозг отдаёт сид
и докручивает ту же волну, а не начинает новую.
"""

from sqlalchemy import func


def _playable_query(db, *entities):
    """Запрос только по играбельному: плеер — источник истины (как везде)."""
    from app.db.models import Track
    from app.services import playable as _pl

    q = db.query(*entities)
    try:
        q = _pl.apply(q, db)
    except Exception:
        q = q.filter(
            ~Track.external_id.like("disk:%"),
            ~Track.external_id.like("demo-%"),
        )
    return q


def _artist_top_tracks(db, artist_name: str, limit: int = 200) -> list:
    from app.db.models import Track

    return (
        _playable_query(db, Track)
        .filter(Track.artist_name == artist_name)
        .order_by(Track.play_count.desc().nullslast())
        .limit(limit)
        .all()
    )


def _similar_artist_names(db, artist_name: str, limit: int = 6) -> list[str]:
    """Похожие артисты локально: те же жанры (+2), общие кластеры (+3).

    Упрощённый вариант `/api/library/artists/similar` без серверной добивки —
    сервер идёт отдельно целыми треками (см. ниже), а не именами.
    """
    from collections import Counter

    from app.db.models import Track, TrackCluster

    grows = (
        db.query(Track.genre, func.count(Track.id))
        .filter(Track.artist_name == artist_name, Track.genre.isnot(None))
        .group_by(Track.genre)
        .order_by(func.count(Track.id).desc())
        .limit(3)
        .all()
    )
    genres = [g for g, _ in grows]
    own_ids = [
        str(r[0])
        for r in db.query(Track.id).filter(Track.artist_name == artist_name).limit(2000).all()
    ]
    cluster_ids: list[int] = []
    if own_ids:
        cluster_ids = [
            c
            for c, _ in db.query(TrackCluster.cluster_id, func.count(TrackCluster.track_id))
            .filter(TrackCluster.track_id.in_(own_ids))
            .group_by(TrackCluster.cluster_id)
            .order_by(func.count(TrackCluster.track_id).desc())
            .limit(2)
            .all()
        ]
    in_cluster: set[str] = set()
    if cluster_ids:
        in_cluster = {
            str(r[0])
            for r in db.query(TrackCluster.track_id)
            .filter(TrackCluster.cluster_id.in_(cluster_ids))
            .limit(5000)
            .all()
        }
    cand_q = db.query(Track.artist_name, Track.genre, Track.id).filter(
        Track.artist_name.isnot(None), Track.artist_name != artist_name
    )
    if genres:
        cand_q = cand_q.filter(Track.genre.in_(genres))
    scores: Counter = Counter()
    for aname, g, tid in cand_q.limit(5000).all():
        if not aname:
            continue
        if g in genres:
            scores[aname] += 2
        if str(tid) in in_cluster:
            scores[aname] += 3
    return [n for n, _ in scores.most_common(limit)]


def track_radio(user_id: str, track_ref: str, n: int = 30) -> dict:
    """Очередь «волна по треку»: центроид трека + кластер + сессионные ассоциации.

    Движок — `cold_start_playlist(seed_track_id)`: audio/CLAP-косинус к сиду,
    бонус своему кластеру, новизна, дизлайки/баны мимо. Серверные похожие
    (getSimilarSongs2 по external_id сида) идут добором в пул, как у артиста.
    Возвращает {tracks, label, seed_track_id, server_used}.
    """
    from app.db.database import session_scope
    from app.services.track_resolve import get_track as _gt

    ref = (track_ref or "").strip()
    if not ref:
        return {"tracks": [], "error": "empty track"}
    with session_scope() as db:
        t = _gt(db, ref)
        if t is None:
            return {"tracks": [], "error": "track not found"}
        seed_id = str(t.id)
        seed_ext = str(t.external_id or "")
        server_id = str(t.server_id)
        artist = str(t.artist_name or "")
        title = str(t.title or "")
    label = f"{artist} — {title}".strip(" —") or title or seed_id

    from app.services.ml import cold_start_playlist as _cold

    try:
        res = _cold(server_id, n=n * 2, user_id=user_id, seed_track_id=seed_id)
        base = [str(x) for x in (res.get("tracks") or [])]
    except Exception:
        base = []
    if seed_id in base:
        base.remove(seed_id)
    # Серверный добор: похожие на сид → наши id (мозг шире, сервер уже).
    server_used = False
    if seed_ext and not seed_ext.startswith(("disk:", "demo-")):
        srv_ids, server_used = _server_seed_tracks_db(seed_ext)
        for tid in srv_ids:
            if tid != seed_id and tid not in base:
                base.append(tid)
    # Сид первым номером — энергетический мостик (как просил: трек-ядро впереди).
    tracks = [seed_id] + [t for t in base if t != seed_id]
    return {"tracks": tracks[: max(n, 1)], "label": label,
            "seed_track_id": seed_id, "server_used": server_used}


def _tuning_int(db, user_id: str | None, key: str, default: int,
                lo: int, hi: int) -> int:
    """Одно число из личного тюнинга. Ошибка/мусор -> дефолт."""
    try:
        from app.services import rec_tuning as _rt

        v = (_rt.get_all(db, user_id).get("playlists") or {}).get(
            key, default)
        return max(lo, min(hi, int(float(v if v is not None else default))))
    except Exception:
        return default


def _server_seed_tracks(db, seed_external_id: str,
                        count: int = 20) -> tuple[list[str], bool]:
    """Треки Navidrome-похожих, спроецированные на наши id. (True — сервер участвовал.)"""
    try:
        from app.api.library import _server_similar_songs
    except Exception:
        return [], False
    try:
        songs = _server_similar_songs(seed_external_id, count=count)
    except Exception:
        return [], False
    if not songs:
        return [], False
    from app.db.models import Track

    ext_ids = [str(s.get("id") or "") for s in songs if s.get("id")]
    if not ext_ids:
        return [], False
    rows = db.query(Track.id).filter(Track.external_id.in_(ext_ids)).all()
    return [str(r[0]) for r in rows], True


def _server_seed_tracks_db(seed_external_id: str) -> tuple[list[str], bool]:
    """Тот же добор, когда сессии нет под рукой (track_radio живёт в своих)."""
    from app.db.database import session_scope

    with session_scope() as db:
        return _server_seed_tracks(db, seed_external_id)


def artist_radio(db, user_id: str, artist_name: str, n: int = 30) -> dict:
    """Очередь «волна по артисту»: его треки (ядро) + похожие + серверные.

    Возвращает {tracks, seed_ids, similar_artists, server_used, label}.
    Пустой tracks — артиста нет в играбельной библиотеке.
    """
    from app.db.models import ArtistBan, Track, TrackDislike

    artist_name = (artist_name or "").strip()
    if not artist_name:
        return {"tracks": [], "error": "empty artist"}
    own = _artist_top_tracks(db, artist_name)
    if not own:
        return {"tracks": [], "error": "artist not in library"}
    seed_ids = [str(t.id) for t in own[:5]]

    pool: list[str] = [str(t.id) for t in own]
    seen = set(pool)
    _n_sim = _tuning_int(db, user_id, "radio_similar", 6, 1, 20)
    _n_per = _tuning_int(db, user_id, "radio_per_similar", 15, 1, 100)
    similar = _similar_artist_names(db, artist_name, limit=_n_sim)
    for aname in similar:
        for t in _artist_top_tracks(db, aname, limit=_n_per):
            tid = str(t.id)
            if tid not in seen:
                seen.add(tid)
                pool.append(tid)

    server_used = False
    seed_ext: str | None = None
    for t in own:
        ext = str(t.external_id or "")
        if ext and not ext.startswith(("disk:", "demo-")):
            seed_ext = ext
            break
    if seed_ext:
        _n_srv = _tuning_int(db, user_id, "radio_server", 20, 0, 100)
        srv_ids, server_used = _server_seed_tracks(db, seed_ext,
                                                  count=_n_srv)
        for tid in srv_ids:
            if tid not in seen:
                seen.add(tid)
                pool.append(tid)
    pool = pool[:1500]

    # Дизлайки/баны — мимо очереди (как cold_start).
    try:
        dis = {str(r[0]) for r in db.query(TrackDislike.track_id).filter_by(user_id=user_id).all()}
        bans = {str(r.artist_name or "").lower() for r in db.query(ArtistBan).filter_by(user_id=user_id).all()}
    except Exception:
        dis, bans = set(), set()
    if dis or bans:
        keep_pool = []
        by_id = {str(t.id): t for t in db.query(Track).filter(Track.id.in_(pool[:2000])).all()}
        for tid in pool:
            if tid in dis:
                continue
            a = str((by_id.get(tid).artist_name if by_id.get(tid) else "") or "").lower()
            if a and a in bans:
                continue
            keep_pool.append(tid)
        pool = keep_pool

    from app.services import wave as _wave

    try:
        from app.services import wave_settings as _wset

        settings = _wset.effective_settings(db, user_id, None)
    except Exception:
        settings = {}
    ranked = _wave.score_candidates(
        db, user_id, pool, seed_ids, settings,
        jitter_seed=f"{user_id}:seed:{artist_name}",
    )
    ordered = [str(r.get("track_id") or r.get("id")) for r in ranked[: max(n * 3, n)]]
    ordered = [t for t in ordered if t][:n]

    # Энергетическая арка поверх (как daily/smart) — радио слушается волной.
    try:
        from app.services.orchestrator import create_energy_wave

        tracks = [t for t in (db.get(Track, tid) for tid in ordered) if t]
        waved = create_energy_wave(tracks)
        order = {str(t.id): i for i, t in enumerate(waved)}
        ordered = sorted(ordered, key=lambda tid: order.get(tid, 999))
    except Exception:
        pass
    return {
        "tracks": ordered,
        "seed_ids": seed_ids,
        "similar_artists": similar,
        "server_used": server_used,
        "label": artist_name,
    }
