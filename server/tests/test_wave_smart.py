"""Key/оркестратор/кластеры/лирика в волне. Без сети, sqlite."""
import os

os.environ["REDIS_HOST"] = "localhost"

import uuid as _uuid

from app.db.database import SessionLocal, init_db
from app.db.models import MediaServer, MediaUser, Track, TrackCluster, TrackFeatures
from app.services import wave as _wave
from app.services.orchestrator import key_compatibility as _kc


def _seed():
    # Идемпотентно: engine общий на все модули (первый импорт решает файл),
    # повторный прогон не должен плодить дубли.
    init_db()
    db = SessionLocal()
    srv = db.query(MediaServer).filter_by(name="t", url="http://x").first()
    if srv is None:
        srv = MediaServer(id=str(_uuid.uuid4()), type="navidrome", name="t",
                          url="http://x", enabled=True)
        db.add(srv)
        db.commit()
    u = db.query(MediaUser).filter_by(username="smart_tester").first()
    if u is None:
        u = MediaUser(id=str(_uuid.uuid4()), server_id=srv.id,
                      external_id="smart_tester", username="smart_tester")
        db.add(u)
        db.commit()
    # 5 треков с разнесённой energy для проверки энергетической дуги
    energies = [0.05, 0.25, 0.45, 0.65, 0.85]
    keys = [("C", "major"), ("G", "major"), ("A", "minor"),
            ("D", "major"), ("F#", "major")]
    for i, (en, (kn, sc)) in enumerate(zip(energies, keys)):
        t = db.query(Track).filter_by(server_id=srv.id,
                                      external_id=f"sm-{i}").first()
        if t is None:
            t = Track(id=str(_uuid.uuid4()), server_id=srv.id,
                      external_id=f"sm-{i}", title=f"Smart {i}",
                      artist_name=f"Smartist {i}")
            db.add(t)
            db.commit()
        if db.get(TrackFeatures, str(t.id)) is None:
            db.add(TrackFeatures(track_id=str(t.id), energy=en, tempo_bpm=120.0,
                                 key_name=kn, scale=sc,
                                 mood_vector={"ai_sentiment": "positive"},
                                 mood_labels=["happy"]))
            db.commit()
    uid = u.id
    db.close()
    return uid


UID = _seed()


def _db():
    return SessionLocal()


def test_key_compatibility_fifths():
    assert _kc("C", "major", "C", "major") == 1.0
    assert _kc("C", "major", "A", "minor") == 0.9  # относительные
    assert _kc("C", "major", "G", "major") == 0.8  # квинта
    assert _kc("C", "major", "F#", "major") == 0.2  # тритон/далеко
    assert _kc(None, None, "C", "major") == 0.5  # неизвестно — нейтрально
    assert _kc("G#", "minor", "g#", "minor") == 1.0  # регистр не важен


def test_smooth_keys_improves_transitions():
    items = [{"track_id": "a", "key": "C", "scale": "major", "energy": 0.5},
             {"track_id": "b", "key": "F#", "scale": "major", "energy": 0.5},
             {"track_id": "c", "key": "G", "scale": "major", "energy": 0.5}]

    def _tot(ts):
        return sum(_kc(x["key"], x["scale"], y["key"], y["scale"])
                   for x, y in zip(ts, ts[1:]))

    out = _wave._smooth_keys_order(items)
    assert {r["track_id"] for r in out} == {"a", "b", "c"}  # множество то же
    assert _tot(out) == 1.0  # C-G + G-F# вместо C-F# + F#-G
    assert _tot(items) == 0.4


def test_key_and_lyrics_comps_in_score():
    db = _db()
    try:
        ids = [str(r.id) for r in
               db.query(Track).filter(Track.external_id.like("sm-%")).all()]
        ranked = _wave.score_candidates(
            db, UID, ids, [ids[0]], settings={}, recent_events=None,
            collab_scores={}, jitter_seed="fixed",
            ref_key=("C", "major"), target_sentiment="positive")
        by_id = {r["track_id"]: r for r in ranked}
        # sm-0: C-dur + positive sentiment
        assert by_id[ids[0]]["comp"]["key"] == 1.0
        assert by_id[ids[0]]["comp"]["lyrics"] == 1.0
        # sm-4: F# далеко от C
        assert by_id[ids[4]]["comp"]["key"] == 0.2
    finally:
        db.close()


