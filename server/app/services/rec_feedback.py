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
        # В БД пишем СЫРОЙ скор модели ('score_raw'), а не растянутый
        # min-max по окну ('score' к этому моменту уже размазан для показа).
        # Иначе цикл обучения получал бы неверные числа: у всех треков в
        # топе было бы по 0..1 «в среднем».
        score = t.get("score_raw")
        if score is None:
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
    """Проставить исход по последнему открытому показу трека.

    Нюанс с лайками: показ обычно закрывается раньше (play/complete),
    а ♥/👎 прилетает позже — открытого показа уже нет, и «лайк с волны»
    вечно показывал 0% при живых лайках. Поэтому like/dislike/favorite/ban
    АПГРЕЙДЯТ уже закрытую строку (сильнейший сигнал побеждает), остальные
    действия — только открытые, как раньше.
    """
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
    a = (action or "").strip().lower()
    upgrade = a in ("like", "favorite", "dislike", "ban")
    try:
        q = (
            db.query(_RF)
            .filter(
                _RF.user_id == str(user_id),
                _RF.track_id == tid,
                _RF.served_at >= fresh,
            )
        )
        if not upgrade:
            q = q.filter(_RF.outcome.is_(None))
        else:
            # сначала открытый, иначе последний закрытый (его апгрейдим)
            rows = q.order_by(_RF.served_at.desc()).limit(5).all()
            row = next((r for r in rows if r.outcome is None), None)
            if row is None:
                row = rows[0] if rows else None
            if row is None:
                return False
            # уже тот же исход — нечего делать, но считаем успехом
            if row.outcome == outcome:
                row.position_sec = position_sec
                row.duration_sec = duration_sec
                row.decided_at = _utcnow()
                db.commit()
                return True
            row.outcome = outcome
            row.position_sec = position_sec
            row.duration_sec = duration_sec
            row.decided_at = _utcnow()
            db.commit()
            return True
        row = q.order_by(_RF.served_at.desc()).first()
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


def _rank_ok(r, pred) -> bool:
    try:
        return bool(pred(r))
    except (TypeError, ValueError):
        return False


def _pct(num: int, den: int) -> float:
    return round(100.0 * num / den, 1) if den else 0.0


def _window_rates(rows: list) -> dict[str, Any]:
    """Ставки исходов по выборке строк (для тренда: текущее окно vs прошлое)."""
    total = len(rows)
    counts: dict[str, int] = {}
    for r in rows:
        key = r.outcome or "no_signal"
        counts[key] = counts.get(key, 0) + 1
    decided = total - counts.get("no_signal", 0)
    early = counts.get("early_skip", 0)
    skip_all = early + counts.get("skipped", 0)
    by_source: dict[str, dict[str, Any]] = {}
    for r in rows:
        b = by_source.setdefault(r.source, {"served": 0, "decided": 0,
                                            "early_skip": 0, "completed": 0,
                                            "liked": 0})
        b["served"] += 1
        if r.outcome:
            b["decided"] += 1
            if r.outcome == "early_skip":
                b["early_skip"] += 1
            elif r.outcome == "completed":
                b["completed"] += 1
            elif r.outcome == "liked":
                b["liked"] += 1
    for b in by_source.values():
        d = b["decided"]
        b["early_skip_rate"] = _pct(b["early_skip"], d)
        b["completion_rate"] = _pct(b["completed"], d)
        b["like_rate"] = _pct(b["liked"], d)
    return {"served": total, "decided": decided,
            "early_skip_rate": _pct(early, decided),
            "skip_rate": _pct(skip_all, decided),
            "completion_rate": _pct(counts.get("completed", 0), decided),
            "like_rate": _pct(counts.get("liked", 0), decided),
            "by_source": by_source}


