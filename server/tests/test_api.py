from fastapi.testclient import TestClient
import os
os.environ["REDIS_HOST"] = "localhost"

from app.main import app

client = TestClient(app)

def test_health():
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

def test_yandex_status():
    r = client.get("/api/yandex/status")
    assert r.status_code == 200
    assert "enabled" in r.json()

def test_yandex_enrich_501():
    r = client.post("/api/yandex/enrich")
    assert r.status_code in (400, 501)  # 400 без токена, 501 legacy

def test_search_by_text_empty():
    r = client.post("/api/analysis/search-by-text", json={"q": ""})
    assert r.status_code == 200
    assert r.json()["items"] == []

def test_search_by_text_keyword():
    r = client.post("/api/analysis/search-by-text", json={"q": "test", "top_k": 5})
    assert r.status_code == 200
    assert "mode" in r.json()
    assert r.json()["mode"] in ("keyword", "empty")