def test_cluster_bonus_prefers_same_shelf():
    db = _db()
    try:
        ids = [str(r.id) for r in
               db.query(Track).filter(Track.external_id.like("sm-%")).all()]
        for tid, cid in [(ids[0], 7), (ids[1], 7), (ids[2], 9)]:
            db.add(TrackCluster(track_id=tid, algorithm="kmeans",
                                cluster_id=cid))
        db.commit()
        cmap = {ids[0]: 7, ids[1]: 7, ids[2]: 9}
        ranked = _wave.score_candidates(
            db, UID, ids, [ids[0]], settings={}, recent_events=None,
            collab_scores={}, jitter_seed="fixed",
            cluster_map=cmap, seed_clusters={7})
        by_id = {r["track_id"]: r for r in ranked}
        assert by_id[ids[1]]["comp"]["cluster"] == 1.0
        assert by_id[ids[2]]["comp"]["cluster"] == 0.0
    finally:
        db.query(TrackCluster).delete()
        db.commit()
        db.close()


def test_warming_drift_on_likes():
    db = _db()
    try:
        evs = [{"track_id": "sm-0", "action": "like"},
               {"track_id": "sm-1", "action": "replay"},
               {"track_id": "sm-2", "action": "like"}]
        d = _wave.session_drift(db, evs)
        assert d["warmth"] == "mild" and d["positive_streak"] == 3
        assert d["energy_shift"] == 0.1 and d["severity"] is None
    finally:
        db.close()


def _sm_ids(db):
    return [str(r.id) for r in
            db.query(Track).filter(Track.external_id.like("sm-%"))
            .order_by(Track.external_id).all()]


def test_negative_vector_penalizes_similar():
    from app.services.ml import _cosine, _feature_vector
    db = _db()
    try:
        ids = _sm_ids(db)
        plain = {r["track_id"]: r for r in _wave.score_candidates(
            db, UID, ids, [ids[0]], settings={}, recent_events=None,
            collab_scores={}, jitter_seed="neg")}
        neg = {r["track_id"]: r for r in _wave.score_candidates(
            db, UID, ids, [ids[0]], settings={}, recent_events=None,
            collab_scores={}, jitter_seed="neg", neg_ids=[ids[0]])}
        assert neg[ids[1]]["comp"]["neg"] > 0.1
        assert neg[ids[1]]["score"] < plain[ids[1]]["score"]
    finally:
        db.close()


def test_session_fingerprint_boosts_current_vibe():
    db = _db()
    try:
        ids = _sm_ids(db)
        plain = {r["track_id"]: r for r in _wave.score_candidates(
            db, UID, ids, [ids[0]], settings={}, recent_events=None,
            collab_scores={}, jitter_seed="sess")}
        sess = {r["track_id"]: r for r in _wave.score_candidates(
            db, UID, ids, [ids[0]], settings={}, recent_events=None,
            collab_scores={}, jitter_seed="sess", session_ids=[ids[4]])}
        # сессия тянет к далёкому вайбу (0.85): audio sm-1 падает
        assert sess[ids[1]]["comp"]["audio"] < plain[ids[1]]["comp"]["audio"]
    finally:
        db.close()


def test_recency_decay_and_fatigue():
    from datetime import datetime, timedelta
    db = _db()
    try:
        ids = _sm_ids(db)
        base = {r["track_id"]: r for r in _wave.score_candidates(
            db, UID, ids, [ids[0]], settings={}, recent_events=None,
            collab_scores={}, jitter_seed="rec")}
        recent = {r["track_id"]: r for r in _wave.score_candidates(
            db, UID, ids, [ids[0]], settings={}, recent_events=None,
            collab_scores={}, jitter_seed="rec",
            last_played={ids[1]: datetime.utcnow() - timedelta(hours=1)})}
        assert recent[ids[1]]["comp"]["novelty"] < \
            base[ids[1]]["comp"]["novelty"]
        tired = {r["track_id"]: r for r in _wave.score_candidates(
            db, UID, ids, [ids[0]], settings={}, recent_events=None,
            collab_scores={}, jitter_seed="rec",
            fatigue={"Smartist 1": 20})}
        assert tired[ids[1]]["score"] < base[ids[1]]["score"]
    finally:
        db.close()


def test_bridge_pulls_smooth_first_track():
    db = _db()
    try:
        others = [str(r[0]) for r in db.query(Track.external_id).all()
                  if not str(r[0]).startswith("sm-")]
        res = _wave.wave_continue(db, UID, queue=[], count=4,
                                  exclude_ids=others,
                                  current_track_id="sm-4")
        assert len(res["tracks"]) == 4
        # текущий sm-4 (0.85, F#): лучший мостик — sm-3 (0.65, ближе всех)
        assert res["tracks"][0]["external_id"] == "sm-3"
    finally:
        db.close()


