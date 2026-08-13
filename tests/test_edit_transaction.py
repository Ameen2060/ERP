"""Reusable edit-transaction framework: Journal Entry (edit-in-place) and Expense
(reverse-and-repost) editing with recalculation, period lock, transaction audit trail
(reason + status), and role-based edit permission. 2057 dates."""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models import User


def _accounts(client):
    return {a["code"]: a["id"] for a in client.get("/api/accounts").json()}


def test_edit_journal_entry_updates_gl_and_audits():
    with TestClient(app) as client:
        acc = _accounts(client)
        cash, sales = acc["1010"], acc["4010"]
        je = client.post("/api/journal-entries", json={
            "date": "2057-01-05", "memo": "Original", "reference": "JV-A",
            "lines": [{"account_id": cash, "debit": "100"}, {"account_id": sales, "credit": "100"}],
        }).json()
        jid = je["id"]; entry_no = je["entry_no"]

        # Edit: change amount + memo. Same id + number preserved.
        up = client.put(f"/api/journal-entries/{jid}?reason=Correct+amount", json={
            "date": "2057-01-05", "memo": "Corrected", "reference": "JV-A",
            "lines": [{"account_id": cash, "debit": "250"}, {"account_id": sales, "credit": "250"}],
        })
        assert up.status_code == 200, up.text
        body = up.json()
        assert body["id"] == jid and body["entry_no"] == entry_no
        assert Decimal(body["total"]) == Decimal("250.00") and body["memo"] == "Corrected"

        # Reason is mandatory.
        assert client.put(f"/api/journal-entries/{jid}", json={
            "date": "2057-01-05", "memo": "x",
            "lines": [{"account_id": cash, "debit": "250"}, {"account_id": sales, "credit": "250"}],
        }).status_code == 400

        # Audit records the edit with before→after + reason.
        audit = client.get(f"/api/journal-entries/{jid}/audit").json()
        assert audit and audit[0]["reason"] == "Correct amount"
        fields = {c["field"] for c in audit[0]["changes"]}
        assert "total" in fields and "memo" in fields


def test_edit_expense_reverses_and_reposts_with_audit():
    with TestClient(app) as client:
        acc = _accounts(client)
        exp = client.post("/api/expenses", json={
            "date": "2057-02-01", "category": "Freight", "expense_account_id": acc["5090"],
            "payment_account_id": acc["1020"], "paid_directly": True,
            "net_amount": "1000", "vat_rate": "0.05"}).json()
        eid = exp["id"]; num = exp["number"]
        old_je = exp["journal_entry_id"]

        up = client.put(f"/api/expenses/{eid}?reason=Wrong+amount", json={
            "date": "2057-02-01", "category": "Freight", "expense_account_id": acc["5090"],
            "payment_account_id": acc["1020"], "paid_directly": True,
            "net_amount": "2000", "vat_rate": "0.05"})
        assert up.status_code == 200, up.text
        body = up.json()
        assert body["id"] == eid and body["number"] == num          # identity preserved
        assert body["net_amount"] == "2000.00" and body["vat_amount"] == "100.00"
        assert body["total_amount"] == "2100.00"                     # recalculated
        assert body["journal_entry_id"] != old_je                    # reposted onto a fresh JE

        # Old JE was voided (reversed out of the GL).
        assert client.get(f"/api/journal-entries/{old_je}").json()["status"] == "void"

        audit = client.get(f"/api/expenses/{eid}/audit").json()
        assert audit[0]["reason"] == "Wrong amount" and audit[0]["prev_status"] == "posted"
        changed = {c["field"] for c in audit[0]["changes"]}
        assert {"net_amount", "total_amount"} <= changed


def test_period_lock_blocks_edit():
    with TestClient(app) as client:
        acc = _accounts(client)
        je = client.post("/api/journal-entries", json={
            "date": "2057-03-01", "memo": "Locked period",
            "lines": [{"account_id": acc["1010"], "debit": "50"}, {"account_id": acc["4010"], "credit": "50"}],
        }).json()
        # Lock books on/before 2057-03-31.
        client.post("/api/system-settings/period-lock?lock_date=2057-03-31")
        blocked = client.put(f"/api/journal-entries/{je['id']}?reason=x", json={
            "date": "2057-03-01", "memo": "y",
            "lines": [{"account_id": acc["1010"], "debit": "60"}, {"account_id": acc["4010"], "credit": "60"}],
        })
        assert blocked.status_code == 400 and "locked" in blocked.text.lower()
        client.post("/api/system-settings/period-lock?lock_date=")  # unlock for other tests


def test_edit_permission_denied_for_viewer(monkeypatch):
    monkeypatch.setattr(get_settings(), "auth_enabled", True)
    with TestClient(app) as client:
        with SessionLocal() as db:
            a = db.query(User).filter(User.username == "admin").first()
            if not a:
                db.add(User(username="admin", password_hash=auth.hash_password("admin123"), role="admin")); db.commit()
            else:
                a.password_hash = auth.hash_password("admin123"); a.is_active = True; db.commit()
        atok = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).json()["access_token"]
        ahdr = {"Authorization": f"Bearer {atok}"}
        acc = {a["code"]: a["id"] for a in client.get("/api/accounts", headers=ahdr).json()}
        je = client.post("/api/journal-entries", headers=ahdr, json={
            "date": "2057-04-01", "memo": "perm",
            "lines": [{"account_id": acc["1010"], "debit": "10"}, {"account_id": acc["4010"], "credit": "10"}],
        }).json()
        # Create a viewer and try to edit.
        import secrets
        vname = f"viewer_{secrets.token_hex(3)}"
        client.post("/api/auth/users", headers=ahdr,
                    json={"username": vname, "password": "Viewer#123", "role": "viewer"})
        vtok = client.post("/api/auth/login", json={"username": vname, "password": "Viewer#123"}).json()["access_token"]
        vhdr = {"Authorization": f"Bearer {vtok}"}
        denied = client.put(f"/api/journal-entries/{je['id']}?reason=x", headers=vhdr, json={
            "date": "2057-04-01", "memo": "z",
            "lines": [{"account_id": acc["1010"], "debit": "10"}, {"account_id": acc["4010"], "credit": "10"}],
        })
        assert denied.status_code == 403
        # Viewer permission map reflects no edit.
        perms = client.get("/api/permissions", headers=vhdr).json()
        assert perms["can"]["edit"] is False and perms["can"]["view"] is True
