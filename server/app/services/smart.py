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

from datetime import date, datetime, timedelta

FORGOTTEN_DAYS = 90

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
    if skip:
        # чанкуем NOT IN, чтобы не упереться в лимиты переменных
        skip = list(skip)
        cand: list[str] = []
        seen: set[str] = set()
        for i in range(0, len(skip), 500):
            for (tid,) in db.query(Track.id).filter(
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
    try:
        from app.services import collab as _cb
        rec = _cb.recommend_for_user(db, user_id, n=200)
        collab_scores = {str(it['track_id']): float(it.get('score', 0) or 0)
                         for it in rec.get('items', [])}
    except Exception:
        collab_scores = {}
    ranked = _wave.score_candidates(db, user_id, cand[:800], seeds,
                                    settings={}, recent_events=None,
                                    collab_scores=collab_scores,
                                    jitter_seed=f"{user_id}:discoveries:{date.today().isoformat()}")
    return [r['track_id'] for r in ranked[:n]]


def forgotten(db, user_id: str, n: int = 30,
              days: int = FORGOTTEN_DAYS) -> list[str]:
    """Лайки, не игравшие days+ дней — по скору вкуса."""
    from app.db.models import Favorite, Track
    from app.services import taste as _taste

    _, last = _play_sets(db, user_id)
    cutoff = datetime.utcnow() - timedelta(days=days)
    fav_ids = [str(r.track_id) for r in
               db.query(Favorite).filter_by(user_id=user_id).all()]
    agg = _taste._user_track_aggregates(db, user_id)
    scored = []
    for tid in fav_ids:
        lp = last.get(tid)
        if lp is not None and lp >= cutoff:
            continue
        a = agg.get(tid, {})
        try:
            s = _taste.track_score(a) if a else 100.0
        except Exception:
            s = 100.0
        scored.append((s, tid))
    scored.sort(reverse=True)
    return [tid for _, tid in scored[:n]]


def _energy_pool(db, user_id: str, predicate: str, n: int) -> list[str]:
    """Пул по sonic-фичам + досортировка скором вкуса.

    Персонализация даже при пустой истории: подмешиваем веса жанров/артистов
    из профиля (включая слепок с мобилы), а ничьи рвём детерминированным
    per-user джиттером (иначе у всех юзеров без данных плейлисты одинаковые —
    stable sort на равных скорах даёт один порядок).
    """
    import random as _rnd
    from datetime import date as _date

    from app.db.models import Track, TrackFeatures
    from app.services import taste as _taste

    dis, bans = _exclusions(db, user_id)
    try:
        _prof = _taste.user_profile(db, user_id, top_n=0)
        _pref_g = {str(k).lower(): float(v) for k, v in
                   (_prof.get('preferredGenres') or {}).items()}
        _pref_a = {str(k): float(v) for k, v in
                   (_prof.get('preferredArtists') or {}).items()}
    except Exception:
        _pref_g, _pref_a = {}, {}
    # джиттер свой у каждого юзера и каждый день свой (иначе daily одинаковый вечно)
    _rng = _rnd.Random(f"{user_id}:{predicate}:{_date.today().isoformat()}")
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
            if en < 0.4 or any(k in moods for k in
                               ("calm", "chill", "спокой", "тих", "sleep", "сон",
                                "ambient", "эмбиент", "нежн")):
                pool.append((tid, en))
        else:  # sport
            if bpm > 110 or en > 0.7:
                pool.append((tid, bpm / 200.0 + en))
    # баны + сортировка: ночь — спокойнее выше, спорт — бодрее выше
    if bans and pool:
        from app.services.artist_names import is_banned as _is_banned

        ids = [t for t, _ in pool]
        meta = {str(t.id): t for t in
                db.query(Track).filter(Track.id.in_(ids[:2000])).all()}
        pool = [(t, s) for t, s in pool
                if not (meta.get(t) and _is_banned(meta[t].artist_name, bans))]
    pool.sort(key=lambda kv: kv[1], reverse=(predicate == "sport"))
    if predicate == "night":
        pool.sort(key=lambda kv: kv[1])  # спокойнее — выше
    top = [t for t, _ in pool[:max(n * 5, 100)]]
    if not top:
        return []
    meta = {str(t.id): t for t in
            db.query(Track).filter(Track.id.in_(top[:500])).all()} if top else {}
    agg = _taste._user_track_aggregates(db, user_id)
    scored = []
    for tid in top:
        a = agg.get(tid)
        try:
            s = _taste.track_score(a) if a else 0.0
        except Exception:
            s = 0.0
        # вкус из профиля (работает и без истории — по синку с мобилы)
        t = meta.get(tid)
        if t is not None:
            try:
                if t.genre:
                    s += min(max(_pref_g.get(str(t.genre).lower(), 0.0) / 10.0, 0.0), 1.0) * 20.0
                if t.artist_name:
                    s += min(max(_pref_a.get(str(t.artist_name), 0.0) / 10.0, 0.0), 1.0) * 20.0
            except Exception:
                pass
        s += _rng.random() * 2.0  # per-user tiebreak: ничьи 0.0 рвутся по-разному
        scored.append((s, tid))
    scored.sort(reverse=True)
    return [tid for _, tid in scored[:n]]


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
