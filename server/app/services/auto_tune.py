"""Авто-мозг R3 (MVP): per-user подстройка тюнинга по сигналам.

Каждому слушателю отдельно: его early_skip/completion/like из
rec_feedback за WINDOW_DAYS дней -> маленькие шаги его личного оверлея
(`rec_tuning:user:<id>`). Глобальные рамки не трогаем никогда.
Группа выключена (`auto.<group>=False`, глобально) -> по её ключам молчим.

MVP-правила (ключи repeats + forgotten_days):
- скипов много -> разнообразие вверх (штрафы повторов ×1.1, новизна +);
- всё хорошо (скипов мало, дослушивания высокие) -> чуть ближе к любимому
  (штрафы ×0.95, новизна −);
- причины с высоким early_skip: `By ...` -> штраф артиста ×1.15,
  `... you enjoy` -> штраф жанра ×1.15, `New discovery` сыпется ->
  новизна −, заходит -> новизна +;
- forgotten_days (только при auto.playlists): любят старое -> окно уже,
  скипают -> окно шире.

Шаги маленькие, кламп — схемой rec_tuning при записи. Холодный старт
(decided < MIN_DECIDED) -> молчим, пишем skip-причину в историю.
История per-user — `AppSetting(rec_tuning_auto_log:<id>)`, последние 20:
её читает панель «Сейчас применено» в Настройках.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

WINDOW_DAYS = 7
MIN_DECIDED = 50
LOG_PREFIX = "rec_tuning_auto_log:"
LOG_KEEP = 20

# Пороги сигналов (проценты rec_feedback.summary).
SKIP_HIGH = 25.0
SKIP_LOW = 12.0
COMPLETE_HIGH = 50.0


def _utcnow() -> datetime:
    try:
        from app.core.time import utcnow as _u

        return _u()
    except Exception:
        return datetime.utcnow()


def _mul(cur: Any, f: float, n: int = 3) -> list[float] | None:
    try:
        items = list(cur)
        if len(items) != n:
            return None
        return [float(x) * f for x in items]
    except (TypeError, ValueError):
        return None


def _reason_rate(entry: dict[str, Any]) -> float | None:
    """early_skip/доля показов причины. Мало показов -> None (шум)."""
    try:
        served = int(entry.get("served") or 0)
        if served < 10:
            return None
        return 100.0 * int(entry.get("early_skip") or 0) / served
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _signals(db, user_id: str) -> dict[str, Any]:
    from app.services import rec_feedback as _rfb

    s = _rfb.summary(db, user_id=str(user_id), days=WINDOW_DAYS)
    return s


def _rules(signals: dict[str, Any], eff: dict[str, dict[str, Any]],
           allow_playlists: bool) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Патч оверлея + человеческие причины. Абсолютные значения (от effect)."""
    patch: dict[str, dict[str, Any]] = {}
    reasons: list[str] = []

    def _put(group: str, key: str, value: Any) -> None:
        patch.setdefault(group, {})[key] = value

    rep = eff.get("repeats", {})
    early = float(signals.get("early_skip_rate") or 0.0)
    comp = float(signals.get("completion_rate") or 0.0)

    if early >= SKIP_HIGH:
        for k in ("artist_penalty", "genre_penalty", "mood_penalty"):
            v = _mul(rep.get(k), 1.1)
            if v is not None:
                _put("repeats", k, v)
        try:
            _put("repeats", "novelty_extra",
                 float(rep.get("novelty_extra", 0.08)) + 0.01)
        except (TypeError, ValueError):
            pass
        reasons.append(f"ранних скипов {early:.0f}%: разнообразие +")
    elif early <= SKIP_LOW and comp >= COMPLETE_HIGH:
        for k in ("artist_penalty", "genre_penalty", "mood_penalty"):
            v = _mul(rep.get(k), 0.95)
            if v is not None:
                _put("repeats", k, v)
        try:
            _put("repeats", "novelty_extra",
                 max(0.0, float(rep.get("novelty_extra", 0.08)) - 0.005))
        except (TypeError, ValueError):
            pass
        reasons.append(f"всё хорошо (скипы {early:.0f}%, дослушивания "
                       f"{comp:.0f}%): чуть ближе к любимому")

    for entry in (signals.get("by_reason") or []):
        if not isinstance(entry, dict):
            continue
        rate = _reason_rate(entry)
        if rate is None:
            continue
        name = str(entry.get("reason") or "")
        if rate >= SKIP_HIGH:
            if name.startswith("By "):
                v = _mul(rep.get("artist_penalty"), 1.15)
                if v is not None:
                    _put("repeats", "artist_penalty", v)
                    reasons.append(f"«{name[:40]}» скипают ({rate:.0f}%): "
                                   f"штраф артиста +")
            elif "you enjoy" in name:
                v = _mul(rep.get("genre_penalty"), 1.15)
                if v is not None:
                    _put("repeats", "genre_penalty", v)
                    reasons.append(f"«{name[:40]}» скипают ({rate:.0f}%): "
                                   f"штраф жанра +")
            elif "discovery" in name.lower():
                try:
                    _put("repeats", "novelty_extra",
                         max(0.0, float(rep.get("novelty_extra", 0.08)) - 0.01))
                    reasons.append(f"открытия скипают ({rate:.0f}%): новизна −")
                except (TypeError, ValueError):
                    pass
        elif rate <= SKIP_LOW and "discovery" in name.lower():
            try:
                served = int(entry.get("served") or 0)
                liked = int(entry.get("liked") or 0)
                completed = int(entry.get("completed") or 0)
                if served >= 10 and (liked + completed) > 0:
                    _put("repeats", "novelty_extra",
                         float(rep.get("novelty_extra", 0.08)) + 0.01)
                    reasons.append("открытия заходят: новизна +")
            except (TypeError, ValueError):
                pass

    if allow_playlists:
        try:
            cur_days = int(float((eff.get("playlists") or {}).get(
                "forgotten_days", 90)))
        except (TypeError, ValueError):
            cur_days = 90
        like = float(signals.get("like_rate") or 0.0)
        if comp >= COMPLETE_HIGH and like >= 3.0 and cur_days > 30:
            _put("playlists", "forgotten_days", cur_days - 15)
            reasons.append("старое любят: «забытое» чаще")
        elif early >= SKIP_HIGH and cur_days < 365:
            _put("playlists", "forgotten_days", cur_days + 15)
            reasons.append(f"скипов много ({early:.0f}%): «забытое» реже")

    return patch, reasons


