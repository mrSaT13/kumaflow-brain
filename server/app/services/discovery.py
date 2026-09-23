"""Weekly Discovery по CLAP-эмбеддингам + объяснение «почему этот трек».

Логика:
- берём лайки юзера -> их TrackEmbedding(clap_text) -> средний вектор вкуса
- ищем ближайшие НЕслушанные треки по косинусу (brute-force чанками, без OOM на 150k)
- fallback на cold_start_playlist если CLAP нет / лайков <3
- explanation на каждый трек: ближайший любимый + совпадение жанра/настроения
"""
from __future__ import annotations


def weekly_discovery(db, user_id: str, n: int = 30) -> dict:
    from app.db.models import Favorite, Track, TrackEmbedding, TrackFeatures

    n = max(5, min(100, int(n or 30)))
    fav_ids = [str(r.track_id) for r in db.query(Favorite).filter_by(user_id=user_id).all()]

    try:
        from app.services.clap import is_available as _clap_ok
        clap_ok = bool(_clap_ok())
    except Exception:
        clap_ok = False

    if not clap_ok or len(fav_ids) < 3:
        from app.services.ml import cold_start_playlist

        u = None
        try:
            from app.db.models import MediaUser as _MU
            u = db.get(_MU, user_id)
            sid = str(u.server_id) if u else None
        except Exception:
            sid = None
        if not sid:
            from app.services.media_server import resolve_active_server as _ras
            sid = str(_ras(db).id)
            db.commit()
        r = cold_start_playlist(sid, n=n, user_id=user_id)
        return {"mode": "cold_start_fallback", "reason": "CLAP нет или мало лайков (<3)",
                "tracks": r.get("tracks") or [], "items": r.get("items") or [],
                "explanations": [], "steps": r.get("steps") or []}

    import numpy as _np

    # вектора лайков
    rows = db.query(TrackEmbedding).filter(
        TrackEmbedding.model == "clap_text",
        TrackEmbedding.track_id.in_(fav_ids[:2000])).all() if fav_ids else []
    liked_vecs: dict[str, _np.ndarray] = {}
    for r in rows:
        try:
            v = _np.frombuffer(r.vector, dtype=_np.float32)
            if v.shape[0] != 512:
                continue
            nv = float(_np.linalg.norm(v))
            if nv <= 0:
                continue
            liked_vecs[str(r.track_id)] = v / nv
        except Exception:
            continue
    if len(liked_vecs) < 3:
        from app.services.ml import cold_start_playlist

        from app.db.models import MediaUser as _MU
        u = db.get(_MU, user_id)
        sid = str(u.server_id) if u else None
        if not sid:
            from app.services.media_server import resolve_active_server as _ras2
            sid = str(_ras2(db).id)
            db.commit()
        r = cold_start_playlist(sid, n=n, user_id=user_id)
        return {"mode": "cold_start_fallback", "reason": "мало CLAP-векторов у лайков",
                "tracks": r.get("tracks") or [], "items": r.get("items") or [],
                "explanations": [], "steps": r.get("steps") or []}

    q = _np.mean(_np.vstack(list(liked_vecs.values())), axis=0)
    q = q / (float(_np.linalg.norm(q)) or 1.0)

    # исключения: игранное, дизлайки, баны, сами лайки
    from app.services.smart import _exclusions as _exc
    from app.services.smart import _play_sets as _ps

    played, _ = _ps(db, user_id)
    dis, bans = _exc(db, user_id)
    skip = set(played) | set(dis) | set(fav_ids)
    skip_list = list(skip)

    # кандидаты: TrackEmbedding которых нет в skip (чанками NOT IN)
    cand_ids: list[str] = []
    seen: set[str] = set()
    if skip_list:
        for i in range(0, len(skip_list), 500):
            chunk = skip_list[i:i + 500]
            for (tid,) in db.query(TrackEmbedding.track_id).filter(
                    TrackEmbedding.model == "clap_text",
                    ~TrackEmbedding.track_id.in_(chunk)).limit(4000).all():
                tid = str(tid)
                if tid not in seen:
                    seen.add(tid)
                    cand_ids.append(tid)
            if len(cand_ids) >= 3000:
                break
    else:
        cand_ids = [str(r[0]) for r in db.query(TrackEmbedding.track_id).filter(
            TrackEmbedding.model == "clap_text").limit(3000).all()]

    # мета для explanation + банов
    meta = {str(t.id): t for t in db.query(Track).filter(Track.id.in_(cand_ids[:3000])).all()} if cand_ids else {}
    if bans and cand_ids:
        cand_ids = [c for c in cand_ids if not (meta.get(c) and meta[c].artist_name in bans)]
    liked_meta = {str(t.id): t for t in db.query(Track).filter(
        Track.id.in_(list(liked_vecs.keys())[:500])).all()} if liked_vecs else {}
    feat_map = {}
    try:
        for f in db.query(TrackFeatures).filter(TrackFeatures.track_id.in_(cand_ids[:2000])).all():
            feat_map[str(f.track_id)] = f
    except Exception:
        pass

    # косинус чанками по 500 (без OOM)
    scored: list[tuple[float, str]] = []
    for i in range(0, len(cand_ids), 500):
        chunk = cand_ids[i:i + 500]
        erows = db.query(TrackEmbedding).filter(
            TrackEmbedding.model == "clap_text", TrackEmbedding.track_id.in_(chunk)).all()
        for er in erows:
            try:
                v = _np.frombuffer(er.vector, dtype=_np.float32)
                if v.shape[0] != 512:
                    continue
                nv = float(_np.linalg.norm(v))
                if nv <= 0:
                    continue
                s = float(_np.dot(q, v / nv))
                scored.append((s, str(er.track_id)))
            except Exception:
                continue
    scored.sort(reverse=True)
    top = scored[:max(n * 4, 60)]

    # MMR-диверсификация по артисту (как в cold_start, но лёгкая)
    picks: list[str] = []
    seen_artists: dict[str, int] = {}
    for s, tid in top:
        t = meta.get(tid)
        a = (t.artist_name or "") if t else ""
        if seen_artists.get(a, 0) >= 2:
            continue
        picks.append(tid)
        seen_artists[a] = seen_artists.get(a, 0) + 1
        if len(picks) >= n:
            break
    if len(picks) < n:  # добить без MMR
        for s, tid in top:
            if tid not in picks:
                picks.append(tid)
            if len(picks) >= n:
                break

    # explanations
    liked_list = list(liked_vecs.items())
    explanations: list[dict] = []
    items: list[dict] = []
    for tid in picks:
        t = meta.get(tid)
        if not t:
            t = db.get(Track, tid)
        if not t:
            continue
        # ближайший любимый
        best_id, best_s = "", -2.0
        try:
            er = db.query(TrackEmbedding).filter_by(track_id=tid, model="clap_text").first()
            if er is not None:
                import numpy as _np2
                v = _np2.frombuffer(er.vector, dtype=_np2.float32)
                v = v / (float(_np2.linalg.norm(v)) or 1.0)
                for lid, lv in liked_list:
                    s = float(_np2.dot(v, lv))
                    if s > best_s:
                        best_s, best_id = s, lid
        except Exception:
            pass
        bl = liked_meta.get(best_id)
        genre_match = bool(bl and t.genre and bl.genre and t.genre == bl.genre)
        mood = None
        try:
            f = feat_map.get(tid)
            ml = (f.mood_labels or []) if f else []
            mood = ml[0] if ml else None
        except Exception:
            mood = None
        bits = []
        if bl:
            bits.append(f"похоже на «{bl.artist_name} — {bl.title}» ({best_s:.2f})" if best_s > -1 else f"рядом с «{bl.title}»")
        if genre_match:
            bits.append(f"жанр {t.genre}")
        if mood:
            bits.append(str(mood))
        text = " · ".join(bits) if bits else "новое направление по CLAP"
        explanations.append({"track_id": tid, "score": round(float(dict(top).get(tid, 0.0)), 4),
                             "because_of_track_id": best_id or None,
                             "because_of_title": f"{bl.artist_name} — {bl.title}" if bl else None,
                             "genre_match": genre_match, "mood": mood, "text": text})
        items.append({"track_id": tid, "title": t.title, "artist_name": t.artist_name,
                      "album_name": t.album_name, "genre": t.genre})

    return {"mode": "clap", "tracks": picks, "items": items, "explanations": explanations,
            "steps": [{"step": 1, "name": "liked_vectors", "items": len(liked_vecs)},
                      {"step": 2, "name": "candidates", "items": len(scored)},
                      {"step": 3, "name": "picks", "items": len(picks)}]}
