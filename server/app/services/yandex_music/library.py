"""Импорт библиотеки пользователя из Яндекс Музыки в taste engine.

Источник: Marshal `yandex-music-api` (https://ym.marshal.dev) — Python-клиент
к неофициальному API. Токен — персонифицированный OAuth пользователя
(device flow из их доки), хранится шифром Fernet через vault (как пароли
Navidrome), ключ AppSetting `yandex_token_{user_id}`.

Что умеем:
- import_taste: лайки/дизлайки треков+артистов+альбомов -> sync_from_mobile
  (мэтч к локальной библиотеке по артист+название; совпавшее — Favorite/
  TrackDislike, имена артистов — preferredArtists/bannedArtists).
- import_history: music_history -> PlayHistory + PlayEvent(play). За тумблером
  history_enabled.
- refresh_charts/get_charts: chart + new_releases -> снапшот AppSetting
  `yandex_charts` + мэтч к локальному каталогу (для cold start через
  seed-taste). За тумблером charts_enabled.

Всё best-effort: объекты Marshal нормализуем duck-typing'ом, несопоставленное
считаем и возвращаем в сводке, а не падаем.
"""
from __future__ import annotations

import base64
import time
from collections import Counter
from datetime import datetime
from typing import Any

IMPORT_SETTINGS_KEY = "yandex_import"
CHARTS_KEY = "yandex_charts"


def _token_key(user_id: str) -> str:
    return f"yandex_token_{user_id}"


def _client_cls():
    try:
        from yandex_music import Client as _C

        return _C
    except ImportError as e:
        raise RuntimeError(
            "Библиотека yandex-music не установлена (pip install yandex-music)"
        ) from e


# --- Токен пользователя (шифр как в vault) ---

def save_user_token(db, user_id: str, token: str) -> dict:
    """Проверить токен через account_status и запомнить шифром."""
    from app.db.models import AppSetting
    from app.services import vault as _vault

    token = (token or "").strip()
    if token.lower().startswith("oauth "):
        token = token[6:].strip()
    if not token:
        return {"ok": False, "error": "Пустой токен"}
    try:
        client = _client_cls()(token).init()
        me = client.account_status() if hasattr(client, "account_status") else None
        account = getattr(me, "account", None)
        login = getattr(account, "login", None) if account is not None else None
    except Exception as e:  # noqa: BLE001 — сеть/401/прочее отдаём текстом
        return {"ok": False, "error": f"Токен не принят Яндексом: {str(e)[:200]}"}
    _vault.ensure_vault_key()
    blob = base64.b64encode(_vault.encrypt_password(token)).decode()
    key = _token_key(user_id)
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value={"enc": blob}))
    else:
        row.value = {"enc": blob}
    db.commit()
    return {"ok": True, "login": login}


def has_user_token(db, user_id: str) -> bool:
    from app.db.models import AppSetting

    row = db.get(AppSetting, _token_key(user_id))
    return bool(row is not None and isinstance(row.value, dict) and row.value.get("enc"))


def forget_user_token(db, user_id: str) -> dict:
    from app.db.models import AppSetting

    row = db.get(AppSetting, _token_key(user_id))
    if row is not None:
        db.delete(row)
        db.commit()
    return {"ok": True}


def _load_token(db, user_id: str) -> str:
    from app.db.models import AppSetting
    from app.services import vault as _vault

    row = db.get(AppSetting, _token_key(user_id))
    if row is None or not isinstance(row.value, dict) or not row.value.get("enc"):
        raise RuntimeError("Токен Яндекс Музыки не сохранён — введи его в Настройках")
    return _vault.decrypt_password(base64.b64decode(row.value["enc"]))


def get_client(db, user_id: str, client=None):
    """Готовый Client либо {"ok": False, ...}."""
    if client is not None:
        return client
    try:
        token = _load_token(db, user_id)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    try:
        return _client_cls()(token).init()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Яндекс недоступен: {str(e)[:200]}"}


# --- Настройки-тумблеры ---

def get_import_settings(db) -> dict:
    from app.db.models import AppSetting

    out = {"history_enabled": True, "charts_enabled": True,
           "corrections_enabled": True}
    try:
        row = db.get(AppSetting, IMPORT_SETTINGS_KEY)
        if row is not None and isinstance(row.value, dict):
            out.update({k: bool(row.value.get(k, v)) for k, v in out.items()})
    except Exception:
        pass
    return out


