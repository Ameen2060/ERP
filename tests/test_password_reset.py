"""Password reset / forgot-password flow, admin-initiated reset, strong-password policy,
single-use + expiry, anti-enumeration, rate limiting, and audit logging.

Auth is disabled in the default test settings, so these tests enable it explicitly. The app
uses a persistent SQLite file shared across tests, so each test provisions its OWN uniquely
named user (with a recovery email) to stay isolated from other tests' reset-token counts.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models import User

_PW = "Start#2026"


def _make_user(prefix: str) -> tuple[str, str]:
    """Create an active user with a known password + email; return (username, email)."""
    import secrets
    uname = f"{prefix}_{secrets.token_hex(4)}"
    email = f"{uname}@example.com"
    with SessionLocal() as db:
        db.add(User(username=uname, email=email, password_hash=auth.hash_password(_PW),
                    role="accountant", is_active=True))
        db.commit()
    return uname, email


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(get_settings(), "auth_enabled", True)
    with TestClient(app) as c:
        yield c


def _login(client, username, password):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def test_full_forgot_reset_flow(client):
    uname, email = _make_user("reset")
    r = client.post("/api/auth/forgot-password", json={"identifier": email})
    assert r.status_code == 200
    token = r.json().get("reset_token")
    assert token, r.json()

    weak = client.post("/api/auth/reset-password", json={"token": token, "new_password": "short"})
    assert weak.status_code == 400

    ok = client.post("/api/auth/reset-password", json={"token": token, "new_password": "NewPass#2026"})
    assert ok.status_code == 200

    assert _login(client, uname, _PW).status_code == 401          # old password invalidated
    assert _login(client, uname, "NewPass#2026").status_code == 200

    again = client.post("/api/auth/reset-password", json={"token": token, "new_password": "Another#99"})
    assert again.status_code == 400                                # single-use


def test_forgot_password_no_enumeration(client):
    _, email = _make_user("enum")
    known = client.post("/api/auth/forgot-password", json={"identifier": email})
    unknown = client.post("/api/auth/forgot-password", json={"identifier": "ghost@nowhere.test"})
    assert known.status_code == 200 and unknown.status_code == 200
    assert known.json()["message"] == unknown.json()["message"]
    assert "reset_token" not in unknown.json()


def test_rate_limiting(client, monkeypatch):
    uname, _ = _make_user("rate")
    monkeypatch.setattr(get_settings(), "reset_request_max", 2)
    for _ in range(2):
        assert client.post("/api/auth/forgot-password", json={"identifier": uname}).status_code == 200
    blocked = client.post("/api/auth/forgot-password", json={"identifier": uname})
    assert blocked.status_code == 429


def test_admin_initiated_reset_link(client):
    # Bootstrapped admin (admin/admin123) drives the admin-only endpoint.
    with SessionLocal() as db:
        a = db.query(User).filter(User.username == "admin").first()
        if not a:
            db.add(User(username="admin", password_hash=auth.hash_password("admin123"), role="admin"))
            db.commit()
        else:
            a.password_hash = auth.hash_password("admin123"); a.is_active = True; db.commit()
    tok = _login(client, "admin", "admin123").json()["access_token"]
    hdr = {"Authorization": f"Bearer {tok}"}
    uname, _ = _make_user("clerk")
    uid = client.get("/api/auth/users", headers=hdr).json()
    uid = next(u["id"] for u in uid if u["username"] == uname)
    link = client.post(f"/api/auth/users/{uid}/reset-link", headers=hdr)
    assert link.status_code == 200, link.text
    reset_token = link.json()["reset_token"]
    done = client.post("/api/auth/reset-password", json={"token": reset_token, "new_password": "Fresh#2026"})
    assert done.status_code == 200
    assert _login(client, uname, "Fresh#2026").status_code == 200


def test_change_password_requires_current(client):
    uname, _ = _make_user("chg")
    tok = _login(client, uname, _PW).json()["access_token"]
    hdr = {"Authorization": f"Bearer {tok}"}
    bad = client.post("/api/auth/change-password", headers=hdr,
                      json={"current_password": "wrong", "new_password": "Newer#2026"})
    assert bad.status_code == 400
    good = client.post("/api/auth/change-password", headers=hdr,
                       json={"current_password": _PW, "new_password": "Newer#2026"})
    assert good.status_code == 200
    assert _login(client, uname, "Newer#2026").status_code == 200


def test_security_events_logged(client):
    uname, email = _make_user("audit")
    client.post("/api/auth/forgot-password", json={"identifier": email})
    with SessionLocal() as db:
        events = auth.list_security_events(db, username=uname)
    kinds = {e.event for e in events}
    assert "reset_request" in kinds
