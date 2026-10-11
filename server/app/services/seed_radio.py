from __future__ import annotations

"""Радио по сиду: «волна по Eminem», «волна по треку».

Идея: Navidrome умеет похожих (`getSimilarSongs2` — узко, только свой каталог
и Last.fm), мозг — широко (жанры, кластеры, CLAP/аудио-косинус, коллаборативка,
скип-риск, UCB). Объединяем: серверные похожие идут ДОБОРОМ в пул кандидатов,
а ранжирует общий скоринг волны (`score_candidates`) от центроида сида — так
теперь работают ОБЕ ветки, и по треку, и по артисту. Сид записывается в
`wave_seeds` — главная показывает капсулу, мозг отдаёт сид и докручивает ту же
волну, а не начинает новую.

Имя артиста сравнивается нормализованно (`artist_names.norm_name`): в PostgreSQL
`=` регистрозависим, и без этого «Eminem»/«eminem» давали пустую волну.
"""

from sqlalchemy import func


def _log_warning(msg: str, *args) -> None:
    try:
        from app.core.logging import get_logger as _gl

        _gl("wave").warning(msg, *args)
    except Exception:
        pass


def _log_info(msg: str, *args) -> None:
    try:
        from app.core.logging import get_logger as _gl

        _gl("wave").info(msg, *args)
    except Exception:
        pass


def _filter_dislikes_bans(db, user_id: str, pool: list[str]) -> list[str]:
    """Выкинуть дизлайки и забаненных артистов из пула.

    Общая часть для обоих радио: раньше она жила только в artist_radio, и волна
    по треку могла подсунуть забаненного артиста.
    """
    from app.db.models import ArtistBan, Track, TrackDislike

    try:
        dis = {str(r[0]) for r in
               db.query(TrackDislike.track_id).filter_by(user_id=user_id).all()}
        bans = {str(r.artist_name or "").lower() for r in
                db.query(ArtistBan).filter_by(user_id=user_id).all()}
        try:
            from app.services.artist_names import load_bans as _lb

            _bn, _bi = _lb(db, user_id)
            bans |= {b.lower() for b in _bn}
            ban_ids = set(_bi)
        except Exception:
            ban_ids = set()
    except Exception:
        return list(pool)
    if not (dis or bans or ban_ids):
        return list(pool)
    by_id = {str(t.id): t for t in
             db.query(Track).filter(Track.id.in_(pool[:2000])).all()}
    keep: list[str] = []
    for tid in pool:
        if tid in dis:
            continue
        bt = by_id.get(tid)
        a = str((bt.artist_name if bt else "") or "").lower()
        if a and a in bans:
            continue
        aid = str((bt.artist_external_id if bt else "") or "")
        if aid and aid in ban_ids:
            continue
        keep.append(tid)
    return keep