def save_import_settings(db, patch: dict) -> dict:
    from app.db.models import AppSetting

    cur = get_import_settings(db)
    for k in ("history_enabled", "charts_enabled", "corrections_enabled"):
        if k in (patch or {}):
            cur[k] = bool(patch[k])
    row = db.get(AppSetting, IMPORT_SETTINGS_KEY)
    if row is None:
        db.add(AppSetting(key=IMPORT_SETTINGS_KEY, value=cur))
    else:
        row.value = cur
    db.commit()
    return cur


# --- Нормализация объектов Marshal (duck typing) ---

def _norm(s: Any) -> str:
    return " ".join(str(s or "").strip().lower().split())


def _ym_id(obj: Any) -> str:
    for attr in ("id", "track_id"):
        v = obj.get(attr) if isinstance(obj, dict) else getattr(obj, attr, None)
        if v:
            return str(v)
    return ""


def _ym_artists(obj: Any) -> list[str]:
    raw = obj.get("artists") if isinstance(obj, dict) else getattr(obj, "artists", None)
    out: list[str] = []
    for a in raw or []:
        name = a.get("name") if isinstance(a, dict) else getattr(a, "name", None)
        if name:
            out.append(str(name))
    return out


def _ym_title(obj: Any) -> str:
    v = obj.get("title") if isinstance(obj, dict) else getattr(obj, "title", None)
    return str(v or "")


def _ym_name(obj: Any) -> str:
    if isinstance(obj, dict):
        v = obj.get("name")
    else:
        v = getattr(obj, "name", None)
        if v is None:
            v = obj
    return str(v or "").strip()


def _ym_genre(obj: Any) -> str:
    albums = obj.get("albums") if isinstance(obj, dict) else getattr(obj, "albums", None)
    alb = (albums or [None])[0]
    if not alb:
        return ""
    g = alb.get("genre") if isinstance(alb, dict) else getattr(alb, "genre", None)
    return str(g or "")


def _ym_ts(obj: Any) -> datetime | None:
    for attr in ("timestamp", "played_at", "date"):
        v = obj.get(attr) if isinstance(obj, dict) else getattr(obj, attr, None)
        if not v:
            continue
        try:
            if isinstance(v, datetime):
                return v
            return datetime.fromisoformat(str(v).replace("Z", "+00:00").split("+")[0])
        except (ValueError, TypeError):
            continue
    return None


def _short_ref(short: Any) -> str:
    """'track_id:album_id' для batch-запроса client.tracks()."""
    tid = _ym_id(short)
    alb = short.get("album_id") or short.get("albumId") if isinstance(short, dict) \
        else (getattr(short, "album_id", None) or getattr(short, "albumId", None))
    return f"{tid}:{alb}" if tid and alb else (tid or "")


def _fetch_full(client, shorts: list, chunk: int = 50,
                cap: int = 400) -> tuple[list, int]:
    """TrackShort -> полные Track (название/артисты/жанр). Возвращает (треки, пропущено)."""
    full: list = []
    skipped = 0
    refs = [_short_ref(s) for s in (shorts or [])[:cap]]
    refs = [r for r in refs if r]
    for i in range(0, len(refs), chunk):
        try:
            got = client.tracks(refs[i:i + chunk]) or []
            full.extend(got)
        except Exception:
            skipped += len(refs[i:i + chunk])
        time.sleep(0.3)
    return full, skipped


def _build_local_index(db) -> dict[tuple[str, str], Any]:
    """(артист, название) -> Track. Первый выигрывает."""
    from app.db.models import Track

    idx: dict[tuple[str, str], Any] = {}
    try:
        rows = db.query(Track.id, Track.external_id, Track.title,
                        Track.artist_name).all()
    except Exception:
        return idx
    for tid, ext, title, artist in rows:
        key = (_norm(artist), _norm(title))
        if key[0] and key[1] and key not in idx:
            idx[key] = {"id": str(tid), "external_id": str(ext),
                        "title": title, "artist": artist}
    return idx


