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

from typing import Any

# Кэш векторного добора: {(сиды, лимит): (ts, [track_id])}, TTL 15 минут
# (см. _vector_neighbor_pool). Процесс-локально, best-effort.
_VEC_POOL_CACHE: dict[tuple, tuple[float, list[str]]] = {}


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


def _genre_tokens(g: str) -> set[str]:
    """Токены жанра для мягкого матча (длина ≥3, мусор вроде «nu» отлетает)."""
    return {t.strip().lower() for t in
            str(g or "").replace("/", " ").replace("-", " ").split()
            if len(t.strip()) >= 3}


def _genre_score(seed_g: set[str], seed_toks: set[str], cand: str) -> int:
    """Вес жанрового совпадения: точное = 2, токен/сабстринг = 1, мимо = 0.

    Сабстринг нужен рваным тегам без пробела: сид «numetal» одним словом
    иначе не матчится ни с «nu metal», ни с «alternative metal»
    (токен «metal» ⊂ «numetal»).
    Порог длины 4 — чтобы «pop» не цеплял «popular», а «rock» — «brock».
    """
    _gl = str(cand or "").strip().lower()
    if not _gl:
        return 0
    if _gl in seed_g:
        return 2
    if seed_toks:
        _ct = _genre_tokens(_gl)
        if _ct & seed_toks:
            return 1
        for _t in seed_toks:
            if len(_t) < 4:
                continue
            if _t in _gl or _gl in _t:
                return 1
    return 0


