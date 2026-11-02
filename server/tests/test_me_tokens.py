"""Самовыпуск токенов: обычный юзер выпускает/удаляет СВОИ токены без админа."""
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
        u1 = models.MediaUser(server_id=srv.id, external_id="me-tok-u1",
                              username="me-tok-u1")
        u2 = models.MediaUser(server_id=srv.id, external_id="me-tok-u2",
                              username="me-tok-u2")
        db.add(u1)
        db.add(u2)
        db.flush()
        t_user = _tokens.create_token(
            db, str(u1.id), "login-token",
            ["wave", "sync", "covers", "playlists"])["token"]
        t_other = _tokens.create_token(
            db, str(u2.id), "other-token",
            ["wave", "sync", "covers", "playlists"])["token"]
        t_admin = _tokens.create_token(db, None, "admin", ["admin"])["token"]
        return str(u1.id), str(u2.id), t_user, t_other, t_admin
    finally:
        db.close()


def _h(tok):
    return {"Authorization": f"Bearer {tok}"}


def test_user_creates_token_for_self():
    u1, _, t_user, _, _ = _setup()
    r = client.post("/api/me/tokens", json={"name": "мой телефон", "preset": "mobile"},
                    headers=_h(t_user))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["owner_user_id"] == u1
    assert body["token"]  # plaintext один раз
    assert set(body["scopes"]) == {"wave", "sync", "covers", "playlists"}
    # выпущенным токеном можно пользоваться сразу
    r2 = client.get("/api/me/tokens", headers={"Authorization": f"Bearer {body['token']}"})
    assert r2.status_code == 200, r2.text


def test_user_cannot_grab_admin_scope():
    _, _, t_user, _, _ = _setup()
    r = client.post("/api/me/tokens", json={"name": "хитрый", "scopes": ["admin"]},
                    headers=_h(t_user))
    assert r.status_code == 403
    r = client.post("/api/me/tokens", json={"name": "хитрый", "preset": "admin"},
                    headers=_h(t_user))
    assert r.status_code == 403


def test_user_cannot_delete_foreign_token():
    _, _, t_user, t_other, _ = _setup()
    # id чужого токена узнаём через свой список? нет — чужого там нет;
    # берём напрямую из БД
    db = SessionLocal()
    try:
        from app.db.models import ApiToken
        foreign = db.query(ApiToken).filter(
            ApiToken.owner_user_id == _tokens.verify(db, t_other)["owner_user_id"]).first()
        fid = str(foreign.id)
    finally:
        db.close()
    r = client.delete(f"/api/me/tokens/{fid}", headers=_h(t_user))
    assert r.status_code == 403


def test_user_deletes_own_token():
    _, _, t_user, _, _ = _setup()
    created = client.post("/api/me/tokens", json={"name": "временный"},
                          headers=_h(t_user)).json()
    r = client.delete(f"/api/me/tokens/{created['id']}", headers=_h(t_user))
    assert r.status_code == 200, r.text
    # удалённым токеном пользоваться нельзя
    r2 = client.get("/api/me/tokens",
                    headers={"Authorization": f"Bearer {created['token']}"})
    assert r2.status_code == 401


def test_admin_can_create_without_admin_scope_leak():
    # env-админу без владельца нужен явный user_id
    _, _, _, _, t_admin = _setup()
    r = client.post("/api/me/tokens", json={"name": "x"}, headers=_h(t_admin))
    assert r.status_code == 403