def _match(idx: dict, artists: list[str], title: str) -> dict | None:
    t = _norm(title)
    for a in artists:
        hit = idx.get((_norm(a), t))
        if hit:
            return hit
    return None


# --- Импорт вкуса ---

def import_taste(db, user_id: str, client=None, max_tracks: int = 400) -> dict:
    """Лайки/дизлайки из Яндекса -> sync_from_mobile. Тумблера нет — всегда доступен."""
    from app.services import taste as _taste

    c = get_client(db, user_id, client)
    if isinstance(c, dict):
        return c
    try:
        liked_shorts = list(c.users_likes_tracks() or [])
        disliked_shorts = list(c.users_dislikes_tracks() or [])
        liked_artists = [_ym_name(a) for a in (c.users_likes_artists() or [])]
        disliked_artists = [_ym_name(a) for a in (c.users_dislikes_artists() or [])]
        liked_albums = list(c.users_likes_albums() or [])
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Яндекс не отдал лайки: {str(e)[:200]}"}

    liked_full, skip1 = _fetch_full(c, liked_shorts, cap=max_tracks)
    disliked_full, skip2 = _fetch_full(c, disliked_shorts, cap=200)
    idx = _build_local_index(db)

    ratings: list[dict] = []
    liked_ext: list[str] = []
    disliked_ext: list[str] = []
    pref_a: Counter = Counter()
    matched = unmatched = 0
    for t in liked_full:
        artists, title = _ym_artists(t), _ym_title(t)
        for a in artists:
            pref_a[a] += 2
        hit = _match(idx, artists, title)
        if hit:
            matched += 1
            ratings.append({"external_id": hit["external_id"], "like": True,
                            "playCount": 1, "score": 80})
            liked_ext.append(hit["external_id"])
        else:
            unmatched += 1
    for t in disliked_full:
        artists, title = _ym_artists(t), _ym_title(t)
        hit = _match(idx, artists, title)
        if hit:
            ratings.append({"external_id": hit["external_id"], "like": False})
            disliked_ext.append(hit["external_id"])
    for a in liked_artists:
        if a.strip():
            pref_a[a.strip()] += 5
    for alb in liked_albums:
        for a in _ym_artists(alb):
            pref_a[a] += 1
    banned = sorted({a.strip() for a in disliked_artists if a.strip()})[:1000]

    res = _taste.sync_from_mobile(db, user_id, {
        "ratings": ratings,
        "profile": {"preferredGenres": {},
                    "preferredArtists": dict(pref_a.most_common(500)),
                    "likedSongs": liked_ext[:10000],
                    "dislikedSongs": disliked_ext[:10000],
                    "bannedArtists": banned,
                    "artistDislikeCounts": {}},
        "events": [],
    })
    res.update({"ok": True, "source": "yandex",
                "liked_total": len(liked_shorts), "disliked_total": len(disliked_shorts),
                "matched": matched, "unmatched": unmatched,
                "fetch_skipped": skip1 + skip2,
                "liked_artists": len([a for a in liked_artists if a.strip()]),
                "banned_artists": len(banned)})
    return res


# --- Импорт истории (за тумблером) ---

def _history_items(c) -> list:
    try:
        h = c.music_history()
    except Exception:
        return []
    if h is None:
        return []
    if isinstance(h, list):
        return h
    for attr in ("days", "items", "tracks"):
        v = h.get(attr) if isinstance(h, dict) else getattr(h, attr, None)
        if isinstance(v, list) and v:
            if attr == "days":
                out: list = []
                for d in v:
                    tr = d.get("tracks") if isinstance(d, dict) else getattr(d, "tracks", None)
                    out.extend(tr or [])
                return out
            return v
    return []


