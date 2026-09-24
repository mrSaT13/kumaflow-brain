"""Now Playing для виджета «Слушает сейчас» на главной.

GET /api/now-playing?user_id=...&n=5
- тянет getNowPlaying из Navidrome под глобальной учёткой (как скан);
- матчит external_id -> наш Track;
- подбирает next-5 через wave-скоринг (наша алхимия) + человеческое «почему похоже».

Polling из UI раз в 10-15 сек. Никаких вебсокетов в MVP.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.core.auth import require_scope

router = APIRouter(dependencies=[Depends(require_scope("wave"))])


def _explain(cur_feat, cur_track, cand_track, cand_feat,
             collab: float = 0.0, comp: dict | None = None,
             score: float = 0.0) -> str:
    """Почему кандидат рядом: топ-3 факта по силе evidence.

    Честно: если sonic-фичей нет — про аудио молчим, говорим по метаданным.
    comp — покомпонентный скоринг из wave.score_candidates.
    """
    comp = comp or {}
    c_audio = float(comp.get("audio", 0.0) or 0.0)
    c_genre = float(comp.get("genre", 0.0) or 0.0)
    c_artist = float(comp.get("artist", 0.0) or 0.0)
    c_behav = float(comp.get("behavior", 0.0) or 0.0)
    c_collab = float(comp.get("collab", collab or 0.0) or 0.0)
    c_novel = float(comp.get("novelty", 0.0) or 0.0)
    has_audio = cur_feat is not None and cand_feat is not None

    scored: list[tuple[float, str]] = []

    def _num(v, default=None):
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    try:
        ca = (cand_track.artist_name or "").strip()
        ta = (cur_track.artist_name or "").strip()
        if ca and ta and ca.lower() == ta.lower():
            scored.append((1.0, "тот же артист"))
        cg = (cand_track.genre or "").strip()
        tg = (cur_track.genre or "").strip()
        if cg and tg and cg.lower() == tg.lower():
            scored.append((0.85 + 0.1 * c_genre, f"жанр {cg}"))
        if has_audio and c_audio > 0.7:
            scored.append((0.9, "звук очень близко"))
        elif has_audio and c_audio > 0.45:
            scored.append((0.6, "похоже по звуку"))
        if c_collab > 0.6:
            scored.append((0.8, "слушают те же люди"))
        elif c_collab > 0.35:
            scored.append((0.55, "заходит похожим слушателям"))
        if c_behav > 0.6:
            scored.append((0.7, "из твоего ротации"))
        if c_artist > 0.5:
            scored.append((0.65, "артист из твоего вкуса"))
        if has_audio:
            tb, cb = _num(getattr(cur_feat, "tempo_bpm", None)), _num(getattr(cand_feat, "tempo_bpm", None))
            if tb and cb and abs(tb - cb) <= 8:
                scored.append((0.5, f"темп {int(tb)}→{int(cb)}"))
            te, ce = _num(getattr(cur_feat, "energy", None)), _num(getattr(cand_feat, "energy", None))
            if te is not None and ce is not None and abs(te - ce) <= 0.15:
                scored.append((0.5, "энергия рядом"))
            try:
                cm = set(str(m).lower() for m in (cand_feat.mood_labels or []))
                tm = set(str(m).lower() for m in (cur_feat.mood_labels or []))
                common = cm & tm
                if common:
                    scored.append((0.55, f"вайб {sorted(common)[0]}"))
            except Exception:
                pass
            try:
                ck, tk = getattr(cand_feat, "key_name", None), getattr(cur_feat, "key_name", None)
                if ck and tk and ck == tk:
                    scored.append((0.4, f"тональность {ck}"))
            except Exception:
                pass
        try:
            from datetime import datetime as _dt

            _cur_year = _dt.now().year
            cy, ty = getattr(cand_track, "year", None), getattr(cur_track, "year", None)
            try:
                cyi = int(cy) if cy else None
                tyi = int(ty) if ty else None
            except (TypeError, ValueError):
                cyi = tyi = None
            # Год 2026 (=текущий) у всех треков — это дефолт скана, не факт.
            # Показываем год только если он осмысленный (не текущий, не пустой).
            if cyi and tyi and cyi < _cur_year and tyi < _cur_year:
                if cyi == tyi:
                    scored.append((0.45, f"один год · {cyi}"))
                elif cyi // 10 == tyi // 10:
                    scored.append((0.35, f"эпоха {cyi // 10 * 10}-х"))
            elif cyi and cyi < _cur_year and not tyi:
                scored.append((0.3, f"{cyi} год"))
        except (TypeError, ValueError):
            pass
        # слабые, но РАЗЛИЧАЮЩИЕ биты: когда сильных совпадений нет,
        # показываем что-то про сам трек-кандидат, а не одинаковую заглушку.
        try:
            _cg = (cand_track.genre or "").strip()
            _tg = (cur_track.genre or "").strip()
            if _cg and (not _tg or _cg.lower() != _tg.lower()):
                scored.append((0.20, f"жанр {_cg}"))
        except Exception:
            pass
        try:
            if cand_feat is not None:
                _ml = list(getattr(cand_feat, "mood_labels", None) or [])
                if _ml:
                    _m0 = str(_ml[0]).lower()
                    _tm = set(str(m).lower() for m in (cur_feat.mood_labels or [])) if cur_feat else set()
                    if _m0 not in _tm:
                        scored.append((0.28, f"вайб {_m0}"))
                _ce = _num(getattr(cand_feat, "energy", None))
                if _ce is not None:
                    if _ce >= 0.75:
                        scored.append((0.22, "качает"))
                    elif _ce <= 0.35:
                        scored.append((0.22, "спокойный"))
                _cbpm = _num(getattr(cand_feat, "tempo_bpm", None))
                if _cbpm:
                    scored.append((0.18, f"{int(round(_cbpm))} bpm"))
        except Exception:
            pass
        try:
            _pc = int(getattr(cand_track, "play_count", 0) or 0)
            if _pc >= 30:
                scored.append((0.18, "хит твоей ротации"))
            elif _pc == 0:
                scored.append((0.16, "ни разу не играл"))
        except (TypeError, ValueError):
            pass
        if not scored and c_novel > 0.9:
            scored.append((0.25, "ещё не слушал"))
        # второй резорт: новизна + слабая зацепка — уточняем чем новое
        elif len(scored) == 1 and c_novel > 0.9:
            try:
                _cm = set(str(m).lower() for m in (cand_feat.mood_labels or [])) if cand_feat else set()
                if _cm:
                    scored.append((0.24, f"новый вайб {sorted(_cm)[0]}"))
                else:
                    scored.append((0.24, "мимо ротации"))
            except Exception:
                scored.append((0.24, "мимо ротации"))
    except Exception:
        pass
    scored.sort(key=lambda kv: kv[0], reverse=True)
    bits = [t for _, t in scored[:3]]
    if not bits:
        # честный фолбэк со скором вместо вранья про «аудио-фичи»
        bits = [f"край вкуса · скор {score:.2f}"] if score else ["край вкуса"]
    return " · ".join(bits)


def _next_for_track(db: Session, track_id: str, user_id: str | None, n: int = 5,
                    offset: int = 0, jitter_seed: str | None = None) -> list[dict]:
    from app.db.models import ArtistBan, Track, TrackDislike, TrackFeatures

    cur = db.get(Track, track_id)
    if not cur:
        return []
    dis: set[str] = set()
    bans: set[str] = set()
    if user_id:
        try:
            dis = {str(r.track_id) for r in
                   db.query(TrackDislike).filter_by(user_id=user_id).all()}
            bans = {str(r.artist_name) for r in
                    db.query(ArtistBan).filter_by(user_id=user_id).all()}
        except Exception:
            pass
    dis.add(str(cur.id))

    q = db.query(Track).filter(Track.id != str(cur.id))
    # лёгкий приоритет: тот же жанр/артист выше, остальное доберём
    pool: list[Track] = []
    try:
        same = q.filter(
            (Track.genre == cur.genre) | (Track.artist_name == cur.artist_name)
        ).limit(300).all()
        pool.extend(same)
    except Exception:
        pass
    if len(pool) < 400:
        try:
            rest = q.order_by(Track.play_count.desc()).limit(600).all()
            seen = {str(t.id) for t in pool}
            for t in rest:
                if str(t.id) not in seen:
                    pool.append(t)
                if len(pool) >= 600:
                    break
        except Exception:
            pass
    try:
        from app.services.artist_names import is_banned as _is_banned
    except Exception:
        _is_banned = lambda a, b: bool(a and a in b)  # noqa: E731
    pool = [t for t in pool if str(t.id) not in dis
            and not _is_banned(t.artist_name, bans)]
    if not pool:
        return []

    cand_ids = [str(t.id) for t in pool[:600]]
    ranked: list[dict] = []
    collab_map: dict[str, float] = {}
    # Ротация: один и тот же текущий трек больше не даёт вечно один и тот же топ-5.
    # jitter_seed детерминирован (трек+сдвиг), чтобы polling не тасовал паутину каждые 10с,
    # а кнопка «Другие 5» честно показывала следующий срез.
    offset = max(0, int(offset or 0))
    jitter = jitter_seed or f"{track_id}:{offset}"
    if user_id:
        try:
            from app.services import collab as _cb
            from app.services import wave as _wave

            rec = _cb.recommend_for_user(db, user_id, n=200)
            collab_map = {str(it["track_id"]): float(it.get("score", 0) or 0)
                          for it in rec.get("items", [])}
            seeds = _wave.select_seeds(db, user_id, limit=4)
            seeds = [str(cur.id)] + [s for s in seeds if s != str(cur.id)][:4]
            ranked = _wave.score_candidates(db, user_id, cand_ids[:400], seeds,
                                            settings={}, recent_events=None,
                                            collab_scores=collab_map,
                                            jitter_seed=jitter)
        except Exception:
            ranked = []
    if not ranked:
        # без пользователя: чистая аудио-похожесть на текущий трек
        try:
            from app.services import wave as _wave

            ranked = _wave.score_candidates(db, user_id or str(cur.server_id),
                                            cand_ids[:400], [str(cur.id)],
                                            settings={}, recent_events=None,
                                            collab_scores={}, jitter_seed=jitter)
        except Exception:
            # совсем fallback: как есть из пула
            ranked = [{"track_id": tid, "score": 0.0} for tid in cand_ids[:n]]
    window = ranked[offset:offset + n]

    meta = {str(t.id): t for t in pool}
    feats = {str(f.track_id): f for f in
             db.query(TrackFeatures).filter(
                 TrackFeatures.track_id.in_([r["track_id"] for r in window] + [str(cur.id)])
             ).all()}
    cur_feat = feats.get(str(cur.id))
    try:
        from app.services.covers import resolve_track_cover_id as _resolve_cover
    except Exception:
        _resolve_cover = None  # noqa: F841
    out: list[dict] = []
    for r in window:
        tid = str(r.get("track_id") or "")
        t = meta.get(tid)
        if not t:
            continue
        try:
            cid = _resolve_cover(db, t) if _resolve_cover else (t.cover_art_id or None)
        except Exception:
            cid = t.cover_art_id or None
        out.append({
            "track_id": tid,
            "title": t.title,
            "artist_name": t.artist_name,
            "album_name": t.album_name,
            "genre": t.genre,
            "cover_art_id": cid,
            "score": round(float(r.get("score", 0) or 0), 3),
            "reason": _explain(cur_feat, cur, t, feats.get(tid),
                               collab_map.get(tid, 0.0),
                               comp=r.get("comp"),
                               score=float(r.get("score", 0) or 0)),
        })
    # Растяжка скоров окна (как в wave_continue): сырые total упираются
    # в кламп 1.0 и вся пятёрка выглядит как «1.00 × 5». Порядок не трогаем.
    try:
        from app.services.wave import spread_scores as _spread

        _spread(out)
    except Exception:
        pass
    return out


@router.get("", include_in_schema=False)
@router.get("/")
def now_playing(user_id: str | None = None, n: int = 5, offset: int = 0,
                seed: str | None = None, db: Session = Depends(get_db)):
    """Что играет сейчас в Navidrome + next-N от нашей алхимии.

    offset/seed — ротация: один и тот же текущий трек больше не даёт вечно
    один и тот же топ-5. «Другие 5» = offset+5, «перемешать» = случайный seed.
    """
    from app.db.models import MediaUser, Track
    from app.services.media_server import get_media_server_config, is_real_config

    n = max(1, min(int(n or 5), 10))
    offset = max(0, int(offset or 0))
    cfg = get_media_server_config(db)
    if not is_real_config(cfg) or not cfg.get("user"):
        return {"playing": None, "next": [], "source": "no-server"}

    want_username: str | None = None
    if user_id:
        try:
            u = db.get(MediaUser, user_id)
            if u is not None:
                want_username = u.external_id
        except Exception:
            want_username = None

    async def _fetch() -> list[dict]:
        from app.services.navidrome.client import SubsonicAuth, SubsonicClient

        async with SubsonicClient(cfg["url"], SubsonicAuth(
                user=cfg["user"], password=cfg.get("password") or ""), timeout=15.0) as client:
            try:
                return await client.get_now_playing() or []
            except Exception:
                return []

    try:
        entries = asyncio.run(_fetch())
    except Exception:
        entries = []
    if not entries:
        return {"playing": None, "next": [], "source": "idle"}

    def _age(e: dict) -> float:
        try:
            v = e.get("minutesAgo")
            if v is None:
                return 0.0
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    # Navidrome отдаёт и залипшие сессии — сортируем по свежести,
    # иначе виджет залипает на первом (старом) треке.
    entries = sorted([e for e in entries if isinstance(e, dict)], key=_age)
    # предпочитаем запись нашего пользователя, иначе самую свежую
    picked: dict | None = None
    if want_username:
        for e in entries:
            eu = str(e.get("username") or e.get("userName") or e.get("user") or "")
            if eu == want_username:
                picked = e
                break
    if picked is None:
        picked = entries[0] if entries else None
    if not picked:
        return {"playing": None, "next": [], "source": "idle"}

    ext_id = str(picked.get("id") or "")
    local: Track | None = None
    if ext_id:
        try:
            local = db.query(Track).filter(Track.external_id == ext_id).first()
        except Exception:
            local = None
    playing = {
        "external_id": ext_id or None,
        "track_id": str(local.id) if local else None,
        "title": (local.title if local else picked.get("title")) or "—",
        "artist_name": (local.artist_name if local else picked.get("artist")) or "—",
        "album_name": (local.album_name if local else picked.get("album")) or None,
        "username": picked.get("username") or picked.get("userName"),
        "minutes_ago": picked.get("minutesAgo"),
        "player": picked.get("playerName") or picked.get("player"),
    }
    if local is not None:
        try:
            from app.services.covers import resolve_track_cover_id as _rc

            playing["cover_art_id"] = _rc(db, local)
        except Exception:
            playing["cover_art_id"] = None
    nxt: list[dict] = []
    if local is not None:
        try:
            nxt = _next_for_track(db, str(local.id), user_id, n=n,
                                  offset=offset, jitter_seed=seed)
        except Exception:
            nxt = []
    return {"playing": playing, "next": nxt, "source": "navidrome", "offset": offset}
