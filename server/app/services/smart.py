"""Умные автоплейлисты — персонально для каждого пользователя.

Виды (все is_auto_generated, старый того же вида заменяется):
- discoveries «Открытия недели» — ни разу не играло, но похоже на вкус
  (скоринг wave как в мобильном TrackScorer);
- forgotten «Забытые любимые» — лайк + не играло 90+ дней (или никогда);
- night «Ночь» — спокойное (energy<0.4 / calm-муд), по скору вкуса;
- sport «Спорт» — tempo>110 или energy>0.7.

Дизлайки и баны исключаются везде — как в волне.
"""
from __future__ import annotations

from datetime import datetime, timedelta

FORGOTTEN_DAYS = 90


def _tun_all(db, user_id: str | None = None) -> dict:
    """Эффективный rec_tuning для юзера. Ошибка -> {} (дальше дефолты)."""
    try:
        from app.services import rec_tuning as _rt

        return _rt.get_all(db, user_id) or {}
    except Exception:
        return {}


def _tun_float(tun: dict, key: str, default: float) -> float:
    try:
        v = (tun.get("playlists") or {}).get(key, default)
        return float(default if v is None else v)
    except (TypeError, ValueError):
        return float(default)


def _tun_int(tun: dict, key: str, default: int) -> int:
    try:
        v = (tun.get("playlists") or {}).get(key, default)
        return int(float(default if v is None else v))
    except (TypeError, ValueError):
        return int(default)

KINDS: dict[str, str] = {
    "discoveries": "Открытия недели",
    "forgotten": "Забытые любимые",
    "night": "Ночь",
    "sport": "Спорт",
}


def _exclusions(db, user_id: str) -> tuple[set[str], set[str]]:
    from app.db.models import ArtistBan, TrackDislike

    dis = {str(r.track_id) for r in
           db.query(TrackDislike).filter_by(user_id=user_id).all()}
    bans = {str(r.artist_name) for r in
            db.query(ArtistBan).filter_by(user_id=user_id).all()}
    return dis, bans


def _play_sets(db, user_id: str) -> tuple[set[str], dict[str, datetime]]:
    """(игранные track_id, последний раз). PlayHistory + TrackStat."""
    from app.db.models import PlayHistory, TrackStat

    last: dict[str, datetime] = {}
    for tid, when in db.query(PlayHistory.track_id, PlayHistory.played_at).filter(
            PlayHistory.user_id == user_id).all():
        tid = str(tid)
        if when and (tid not in last or when > last[tid]):
            last[tid] = when
    for st in db.query(TrackStat).filter(TrackStat.user_id == user_id).all():
        tid = str(st.track_id)
        if (st.plays or 0) > 0 or (st.last_played and tid not in last):
            if st.last_played and (tid not in last or st.last_played > last[tid]):
                last[tid] = st.last_played
            elif tid not in last:
                last[tid] = st.updated_at or datetime.min
    return set(last), last


def discoveries(db, user_id: str, n: int = 30) -> list[str]:
    """Неслышанное, похожее на вкус (wave-скоринг по сидам вкуса)."""
    from app.services import wave as _wave

    played, _ = _play_sets(db, user_id)
    dis, bans = _exclusions(db, user_id)
    from app.db.models import Favorite, Track

    fav = {str(r.track_id) for r in db.query(Favorite).filter_by(user_id=user_id).all()}
    skip = played | dis | fav
    q = db.query(Track.id, Track.artist_name)
    try:
        # Только то, что сыграет плеер: disk:/demo- при выгрузке в Navidrome
        # отбрасываются и плейлист выходит короче заказанного. В демо фильтр
        # выключается сам (playable.has_navidrome).
        from app.services import playable as _pl

        q = _pl.apply(q, db)
    except Exception:  # noqa: BLE001 — не блокируем подбор из-за фильтра
        pass
    if skip:
        # чанкуем NOT IN, чтобы не упереться в лимиты переменных
        skip = list(skip)
        cand: list[str] = []
        seen: set[str] = set()
        try:
            from app.services import playable as _pl2

            _pl_filter = _pl2.playable_filter(db)
        except Exception:  # noqa: BLE001
            _pl_filter = None
        for i in range(0, len(skip), 500):
            _cq = db.query(Track.id)
            if _pl_filter is not None:
                _cq = _cq.filter(_pl_filter)
            for (tid,) in _cq.filter(
                    ~Track.id.in_(skip[i:i + 500])).limit(3000).all():
                tid = str(tid)
                if tid not in seen:
                    seen.add(tid)
                    cand.append(tid)
            if len(cand) >= 1500:
                break
    else:
        cand = [str(r[0]) for r in q.limit(1500).all()]
    if bans and cand:
        from app.services.artist_names import is_banned as _is_banned

        meta = {str(t.id): t for t in
                db.query(Track).filter(Track.id.in_(cand[:2000])).all()}
        cand = [c for c in cand
                if not (meta.get(c) and _is_banned(meta[c].artist_name, bans))]
    if not cand:
        return []
    seeds = _wave.select_seeds(db, user_id, limit=5)
    # Единый скоринг: полный контекст ядра (дрейф, руки, CLAP, skip-модель,
    # коллаборативка, авто-муд). Предфильтр выше (неслышанное, баны) сохранён.
    tun = _tun_all(db, user_id)
    _sw = _tun_int(tun, "score_window", 800)
    return _wave.rerank_pool(db, user_id, cand[:max(50, _sw)], seeds=seeds,
                             kind="discoveries", n=n, source="smart")


