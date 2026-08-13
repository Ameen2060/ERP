"""Budget Management: company budget vs actual pulled live from the ledger, workflow lock,
revisions, alerts, dashboard. Fiscal year 2044 to isolate from other tests' postings."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

FY = 2044


def _codes(client):
    return {a["code"]: a["id"] for a in client.get("/api/accounts").json()}


def test_company_budget_vs_actual_and_alerts():
    with TestClient(app) as client:
        a = _codes(client)
        # budget: revenue 4010 = 100,000 ; expense 5020 = 20,000 (annual)
        b = client.post("/api/budgets", json={
            "name": "Company FY44", "fiscal_year": FY, "scope": "company",
            "lines": [{"account_id": a["4010"], "amount": "100000"},
                      {"account_id": a["5020"], "amount": "20000"}]})
        assert b.status_code == 200, b.text
        bid = b.json()["id"]
        # actuals: a sale (revenue 60,000) and an expense (25,000) in FY44
        cid = client.post("/api/sales/customers", json={"name": "BgtCust"}).json()["id"]
        client.post("/api/sales/invoices", json={
            "customer_id": cid, "date": f"{FY}-03-01",
            "lines": [{"description": "sale", "quantity": "1", "unit_price": "60000", "vat_rate": "0"}]})
        client.post("/api/expenses", json={
            "date": f"{FY}-04-01", "expense_account_id": a["5020"], "payment_account_id": a["1020"],
            "paid_directly": True, "net_amount": "25000", "vat_rate": "0"})
        v = client.get(f"/api/budgets/{bid}/variance").json()
        by = {r["account_code"]: r for r in v["rows"]}
        assert by["4010"]["actual"] == 60000.0 and by["4010"]["variance"] == -40000.0
        assert by["4010"]["status"] == "below_budget"           # revenue under budget
        assert by["5020"]["actual"] == 25000.0 and by["5020"]["variance"] == 5000.0
        assert by["5020"]["status"] == "over_budget"
        assert by["5020"]["alert"] == "critical"                # 125% utilisation
        assert v["total_budget"] == 120000.0 and v["total_actual"] == 85000.0


def test_workflow_locks_edits_and_revision():
    with TestClient(app) as client:
        a = _codes(client)
        bid = client.post("/api/budgets", json={
            "name": "Lock test", "fiscal_year": FY, "version": "v9", "scope": "company",
            "lines": [{"account_id": a["5020"], "amount": "1000"}]}).json()["id"]
        for act in ("submit", "approve", "lock"):
            assert client.post(f"/api/budgets/{bid}/transition", params={"action": act}).status_code == 200
        # locked → cannot edit lines
        bad = client.put(f"/api/budgets/{bid}/lines", json={"lines": [{"account_id": a["5020"], "amount": "2"}]})
        assert bad.status_code == 400
        # revision creates a fresh draft version
        rev = client.post(f"/api/budgets/{bid}/revision", json={"new_version": "v10", "reason": "reforecast"})
        assert rev.status_code == 200 and rev.json()["version"] == "v10" and rev.json()["status"] == "draft"


def test_duplicate_budget_rejected_and_dashboard():
    with TestClient(app) as client:
        a = _codes(client)
        payload = {"name": "Dup", "fiscal_year": 2045, "version": "v1", "scope": "company",
                   "lines": [{"account_id": a["5020"], "amount": "500"}]}
        assert client.post("/api/budgets", json=payload).status_code == 200
        assert client.post("/api/budgets", json=payload).status_code == 400   # same scope/year/version
        d = client.get("/api/budgets/dashboard").json()
        assert "total_budget" in d and "total_actual" in d and "utilization" in d
