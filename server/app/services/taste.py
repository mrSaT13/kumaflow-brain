"""Серверный движок вкусов — зеркало мобильного ML (ml_service + ml_store).

Мобилу не трогаем: сюда прилетают события/оценки (POST events/rate), а сервер
считает ТОЧНО по тем же формулам и отдаёт профиль для веба и обмена:

  Track score (mobile calculateTrackScore + инкрементные правила):
    like=true  → +100            (Favorite)
    like=false → -100            (TrackDislike)
    play (PlayHistory) → +10
    complete → +15               (бонус за >90%, как completedBonus)
    replay → +50, seek_back → +30, abandon → -8
    skip: ранний (<30с) → -15, иначе → -5   (позиция из PlayEvent)
    skipCount>1 → дополнительно -skipCount*10
  Profile weights (mobile rateSong/seekBack/abandon):
    Favorite → artist +1.0, genre +1.0
    Dislike  → artist -0.5, genre -0.3
    seek_back → +0.3/+0.2 ; abandon → -0.2/-0.1
  Правила:
    3 скипа трека → автодизлайк (source=skip3)
    3 дизлайка треков артиста → автобан артиста (reason=auto3)
    banned/disliked исключаются из cold-start (см. ml.cold_start_playlist)
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from app.core.logging import get_logger
from app.db.models import ArtistBan, Favorite, PlayEvent, PlayHistory, Track, TrackDislike

logger = get_logger("taste")

ACTIONS = ("play", "complete", "skip", "replay", "seek_back", "abandon")

# правим именами — артисты в нашей схеме идентифицируются именем
def _artist_of(db, track_id: str) -> str | None:
    t = db.get(Track, track_id)
    return (t.artist_name or "").strip() or None if t else None


def artist_dislike_count(db, user_id: str, artist_name: str) -> int:
    """Сколько дизлайков у треков артиста (порт artistDislikeCounts)."""
    if not artist_name:
        return 0
    return (
        db.query(TrackDislike)
        .join(Track, Track.id == TrackDislike.track_id)
        .filter(TrackDislike.user_id == user_id, Track.artist_name == artist_name)
        .count()
    )


def _maybe_autoban(db, user_id: str, artist_name: str | None, extra: int = 0) -> bool:
    """Правило mobile: 3 дизлайка треков артиста = бан. Возвращает True если только что забанен.

    extra: дизлайки этого же пакета, ещё не видимые в committed (autoflush=False).
    """
    if not artist_name:
        return False
    exists = (
        db.query(ArtistBan)
        .filter_by(user_id=user_id, artist_name=artist_name)
        .first()
    )
    if exists:
        return False
    if artist_dislike_count(db, user_id, artist_name) + extra >= 3:
        db.add(ArtistBan(user_id=user_id, artist_name=artist_name, reason="auto3"))
        logger.info("autoban artist '{}' for user {}", artist_name, user_id[:8])
        return True
    return False


def _add_dislike(db, user_id: str, track_id: str, source: str) -> bool:
    """Поставить дизлайк (убрать лайк). True если новый."""
    db.query(Favorite).filter_by(user_id=user_id, track_id=track_id).delete()
    exists = db.query(TrackDislike).filter_by(user_id=user_id, track_id=track_id).first()
    if exists:
        return False
    db.add(TrackDislike(user_id=user_id, track_id=track_id, source=source))
    return True


def record_rate(db, user_id: str, track_id: str, like: bool | None) -> dict:
    """Оценка трека (порт mobile rateSong): True/False/None(снять).

    Возвращает {like, auto_banned_artist}.
    """
    t = db.get(Track, track_id)
    if not t:
        return {"ok": False, "error": "track not found"}
    auto_banned = None
    if like is True:
        db.query(TrackDislike).filter_by(user_id=user_id, track_id=track_id).delete()
        if db.query(Favorite).filter_by(user_id=user_id, track_id=track_id).first() is None:
            db.add(Favorite(user_id=user_id, track_id=track_id))
    elif like is False:
        _add_dislike(db, user_id, track_id, "manual")
        if _maybe_autoban(db, user_id, (t.artist_name or "").strip() or None):
            auto_banned = (t.artist_name or "").strip()
    else:
        db.query(Favorite).filter_by(user_id=user_id, track_id=track_id).delete()
        db.query(TrackDislike).filter_by(user_id=user_id, track_id=track_id).delete()
    db.commit()
    return {"ok": True, "like": like, "auto_banned_artist": auto_banned}


def record_events(db, user_id: str, events: list[dict], limit: int = 500) -> dict:
    """Пакет событий плеера (порт incrementPlay/Skip/Replay/SeekBack/Abandon).

    Событие: {track_id, action, position_sec?}. Play идёт в паттерны час/день;
    скоринг play — через PlayHistory (см. модуль), 'play'-события в счёт не идут
    (иначе двойной подсчёт с PlayHistory). Возвращает сводку применённых правил.
    limit: защита от гигантских пакетов (мобила шлёт историю страницами).
    """
    from app.services.track_resolve import get_track as _gt

    # Час/день недели — по «домашнему» времени сервера (иначе ночные скипы
    # падали не в те паттерны при APP_TIMEZONE != UTC).
    try:
        from app.core.time import server_now as _server_now

        _snow = _server_now()
        _hour, _dow = _snow.hour, _snow.isoweekday() % 7
    except Exception:
        _hour, _dow = datetime.utcnow().hour, datetime.utcnow().isoweekday() % 7
    auto_dislikes: list[str] = []
    auto_bans: list[str] = []
    stored = 0
    skipped = 0
    # сессия autoflush=False: проверки НЕ видят pending-строки этого же пакета,
    # поэтому дубли/пороги считаем ещё и в памяти (иначе UNIQUE на flush
    # и несработанное правило "3 скипа" внутри одного пакета)
    pend_skips: Counter = Counter()
    pend_dis: set[str] = set()
    pend_artist_dis: Counter = Counter()
    for e in (events or [])[:max(1, limit)]:
        raw_tid = str(e.get("track_id") or "")
        action = str(e.get("action") or "")
        # Мобила шлёт external_id (Navidrome song id) — резолвим в наш uuid.
        _t = _gt(db, raw_tid) if raw_tid else None
        if _t is None or action not in ACTIONS:
            skipped += 1
            continue
        tid = str(_t.id)
        try:
            pos = e.get("position_sec")
            pos = int(pos) if pos is not None else None
        except (TypeError, ValueError):
            pos = None
        db.add(PlayEvent(user_id=user_id, track_id=tid, action=action,
                         position_sec=pos, hour=_hour,
                         day_of_week=_dow))
        stored += 1
        if action == "skip":
            n_skips = db.query(PlayEvent).filter_by(
                user_id=user_id, track_id=tid, action="skip").count()
            n_skips += pend_skips[tid]  # pending этого пакета
            pend_skips[tid] += 1
            # +1 за только что добавленное
            if n_skips + 1 >= 3 and tid not in pend_dis:
                already = db.query(TrackDislike).filter_by(
                    user_id=user_id, track_id=tid).first() is not None
                if not already:
                    _add_dislike(db, user_id, tid, "skip3")
                    pend_dis.add(tid)
                    auto_dislikes.append(tid)
                    artist = _artist_of(db, tid)
                    if artist:
                        pend_artist_dis[artist] += 1
                    if _maybe_autoban(db, user_id, artist, extra=pend_artist_dis.get(artist or "", 0)) and artist:
                        auto_bans.append(artist)
    db.commit()
    return {"ok": True, "stored": stored, "skipped": skipped,
            "auto_dislikes": auto_dislikes, "auto_bans": auto_bans}


def _user_track_aggregates(db, user_id: str) -> dict[str, dict]:
    """Агрегаты по трекам пользователя: plays/favs/dislikes/события/статы.

    TrackStat (bulk-синк с мобилы) складывается с локальными рядами.
    """
    agg: dict[str, dict] = defaultdict(
        lambda: {"plays": 0, "fav": False, "dislike": False, "dislike_source": None,
                 "skips": 0, "early_skips": 0, "replays": 0, "seek_backs": 0,
                 "abandons": 0, "completes": 0, "mobile_score": 0, "last_played": None})
    for (tid,) in db.query(PlayHistory.track_id).filter(PlayHistory.user_id == user_id).all():
        tid = str(tid)
        agg[tid]["plays"] += 1
    for (tid,) in db.query(Favorite.track_id).filter(Favorite.user_id == user_id).all():
        agg[str(tid)]["fav"] = True
    for row in db.query(TrackDislike).filter(TrackDislike.user_id == user_id).all():
        agg[str(row.track_id)]["dislike"] = True
        agg[str(row.track_id)]["dislike_source"] = row.source
    for row in db.query(PlayEvent).filter(PlayEvent.user_id == user_id).all():
        a = agg[str(row.track_id)]
        if row.action == "skip":
            a["skips"] += 1
            if row.position_sec is not None and row.position_sec < 30:
                a["early_skips"] += 1
        elif row.action == "replay":
            a["replays"] += 1
        elif row.action == "seek_back":
            a["seek_backs"] += 1
        elif row.action == "abandon":
            a["abandons"] += 1
        elif row.action == "complete":
            a["completes"] += 1
    # last_played
    rows = db.query(PlayHistory.track_id, PlayHistory.played_at).filter(
        PlayHistory.user_id == user_id).all()
    for tid, when in rows:
        cur = agg[str(tid)]["last_played"]
        if when and (cur is None or when > cur):
            agg[str(tid)]["last_played"] = when
    # TrackStat с мобилы: складываем счётчики, mobile_score — отдельно
    from app.db.models import TrackStat as _TS

    for st in db.query(_TS).filter(_TS.user_id == user_id).all():
        tid = str(st.track_id)
        a = agg[tid]
        a["plays"] += st.plays or 0
        a["skips"] += st.skips or 0
        a["early_skips"] += st.early_skips or 0
        a["replays"] += st.replays or 0
        a["seek_backs"] += st.seek_backs or 0
        a["abandons"] += st.abandons or 0
        a["completes"] += st.completes or 0
        a["mobile_score"] = st.mobile_score or 0
        if st.last_played and (a["last_played"] is None or st.last_played > a["last_played"]):
            a["last_played"] = st.last_played
    return agg


def track_score(a: dict) -> float:
    """Счёт трека по формулам mobile (calculateTrackScore + инкременты).

    mobile_score с телефона подмешивается ×0.5: лайки/плеи оттуда уже учтены
    у нас (иначе двойной подсчёт), половина — за уникальные нюансы (replay/seek
    на устройстве, которых сервер не видел).
    """
    s = 0.0
    if a["fav"]:
        s += 100
    if a["dislike"]:
        s -= 100
    s += a["plays"] * 10
    s += a["completes"] * 15
    s += a["replays"] * 50
    s += a["seek_backs"] * 30
    s -= a["abandons"] * 8
    s -= (a["early_skips"] * 15 + (a["skips"] - a["early_skips"]) * 5)
    if a["skips"] > 1:
        s -= a["skips"] * 10
    s += (a.get("mobile_score") or 0) * 0.5
    return round(s, 1)


def user_profile(db, user_id: str, top_n: int = 50) -> dict[str, Any]:
    """Производный профиль вкусов (аналог mobile MLProfile + паттерны).

    preferredGenres/Artists — веса по формулам rateSong/seekBack/abandon.
    Плюс: облако жанров, топ треков со скором, часы/дни, настроения.
    """
    from app.db.models import MediaUser, TrackFeatures

    u = db.get(MediaUser, user_id)
    if not u:
        return {"ok": False, "error": "not found"}
    agg = _user_track_aggregates(db, user_id)
    # мета треков одним запросом
    tids = list(agg)
    meta: dict[str, Track] = {}
    if tids:
        for t in db.query(Track).filter(Track.id.in_(tids)).all():
            meta[str(t.id)] = t
    feat_map: dict[str, Any] = {}
    if tids:
        for f in db.query(TrackFeatures).filter(TrackFeatures.track_id.in_(tids)).all():
            feat_map[str(f.track_id)] = f

    genre_w: Counter = Counter()
    genre_likes: Counter = Counter()
    genre_plays: Counter = Counter()
    artist_w: Counter = Counter()
    artist_likes: Counter = Counter()
    artist_plays: Counter = Counter()
    mood_c: Counter = Counter()
    hours = [0] * 24
    days = [0] * 7
    scored: list[dict] = []
    liked_ids: list[str] = []
    disliked_ids: list[str] = []

    for tid, a in agg.items():
        t = meta.get(tid)
        # жанры — в нижнем регистре (как mobile: seedTaste/explainable lowercase),
        # иначе 'Jazz' и 'jazz' не сливаются ни в облаке, ни в обмене
        genre = ((t.genre or "").strip().lower() or "—") if t else "—"
        artist = (t.artist_name or "—") if t else "—"
        if a["fav"]:
            genre_w[genre] += 1.0
            artist_w[artist] += 1.0
            genre_likes[genre] += 1
            artist_likes[artist] += 1
            liked_ids.append(tid)
        if a["dislike"]:
            genre_w[genre] -= 0.3
            artist_w[artist] -= 0.5
            disliked_ids.append(tid)
        plays = a["plays"] + a["completes"]
        genre_plays[genre] += plays
        artist_plays[artist] += plays
        genre_w[genre] += a["seek_backs"] * 0.2 - a["abandons"] * 0.1
        artist_w[artist] += a["seek_backs"] * 0.3 - a["abandons"] * 0.2
        f = feat_map.get(tid)
        if f and f.mood_labels:
            for m in f.mood_labels[:2]:
                mood_c[str(m).lower()] += 1
        s = track_score(a)
        scored.append({
            "track_id": tid,
            "title": t.title if t else "—",
            "artist_name": t.artist_name if t else None,
            "score": s,
            "like": True if a["fav"] else (False if a["dislike"] else None),
            "plays": a["plays"],
            "skips": a["skips"],
            "replays": a["replays"],
            "last_played": a["last_played"].isoformat() if a["last_played"] else None,
        })

    for row in db.query(PlayEvent.hour).filter(PlayEvent.user_id == user_id).all():
        h = row[0]
        if isinstance(h, int) and 0 <= h < 24:
            hours[h] += 1
    for row in db.query(PlayEvent.day_of_week).filter(PlayEvent.user_id == user_id).all():
        d = row[0]
        if isinstance(d, int) and 0 <= d < 7:
            days[d] += 1
    if not any(hours):
        # fallback: часы из PlayHistory
        for row in db.query(PlayHistory).filter(PlayHistory.user_id == user_id).all():
            if row.played_at:
                hours[row.played_at.hour] += 1
                days[row.played_at.isoweekday() % 7] += 1

    bans = [str(r.artist_name) for r in
            db.query(ArtistBan).filter_by(user_id=user_id).order_by(ArtistBan.created_at.desc()).all()]
    # счётчики дизлайков по артистам (порт artistDislikeCounts)
    artist_dislike_counts: dict[str, int] = {}
    for row in db.query(TrackDislike).filter(TrackDislike.user_id == user_id).all():
        t = meta.get(str(row.track_id))
        an = (t.artist_name or "—") if t else "—"
        artist_dislike_counts[an] = artist_dislike_counts.get(an, 0) + 1

    scored.sort(key=lambda x: x["score"], reverse=True)
    genres = [{"name": g, "weight": round(w, 2), "likes": genre_likes.get(g, 0),
               "plays": genre_plays.get(g, 0)}
              for g, w in genre_w.most_common(40)]
    artists = [{"name": an, "weight": round(w, 2), "likes": artist_likes.get(an, 0),
                "plays": artist_plays.get(an, 0),
                "dislikes": artist_dislike_counts.get(an, 0),
                "banned": an in set(bans)}
               for an, w in artist_w.most_common(40)]

    # подмешиваем слепок с мобилы ×0.5 (волна и плейлисты точнее)
    mobile_snap: dict = {}
    try:
        from app.db.models import TasteProfile as _TP

        _tp = db.get(_TP, user_id)
        if _tp:
            for g, w in (dict(_tp.mobile_genres or {}).items()):
                try:
                    genre_w[str(g)] += float(w) * 0.5
                except (TypeError, ValueError):
                    pass
            for an, w in (dict(_tp.mobile_artists or {}).items()):
                try:
                    artist_w[str(an)] += float(w) * 0.5
                except (TypeError, ValueError):
                    pass
            mobile_snap = {
                "synced_at": _tp.mobile_synced_at.isoformat() if _tp.mobile_synced_at else None,
                "counts": dict(_tp.mobile_counts or {}),
                "genres_top": sorted((dict(_tp.mobile_genres or {})).items(),
                                     key=lambda kv: kv[1], reverse=True)[:10],
                "artists_top": sorted((dict(_tp.mobile_artists or {})).items(),
                                      key=lambda kv: kv[1], reverse=True)[:10],
            }
            # пересобираем топы с учётом подмеса
            genres = [{"name": g, "weight": round(w, 2), "likes": genre_likes.get(g, 0),
                       "plays": genre_plays.get(g, 0)}
                      for g, w in genre_w.most_common(40)]
            artists = [{"name": an, "weight": round(w, 2), "likes": artist_likes.get(an, 0),
                        "plays": artist_plays.get(an, 0),
                        "dislikes": artist_dislike_counts.get(an, 0),
                        "banned": an in set(bans)}
                       for an, w in artist_w.most_common(40)]
    except Exception:
        pass

    return {
        "ok": True,
        "user_id": user_id,
        "username": u.username,
        "preferredGenres": {g["name"]: g["weight"] for g in genres},
        "preferredArtists": {a["name"]: a["weight"] for a in artists},
        "likedSongs": liked_ids,
        "dislikedSongs": disliked_ids,
        "bannedArtists": bans,
        "artistDislikeCounts": artist_dislike_counts,
        "genres": genres,
        "artists": artists,
        "moods": [{"name": m, "count": c} for m, c in mood_c.most_common(12)],
        "hours": hours,
        "days": days,
        "top_tracks": scored[:top_n],
        "mobile": mobile_snap,
        "counts": {"likes": len(liked_ids), "dislikes": len(disliked_ids),
                   "banned": len(bans), "events": db.query(PlayEvent).filter_by(user_id=user_id).count(),
                   "plays": db.query(PlayHistory).filter_by(user_id=user_id).count()},
    }


def _parse_dt(v) -> datetime | None:
    if not v:
        return None
    try:
        s = str(v).replace("Z", "")
        if "." in s:
            s = s.split(".")[0]
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _to_int(v, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def sync_from_mobile(db, user_id: str, payload: dict) -> dict:
    """Полный синк истории с мобилы: рейтинги + профиль + свежие события.

    payload: {
      ratings: [{external_id, like, playCount, skipCount, replayCount,
                 seekBackCount, abandonCount, score, lastPlayed}],
      profile: {preferredGenres{}, preferredArtists{}, likedSongs[ext],
                dislikedSongs[ext], bannedArtists[names], artistDislikeCounts{}},
      events: [{track_id|external_id, action, position_sec}]  (track_id优先)
    }
    Счётчики — max-семантика (повторный синк идемпотентен).
    Возвращает сводку (для веба и отладки обмена).
    """
    from app.db.models import Favorite as _Fav
    from app.db.models import MediaUser as _MU
    from app.db.models import TasteProfile as _TP
    from app.db.models import TrackDislike as _TD
    from app.db.models import TrackStat as _TS

    u = db.get(_MU, user_id)
    if not u:
        return {"ok": False, "error": "not found"}
    payload = payload or {}

    # external_id -> track_id (один запрос)
    ext_map: dict[str, str] = {}
    for t in db.query(Track.id, Track.external_id).all():
        ext_map[str(t.external_id)] = str(t.id)

    def _resolve(item: dict) -> str | None:
        if item.get("track_id"):
            return str(item["track_id"]) if db.get(Track, str(item["track_id"])) else None
        ext = str(item.get("external_id") or "")
        return ext_map.get(ext)

    ratings = (payload.get("ratings") or [])[:20000]
    r_seen = r_fav = r_dis = r_stats = 0
    # pending-множества: сессия autoflush=False, проверки не видят свои же вставки
    pend_fav: set[str] = set()
    pend_dis: set[str] = set()
    touched_artists: set[str] = set()
    for r in ratings:
        if not isinstance(r, dict):
            continue
        tid = _resolve(r)
        if not tid:
            continue
        r_seen += 1
        like = r.get("like")
        if like is True:
            if tid not in pend_fav and db.query(_Fav).filter_by(
                    user_id=user_id, track_id=tid).first() is None:
                db.query(_TD).filter_by(user_id=user_id, track_id=tid).delete()
                pend_dis.discard(tid)
                db.add(_Fav(user_id=user_id, track_id=tid))
                pend_fav.add(tid)
                r_fav += 1
        elif like is False:
            if tid not in pend_dis and db.query(_TD).filter_by(
                    user_id=user_id, track_id=tid).first() is None:
                db.query(_Fav).filter_by(user_id=user_id, track_id=tid).delete()
                pend_fav.discard(tid)
                db.add(_TD(user_id=user_id, track_id=tid, source="mobile"))
                pend_dis.add(tid)
                r_dis += 1
            artist = _artist_of(db, tid)
            if artist:
                touched_artists.add(artist)
        st = db.get(_TS, {"user_id": user_id, "track_id": tid})
        if st is None:
            st = _TS(user_id=user_id, track_id=tid)
            db.add(st)
        st.plays = max(st.plays or 0, _to_int(r.get("playCount")))
        st.skips = max(st.skips or 0, _to_int(r.get("skipCount")))
        st.replays = max(st.replays or 0, _to_int(r.get("replayCount")))
        st.seek_backs = max(st.seek_backs or 0, _to_int(r.get("seekBackCount")))
        st.abandons = max(st.abandons or 0, _to_int(r.get("abandonCount")))
        st.mobile_score = _to_int(r.get("score"))
        lp = _parse_dt(r.get("lastPlayed"))
        if lp and (not st.last_played or lp > st.last_played):
            st.last_played = lp
        r_stats += 1
    db.flush()

    # профиль-слепок + баны/списки как external ids
    prof = payload.get("profile") or {}
    m_liked = m_disliked = m_banned = 0
    if isinstance(prof, dict):
        for ext in (prof.get("likedSongs") or [])[:10000]:
            tid = ext_map.get(str(ext))
            if tid and tid not in pend_fav and db.query(_Fav).filter_by(
                    user_id=user_id, track_id=tid).first() is None:
                db.query(_TD).filter_by(user_id=user_id, track_id=tid).delete()
                pend_dis.discard(tid)
                db.add(_Fav(user_id=user_id, track_id=tid))
                pend_fav.add(tid)
                m_liked += 1
        for ext in (prof.get("dislikedSongs") or [])[:10000]:
            tid = ext_map.get(str(ext))
            if tid and tid not in pend_dis and db.query(_TD).filter_by(
                    user_id=user_id, track_id=tid).first() is None:
                db.query(_Fav).filter_by(user_id=user_id, track_id=tid).delete()
                pend_fav.discard(tid)
                db.add(_TD(user_id=user_id, track_id=tid, source="mobile"))
                pend_dis.add(tid)
                m_disliked += 1
                artist = _artist_of(db, tid)
                if artist:
                    touched_artists.add(artist)
        for an in (prof.get("bannedArtists") or [])[:1000]:
            an = str(an).strip()
            if an and db.query(ArtistBan).filter_by(user_id=user_id, artist_name=an).first() is None:
                db.add(ArtistBan(user_id=user_id, artist_name=an[:512], reason="mobile"))
                m_banned += 1
        tp = db.get(_TP, user_id)
        if tp is None:
            tp = _TP(user_id=user_id)
            db.add(tp)
        tp.mobile_genres = {str(k).strip().lower(): v
                              for k, v in (dict(prof.get("preferredGenres") or {}).items())}
        tp.mobile_artists = dict(prof.get("preferredArtists") or {})
        tp.mobile_liked = list(prof.get("likedSongs") or [])[:10000]
        tp.mobile_disliked = list(prof.get("dislikedSongs") or [])[:10000]
        tp.mobile_banned = list(prof.get("bannedArtists") or [])[:1000]
        tp.mobile_counts = dict(prof.get("artistDislikeCounts") or {})
        tp.mobile_synced_at = datetime.utcnow()
    db.flush()

    # свежие события (большой лимит — это синк истории).
    # Мобила шлёт external_id — резолвим в track_id, чужие отбрасываем.
    sync_events: list[dict] = []
    for e in (payload.get("events") or [])[:5000]:
        if not isinstance(e, dict):
            continue
        if e.get("track_id"):
            sync_events.append(e)
            continue
        tid = ext_map.get(str(e.get("external_id") or ""))
        if tid:
            sync_events.append({**e, "track_id": tid})
    ev_res = record_events(db, user_id, sync_events, limit=5000)
    # финальный проход автобана по затронутым артистам (с учётом всего пакета)
    auto_bans_extra: list[str] = []
    for artist in touched_artists:
        if _maybe_autoban(db, user_id, artist):
            auto_bans_extra.append(artist)
    db.commit()
    return {"ok": True, "ratings_seen": r_seen, "ratings_stats": r_stats,
            "fav_added": r_fav, "dis_added": r_dis,
            "profile_liked": m_liked, "profile_disliked": m_disliked,
            "profile_banned": m_banned,
            "events_stored": ev_res.get("stored", 0),
            "auto_dislikes": ev_res.get("auto_dislikes", []),
            "auto_bans": ev_res.get("auto_bans", [])}


def sync_to_mobile(db, user_id: str) -> dict:
    """Слепок сервера в форме для мобилы: external ids, веса, статы по трекам."""
    from app.db.models import ArtistBan as _AB
    from app.db.models import Favorite as _Fav
    from app.db.models import MediaUser as _MU
    from app.db.models import TrackDislike as _TD
    from app.db.models import TrackStat as _TS

    u = db.get(_MU, user_id)
    if not u:
        return {"ok": False, "error": "not found"}
    id2ext = {str(t.id): str(t.external_id) for t in db.query(Track.id, Track.external_id).all()}
    liked = [id2ext[tid] for (tid,) in db.query(_Fav.track_id).filter(_Fav.user_id == user_id).all()
             if str(tid) in id2ext]
    disliked = [id2ext[tid] for (tid,) in db.query(_TD.track_id).filter(_TD.user_id == user_id).all()
                if str(tid) in id2ext]
    banned = [str(r.artist_name) for r in db.query(_AB).filter_by(user_id=user_id).all()]
    stats = {}
    for st in db.query(_TS).filter(_TS.user_id == user_id).all():
        ext = id2ext.get(str(st.track_id))
        if ext:
            stats[ext] = {"plays": st.plays, "skips": st.skips, "replays": st.replays,
                          "seek_backs": st.seek_backs, "abandons": st.abandons,
                          "completes": st.completes}
    prof = user_profile(db, user_id, top_n=0)
    return {"ok": True, "user_id": user_id, "username": u.username,
            "likedSongs": liked, "dislikedSongs": disliked, "bannedArtists": banned,
            "preferredGenres": prof.get("preferredGenres", {}),
            "preferredArtists": prof.get("preferredArtists", {}),
            "trackStats": stats,
            "counts": prof.get("counts", {})}
