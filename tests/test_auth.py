"""Authentication: password hashing, signed tokens, login endpoint, and the gate."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.auth import (
    UserError,
    change_password,
    create_token,
    create_user,
    decode_token,
    hash_password,
    set_active,
    verify_password,
)
from app.database import SessionLocal
from app.main import app


def test_password_hash_roundtrip():
    h = hash_password("s3cret!")
    assert h != "s3cret!" and h.startswith("pbkdf2_sha256$")
    assert verify_password("s3cret!", h) is True
    assert verify_password("wrong", h) is False


def test_token_roundtrip_and_tamper():
    t = create_token("admin")
    assert decode_token(t) == "admin"
    assert decode_token(t + "x") is None          # tampered
    assert decode_token("not-a-token") is None


def test_login_endpoint():
    with TestClient(app) as client:
        # Bootstrap admin (admin / admin123) is created on startup.
        ok = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        assert ok.status_code == 200, ok.text
        body = ok.json()
        assert body["token_type"] == "bearer" and body["user"]["username"] == "admin"
        assert decode_token(body["access_token"]) == "admin"
        bad = client.post("/api/auth/login", json={"username": "admin", "password": "nope"})
        assert bad.status_code == 401


def test_me_reports_auth_state():
    with TestClient(app) as client:
        # Auth disabled in the test env → pass-through identity.
        me = client.get("/api/auth/me").json()
        assert me["auth_enabled"] is False
        cfg = client.get("/api/auth/config").json()
        assert cfg["auth_enabled"] is False


def test_create_user_and_change_password():
    with SessionLocal() as db:
        u = create_user(db, "alice_pw", "Initpass#123", "accountant")
        assert u.role == "accountant" and u.is_active
        with pytest.raises(UserError):
            create_user(db, "alice_pw", "Another#123")         # duplicate username
        with pytest.raises(UserError):
            create_user(db, "shorty", "short")                 # password too short
        with pytest.raises(UserError):
            create_user(db, "weakpw", "alllowercase")          # only one character class
        with pytest.raises(UserError):
            create_user(db, "badrole", "Longenough#1", "king")  # invalid role

        with pytest.raises(UserError):
            change_password(db, "alice_pw", "wrong", "Newpass#123")     # wrong current
        with pytest.raises(UserError):
            change_password(db, "alice_pw", "Initpass#123", "short")    # too short
        with pytest.raises(UserError):
            change_password(db, "alice_pw", "Initpass#123", "Initpass#123")  # unchanged
        change_password(db, "alice_pw", "Initpass#123", "Newpass#456")
        db.refresh(u)
        assert verify_password("Newpass#456", u.password_hash)


def test_cannot_deactivate_self():
    with SessionLocal() as db:
        admin = create_user(db, "self_admin", "Adminpass#1", "admin")
        with pytest.raises(UserError):
            set_active(db, admin.id, False, acting_user_id=admin.id)


def test_user_management_endpoints():
    with TestClient(app) as client:
        r = client.post("/api/auth/users",
                        json={"username": "carol_api", "password": "Carolpass#1", "role": "viewer"})
        assert r.status_code == 200, r.text
        uid = r.json()["id"]
        assert any(u["username"] == "carol_api" for u in client.get("/api/auth/users").json())
        # duplicate → 400
        assert client.post("/api/auth/users",
                           json={"username": "carol_api", "password": "Carolpass#1"}).status_code == 400
        # admin reset password
        assert client.post(f"/api/auth/users/{uid}/reset-password",
                           json={"new_password": "Resetpass#1"}).status_code == 200
        # deactivate
        rr = client.post(f"/api/auth/users/{uid}/active", json={"is_active": False})
        assert rr.status_code == 200 and rr.json()["is_active"] is False


def test_change_password_endpoint_blocked_when_auth_disabled():
    with TestClient(app) as client:
        r = client.post("/api/auth/change-password",
                        json={"current_password": "x", "new_password": "yyyyyyyy"})
        assert r.status_code == 400  # no authenticated user to change
