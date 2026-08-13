"""Ledger: CoA seed, balanced/unbalanced posting, group-account guard, trial balance, GL."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app


def D(x) -> Decimal:
    return Decimal(str(x))


def _codes(client: TestClient) -> dict[str, dict]:
    return {a["code"]: a for a in client.get("/api/accounts").json()}


def test_seed_is_idempotent_and_normal_balances():
    with TestClient(app) as client:
        # Seeded automatically on startup; a manual seed adds nothing.
        assert client.post("/api/accounts/seed").json()["added"] == 0
        c = _codes(client)
        assert c["1000"]["is_group"] is True
        assert c["1010"]["normal_balance"] == "debit"
        assert c["2010"]["normal_balance"] == "credit"
        assert c["1510"]["normal_balance"] == "credit"  # contra-asset
        assert c["5060"]["is_cost_of_sales"] is True


def test_post_balanced_and_trial_balance():
    with TestClient(app) as client:
        c = _codes(client)
        r = client.post("/api/journal-entries", json={"date": "2025-01-01", "lines": [
            {"account_id": c["1010"]["id"], "debit": "100000"},
            {"account_id": c["3010"]["id"], "credit": "100000"},
        ]})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "posted"
        tb = client.get("/api/trial-balance").json()
        assert tb["balanced"] is True
        assert D(tb["total_debit"]) == D(tb["total_credit"])


def test_unbalanced_rejected_and_group_guard():
    with TestClient(app) as client:
        c = _codes(client)
        bad = client.post("/api/journal-entries", json={"date": "2025-02-01", "lines": [
            {"account_id": c["1010"]["id"], "debit": "100"},
            {"account_id": c["4010"]["id"], "credit": "90"},
        ]})
        assert bad.status_code == 422
        grp = client.post("/api/journal-entries", json={"date": "2025-02-01", "lines": [
            {"account_id": c["1000"]["id"], "debit": "100"},
            {"account_id": c["3010"]["id"], "credit": "100"},
        ]})
        assert grp.status_code == 400 and "group account" in grp.json()["detail"]


def test_general_ledger_running_balance_and_void():
    with TestClient(app) as client:
        cash = client.post("/api/accounts", json={"code": "1091", "name": "GL Cash", "type": "asset", "parent_code": "1000"}).json()
        cap = client.post("/api/accounts", json={"code": "3091", "name": "GL Eq", "type": "equity", "parent_code": "3000"}).json()
        rent = client.post("/api/accounts", json={"code": "5091", "name": "GL Rent", "type": "expense", "parent_code": "5000"}).json()
        client.post("/api/journal-entries", json={"date": "2025-03-01", "lines": [
            {"account_id": cash["id"], "debit": "5000"}, {"account_id": cap["id"], "credit": "5000"}]})
        pay = client.post("/api/journal-entries", json={"date": "2025-03-10", "lines": [
            {"account_id": rent["id"], "debit": "1200"}, {"account_id": cash["id"], "credit": "1200"}]}).json()
        gl = client.get(f"/api/general-ledger/{cash['id']}").json()
        assert [D(r["balance"]) for r in gl["rows"]] == [Decimal("5000.00"), Decimal("3800.00")]
        client.post(f"/api/journal-entries/{pay['id']}/void")
        gl2 = client.get(f"/api/general-ledger/{cash['id']}").json()
        assert D(gl2["closing_balance"]) == Decimal("5000.00")