def test_time_bonus_and_arms_from_events():
    from app.db.models import TasteArm, TrackTimeStat
    from app.services import taste as _taste
    from app.services.taste import context_of as _ctx_of
    db = _db()
    try:
        db.query(TrackTimeStat).filter_by(user_id=UID).delete()
        db.query(TasteArm).filter_by(user_id=UID).delete()
        db.commit()
        res = _taste.record_events(db, UID, [
            {"track_id": "sm-0", "action": "play", "position_sec": 200},
            {"track_id": "sm-0", "action": "replay"},
        ])
        assert res["stored"] == 2
        try:
            from app.core.time import local_hour as _lh
            hour = _lh()
        except Exception:
            from datetime import datetime as _dt
            hour = _dt.utcnow().hour
        by_ext = {str(r.external_id): str(r.id) for r in
                  db.query(Track).filter(Track.external_id.like("sm-%")).all()}
        sm0, sm1 = by_ext["sm-0"], by_ext["sm-1"]
        tb = _wave.time_bonus_for(db, UID, [sm0, sm1], hour)
        assert tb.get(sm0, 0.0) > 0.0
        assert tb.get(sm1, 0.0) == 0.0
        # руки: replay +15 и play-дослушивание +5 на артиста sm-0
        try:
            from app.core.time import server_now as _sn
            ctx = _ctx_of(hour, _sn().isoweekday() % 7)
        except Exception:
            ctx = "day_wd"
        ab, _gb = _wave.arm_boost_for(db, UID, ctx)
        assert ab.get("Smartist 0", 0.0) > 0.0
    finally:
        db.query(TrackTimeStat).filter_by(user_id=UID).delete()
        db.query(TasteArm).filter_by(user_id=UID).delete()
        db.commit()
        db.close()


def test_session_assoc_from_history():
    from datetime import datetime, timedelta
    from app.db.models import PlayHistory as _PH
    db = _db()
    try:
        tids = {str(r.external_id): str(r.id) for r in
                db.query(Track).filter(Track.external_id.like("sm-%")).all()}
        base = datetime.utcnow() - timedelta(hours=2)
        db.query(_PH).filter_by(user_id=UID).delete()
        for i, ext in enumerate(["sm-0", "sm-1", "sm-2"]):
            db.add(_PH(user_id=UID, track_id=tids[ext],
                       played_at=base + timedelta(minutes=5 * i)))
        db.commit()
        assoc = _wave.session_assoc(db, UID, [tids["sm-0"]])
        assert assoc.get(tids["sm-1"], 0.0) > 0.0
        assert tids["sm-0"] not in assoc  # сиды исключаются
    finally:
        db.query(_PH).filter_by(user_id=UID).delete()
        db.commit()
        db.close()


def test_adaptive_count_units():
    assert _wave.adaptive_count(10, {}) == (10, None)
    assert _wave.adaptive_count(10, None) == (10, None)
    assert _wave.adaptive_count(3, {"severity": "strong"}) == (3, None)
    n, r = _wave.adaptive_count(10, {"severity": "mild"})
    assert n == 6 and r
    n, r = _wave.adaptive_count(20, {"severity": "moderate"})
    assert n == 5 and r
    n, r = _wave.adaptive_count(20, {"severity": "strong"})
    assert n == 4 and r
    n, r = _wave.adaptive_count(10, {}, morphing=True)
    assert n == 6 and "переход" in (r or "")


def test_wave_continue_adaptive_pack_on_skips():
    db = _db()
    try:
        others = [str(r[0]) for r in db.query(Track.external_id).all()
                  if not str(r[0]).startswith("sm-")]
        evs = [{"track_id": f"sm-{i % 2}", "action": "skip",
                "position_sec": 5} for i in range(7)]
        res = _wave.wave_continue(db, UID, queue=[], count=10,
                                  exclude_ids=others, recent_events=evs)
        assert res["count_requested"] == 10
        assert res["count_effective"] == 4
        assert res["adaptive"]
        assert len(res["tracks"]) <= 4
    finally:
        db.close()


def test_wave_continue_full_pack_when_stable():
    db = _db()
    try:
        others = [str(r[0]) for r in db.query(Track.external_id).all()
                  if not str(r[0]).startswith("sm-")]
        res = _wave.wave_continue(db, UID, queue=[], count=10,
                                  exclude_ids=others)
        assert res["adaptive"] is None
        assert res["count_effective"] == 10
        assert len(res["tracks"]) == 5
    finally:
        db.close()


def test_energy_wave_arc_without_mood():
    db = _db()
    try:
        # Изоляция пула: тестовые модули делят один sqlite-engine (создаётся
        # на импорт), поэтому чужие строки выкидываем через exclude_ids.
        others = [str(r[0]) for r in db.query(Track.external_id).all()
                  if not str(r[0]).startswith("sm-")]
        res = _wave.wave_continue(db, UID, queue=[], count=5,
                                  exclude_ids=others)
        assert len(res["tracks"]) == 5
        ens = [t["energy"] for t in res["tracks"]]
        assert all(e is not None for e in ens)
        assert ens[0] == min(ens)  # старт спокойный
        assert ens[-1] == max(ens)  # хвост — пик сегмента
        assert all(t.get("key") for t in res["tracks"])  # key обогащён
    finally:
        db.close()

