"""Per-user доступ к /api/settings/rec-tuning: свой overlay — можно,
чужой и глобал — только админ."""
import os

os.environ["REDIS_HOST"] = "localhost"

from fastapi.testclient import TestClient

from app.db import models
from app.db.database import SessionLocal
from app.main import app
from app.services import api_tokens as _tokens

client = TestClient(app)


def _setup():
    db = SessionLocal()
    try:
        srv = db.query(models.MediaServer).first()
        if srv is None:
            srv = models.MediaServer(type="navidrome", name="t", url="http://x")
            db.add(srv)
            db.flush()
        u1 = models.MediaUser(server_id=srv.id, external_id="tune-auth-u1",
                             username="tune-auth-u1")
        u2 = models.MediaUser(server_id=srv.id, external_id="tune-auth-u2",
                             username="tune-auth-u2")
        db.add(u1)
        db.add(u2)
        db.flush()
        t_owner = _tokens.create_token(db, str(u1.id), "owner-sync", ["sync"])["token"]
        t_other = _tokens.create_token(db, str(u2.id), "other-sync", ["sync"])["token"]
        t_admin = _tokens.create_token(db, None, "admin", ["admin"])["token"]
        return str(u1.id), str(u2.id), t_owner, t_other, t_admin
    finally:
        db.close()


def _h(tok):
    return {"Authorization": f"Bearer {tok}"}


def test_owner_writes_own_overlay():
    u1, _, t_owner, _, _ = _setup()
    r = client.put("/api/settings/rec-tuning",
                   json={"user_id": u1, "character": {"depth": 0.5}},
                   headers=_h(t_owner))
    assert r.status_code == 200, r.text
    assert r.json()["effective"]["character"]["depth"] == 0.5


def test_owner_cannot_write_global_or_foreign():
    u1, u2, t_owner, t_other, _ = _setup()
    # глобал без user_id — только админ
    r = client.put("/api/settings/rec-tuning",
                   json={"character": {"depth": 0.5}},
                   headers=_h(t_owner))
    assert r.status_code == 403
    # чужой оверлей своим токеном
    r = client.put("/api/settings/rec-tuning",
                   json={"user_id": u2, "character": {"depth": 0.5}},
                   headers=_h(t_owner))
    assert r.status_code == 403
    # и наоборот
    r = client.put("/api/settings/rec-tuning",
                   json={"user_id": u1, "character": {"depth": 0.5}},
                   headers=_h(t_other))
    assert r.status_code == 403


def test_admin_writes_global():
    _, _, _, _, t_admin = _setup()
    r = client.put("/api/settings/rec-tuning",
                   json={"character": {"depth": -0.5}},
                   headers=_h(t_admin))
    assert r.status_code == 200, r.text
    assert r.json()["effective"]["character"]["depth"] == -0.5


def test_owner_reads_effective():
    u1, _, t_owner, _, _ = _setup()
    r = client.get("/api/settings/rec-tuning", params={"user_id": u1},
                   headers=_h(t_owner))
    assert r.status_code == 200, r.text
    assert "effective" in r.json()
