"""Project Master: CRUD + unique-code validation, financial roll-up from transactions carrying
the project code, transaction listing, dashboard, archive. 2043 dates (CT-window isolation)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def _codes(client):
    return {a["code"]: a["id"] for a in client.get("/api/accounts").json()}


def test_project_crud_and_unique_code():
    with TestClient(app) as client:
        r = client.post("/api/projects", json={"code": "PRJ-A", "name": "Tower A",
                                               "contract_value": "1000000", "budget": "700000"})
        assert r.status_code == 200, r.text
        pid = r.json()["id"]
        # duplicate code rejected
        assert client.post("/api/projects", json={"code": "PRJ-A", "name": "Dup"}).status_code == 400
        # missing code rejected
        assert client.post("/api/projects", json={"code": "", "name": "X"}).status_code == 400
        # update + archive
        assert client.put(f"/api/projects/{pid}", json={"code": "PRJ-A", "name": "Tower A1",
                                                         "status": "on_hold"}).json()["name"] == "Tower A1"
        assert client.post(f"/api/projects/{pid}/archive").json()["status"] == "archived"
        assert any(e["action"] == "archived" for e in client.get(f"/api/projects/{pid}/events").json())


def test_project_financial_rollup():
    with TestClient(app) as client:
        a = _codes(client)
        client.post("/api/projects", json={"code": "PRJ-FIN", "name": "Fin Project", "budget": "5000"})
        cid = client.post("/api/sales/customers", json={"name": "PjCust"}).json()["id"]
        # a sale on the project (net 10,000 + 5% VAT), partly paid
        inv = client.post("/api/sales/invoices", json={
            "customer_id": cid, "date": "2043-01-01", "project": "PRJ-FIN",
            "lines": [{"description": "work", "quantity": "1", "unit_price": "10000", "vat_rate": "0.05"}]}).json()
        client.post("/api/sales/payments", json={"invoice_id": inv["id"], "date": "2043-01-05", "amount": "4000"})
        # an expense on the project (net 2,000)
        client.post("/api/expenses", json={
            "date": "2043-01-03", "project": "PRJ-FIN", "expense_account_id": a["5090"],
            "payment_account_id": a["1020"], "paid_directly": True, "net_amount": "2000", "vat_rate": "0"})
        f = client.get("/api/projects/PRJ-FIN/financials").json()
        assert f["total_sales"] == 10000.0 and f["output_vat"] == 500.0
        assert f["collections"] == 4000.0 and f["receivables"] == 6500.0   # 10,500 grand - 4,000 paid
        assert f["total_expenses"] == 2000.0
        assert f["revenue"] == 10000.0 and f["cost"] == 2000.0 and f["gross_profit"] == 8000.0
        assert f["budget_variance"] == 3000.0                              # budget 5,000 - cost 2,000
        # transaction listing includes both docs
        tx = client.get("/api/projects/PRJ-FIN/transactions").json()
        assert len(tx["rows"]) >= 2


def test_projects_dashboard():
    with TestClient(app) as client:
        client.post("/api/projects", json={"code": "PRJ-DASH", "name": "Dash", "contract_value": "250000"})
        d = client.get("/api/projects/dashboard").json()
        assert d["total_projects"] >= 1 and "contract_value" in d and "by_status" in d
