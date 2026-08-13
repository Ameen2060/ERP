"""Editable role→action permission matrix: defaults seed, admin can toggle, changes take
effect on the gates, and admin can never be locked out."""

from __future__ import annotations

import secrets

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models import User


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(get_settings(), "auth_enabled", True)
    with TestClient(app) as c:   # lifespan creates tables + seeds before we touch the DB
        with SessionLocal() as db:
            a = db.query(User).filter(User.username == "admin").first()
            if not a:
                db.add(User(username="admin", password_hash=auth.hash_password("admin123"), role="admin")); db.commit()
            else:
                a.password_hash = auth.hash_password("admin123"); a.is_active = True; db.commit()
        yield c


def _hdr(client, user, pw):
    tok = client.post("/api/auth/login", json={"username": user, "password": pw}).json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}


def test_matrix_defaults_and_toggle_takes_effect(client):
    admin = _hdr(client, "admin", "admin123")
    m = client.get("/api/permissions/matrix", headers=admin).json()
    assert m["matrix"]["viewer"]["edit"] is False
    assert m["matrix"]["accountant"]["edit"] is True

    # Create an accountant; confirm they can edit a JE.
    an = f"acct_{secrets.token_hex(3)}"
    client.post("/api/auth/users", headers=admin, json={"username": an, "password": "Acct#123", "role": "accountant"})
    ah = _hdr(client, an, "Acct#123")
    acc = {a["code"]: a["id"] for a in client.get("/api/accounts", headers=ah).json()}
    je = client.post("/api/journal-entries", headers=ah, json={
        "date": "2064-01-01", "memo": "m",
        "lines": [{"account_id": acc["1010"], "debit": "10"}, {"account_id": acc["4010"], "credit": "10"}],
    }).json()
    ok = client.put(f"/api/journal-entries/{je['id']}?reason=x", headers=ah, json={
        "date": "2064-01-01", "memo": "m2",
        "lines": [{"account_id": acc["1010"], "debit": "10"}, {"account_id": acc["4010"], "credit": "10"}]})
    assert ok.status_code == 200

    # Revoke 'edit' from accountant → the same edit is now 403.
    upd = client.put("/api/permissions/matrix", headers=admin,
                     json={"matrix": {"accountant": {"edit": False}}})
    assert upd.status_code == 200 and upd.json()["matrix"]["accountant"]["edit"] is False
    denied = client.put(f"/api/journal-entries/{je['id']}?reason=y", headers=ah, json={
        "date": "2064-01-01", "memo": "m3",
        "lines": [{"account_id": acc["1010"], "debit": "10"}, {"account_id": acc["4010"], "credit": "10"}]})
    assert denied.status_code == 403
    # Restore for other tests.
    client.put("/api/permissions/matrix", headers=admin, json={"matrix": {"accountant": {"edit": True}}})


def test_admin_cannot_be_locked_out(client):
    admin = _hdr(client, "admin", "admin123")
    client.put("/api/permissions/matrix", headers=admin, json={"matrix": {"admin": {"edit": False}}})
    m = client.get("/api/permissions/matrix", headers=admin).json()
    assert m["matrix"]["admin"]["edit"] is True     # admin stays enabled


def test_non_admin_cannot_edit_matrix(client):
    admin = _hdr(client, "admin", "admin123")
    vn = f"view_{secrets.token_hex(3)}"
    client.post("/api/auth/users", headers=admin, json={"username": vn, "password": "View#123", "role": "viewer"})
    vh = _hdr(client, vn, "View#123")
    r = client.put("/api/permissions/matrix", headers=vh, json={"matrix": {"viewer": {"edit": True}}})
    assert r.status_code == 403
