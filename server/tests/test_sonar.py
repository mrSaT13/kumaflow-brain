"""Сонар: DSP на синтезированных тонах + матчинг/дубли/enrich в sqlite."""
import os

os.environ["REDIS_HOST"] = "localhost"

import uuid as _uuid
import wave as _wave_mod

import numpy as _np

from app.db.database import SessionLocal, init_db
from app.db.models import AudioFingerprint, MediaServer, MediaUser, Track
from app.services import sonar as _sonar

SR = 11025


def _tone(freqs, seconds, noise=0.0, seed=1):
    rnd = _np.random.RandomState(seed)
    t = _np.arange(int(SR * seconds)) / SR
    y = sum(_np.sin(2 * _np.pi * f * t) for f in freqs) / max(len(freqs), 1)
    if noise:
        y = y + rnd.randn(*y.shape) * noise
    y = y / max(abs(y).max(), 1e-6) * 0.8
    return (y * 32767).astype(_np.int16)


def _wav_bytes(y):
    import io as _io
    buf = _io.BytesIO()
    with _wave_mod.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(y.tobytes())
    buf.seek(0)
    return buf


def _load_mem(y):
    import soundfile as _sf
    data, f_sr = _sf.read(_wav_bytes(y), always_2d=False)
    return _np.asarray(data, dtype=float), int(f_sr)


def test_stft_and_peaks_shape():
    y = _tone([440.0], 3.0)
    S, df, dt = _sonar.stft_mag(y.astype(float) / 32768.0, SR)
    assert S.shape[0] > 10 and S.shape[1] > 10
    peaks = _sonar.find_peaks(S, df, dt)
    assert len(peaks) > 5  # чистый тон даёт устойчивые пики
    bins = [b for b, _ in peaks]
    assert min(bins) >= 0


def test_silence_gives_no_hashes():
    y = _np.zeros(SR * 5)
    assert _sonar.fingerprint_samples(y, SR) == []


def test_hashes_deterministic_and_budgeted():
    y, f_sr = _load_mem(_tone([440.0, 660.0], 8.0))
    h1 = _sonar.fingerprint_samples(y, f_sr)
    h2 = _sonar.fingerprint_samples(y, f_sr)
    assert h1 == h2 and len(h1) > 20
    # бюджет: не больше 12/с
    from collections import Counter as _C
    per_sec = _C(int(t) for _, t in h1)
    assert max(per_sec.values()) <= _sonar.MAX_HASHES_PER_SEC


def _seed_tracks(db):
    srv = db.query(MediaServer).filter_by(name="t", url="http://x").first()
    if srv is None:
        srv = MediaServer(id=str(_uuid.uuid4()), type="navidrome",
                          name="t", url="http://x", enabled=True)
        db.add(srv)
        db.commit()
    specs = [
        ("sn-a", [440.0, 660.0, 880.0], "Rock", 2010, "Alpha"),
        ("sn-b", [330.0, 550.0, 770.0], None, None, None),
        ("sn-c", [440.0, 660.0, 880.0], "Rock", 2010, "Alpha"),  # дубль A
    ]
    tids = {}
    for ext, freqs, genre, year, album in specs:
        t = db.query(Track).filter_by(server_id=srv.id, external_id=ext).first()
        if t is None:
            t = Track(id=str(_uuid.uuid4()), server_id=srv.id, external_id=ext,
                      title=f"Sonar {ext}", artist_name="Sonartist",
                      genre=genre, year=year, album_name=album)
            db.add(t)
            db.commit()
        tids[ext] = (str(t.id), freqs)
    return tids


def _enroll(db, tids):
    for ext, (tid, freqs) in tids.items():
        y, f_sr = _load_mem(_tone(freqs, 12.0))
        hh = _sonar.fingerprint_samples(y, f_sr)
        assert len(hh) > 30, (ext, len(hh))
        assert _sonar.store(db, tid, hh) == len(hh)


def test_recognize_offset_excerpt_with_noise():
    db = SessionLocal()
    try:
        init_db()
        tids = _seed_tracks(db)
        _enroll(db, tids)
        tid_a = tids["sn-a"][0]
        # запрос: кусок A с 4с + шум микрофона
        full = _tone([440.0, 660.0, 880.0], 12.0).astype(float) / 32768.0
        q = full[4 * SR:12 * SR] + _np.random.RandomState(3).randn(8 * SR) * 0.05
        qh = _sonar.fingerprint_samples(q, SR, max_seconds=10.0)
        assert len(qh) > 10
        m = _sonar.recognize_track(db, qh)
        assert m is not None
        assert m["track_id"] in (tid_a, tids["sn-c"][0])  # A или её дубль
        assert m["external_id"] in ("sn-a", "sn-c")
    finally:
        db.close()


def test_recognize_foreign_song_returns_none():
    db = SessionLocal()
    try:
        tids = _seed_tracks(db)
        _enroll(db, tids)
        y, f_sr = _load_mem(_tone([200.0, 990.0, 1500.0], 10.0))
        qh = _sonar.fingerprint_samples(y, f_sr)
        assert _sonar.recognize(db, qh) is None
    finally:
        db.close()


def test_duplicates_finds_twin_pair():
    db = SessionLocal()
    try:
        tids = _seed_tracks(db)
        _enroll(db, tids)
        groups = _sonar.duplicates(db, min_shared=10)
        pairs = {(g["track_a"], g["track_b"]) for g in groups}
        a, c = tids["sn-a"][0], tids["sn-c"][0]
        assert (a, c) in pairs or (c, a) in pairs
        # у чужой песни общих хэшей нет
        b = tids["sn-b"][0]
        assert all(b not in (g["track_a"], g["track_b"]) for g in groups)
    finally:
        db.close()


def test_enrich_fills_empties_only():
    db = SessionLocal()
    try:
        tids = _seed_tracks(db)
        _enroll(db, tids)
        # C — двойник A: стираем мету, enrich должен долить из A
        c = db.query(Track).filter_by(external_id="sn-c").first()
        c.genre = None
        c.year = None
        c.album_name = None
        db.commit()
        res = _sonar.enrich(db, min_shared=10)
        assert res["ok"] is True
        assert res["pairs"] >= 1
        db.refresh(c)
        assert c.genre == "Rock" and c.year == 2010 and c.album_name == "Alpha"
        assert res["tracks"] >= 1
        # у A ничего не затерлось
        t = db.query(Track).filter_by(external_id="sn-a").first()
        assert t.genre == "Rock" and t.year == 2010 and t.album_name == "Alpha"
    finally:
        db.close()


def test_coverage_counts():
    db = SessionLocal()
    try:
        tids = _seed_tracks(db)
        _enroll(db, tids)
        cov = _sonar.coverage(db)
        assert cov["fingerprinted"] >= 3
        assert cov["hashes_total"] > 100
        assert cov["tracks_total"] >= 3
    finally:
        db.close()