def _similar_artist_names(db, artist_name: str, limit: int = 6) -> list[str]:
    """Похожие артисты локально: те же жанры (+2), общие кластеры (+3).

    Упрощённый вариант `/api/library/artists/similar` без серверной добивки —
    сервер идёт отдельно целыми треками (см. ниже), а не именами.

    Жанры сравниваем МЯГКО: точное совпадение (+2), иначе пересечение
    токенов («alternative metal» ~ «nu metal» через «metal», +1).
    Раньше было только точное `genre IN (...)` — при рваных тегах
    («Metal» vs «Alternative Metal» vs «metal») похожих находилось 1-2,
    и радио вырождалось в «2 артиста по кругу».
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
    genres = [g for g, _ in grows if g]
    # Нормализованные жанры сида + их токены для мягкого матча.
    _seed_g: set[str] = {str(g).strip().lower() for g in genres if str(g).strip()}
    _seed_toks: set[str] = set()
    for _g in _seed_g:
        _seed_toks |= _genre_tokens(_g)
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
    # Кандидатов берём ШИРЕ: без жёсткого фильтра по жанру (он и резал
    # похожих), скоринг сам разберёт. Случайный порядок — чтобы не есть
    # один и тот же кусок таблицы при каждом запросе.
    try:
        cand_rows = (db.query(Track.artist_name, Track.genre, Track.id)
                     .filter(Track.artist_name.isnot(None), cmp_col != self_norm)
                     .order_by(func.random()).limit(8000).all())
    except Exception:
        cand_rows = (db.query(Track.artist_name, Track.genre, Track.id)
                     .filter(Track.artist_name.isnot(None), cmp_col != self_norm)
                     .limit(8000).all())
    scores: Counter = Counter()
    for aname, g, tid in cand_rows:
        if not aname:
            continue
        scores[aname] += _genre_score(_seed_g, _seed_toks, g)
        if str(tid) in in_cluster:
            scores[aname] += 3
    ranked = [n for n, _ in scores.most_common(limit)]
    # Добивка: если похожих мало (рваные жанры, нет кластеров) — добираем
    # самыми играемыми артистами с тем же жанр-токеном, иначе радио
    # схлопывается в 2 артиста. Без жанра у сида — топ по plays.
    if len(ranked) < limit:
        # Нет вообще никакого сигнала (ни жанра у сида, ни общих кластеров) —
        # честно возвращаем то, что есть (часто пусто), а не топ библиотеки.
        # Иначе Linkin Park / Scooter / Coldplay выдавались за «похожих» на
        # всё подряд, и радио «в стиле Drowning Pool» играло Coldplay.
        if not _seed_g and not _seed_toks and not in_cluster:
            _log_warning("similar: у «{}» нет жанра и кластеров — "
                         "похожих не знаем, топ библиотеки не подсовываем",
                         artist_name)
            return ranked[:limit]
        try:
            _have = {norm_name(n) for n in ranked} | {self_norm}
            # Артисты, делящие кластеры сида: второй шанс после жанра.
            # Одним запросом, а не N: кто вообще стоит в тех же «полках».
            _cluster_artists: set[str] = set()
            if in_cluster:
                try:
                    _cluster_artists = {
                        norm_name(r[0]) for r in
                        db.query(Track.artist_name).filter(
                            Track.id.in_(list(in_cluster)[:2000])).all()
                        if r[0] and norm_name(r[0]) not in _have}
                except Exception:
                    _cluster_artists = set()
            _q = (db.query(Track.artist_name, func.count(Track.id))
                  .filter(Track.artist_name.isnot(None), cmp_col != self_norm)
                  .group_by(Track.artist_name)
                  .order_by(func.sum(Track.play_count).desc().nullslast()))
            for _an, _c in _q.limit(200).all():
                if not _an or norm_name(_an) in _have:
                    continue
                # Принимаем только с жанровым пересечением ИЛИ общим кластером,
                # иначе в «похожие» попадёт топ библиотеки (LP/Scooter/Coldplay).
                _ok = norm_name(_an) in _cluster_artists
                if not _ok and (_seed_g or _seed_toks):
                    _grow = (db.query(Track.genre).filter(
                        func.lower(func.trim(Track.artist_name)) == norm_name(_an),
                        Track.genre.isnot(None)).limit(3).all())
                    for (_gg,) in _grow:
                        if _genre_score(_seed_g, _seed_toks, _gg) > 0:
                            _ok = True
                            break
                if not _ok:
                    continue
                ranked.append(_an)
                _have.add(norm_name(_an))
                if len(ranked) >= limit:
                    break
        except Exception:
            pass
    return ranked[:limit]


def _similar_2hop(db, hop1: list[str], seed_artist: str,
                  per: int = 2, max_hop1: int = 3) -> list[str]:
    """2-й хоп: похожие на похожих (FFDP → Slipknot → соседи Slipknot).

    Раскрываем только первые `max_hop1` артистов 1-го хопа (самые релевантные),
    с каждого берём до `per` имён. Дедпу: сид и 1-й хоп исключены.
    0 на любом из лимитов = выключено (поведение 1в1 как раньше).
    Best-effort: ошибка одного хопа не валит остальных.
    """
    if not hop1 or per <= 0 or max_hop1 <= 0:
        return []
    from app.services.artist_names import norm_name

    seed_norm = norm_name(seed_artist)
    have = {seed_norm} | {norm_name(a) for a in hop1 if a}
    out: list[str] = []
    for _h1 in list(hop1)[:max(1, max_hop1)]:
        try:
            for _n in _similar_artist_names(db, _h1, limit=max(1, per)):
                if not _n:
                    continue
                if norm_name(_n) in have:
                    continue
                have.add(norm_name(_n))
                out.append(_n)
        except Exception:
            continue
    return out


def _vector_neighbor_pool(db, seed_ids: list[str], limit: int = 60,
                          exclude: set[str] | None = None) -> list[str]:
    """Векторный добор: соседи сидов по ЗВУКУ, а не по имени жанра.

    Что считается: 9-D вектор librosa (темп/энергия/валентность/спектр) через
    k-NN к конкретным сидам + тембр (chroma 12-D + форма MFCC c1..c12).
    Это «ноты» трека: гармоника и материал. Текст/лирика в доборе не участвуют —
    их вес (`lyrics_weight`) уже работает в ранжировании пула.

    Как дёшево: смотрим не всю библиотеку, а случайную выборку играбельных
    (2000), сходство считаем в памяти (numpy, фолбэк — чистый Python).
    Нет анализа у сидов → []. Любая ошибка → [] (радио не должно падать).
    Кэш по сидам 15 минут: волну докручивают часто, а соседи якоря за
    четверть часа не протухают — иначе каждый continue это seq-scan + sort.
    """
    if not seed_ids or limit <= 0:
        return []
    import time as _t
    _ckey = (tuple(sorted(str(s) for s in seed_ids)[:24]), int(limit))
    try:
        _ts, _ids = _VEC_POOL_CACHE.get(_ckey, (0.0, []))
        if _ts and _t.time() - _ts < 900 and _ids:
            _ex = set(exclude or set())
            _hit = [i for i in _ids if i not in _ex]
            if _hit:
                return _hit[:max(1, limit)]
    except Exception:
        pass
    try:
        from app.db.models import Track, TrackFeatures
        from app.services import flow as _flow

        _seed_ids = [str(s) for s in seed_ids if s][:24]
        if not _seed_ids:
            return []
        _feats = {str(f.track_id): f for f in
                  db.query(TrackFeatures).filter(
                      TrackFeatures.track_id.in_(_seed_ids)).all()}
        if not _feats:
            return []
        try:
            from app.services.ml import _feature_vector as _fv
        except Exception:
            _fv = None  # type: ignore
        _ref_9d: dict[str, Any] = {}
        _ref_timbre: list[Any] = []
        if _fv is not None:
            for _sid in _seed_ids:
                _f = _feats.get(_sid)
                if _f is None:
                    continue
                try:
                    _v = _fv(None, _f)
                except Exception:
                    _v = None
                if _v is not None:
                    try:
                        _ref_9d[_sid] = [float(x) for x in list(_v)]
                    except Exception:
                        pass
                try:
                    _tv = _flow.timbre_vector(_f)
                except Exception:
                    _tv = None
                if _tv:
                    _ref_timbre.append(_tv)
        if not _ref_9d and not _ref_timbre:
            return []
        try:
            _cent_timbre = _flow.timbre_centroid(_ref_timbre) if _ref_timbre else None
        except Exception:
            _cent_timbre = None
        _excl = set(exclude or set()) | set(_seed_ids)
        try:
            _q = _playable_query(db, Track.id).filter(~Track.id.in_(list(_excl)[:500] or [""]))
        except Exception:
            _q = db.query(Track.id)
        try:
            _sample = [str(r[0]) for r in _q.order_by(func.random()).limit(2000).all()]
        except Exception:
            _sample = [str(r[0]) for r in _q.limit(2000).all()]
        _sample = [s for s in _sample if s not in _excl]
        if not _sample:
            return []
        _cfeats = {str(f.track_id): f for f in
                   db.query(TrackFeatures).filter(
                       TrackFeatures.track_id.in_(_sample)).all()}
        if not _cfeats:
            return []
        _cand_9d: dict[str, Any] = {}
        _cand_timbre: dict[str, Any] = {}
        if _fv is not None:
            for _tid, _f in _cfeats.items():
                try:
                    _v = _fv(None, _f)
                except Exception:
                    _v = None
                if _v is not None:
                    try:
                        _cand_9d[_tid] = [float(x) for x in list(_v)]
                    except Exception:
                        pass
                try:
                    _tv2 = _flow.timbre_vector(_f)
                except Exception:
                    _tv2 = None
                if _tv2:
                    _cand_timbre[_tid] = _tv2
        _scores: dict[str, float] = {}
        if _ref_9d and _cand_9d:
            try:
                _knn = _flow.knn_scores(_cand_9d, _ref_9d, list(_ref_9d))
            except Exception:
                _knn = {}
            for _tid, _s in _knn.items():
                _scores[_tid] = _scores.get(_tid, 0.0) + 0.6 * float(_s or 0.0)
        if _cent_timbre and _cand_timbre:
            for _tid, _tv in _cand_timbre.items():
                try:
                    _ts = float(_flow.timbre_similarity(_tv, _cent_timbre) or 0.0)
                except Exception:
                    _ts = 0.0
                if _ts > 0:
                    _scores[_tid] = _scores.get(_tid, 0.0) + 0.4 * _ts
        if not _scores:
            return []
        _ranked = sorted(_scores.items(), key=lambda kv: kv[1], reverse=True)
        _out = [str(t) for t, _ in _ranked[:max(1, limit)]]
        try:
            import time as _t2
            _VEC_POOL_CACHE[_ckey] = (_t2.time(), _out)
            if len(_VEC_POOL_CACHE) > 200:
                _old = sorted(_VEC_POOL_CACHE, key=lambda k: _VEC_POOL_CACHE[k][0])[:100]
                for _k in _old:
                    _VEC_POOL_CACHE.pop(_k, None)
        except Exception:
            pass
        return _out
    except Exception as e:  # noqa: BLE001 — добор не должен валить радио
        _log_warning("vector pool не собрался: {}", e)
        return []


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
    # rollback перед сетью: не держать слот пула вызывающей сессии 6с.
    server_used = False
    if seed_ext and not seed_ext.startswith(("disk:", "demo-")):
        try:
            db.rollback()
        except Exception:
            pass
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

    # Векторный добор по звуку (9-D + тембр): ловит то, что cold_start
    # и кластер пропустили. Сид уже есть — ищем его соседей напрямую.
    try:
        _n_vec = _tuning_int(db, user_id, "radio_vector", 60, 0, 500)
        if _n_vec > 0:
            _vseen = set(base) | {seed_id}
            for _tid in _vector_neighbor_pool(db, [seed_id], limit=_n_vec,
                                              exclude=_vseen):
                if _tid != seed_id and _tid not in _vseen:
                    _vseen.add(_tid)
                    base.append(_tid)
    except Exception:
        pass

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
    """Треки Navidrome-похожих, спроецированные на наши id. (True — сервер участвовал.)

    Сеть (до 6с) идёт БЕЗ удержания коннекта вызывающей сессии: перед fetch
    делаем rollback, иначе каждый запрос радио держит слот пула всё время
    ожидания Navidrome и пул уходит в TimeoutError (см. now_playing).
    """
    try:
        db.rollback()
    except Exception:
        pass
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
            # Ослабляем по +1 (было +2): перелёт капа минимальный.
            # Например, 3 артиста × cap 2 → per 3 даёт ровно 9 треков,
            # а не скачок сразу до 4.
            per = min(per + 1, ceiling)
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

    # 2-й хоп: похожие на похожих. Раскрываем топ-3 хопа-1, с каждого до 2 имён,
    # треков с каждого — вдвое меньше, чем с хопа-1 (дальше от сида = тише).
    # 0 в любой ручке = выключено.
    similar_2hop: list[str] = []
    try:
        _n_h1 = _tuning_int(db, user_id, "radio_second_hop_artists", 3, 0, 10)
        _n_h2 = _tuning_int(db, user_id, "radio_second_hop", 2, 0, 10)
        if _n_h1 > 0 and _n_h2 > 0:
            similar_2hop = _similar_2hop(db, similar, artist_name,
                                         per=_n_h2, max_hop1=_n_h1)
            _per2 = max(1, _n_per // 2)
            for aname in similar_2hop:
                for t in _artist_top_tracks(db, aname, limit=_per2):
                    tid = str(t.id)
                    if tid not in seen:
                        seen.add(tid)
                        pool.append(tid)
    except Exception:
        similar_2hop = []

    # Векторный добор: соседи сидов по звуку (9-D + тембр), а не по имени жанра.
    # Ловит то, что жанровый матч пропустил: тот же материал при рваных тегах.
    try:
        _n_vec = _tuning_int(db, user_id, "radio_vector", 60, 0, 500)
        if _n_vec > 0:
            for _tid in _vector_neighbor_pool(db, seed_ids, limit=_n_vec,
                                              exclude=seen):
                if _tid not in seen:
                    seen.add(_tid)
                    pool.append(_tid)
    except Exception:
        pass

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
    # Скоринг может отсеять часть пула (баны/дизлайки/фильтры): добиваем
    # тем, что он не видел, иначе при рваных жанрах очередь короче заказа.
    _got_ids = {str(r.get("track_id") or r.get("id")) for r in ranked if r.get("track_id") or r.get("id")}
    for _tid in pool:
        if _tid not in _got_ids:
            ranked.append({"track_id": _tid})
    ordered = [str(r.get("track_id") or r.get("id")) for r in ranked[: max(n * 3, n)]]
    ordered = [t for t in ordered if t]
    # Опорный трек — ПЕРВЫМ номером: радио начинается с выбранного артиста,
    # уже после идут похожие. Раньше первый трек решал скоринг/дуга и очередь
    # могла стартовать с чужого артиста («в стиле FFDP», а играет Clawfinger).
    _seed_first = ""
    for _sid in seed_ids:
        if _sid in ordered:
            _seed_first = _sid
            break
        if _sid in pool:
            _seed_first = _sid
            break
    if _seed_first:
        ordered = [_seed_first] + [t for t in ordered if t != _seed_first]
    ordered = ordered[:n]

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

        # Было N отдельных db.get (N roundtrip'ов к PG): одним IN-запросом.
        by_id = {str(t.id): t for t in
                 db.query(Track).filter(Track.id.in_(ordered)).all()} \
            if ordered else {}
        tracks = [by_id[tid] for tid in ordered if tid in by_id]
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
    # Финальный guard: опорный трек — первым, даже после дуги и круга.
    if _seed_first and ordered:
        if _seed_first in ordered:
            ordered = [_seed_first] + [t for t in ordered if t != _seed_first]
        else:
            ordered = [_seed_first] + ordered
        ordered = ordered[:n]
    _log_info("artist_radio: «{}» пул={} итог={} похожие={} хоп2={} srv={}",
              artist_name, len(pool), len(ordered), similar,
              similar_2hop, server_used)
    return {
        "tracks": ordered,
        "seed_ids": seed_ids,
        "similar_artists": similar,
        "similar_artists_2hop": similar_2hop,
        "server_used": server_used,
        "label": artist_name,
    }
