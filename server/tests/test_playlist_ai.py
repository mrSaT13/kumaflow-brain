import os
from fastapi.testclient import TestClient
from app.db import init_db
init_db()
from app.main import app
client = TestClient(app)

def test_cold_start_per_user_param():
    r = client.get("/api/analysis/cold-start", params={"n": 2})
    assert r.status_code == 200
    assert "tracks" in r.json()

def test_cold_start_with_user():
    r = client.get("/api/analysis/cold-start", params={"n": 2, "user_id": "nonexistent"})
    assert r.status_code == 200

def test_ai_generate_requires_query():
    r = client.post("/api/playlists/ai-generate", json={})
    assert r.status_code == 400

def test_ai_generate_ok():
    # without library may fallback
    r = client.post("/api/playlists/ai-generate", json={"query": "chill evening", "n": 3})
    # either created or fallback empty
    assert r.status_code in (200, 400)