def forgotten(db, user_id: str, n: int = 30,
              days: int | None = None) -> list[str]:
    """Лайки, не игравшие days+ дней — ранжирует общее ядро волны."""
    from app.db.models import Favorite

    from app.services import wave as _wave

    if days is None:
        try:
            days = _tun_int(_tun_all(db, user_id), "forgotten_days",
                            FORGOTTEN_DAYS)
        except Exception:
            days = FORGOTTEN_DAYS
    _, last = _play_sets(db, user_id)
    cutoff = datetime.utcnow() - timedelta(days=max(1, int(days)))
    fav_ids = [str(r.track_id) for r in
               db.query(Favorite).filter_by(user_id=user_id).all()]
    if fav_ids:
        # Лайк мог стоять на локальном файле — в плеер он не уедет.
        try:
            from app.services import playable as _pl3

            fav_ids, _ = _pl3.playable_ids(db, fav_ids)
        except Exception:  # noqa: BLE001
            pass
    pool = [tid for tid in fav_ids
            if not (last.get(tid) is not None and last.get(tid) >= cutoff)]
    if not pool:
        return []
    # Окно forgotten — предфильтр; порядок отдаёт ядро (вкус+новизна+скип).
    return _wave.rerank_pool(db, user_id, pool, kind="forgotten",
                             n=n, source="smart")


def _energy_pool(db, user_id: str, predicate: str, n: int) -> list[str]:
    """Пул по sonic-предикату (ночь/спорт) + ранжирование ядром волны.

    Характер задаёт предикат пула; персонализация (вкус, новизна, скип-риск,
    per-user джиттер) — из общего скоринга, а не из ручной досортировки.
    """
    from datetime import date as _date

    from app.db.models import Track, TrackFeatures

    from app.services import wave as _wave

    dis, bans = _exclusions(db, user_id)
    tun = _tun_all(db, user_id)
    _night_e = _tun_float(tun, "night_energy_max", 0.4)
    _sport_bpm = _tun_float(tun, "sport_bpm_min", 110)
    _sport_e = _tun_float(tun, "sport_energy_min", 0.7)
    feats = db.query(TrackFeatures).limit(5000).all()
    pool: list[str] = []
    for f in feats:
        tid = str(f.track_id)
        if tid in dis:
            continue
        try:
            en = float(f.energy) if f.energy is not None else 0.5
            bpm = float(f.tempo_bpm) if f.tempo_bpm is not None else 0
        except (TypeError, ValueError):
            continue
        moods = [str(m).lower() for m in (f.mood_labels or [])]
        if predicate == "night":
            if en < _night_e or any(k in moods for k in
                                ("calm", "chill", "спокой", "тих", "sleep", "сон",
                                 "ambient", "эмбиент", "нежн")):
                pool.append(tid)
        else:  # sport
            if bpm > _sport_bpm or en > _sport_e:
                pool.append(tid)
    if bans and pool:
        from app.services.artist_names import is_banned as _is_banned

        ids = pool
        meta = {str(t.id): t for t in
                db.query(Track).filter(Track.id.in_(ids[:2000])).all()}
        pool = [t for t in pool
                if not (meta.get(t) and _is_banned(meta[t].artist_name, bans))]
    if not pool:
        return []
    if pool:
        # Фичи есть и у локальных файлов (ради них диск и сканируется),
        # но в плеер они не уедут — отсекаем до скоринга.
        try:
            from app.services import playable as _pl4

            pool, _ = _pl4.playable_ids(db, pool)
        except Exception:  # noqa: BLE001
            pass
    if not pool:
        return []
    # Порядок — ядро волны; джиттер per-user-per-day внутри (у всех юзеров
    # без данных плейлисты не совпадают).
    _sw = _tun_int(tun, "score_window", 800)
    return _wave.rerank_pool(db, user_id, pool[:max(50, _sw)], kind=predicate,
                             day=_date.today().isoformat(),
                             n=n, source="smart")


def night(db, user_id: str, n: int = 30) -> list[str]:
    return _energy_pool(db, user_id, "night", n)


def sport(db, user_id: str, n: int = 30) -> list[str]:
    return _energy_pool(db, user_id, "sport", n)


GENERATORS = {
    "discoveries": discoveries,
    "forgotten": forgotten,
    "night": night,
    "sport": sport,
}