def _trend_delta(cur: float, prev: float | None) -> float | None:
    """Дельта в процентных пунктах. Без прошлого окна — None, а не 0."""
    if prev is None:
        return None
    try:
        return round(float(cur) - float(prev), 1)
    except (TypeError, ValueError):
        return None


def summary(db, user_id: str | None = None, days: int = 7, source: str | None = None) -> dict[str, Any]:
    """Агрегаты для дашборда: скипы, дослушивания, лайки + разбивка по причинам.

    Плюс тренд: то же окно длиной days прямо перед текущим. Дельты — в
    процентных пунктах (минус = скипов стало меньше = хорошо). Прошлого
    окна нет (мало истории) — trend None, а не нули: ноль соврал бы,
    что «всё по нулям».
    """
    from app.db.models import RecommendationFeedback as _RF

    days = max(1, min(365, int(days or 7)))
    since = _utcnow() - timedelta(days=days)
    prev_since = since - timedelta(days=days)

    def _rows(gte, lt=None):
        q = db.query(_RF).filter(_RF.served_at >= gte)
        if lt is not None:
            q = q.filter(_RF.served_at < lt)
        if user_id:
            q = q.filter(_RF.user_id == str(user_id))
        if source:
            q = q.filter(_RF.source == str(source))
        try:
            return q.all()
        except Exception:
            return []

    rows = _rows(since)
    prev_rows = _rows(prev_since, since)
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
    # Расклад по позиции в выдаче: работает ли ранжир вообще.
    # Было — бакеты по score: волна пишет SPREAD-скор (min-max окна),
    # daily/smart/weekly — сырой, и 1.0 одного окна ≠ 1.0 другого.
    # Плюс сырой total упирается в кламп 1.0. Позиция (rank) от окна
    # не зависит: топ-3 должны скипаться реже хвоста, иначе ранжировать
    # бессмысленно. Строки без rank (старые) — в отдельный бакет.
    by_score: list[dict[str, Any]] = []
    for pred, name in ((lambda r: r.rank is not None and int(r.rank) < 3, "топ-3"),
                       (lambda r: r.rank is not None and 3 <= int(r.rank) < 10, "4–10"),
                       (lambda r: r.rank is not None and int(r.rank) >= 10, "11+")):
        bucket = [r for r in rows if r.outcome and _rank_ok(r, pred)]
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
        # Тренд vs прошлое окно той же длины (п.п.; минус у скипов = лучше).
        "trend": _build_trend(rows, prev_rows),
    }


def _build_trend(rows: list, prev_rows: list) -> dict[str, Any] | None:
    """Дельты текущего окна к прошлому. Мало истории — None."""
    try:
        if not prev_rows:
            return None
        cur = _window_rates(rows)
        prev = _window_rates(prev_rows)
        if not prev.get("decided"):
            return None
        trend: dict[str, Any] = {
            "served": cur["served"], "prev_served": prev["served"],
            "decided": cur["decided"], "prev_decided": prev["decided"],
            "early_skip_rate": _trend_delta(cur["early_skip_rate"],
                                            prev["early_skip_rate"]),
            "skip_rate": _trend_delta(cur["skip_rate"], prev["skip_rate"]),
            "completion_rate": _trend_delta(cur["completion_rate"],
                                            prev["completion_rate"]),
            "like_rate": _trend_delta(cur["like_rate"], prev["like_rate"]),
        }
        by_src: dict[str, dict[str, Any]] = {}
        for src, cb in cur["by_source"].items():
            pb = prev["by_source"].get(src)
            if not pb or not pb.get("decided"):
                continue
            by_src[src] = {
                "early_skip_rate": _trend_delta(cb["early_skip_rate"],
                                                pb["early_skip_rate"]),
                "completion_rate": _trend_delta(cb["completion_rate"],
                                                pb["completion_rate"]),
                "like_rate": _trend_delta(cb["like_rate"], pb["like_rate"]),
            }
        trend["by_source"] = by_src
        return trend
    except Exception:
        return None


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
