"""AI по тексту не перезаписывает акустические valence/arousal/energy."""
import os

os.environ["REDIS_HOST"] = "localhost"

import uuid

from fastapi.testclient import TestClient

from app.db import models
from app.db.database import SessionLocal
from app.main import app

client = TestClient(app)


def _track(db):
    srv = db.query(models.MediaServer).first()
    if srv is None:
        srv = models.MediaServer(type="navidrome", name="lyr-t", url="http://x")
        db.add(srv)
        db.flush()
    t = models.Track(server_id=srv.id, external_id="lyr-" + uuid.uuid4().hex[:10],
                     title="song", artist_name="Artist")
    db.add(t)
    db.flush()
    return t


def _fake_analyze(_text):
    return {"valence": 0.1, "arousal": 0.1, "energy": 0.1,
            "moods": ["грустный"], "sentiment": "sad", "language": "en",
            "themes": ["loss"]}


def test_refresh_keeps_audio_axes_but_adds_ai_words(monkeypatch):
    from app.api import lyrics as lyrics_api

    db = SessionLocal()
    t = _track(db)
    f = models.TrackFeatures(track_id=str(t.id), valence=0.9, arousal=0.8, energy=0.7,
                             mood_labels=["спокойный"], mood_vector={"calm": 0.8})
    db.add(f)
    db.commit()
    monkeypatch.setattr(lyrics_api.lyrics_svc, "fetch",
                        lambda **kw: {"text": "words", "language": "en"})
    monkeypatch.setattr(lyrics_api.lyrics_ai, "analyze", _fake_analyze)

    r = client.post(f"/api/lyrics/{t.id}/refresh")
    assert r.status_code == 200
    db.expire_all()
    f2 = db.get(models.TrackFeatures, str(t.id))
    assert (f2.valence, f2.arousal, f2.energy) == (0.9, 0.8, 0.7)
    assert "грустный" in f2.mood_labels and "спокойный" in f2.mood_labels
    assert f2.mood_vector["ai_sentiment"] == "sad"


def test_refresh_fills_axes_when_no_audio_analysis(monkeypatch):
    from app.api import lyrics as lyrics_api

    db = SessionLocal()
    t = _track(db)
    monkeypatch.setattr(lyrics_api.lyrics_svc, "fetch",
                        lambda **kw: {"text": "words", "language": "en"})
    monkeypatch.setattr(lyrics_api.lyrics_ai, "analyze", _fake_analyze)

    r = client.post(f"/api/lyrics/{t.id}/refresh")
    assert r.status_code == 200
    f = db.get(models.TrackFeatures, str(t.id))
    assert f is not None and abs(float(f.valence) - 0.1) < 1e-6
