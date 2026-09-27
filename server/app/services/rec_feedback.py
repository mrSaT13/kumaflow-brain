"""Метрики рекомендаций: что система предлагала и что из этого вышло.

Зачем эта таблица. События прослушивания (PlayEvent) уже пишутся, но в них нет
двух вещей: что трек показал МОЗГ (а не нашли вручную) и с какой уверенностью
предложил. Из-за этого оценить, стал ли волна лучше, нечем — все треки в
истории выглядят одинаково, и правку скоринга приходится проверять на глаз.

Схема: строка = один показ. Сначала заполняется served_* (источник, причина,
score, позиция), затем, когда приходит событие от клиента, — outcome_*.
outcome остаётся None, если сигнала не было (показали, но не играли).

Порог раннего скипа — 30 секунд. Взят не «с потолка»: это тот же порог, что уже
используется в taste.py при подсчёте early_skips, иначе две метрики в проекте
считали бы одно и то же по-разному.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

# Порог «прослушал, но не зашло».
EARLY_SKIP_SEC = 30
# Дослушал, если вышло не меньше этой доли трека.
COMPLETE_RATIO = 0.7
# Показ считается свежим, пока не прошло столько часов: трек мог попасть в
# волну, а событие пришло через сутки.
OPEN_PENDING_HOURS = 24
# Хранить ли закрытые записи дольше года.
RETENTION_DAYS = 400

SOURCES = ("wave", "daily", "smart", "weekly", "ai", "discovery", "manual")


def _utcnow() -> datetime:
    from app.core.time import utcnow

    return utcnow()


def classify_outcome(
    action: str,
    position_sec: int | None,
    duration_sec: int | None,
) -> str:
    """Превратить событие плеера в исход рекомендации.

    action: play|complete|skip|replay|seek_back|abandon (+ отдельно like/dislike).
    """
    a = (action or "").strip().lower()
    if a in ("like", "favorite"):
        return "liked"
    if a in ("dislike", "ban"):
        return "disliked"
    pos = max(0, int(position_sec or 0))
    dur = int(duration_sec or 0)
    if a == "complete":
        return "completed"
    if a == "abandon":
        return "abandoned"
    if a in ("skip", "seek_back"):
        if pos < EARLY_SKIP_SEC:
            return "early_skip"
        return "skipped"
    # play / replay
    if dur > 0 and pos >= int(dur * COMPLETE_RATIO):
        return "completed"
    return "played"


def mark_served(
    db,
    user_id: str,
    tracks: list[dict[str, Any]],
    source: str,
    limit: int = 100,
) -> int:
    """Записать факт выдачи треков. Возвращает число новых записей.

    Дедуп: если трек уже показан недавно и по нему ещё нет сигнала, повторную
    запись не делаем. Иначе волна, которую дёргают каждые 10 секунд, насыпала бы
    по десять одинаковых показов на трек и портила все проценты.
    """
    from app.db.models import RecommendationFeedback as _RF
    from app.db.models import Track as _T

    src = (source or "wave")[:32]
    rows = (tracks or [])[: max(1, int(limit or 100))]
    if not rows:
        return 0
    tids: list[str] = []
    for t in rows:
        tid = str(t.get("track_id") or "").strip()
        if tid:
            tids.append(tid)
    if not tids:
        return 0
    now = _utcnow()
    fresh = now - timedelta(hours=OPEN_PENDING_HOURS)
    # Уже висят незакрытые показы по этим трекам — не дублируем.
    try:
        pending = {
            str(r.track_id)
            for r in db.query(_RF).filter(
                _RF.user_id == str(user_id),
                _RF.served_at >= fresh,
                _RF.outcome.is_(None),
                _RF.track_id.in_(tids),
            ).all()
        }
    except Exception:
        pending = set()
    # Треки могли прийти как внешние id (Navidrome) — резолвим.
    known: dict[str, str] = {}
    if pending:
        for t in db.query(_T).filter(_T.id.in_(list(pending))).all():
            known[str(t.id)] = str(t.id)
    added = 0
    for idx, t in enumerate(rows):
        tid = str(t.get("track_id") or "").strip()
        if not tid or tid in pending:
            continue
        reason = t.get("reason")
        score = t.get("score")
        try:
            score_f = float(score) if score is not None else None
        except (TypeError, ValueError):
            score_f = None
        db.add(
            _RF(
                user_id=str(user_id),
                track_id=tid,
                source=src,
                reason=(str(reason)[:256] if reason else None),
                score=score_f,
                rank=int(t.get("rank", idx)) if t.get("rank") is not None else idx,
                served_at=now,
                duration_sec=t.get("duration_sec"),
            )
        )
        pending.add(tid)
        added += 1
    try:
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return 0
    return added


def mark_outcome(
    db,
    user_id: str,
    track_id: str,
    action: str,
    position_sec: int | None = None,
    duration_sec: int | None = None,
) -> bool:
    """Проставить исход по последнему открытому показу трека."""
    from app.db.models import RecommendationFeedback as _RF
    from app.db.models import Track as _T

    tid = str(track_id or "").strip()
    if not tid:
        return False
    if duration_sec is None:
        try:
            tr = db.get(_T, tid)
            duration_sec = tr.duration_sec if tr is not None else None
        except Exception:
            duration_sec = None
    outcome = classify_outcome(action, position_sec, duration_sec)
    fresh = _utcnow() - timedelta(hours=OPEN_PENDING_HOURS)
    try:
        row = (
            db.query(_RF)
            .filter(
                _RF.user_id == str(user_id),
                _RF.track_id == tid,
                _RF.outcome.is_(None),
                _RF.served_at >= fresh,
            )
            .order_by(_RF.served_at.desc())
            .first()
        )
        if row is None:
            return False
        row.outcome = outcome
        row.position_sec = position_sec
        row.duration_sec = duration_sec
        row.decided_at = _utcnow()
        db.commit()
        return True
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return False


def _pct(num: int, den: int) -> float:
    return round(100.0 * num / den, 1) if den else 0.0


def summary(db, user_id: str | None = None, days: int = 7, source: str | None = None) -> dict[str, Any]:
    """Агрегаты для дашборда: скипы, дослушивания, лайки + разбивка по причинам."""
    from app.db.models import RecommendationFeedback as _RF

    days = max(1, min(365, int(days or 7)))
    since = _utcnow() - timedelta(days=days)
    q = db.query(_RF).filter(_RF.served_at >= since)
    if user_id:
        q = q.filter(_RF.user_id == str(user_id))
    if source:
        q = q.filter(_RF.source == str(source))
    rows = q.all()
    total = len(rows)
    counts: dict[str, int] = {}
    for r in rows:
        key = r.outcome or "no_signal"
        counts[key] = counts.get(key, 0) + 1
    decided = total - counts.get("no_signal", 0)
    early = counts.get("early_skip", 0)
    skip_all = early + counts.get("skipped", 0)
    # Средняя позиция скипа — насколько глубоко в очереди люди бросали.
    early_pos = [
        r.position_sec
        for r in rows
        if r.outcome == "early_skip" and r.position_sec is not None
    ]
    # Расклад по причинам: какие «потому что слушал X» работают, а какие нет.
    by_reason: dict[str, dict[str, Any]] = {}
    for r in rows:
        if not r.reason:
            continue
        b = by_reason.setdefault(r.reason[:80], {"reason": r.reason[:80], "served": 0, "early_skip": 0, "completed": 0, "liked": 0})
        b["served"] += 1
        if r.outcome == "early_skip":
            b["early_skip"] += 1
        elif r.outcome == "completed":
            b["completed"] += 1
        elif r.outcome == "liked":
            b["liked"] += 1
    # Расклад по источникам.
    by_source: dict[str, dict[str, Any]] = {}
    for r in rows:
        b = by_source.setdefault(r.source, {"source": r.source, "served": 0, "early_skip": 0, "completed": 0, "liked": 0})
        b["served"] += 1
        if r.outcome == "early_skip":
            b["early_skip"] += 1
        elif r.outcome == "completed":
            b["completed"] += 1
        elif r.outcome == "liked":
            b["liked"] += 1
    # Расклад по уверенности ранжира: показывает, работает ли score вообще.
    by_score: list[dict[str, Any]] = []
    for lo, hi, name in ((0.0, 0.3, "низкая"), (0.3, 0.6, "средняя"), (0.6, 0.8, "высокая"), (0.8, 1.01, "очень высокая")):
        bucket = [
            r
            for r in rows
            if r.score is not None and lo <= float(r.score) < hi and r.outcome
        ]
        if not bucket:
            continue
        es = sum(1 for r in bucket if r.outcome == "early_skip")
        cp = sum(1 for r in bucket if r.outcome == "completed")
        lk = sum(1 for r in bucket if r.outcome == "liked")
        by_score.append({
            "bucket": name,
            "served": len(bucket),
            "early_skip_rate": _pct(es, len(bucket)),
            "completion_rate": _pct(cp, len(bucket)),
            "like_rate": _pct(lk, len(bucket)),
        })
    return {
        "days": days,
        "source": source or None,
        "served": total,
        "decided": decided,
        "pending": counts.get("no_signal", 0),
        "counts": counts,
        # Главные цифры дашборда.
        "early_skip_rate": _pct(early, decided),
        "skip_rate": _pct(skip_all, decided),
        "completion_rate": _pct(counts.get("completed", 0), decided),
        "like_rate": _pct(counts.get("liked", 0), decided),
        "avg_early_skip_sec": round(sum(early_pos) / len(early_pos), 1) if early_pos else None,
        "by_source": sorted(by_source.values(), key=lambda x: -x["served"]),
        "by_reason": sorted(by_reason.values(), key=lambda x: -x["served"])[:12],
        "by_score": by_score,
    }


def prune(db, keep_days: int = RETENTION_DAYS) -> int:
    """Подчистить старые записи, чтобы таблица не росла бесконечно."""
    from app.db.models import RecommendationFeedback as _RF

    cutoff = _utcnow() - timedelta(days=max(30, int(keep_days or RETENTION_DAYS)))
    try:
        n = db.query(_RF).filter(_RF.served_at < cutoff).delete(synchronize_session=False)
        db.commit()
        return int(n or 0)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return 0
