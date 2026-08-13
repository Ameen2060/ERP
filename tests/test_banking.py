"""Banking: bank accounts backed by GL accounts, transfers, statement import, auto-matching,
and reconciliation (book vs statement, deposits in transit, difference → 0)."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app


def D(x) -> Decimal:
    return Decimal(str(x))


def _acct_id(client, code):
    return next(a["id"] for a in client.get("/api/accounts").json() if a["code"] == code)


def _post(client, date_, lines, source="manual"):
    r = client.post("/api/journal-entries", json={"date": date_, "source": source, "lines": lines})
    assert r.status_code == 200, r.text


def test_create_bank_account_and_transfer():
    with TestClient(app) as client:
        cap = _acct_id(client, "3010")
        # A dedicated GL account keeps balances isolated from other tests that touch 1020.
        ba1 = client.post("/api/banking/accounts", json={"name": "Main Ops", "gl_account_code": "1051"}).json()
        ba2 = client.post("/api/banking/accounts", json={"name": "Petty", "gl_account_code": "1052"}).json()
        # Fund account 1 with 5,000.
        _post(client, "2025-01-01", [{"account_id": ba1["gl_account_id"], "debit": "5000"}, {"account_id": cap, "credit": "5000"}])
        r = client.post("/api/banking/transfers", json={
            "from_bank_account_id": ba1["id"], "to_bank_account_id": ba2["id"], "date": "2025-01-05", "amount": "1000"})
        assert r.status_code == 200, r.text
        assert D(client.get(f"/api/banking/accounts/{ba1['id']}").json()["balance"]) == Decimal("4000.00")
        assert D(client.get(f"/api/banking/accounts/{ba2['id']}").json()["balance"]) == Decimal("1000.00")
        # Same-account transfer is rejected.
        bad = client.post("/api/banking/transfers", json={
            "from_bank_account_id": ba1["id"], "to_bank_account_id": ba1["id"], "date": "2025-01-05", "amount": "10"})
        assert bad.status_code == 400


def test_import_automatch_and_reconcile():
    with TestClient(app) as client:
        cap = _acct_id(client, "3010")
        rent = _acct_id(client, "5020")
        ba = client.post("/api/banking/accounts", json={"name": "Recon Bank", "gl_account_code": "1055"}).json()
        gl = ba["gl_account_id"]
        # Three book movements: +5000 in, -1200 out, +500 in (this last one is a deposit in transit).
        _post(client, "2025-02-01", [{"account_id": gl, "debit": "5000"}, {"account_id": cap, "credit": "5000"}])
        _post(client, "2025-02-03", [{"account_id": rent, "debit": "1200"}, {"account_id": gl, "credit": "1200"}])
        _post(client, "2025-02-10", [{"account_id": gl, "debit": "500"}, {"account_id": cap, "credit": "500"}])

        client.post("/api/banking/statement/import", json={"bank_account_id": ba["id"], "lines": [
            {"date": "2025-02-01", "description": "Deposit", "amount": "5000"},
            {"date": "2025-02-03", "description": "Rent", "amount": "-1200"},
        ]})
        am = client.post("/api/banking/auto-match", json={"bank_account_id": ba["id"]}).json()
        assert am["matched"] == 2

        summary = client.post("/api/banking/reconcile/summary", json={
            "bank_account_id": ba["id"], "statement_date": "2025-02-28", "statement_balance": "3800"}).json()
        assert D(summary["book_balance"]) == Decimal("4300.00")
        assert D(summary["deposits_in_transit"]) == Decimal("500.00")
        assert D(summary["outstanding_payments"]) == Decimal("0.00")
        assert D(summary["difference"]) == Decimal("0.00")
        assert summary["reconciled"] is True
        assert len(summary["deposits"]) == 1  # the 500 in transit

        done = client.post("/api/banking/reconcile/complete", json={
            "bank_account_id": ba["id"], "statement_date": "2025-02-28", "statement_balance": "3800"}).json()
        assert done["cleared_count"] == 2
        hist = client.get("/api/banking/reconciliations", params={"bank_account_id": ba["id"]}).json()
        assert len(hist) == 1 and D(hist[0]["difference"]) == Decimal("0.00")


def test_unmatched_statement_line_blocks_reconcile():
    with TestClient(app) as client:
        cap = _acct_id(client, "3010")
        ba = client.post("/api/banking/accounts", json={"name": "Charges Bank", "gl_account_code": "1056"}).json()
        gl = ba["gl_account_id"]
        _post(client, "2025-03-01", [{"account_id": gl, "debit": "2000"}, {"account_id": cap, "credit": "2000"}])
        # A bank charge appears on the statement but was never booked.
        client.post("/api/banking/statement/import", json={"bank_account_id": ba["id"], "lines": [
            {"date": "2025-03-01", "description": "Deposit", "amount": "2000"},
            {"date": "2025-03-31", "description": "Bank charge", "amount": "-25"},
        ]})
        client.post("/api/banking/auto-match", json={"bank_account_id": ba["id"]})
        summary = client.post("/api/banking/reconcile/summary", json={
            "bank_account_id": ba["id"], "statement_date": "2025-03-31", "statement_balance": "1975"}).json()
        # The unbooked 25 charge means it does not reconcile yet, and the difference exposes it.
        assert summary["unmatched_statement_count"] == 1
        assert summary["reconciled"] is False
        assert D(summary["difference"]) == Decimal("25.00")


def test_manual_match_and_unmatch():
    with TestClient(app) as client:
        cap = _acct_id(client, "3010")
        ba = client.post("/api/banking/accounts", json={"name": "Manual Bank", "gl_account_code": "1057"}).json()
        gl = ba["gl_account_id"]
        _post(client, "2025-04-01", [{"account_id": gl, "debit": "750"}, {"account_id": cap, "credit": "750"}])
        client.post("/api/banking/statement/import", json={"bank_account_id": ba["id"], "lines": [
            {"date": "2025-04-02", "description": "Deposit", "amount": "750"}]})
        line = client.get("/api/banking/statement-lines", params={"bank_account_id": ba["id"]}).json()[0]
        cands = client.get(f"/api/banking/statement-lines/{line['id']}/candidates").json()
        assert len(cands) == 1 and D(cands[0]["amount"]) == Decimal("750.00")
        matched = client.post("/api/banking/match", json={"statement_line_id": line["id"], "entry_id": cands[0]["entry_id"]}).json()
        assert matched["status"] == "matched"
        un = client.post(f"/api/banking/statement-lines/{line['id']}/unmatch").json()
        assert un["status"] == "unmatched"