def import_history(db, user_id: str, client=None, limit: int = 300) -> dict:
    """music_history -> PlayHistory + PlayEvent(play). Только при history_enabled."""
    from app.db.models import PlayHistory as _PH
    from app.services import taste as _taste

    if not get_import_settings(db).get("history_enabled", True):
        return {"ok": False, "error": "Импорт истории выключен тумблером в Настройках"}
    c = get_client(db, user_id, client)
    if isinstance(c, dict):
        return c
    items = _history_items(c)[:max(1, limit)]
    idx = _build_local_index(db)
    events: list[dict] = []
    hist_added = matched = 0
    for it in items:
        artists, title = _ym_artists(it), _ym_title(it)
        hit = _match(idx, artists, title)
        if not hit:
            continue
        matched += 1
        when = _ym_ts(it) or datetime.utcnow()
        exists = db.query(_PH).filter_by(
            user_id=user_id, track_id=hit["id"], played_at=when).first()
        if exists is None:
            db.add(_PH(user_id=user_id, track_id=hit["id"], played_at=when))
            hist_added += 1
        events.append({"track_id": hit["id"], "action": "play"})
    db.commit()
    ev = _taste.record_events(db, user_id, events, limit=500) if events else {"stored": 0}
    return {"ok": True, "source": "yandex", "seen": len(items), "matched": matched,
            "history_added": hist_added, "events_stored": int(ev.get("stored", 0) or 0)}


# --- Чарты/новинки (за тумблером) ---

def refresh_charts(db, client=None, limit: int = 100) -> dict:
    """chart + new_releases -> снапшот. Мэтч к каталогу считается при чтении."""
    if not get_import_settings(db).get("charts_enabled", True):
        return {"ok": False, "error": "Чарты выключены тумблером в Настройках"}
    from app.db.models import AppSetting

    c = client
    if c is None:
        # серверный токен (обогащение) — персональный не нужен
        from app.services.yandex_music.client import get_yandex_config as _cfg

        cfg = _cfg(db)
        if not cfg["enabled"] or not cfg["token"]:
            return {"ok": False,
                    "error": "Включи Яндекс в Настройках (серверный токен) или передай клиент"}
        try:
            c = _client_cls()(cfg["token"]).init()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"Яндекс недоступен: {str(e)[:200]}"}
    seen: list[dict] = []

    def _push(t, origin: str):
        artists, title = _ym_artists(t), _ym_title(t)
        if not title:
            return
        key = (_norm(artists[0]) if artists else "", _norm(title))
        if any((s["key"] == key for s in seen)):
            return
        seen.append({"key": key, "artist": artists[0] if artists else "",
                     "title": title, "yandex_id": _ym_id(t), "origin": origin})

    try:
        chart = c.chart()
        tracks = chart.get("tracks") if isinstance(chart, dict) else getattr(chart, "tracks", None)
        shorts = tracks or []
        full, _ = _fetch_full(c, shorts, cap=limit) if shorts and not _ym_title(shorts[0]) else (shorts, 0)
        for t in full:
            _push(t, "chart")
    except Exception:
        pass
    try:
        rel = c.new_releases()
        albums = rel.get("albums") if isinstance(rel, dict) else getattr(rel, "albums", None)
        for alb in albums or []:
            tr = alb.get("tracks") if isinstance(alb, dict) else getattr(alb, "tracks", None)
            for t in tr or []:
                if len(seen) >= limit:
                    break
                _push(t, "new")
            if len(seen) >= limit:
                break
    except Exception:
        pass
    snap = {"fetched_at": datetime.utcnow().isoformat() + "Z",
            "tracks": [{k: s[k] for k in ("artist", "title", "yandex_id", "origin")}
                       for s in seen[:limit]]}
    row = db.get(AppSetting, CHARTS_KEY)
    if row is None:
        from app.db.models import AppSetting as _AS

        db.add(_AS(key=CHARTS_KEY, value=snap))
    else:
        row.value = snap
    db.commit()
    return {"ok": True, "tracks": len(snap["tracks"]), "fetched_at": snap["fetched_at"]}


def get_charts(db) -> dict:
    """Снапшот + мэтч к локальному каталогу (для cold start через seed-taste)."""
    from app.db.models import AppSetting

    row = db.get(AppSetting, CHARTS_KEY)
    snap = row.value if row is not None and isinstance(row.value, dict) else {}
    tracks = snap.get("tracks") or []
    idx = _build_local_index(db)
    items: list[dict] = []
    in_lib = 0
    for s in tracks:
        hit = _match(idx, [s.get("artist") or ""], s.get("title") or "")
        it = dict(s)
        if hit:
            in_lib += 1
            it["track_id"] = hit["id"]
        items.append(it)
    return {"fetched_at": snap.get("fetched_at"), "tracks": items,
            "total": len(items), "in_library": in_lib}