def _seed_cluster_pool(db, seed_id: str, limit: int = 400) -> list[str]:
    """Резервный пул: треки того же кластера, что и сид.

    Нужен, когда cold_start не дал кандидатов (например, у сида нет
    проанализированных фич — тогда seed_vec=None и волна по треку вырождалась
    в общую холодную волну, а иногда и в пару треков).
    """
    from app.db.models import TrackCluster

    cid = db.query(TrackCluster.cluster_id).filter(
        TrackCluster.track_id == seed_id
    ).first()
    if not cid:
        return []
    return [str(r[0]) for r in
            db.query(TrackCluster.track_id).filter(
                TrackCluster.cluster_id == cid[0]
            ).limit(limit).all()]


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
    """Треки артиста: сперва точное имя, при промахе — регистронезависимо.

    Раньше был только `Track.artist_name == artist_name`. В PostgreSQL `=` для
    текста регистрозависим, поэтому клиент, приславший "eminem" к "Eminem" (или
    отличающийся пробелами), получал пустой результат, а эндпоинт отдавал 404
    «artist not in library» — то есть «волна по артисту не работает».
    Нормализатор в проекте уже есть, `artist_names.norm_name`.
    """
    from app.db.models import Track
    from app.services.artist_names import norm_name

    q = _playable_query(db, Track)
    rows = (
        q.filter(Track.artist_name == artist_name)
        .order_by(Track.play_count.desc().nullslast())
        .limit(limit)
        .all()
    )
    if rows or not (artist_name or "").strip():
        return rows
    want = norm_name(artist_name)
    if not want:
        return []
    return (
        q.filter(func.lower(func.trim(Track.artist_name)) == want)
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
    from app.services.artist_names import norm_name

    # Имя артиста сравниваем нормализованно (см. _artist_top_tracks): иначе
    # «Eminem» от «eminem» терял и жанры, и свои треки, и похожих артистов.
    self_norm = norm_name(artist_name)
    cmp_col = func.lower(func.trim(Track.artist_name))

    grows = (
        db.query(Track.genre, func.count(Track.id))
        .filter(cmp_col == self_norm, Track.genre.isnot(None))
        .group_by(Track.genre)
        .order_by(func.count(Track.id).desc())
        .limit(3)
        .all()
    )
    genres = [g for g, _ in grows]
    own_ids = [
        str(r[0])
        for r in db.query(Track.id).filter(cmp_col == self_norm).limit(2000).all()
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
        Track.artist_name.isnot(None), cmp_col != self_norm
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


def track_radio(db, user_id: str, track_ref: str, n: int = 30) -> dict:
    """Очередь «волна по треку»: центроид трека + кластер + скоринг волны.

    Раньше движком был `cold_start_playlist(seed_track_id)`, и ранжирования волны
    не было вовсе — при том, что доктрина модуля обещает «ранжирует общий скоринг
    волны (score_candidates) от центроида сида». Теперь как у артиста: кандидаты
    (холодный старт + серверные похожие) → дизлайки/баны мимо → score_candidates
    → энергетическая арка на стороне эндпоинта.

    Пул кандидатов расширен, а не обрезан: раньше тут был ровно один вызов
    cold_start на n*2, и если он падал — тихо возвращалось несколько треков без
    объяснения. Теперь ошибка логируется, и есть запасной пул.

    Возвращает {tracks, label, seed_track_id, server_used, pool_size}.
    """
    from app.services.track_resolve import get_track as _gt

    ref = (track_ref or "").strip()
    if not ref:
        return {"tracks": [], "error": "empty track"}
    t = _gt(db, ref)
    if t is None:
        return {"tracks": [], "error": "track not found"}
    seed_id = str(t.id)
    seed_ext = str(t.external_id or "")
    server_id = str(t.server_id)
    artist = str(t.artist_name or "")
    title = str(t.title or "")
    label = f"{artist} — {title}".strip(" —") or title or seed_id

    base: list[str] = []
    cold_error = ""
    try:
        from app.services.ml import cold_start_playlist as _cold

        # cold_start_playlist держит свою сессию — из db вызывающего не читаем.
        res = _cold(server_id, n=n * 2, user_id=user_id, seed_track_id=seed_id)
        base = [str(x) for x in (res.get("tracks") or [])]
    except Exception as e:  # noqa: BLE001 — раньше это просто роняло пул в []
        cold_error = str(e)
        _log_warning("track_radio: cold_start упал, пойдём на резервный пул: {}", e)
    if seed_id in base:
        base.remove(seed_id)

    # Серверный добор: похожие на сид → наши id (мозг шире, сервер уже).
    server_used = False
    if seed_ext and not seed_ext.startswith(("disk:", "demo-")):
        srv_ids, server_used = _server_seed_tracks_db(seed_ext)
        for tid in srv_ids:
            if tid != seed_id and tid not in base:
                base.append(tid)

    # Резервный пул, если основной путь не дал почти ничего. Без этого «волна
    # по треку» могла вернуть 3–10 треков вместо 30, и непонятно было почему.
    if len(base) < n:
        try:
            for tid in _seed_cluster_pool(db, seed_id):
                if tid != seed_id and tid not in base:
                    base.append(tid)
        except Exception as e:  # noqa: BLE001
            _log_warning("track_radio: резервный пул по кластеру не собрался: {}", e)

    # Дизлайки/баны — мимо. Раньше это делал только artist_radio, и волна по
    # треку могла подсунуть забаненного артиста.
    base = _filter_dislikes_bans(db, user_id, base)
    base = [t_ for t_ in base if t_ != seed_id][: max(n * 3, n)]

    # Общий скоринг волны от центроида сида — как у артиста.
    ordered = base
    try:
        from app.services import wave as _wave
        from app.services import wave_settings as _wset

        settings = _wset.effective_settings(db, user_id, None)
        ranked = _wave.score_candidates(
            db, user_id, base, [seed_id], settings,
            jitter_seed=f"{user_id}:seed:{seed_id}",
        )
        got = [str(r.get("track_id") or r.get("id")) for r in ranked]
        got = [x for x in got if x]
        # Скоринг не должен выкинуть то, чего он не видел: пул уже собран.
        for tid in base:
            if tid not in got:
                got.append(tid)
        ordered = got[: max(n * 3, n)]
    except Exception as e:  # noqa: BLE001
        _log_warning("track_radio: score_candidates упал, отдаю пул как есть: {}", e)

    _log_info(
        "track_radio: сид={} кандидатов={} итог={} cold_error={}",
        label, len(base), min(len(ordered), max(n, 1)), cold_error or "-",
    )
    # Сид первым номером — энергетический мостик (трек-ядро впереди).
    tracks = [seed_id] + [t_ for t_ in ordered if t_ != seed_id]
    return {"tracks": tracks[: max(n, 1)], "label": label,
            "seed_track_id": seed_id, "server_used": server_used,
            "pool_size": len(base)}


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


def _interleave_by_artist(ordered: list[str], artist_of: dict[str, str],
                          seed_artist: str = "", n: int = 30,
                          cap: int = 3) -> list[str]:
    """Круговая перетасовка очереди по артистам. Сид-артист — первым.

    Зачем: плоский скоринг по пулу (сид + похожие) НЕ даёт радио. Сиды —
    это треки самого артиста, поэтому треки похожих к ним заведомо ближе по
    звуку и выигрывают в audio/cluster. На выходе шла подборка из 2-3
    артистов по кругу, а штраф за повтор (0.05/0.10/0.15) слишком мал,
    чтобы это остановить.

    Тут порядок не доверяется скорингу:
      * корзина сид-артиста идёт первой — радио начинается с него;
      * дальше round-robin по остальным корзинам в порядке их лучшего трека;
      * не больше `cap` треков от одного артиста;
      * кап ослабляется на ходу, если артистов мало и очередь не набралась —
        но НАКОПЛИВАЕТСЯ в тот же список, а не начинается заново (иначе
        ранее выбранное терялось и на выходе был обрезок).
    """
    from collections import Counter, defaultdict

    buckets: dict[str, list[str]] = defaultdict(list)
    for tid in ordered:
        a = (artist_of.get(tid) or "").strip().lower()
        buckets[a].append(tid)

    keys = list(buckets.keys())
    seed_key = ""
    if seed_artist:
        want = seed_artist.strip().lower()
        seed_key = next((k for k in keys if k == want), "")
    if seed_key:
        keys = [seed_key] + [k for k in keys if k != seed_key]

    target = min(n, len(ordered))
    ceiling = max(len(ordered), cap)  # выше кап не нужен — всё равно всё
    out: list[str] = []
    taken: Counter = Counter()
    per = max(1, cap)
    while len(out) < target:
        progressed = False
        for k in keys:
            if len(out) >= target:
                break
            b = buckets[k]
            if not b or taken[k] >= per:
                continue
            out.append(b.pop(0))
            taken[k] += 1
            progressed = True
        if not progressed:
            if per >= ceiling:
                break
            per = min(per + 2, ceiling)
    return out


def artist_radio(db, user_id: str, artist_name: str, n: int = 30) -> dict:
    """Очередь «волна по артисту»: его треки (ядро) + похожие + серверные.

    Возвращает {tracks, seed_ids, similar_artists, server_used, label}.
    Пустой tracks — артиста нет в играбельной библиотеке.
    """
    from app.db.models import Track

    artist_name = (artist_name or "").strip()
    if not artist_name:
        return {"tracks": [], "error": "empty artist"}
    own = _artist_top_tracks(db, artist_name)
    if not own:
        # Раньше здесь был молчаливый 404 «artist not in library» — клиенту
        # казалось, что волна по артисту не работает. Чаще всего артист просто
        # не совпадал по регистру/пробелам. Теперь причина в логе и в теле.
        _log_warning(
            "artist_radio: «{}» не найден в играбельной библиотеке "
            "(проверь регистр/пробелы в имени)", artist_name,
        )
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

    # Дизлайки/баны — мимо очереди (как cold_start). Общая реализация с
    # волной по треку: правило не должно расходиться между ветками.
    pool = _filter_dislikes_bans(db, user_id, pool)

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

    # Мета заказанных треков — нужна артистам для перетасовки ниже.
    artist_of: dict[str, str] = {}
    for i in range(0, len(ordered), 500):
        chunk = [t for t in ordered[i:i + 500] if t]
        if not chunk:
            continue
        try:
            for t in db.query(Track).filter(Track.id.in_(chunk)).all():
                artist_of[str(t.id)] = str(t.artist_name or "")
        except Exception:
            continue

    # Энергетическая арка поверх (как daily/smart) — радио слушается волной.
    try:
        from app.services.orchestrator import create_energy_wave

        tracks = [t for t in (db.get(Track, tid) for tid in ordered) if t]
        waved = create_energy_wave(tracks)
        order = {str(t.id): i for i, t in enumerate(waved)}
        ordered = sorted(ordered, key=lambda tid: order.get(tid, 999))
    except Exception:
        pass

    # Перетасовка по артистам — ПОСЛЕ дуги. Раньше её не было вовсе, и
    # результат был «3 артиста по кругу»: сид-артист вытеснялся похожими
    # (они ближе по звуку к его же сидам), а штраф за повтор 0.05/0.10/0.15
    # не мог этому помешать. Дуга выравнивает энергию, круг удерживает
    # разнообразие — по отдельности ни то, ни другое.
    try:
        _cap = _tuning_int(db, user_id, "radio_per_artist", 3, 1, 20)
        ordered = _interleave_by_artist(
            ordered, artist_of, seed_artist=artist_name, n=n, cap=_cap,
        )
    except Exception:
        pass
    return {
        "tracks": ordered,
        "seed_ids": seed_ids,
        "similar_artists": similar,
        "server_used": server_used,
        "label": artist_name,
    }
