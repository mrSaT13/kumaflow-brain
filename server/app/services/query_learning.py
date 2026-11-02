"""Самообучение мозга на «непонятных» запросах.

Идея: юзер говорит голосом, ASR пишет «Эминем», поиск ничего не находит.
Мозг не обязан требовать ручного подтверждения — он сам догадывается:

  1) Авто-резолв промаха (auto_learn). Промах «эминем» прогоняется через
     services/query_resolver: транслит/фонетика/похожесть дают «Eminem».
     Если кандидат один и уверенность высокая — мозг заводит алиас сам.
     Ты ничего не тыкаешь.

  2) Вывод из поведения (learn_from_behavior). После промаха ты поставил трек
     Eminem — мозг видит это в play_events и связывает «Эминем» → «Eminem».
     Это учится на реальных действиях, а не на кнопках.

  3) Явное подтверждение (confirm_alias) — только когда сомнение осталось:
     клиент показал 2–3 варианта, ты выбрал один голосом или тапом, и это
     запомнилось навсегда.

Все три пути пишут в query_aliases; резолвер потом сразу отдаёт правильный
артист, не тратя ни токены, ни твоё внимание.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from app.core.logging import get_logger
from app.core.time import utcnow
from app.services.text_tools import norm_key
from app.services.query_resolver import GOOD_ENOUGH, resolve, score_pair

logger = get_logger("query_learning")

# Промах не дублируем, если такой же был недавно.
MISS_DEDUP_HOURS = 6
# Окно «после промаха», в котором поведение юзера считается ответом.
BEHAVIOR_WINDOW_MIN = 90
# Насколько уверенным должен быть кандидат, чтобы мозг запомнил сам.
AUTO_LEARN_MIN = 0.90


def note_miss(db, query: str, *, user_id: str | None = None,
              kind: str = "artist", source: str = "voice") -> bool:
    """Запомнить промах. Никогда не роняет вызывающего."""
    key = norm_key(query)
    if not key:
        return False
    try:
        from app.db.models import QueryMiss

        since = utcnow() - timedelta(hours=MISS_DEDUP_HOURS)
        q = db.query(QueryMiss).filter(QueryMiss.norm == key)
        if user_id:
            q = q.filter((QueryMiss.user_id == user_id) | (QueryMiss.user_id.is_(None)))
        exists = q.filter(QueryMiss.created_at >= since).first()
        if exists is not None:
            return False
        db.add(QueryMiss(
            query=(query or "")[:512],
            norm=key[:256],
            kind=kind or "artist",
            source=source or "voice",
            user_id=user_id or None,
        ))
        db.flush()
        return True
    except Exception as e:
        logger.warning("не удалось записать промах {!r}: {}", query, e)
        try:
            db.rollback()
        except Exception:
            pass
        return False


def _upsert_alias(db, alias: str, target_name: str, *, user_id: str | None = None,
                  kind: str = "artist", external_id: str | None = None,
                  score: float = 1.0, source: str = "auto") -> bool:
    """Создать/обновить алиас. True, если алиас теперь есть."""
    key = norm_key(alias)
    if not key or not target_name:
        return False
    try:
        from app.db.models import QueryAlias

        q = db.query(QueryAlias).filter(QueryAlias.alias == key)
        if user_id:
            q = q.filter((QueryAlias.user_id == user_id) | (QueryAlias.user_id.is_(None)))
        row = q.first()
        if row is None:
            db.add(QueryAlias(
                user_id=user_id or None,
                alias=key[:256],
                kind=kind or "artist",
                target_name=target_name[:512],
                target_external_id=(external_id or None),
                score=float(score or 1.0),
                source=source or "auto",
            ))
        else:
            row.target_name = target_name[:512]
            if external_id:
                row.target_external_id = external_id
            row.score = float(score or row.score or 1.0)
            row.source = source or row.source
            row.updated_at = utcnow()
        db.flush()
        return True
    except Exception as e:
        logger.warning("не удалось сохранить алиас {!r} → {!r}: {}", alias, target_name, e)
        try:
            db.rollback()
        except Exception:
            pass
        return False


def confirm_alias(db, query: str, target_name: str, *, user_id: str | None = None,
                  kind: str = "artist", external_id: str | None = None) -> dict[str, Any]:
    """Явное подтверждение от клиента: «да, это Eminem»."""
    ok = _upsert_alias(db, query, target_name, user_id=user_id, kind=kind,
                       external_id=external_id, score=1.0, source="confirmed")
    try:
        db.commit()
    except Exception:
        pass
    return {"ok": bool(ok), "alias": norm_key(query), "target": target_name,
            "source": "confirmed"}


def auto_learn(db, limit: int = 200) -> dict[str, Any]:
    """Пройтись по открытым промахам и попробовать догадаться самому.

    Ничего не требует от юзера: если транслит/фонетика дают ровно одного
    уверенного кандидата — заводим алиас.
    """
    stats = {"seen": 0, "learned": 0}
    try:
        from app.db.models import QueryMiss

        misses = (db.query(QueryMiss)
                  .filter(QueryMiss.resolved.is_(False))
                  .order_by(QueryMiss.created_at.desc())
                  .limit(max(1, min(1000, int(limit or 200))))
                  .all())
    except Exception as e:
        logger.warning("не удалось прочитать промахи: {}", e)
        return stats

    for miss in misses:
        stats["seen"] += 1
        try:
            res = resolve(miss.query, db, user_id=miss.user_id, kind=miss.kind, top_k=3)
        except Exception:
            continue
        cands = res.get("candidates") or []
        if not cands:
            continue
        best = cands[0]
        score = float(best.get("score") or 0.0)
        # Уверенно и без конкурентов — запоминаем.
        second = float(cands[1].get("score") or 0.0) if len(cands) > 1 else 0.0
        if score < AUTO_LEARN_MIN or (second and second >= score - 0.02):
            continue
        if _upsert_alias(db, miss.query, str(best.get("name") or ""),
                         user_id=miss.user_id, kind=miss.kind,
                         external_id=best.get("external_id") or None,
                         score=score, source="auto"):
            miss.resolved = True
            stats["learned"] += 1
            logger.info("выучил сам: {!r} → {!r} ({})", miss.query, best.get("name"), best.get("why"))
    try:
        db.commit()
    except Exception:
        pass
    return stats


def learn_from_behavior(db, window_min: int = BEHAVIOR_WINDOW_MIN) -> dict[str, Any]:
    """Учиться на действиях юзера: промах «эминем» + включённый Eminem = алиас.

    Смотрим, что играло (play_events) в окне после промаха: если имя артиста
    из библиотеки похоже на промах — считаем, что это и было ответом.
    """
    stats = {"seen": 0, "learned": 0}
    try:
        from app.db.models import PlayEvent, QueryMiss, Track

        since = utcnow() - timedelta(minutes=max(5, int(window_min or BEHAVIOR_WINDOW_MIN)))
        misses = (db.query(QueryMiss)
                  .filter(QueryMiss.resolved.is_(False))
                  .order_by(QueryMiss.created_at.desc())
                  .limit(50)
                  .all())
        if not misses:
            return stats
        q = (db.query(Track.artist_name)
             .join(PlayEvent, PlayEvent.track_id == Track.id)
             .filter(PlayEvent.created_at >= since))
        played = [str(r[0]) for r in q.distinct().all() if r and r[0]]
    except Exception as e:
        logger.warning("не удалось собрать поведение: {}", e)
        return stats

    for miss in misses:
        stats["seen"] += 1
        best_name = ""
        best_score = 0.0
        for nm in played:
            sc, _ = score_pair(miss.query, nm)
            if sc > best_score:
                best_score = sc
                best_name = nm
        if not best_name or best_score < GOOD_ENOUGH:
            continue
        if _upsert_alias(db, miss.query, best_name, user_id=miss.user_id,
                         kind=miss.kind, score=best_score, source="behavior"):
            miss.resolved = True
            stats["learned"] += 1
            logger.info("выучил из поведения: {!r} → {!r}", miss.query, best_name)
    try:
        db.commit()
    except Exception:
        pass
    return stats


def delete_alias(db, alias: str, *, user_id: str | None = None,
                 kind: str = "artist") -> bool:
    """Забыть соответствие (юзер решил, что мозг понял неверно)."""
    key = norm_key(alias)
    if not key:
        return False
    try:
        from app.db.models import QueryAlias

        q = db.query(QueryAlias).filter(QueryAlias.alias == key)
        if user_id:
            q = q.filter((QueryAlias.user_id == user_id) | (QueryAlias.user_id.is_(None)))
        else:
            q = q.filter(QueryAlias.user_id.is_(None))
        rows = q.all()
        for row in rows:
            db.delete(row)
        db.flush()
        return bool(rows)
    except Exception as e:
        logger.warning("не удалось удалить алиас {!r}: {}", alias, e)
        try:
            db.rollback()
        except Exception:
            pass
        return False


# Разделители в текстовом файле соответствий: «слово = артист».
_IMPORT_SEPARATORS = ("=", "->", "→", "=>", "\t", ";", ",")


def parse_alias_text(text: str) -> list[tuple[str, str]]:
    """Разобрать текст соответствий: «слово = артист» (по строке на пару).

    Разделители: =, ->, →, =>, таб, ;, запятая. Пустые строки и строки без
    разделителя пропускаются — вернём только разобранные пары.
    """
    out: list[tuple[str, str]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        for sep in _IMPORT_SEPARATORS:
            if sep in line:
                left, _, right = line.partition(sep)
                left, right = left.strip(), right.strip()
                if left and right:
                    out.append((left, right))
                break
    return out


def import_aliases(db, text: str, *, user_id: str | None = None,
                   kind: str = "artist") -> dict[str, Any]:
    """Массово добавить соответствия из текста (файл/вставка в веб-морде)."""
    pairs = parse_alias_text(text)
    added: list[dict[str, str]] = []
    failed: list[str] = []
    for left, right in pairs:
        if _upsert_alias(db, left, right, user_id=user_id, kind=kind,
                         score=1.0, source="confirmed"):
            added.append({"alias": norm_key(left), "target": right})
        else:
            failed.append(left)
    try:
        db.commit()
    except Exception:
        pass
    return {"ok": True, "added": added, "failed": failed,
            "parsed": len(pairs), "count": len(added)}


def list_aliases(db, user_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """Список выученных соответствий (для UI и отладки)."""
    try:
        from app.db.models import QueryAlias

        q = db.query(QueryAlias)
        if user_id:
            q = q.filter((QueryAlias.user_id == user_id) | (QueryAlias.user_id.is_(None)))
        rows = q.order_by(QueryAlias.updated_at.desc()).limit(max(1, min(1000, int(limit or 100)))).all()
        return [{
            "alias": r.alias,
            "target": r.target_name,
            "kind": r.kind,
            "source": r.source,
            "score": round(float(r.score or 0.0), 3),
            "hits": r.hits,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in rows]
    except Exception as e:
        logger.warning("не удалось прочитать список алиасов: {}", e)
        return []


def open_misses(db, limit: int = 50) -> list[dict[str, Any]]:
    """Нерасшифрованные промахи — их видно в UI, чтобы понять, где мозг спотыкается."""
    try:
        from app.db.models import QueryMiss

        rows = (db.query(QueryMiss)
                .filter(QueryMiss.resolved.is_(False))
                .order_by(QueryMiss.created_at.desc())
                .limit(max(1, min(500, int(limit or 50)))).all())
        return [{
            "query": r.query,
            "kind": r.kind,
            "source": r.source,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in rows]
    except Exception:
        return []