# --- Коррекция локальных метаданных по Яндексу (Яндекс = source of truth) ---

def apply_metadata_corrections(db, track_id: str, res: dict) -> dict:
    """Сверить Track с ответом search_track и поправить как в Яндекс Музыке.

    - year: верим Яндексу всегда (у нас 2026 — дефолт скана, см. now_playing).
    - genre/album_name: заполняем когда пусто (чужие значения не затираем).
    Возвращает {field: [old, new]} для лога/сводки.
    """
    from app.db.models import Track

    t = db.get(Track, str(track_id))
    if t is None or not isinstance(res, dict):
        return {}
    changed: dict[str, list] = {}
    try:
        y_year = res.get("year")
        if y_year:
            try:
                y_year = int(y_year)
            except (TypeError, ValueError):
                y_year = None
        if y_year and (t.year or None) != y_year:
            changed["year"] = [t.year, y_year]
            t.year = y_year
        y_genre = (res.get("genre") or "").strip()
        if y_genre and not (t.genre or "").strip():
            changed["genre"] = [t.genre, y_genre]
            t.genre = y_genre[:256]
        y_album = (res.get("album") or "").strip()
        if y_album and not (t.album_name or "").strip():
            changed["album_name"] = [t.album_name, y_album]
            t.album_name = y_album[:512]
    except Exception:
        return changed
    return changed


def corrections_enabled(db) -> bool:
    return bool(get_import_settings(db).get("corrections_enabled", True))


def list_corrections(db, limit: int = 50, offset: int = 0) -> dict:
    """Треки, где Яндекс правил метаданные: что/было/стало. Для контроля в вебе."""
    from app.db.models import Track, TrackMetadataEnrich

    limit = max(1, min(200, int(limit or 50)))
    offset = max(0, int(offset or 0))
    try:
        rows = (db.query(TrackMetadataEnrich, Track)
                .join(Track, Track.id == TrackMetadataEnrich.track_id)
                .filter(TrackMetadataEnrich.source == "yandex")
                .order_by(TrackMetadataEnrich.fetched_at.desc())
                .limit(500).all())
    except Exception:
        return {"items": [], "total": 0, "limit": limit, "offset": offset}
    items: list[dict] = []
    for enr, t in rows:
        corr = (enr.data or {}).get("corrected") if isinstance(enr.data, dict) else None
        if not corr:
            continue
        items.append({"track_id": str(t.id), "title": t.title,
                      "artist_name": t.artist_name, "album_name": t.album_name,
                      "corrected": corr,
                      "fetched_at": enr.fetched_at.isoformat() if enr.fetched_at else None})
    total = len(items)
    return {"items": items[offset:offset + limit], "total": total,
            "limit": limit, "offset": offset}


def revert_correction(db, track_id: str, fields: list[str] | None = None) -> dict:
    """Откатить правки Яндекса: вернуть старые значения в Track, убрать из corrected."""
    from app.db.models import Track, TrackMetadataEnrich

    t = db.get(Track, str(track_id))
    if t is None:
        return {"ok": False, "error": "track not found"}
    enr = db.query(TrackMetadataEnrich).filter_by(
        track_id=str(track_id), source="yandex").first()
    if enr is None or not isinstance(enr.data, dict) or not enr.data.get("corrected"):
        return {"ok": False, "error": "У трека нет правок Яндекса"}
    corr = dict(enr.data["corrected"])
    want = set(fields or corr.keys())
    restored: dict[str, list] = {}
    for f in [f for f in corr.keys() if f in want]:
        old = (corr[f] or [None, None])[0]
        if f == "year":
            try:
                t.year = int(old) if old is not None else None
            except (TypeError, ValueError):
                t.year = None
        elif f == "genre":
            t.genre = old
        elif f == "album_name":
            t.album_name = old
        else:
            continue
        restored[f] = [corr[f][1] if len(corr[f]) > 1 else None, old]
        corr.pop(f, None)
    data = dict(enr.data)
    if corr:
        data["corrected"] = corr
    else:
        data.pop("corrected", None)
    enr.data = data
    db.commit()
    return {"ok": True, "track_id": str(track_id), "restored": restored}