def get_log(db, user_id: str) -> list[dict[str, Any]]:
    """История авто-правок юзера (новые первые, до LOG_KEEP)."""
    try:
        from app.db.models import AppSetting

        row = db.get(AppSetting, LOG_PREFIX + str(user_id))
        if row is not None and isinstance(row.value, dict):
            items = row.value.get("entries") or []
            return [e for e in items if isinstance(e, dict)][:LOG_KEEP]
    except Exception:
        pass
    return []


def _append_log(db, user_id: str, entry: dict[str, Any]) -> None:
    try:
        from app.db.models import AppSetting

        key = LOG_PREFIX + str(user_id)
        row = db.get(AppSetting, key)
        items = []
        if row is not None and isinstance(row.value, dict):
            items = [e for e in (row.value.get("entries") or [])
                     if isinstance(e, dict)]
        items = [entry] + items
        payload = {"entries": items[:LOG_KEEP]}
        if row is None:
            db.add(AppSetting(key=key, value=payload))
        else:
            row.value = payload
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def tune_user(db, user_id: str) -> dict[str, Any]:
    """Одна итерация авто для юзера. Не кидает исключений наружу."""
    uid = str(user_id)
    try:
        from app.services import rec_tuning as _rt

        auto_rep = _rt.is_auto("repeats", db)
        auto_pl = _rt.is_auto("playlists", db)
        if not auto_rep and not auto_pl:
            return {"user_id": uid, "tuned": False,
                    "skip": "авто выключено (группы repeats/playlists)"}
        signals = _signals(db, uid)
        decided = int(signals.get("decided") or 0)
        if decided < MIN_DECIDED:
            entry = {"at": _utcnow().isoformat() + "Z", "tuned": False,
                     "skip": f"мало сигналов ({decided}<{MIN_DECIDED})",
                     "signals": {"decided": decided}}
            _append_log(db, uid, entry)
            return {"user_id": uid, **entry}
        eff = _rt.get_all(db, uid)
        patch: dict[str, dict[str, Any]] = {}
        reasons: list[str] = []
        if auto_rep:
            p, r = _rules(signals, eff, allow_playlists=False)
            for g, sub in p.items():
                patch.setdefault(g, {}).update(sub)
            reasons.extend(r)
        if auto_pl:
            _p = dict(patch.get("playlists", {}))
            p2, r2 = _rules(signals, eff, allow_playlists=True)
            for k, v in p2.get("playlists", {}).items():
                if k == "forgotten_days":
                    _p[k] = v
            if _p:
                patch["playlists"] = _p
            reasons.extend([x for x in r2 if "забытое" in x])
        if not patch:
            entry = {"at": _utcnow().isoformat() + "Z", "tuned": False,
                     "skip": "сигналы в норме — менять нечего",
                     "signals": {"decided": decided,
                                 "early_skip_rate": signals.get("early_skip_rate"),
                                 "completion_rate": signals.get("completion_rate"),
                                 "like_rate": signals.get("like_rate")}}
            _append_log(db, uid, entry)
            return {"user_id": uid, **entry}
        # before/after для панели «Сейчас применено»
        changes: dict[str, dict[str, list]] = {}
        for g, sub in patch.items():
            for k, v in sub.items():
                try:
                    before = (eff.get(g) or {}).get(k)
                except Exception:
                    before = None
                changes.setdefault(g, {})[k] = [before, v]
        _rt.set_flags(patch, db, uid)
        entry = {"at": _utcnow().isoformat() + "Z", "tuned": True,
                 "reasons": reasons, "changes": changes,
                 "signals": {"decided": decided,
                             "early_skip_rate": signals.get("early_skip_rate"),
                             "completion_rate": signals.get("completion_rate"),
                             "like_rate": signals.get("like_rate")}}
        _append_log(db, uid, entry)
        return {"user_id": uid, **entry}
    except Exception as e:  # noqa: BLE001 — один юзер не валит всех
        try:
            _append_log(db, uid, {"at": _utcnow().isoformat() + "Z",
                                  "tuned": False,
                                  "skip": f"ошибка: {str(e)[:160]}"})
        except Exception:
            pass
        return {"user_id": uid, "tuned": False,
                "skip": f"ошибка: {str(e)[:160]}"}


def tune_all(db) -> dict[str, Any]:
    """Проход по всем юзерам. Каждый — независимо: свой сигналы -> свой оверлей."""
    from app.db.models import MediaUser

    results: list[dict[str, Any]] = []
    try:
        users = db.query(MediaUser).all()
    except Exception as e:  # noqa: BLE001
        return {"users": 0, "tuned": 0, "results": [],
                "error": str(e)[:200]}
    for u in users:
        try:
            uid = str(u.id)
        except Exception:
            continue
        results.append(tune_user(db, uid))
    tuned = sum(1 for r in results if r.get("tuned"))
    return {"users": len(results), "tuned": tuned, "results": results}
