"""Corporate Tax computation (provisional) and product movement reports."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app


def D(x) -> Decimal:
    return Decimal(str(x))


def _acct(client, code):
    return next(a["id"] for a in client.get("/api/accounts").json() if a["code"] == code)


def _post(client, date_, lines):
    assert client.post("/api/journal-entries", json={"date": date_, "lines": lines}).status_code == 200


def test_ct_computation_provisional():
    with TestClient(app) as client:
        cash = _acct(client, "1010")
        rev = client.post("/api/accounting/accounts" if False else "/api/accounts",
                          json={"code": "4950", "name": "CT Test Rev", "type": "income"}).json()["id"]
        exp = client.post("/api/accounts", json={"code": "5960", "name": "CT Test Exp", "type": "expense"}).json()["id"]
        _post(client, "2035-02-01", [{"account_id": cash, "debit": "500000"}, {"account_id": rev, "credit": "500000"}])
        _post(client, "2035-03-01", [{"account_id": exp, "debit": "100000"}, {"account_id": cash, "credit": "100000"}])
        ct = client.get("/api/reports/ct-computation", params={"start": "2035-01-01", "end": "2035-12-31"}).json()
        assert D(ct["accounting_profit"]) == Decimal("400000.00")
        assert ct["reconciled"] is True and ct["status"] == "provisional"
        # 0% up to 375,000; 9% above → (400000 - 375000) * 0.09 = 2250.
        assert D(ct["taxable_income"]) == Decimal("400000.00")
        assert D(ct["ct_payable"]) == Decimal("2250.00")
        assert "PROVISIONAL" in ct["notes"][0]


def test_product_sales_and_purchase_reports():
    with TestClient(app) as client:
        wh = client.post("/api/inventory/warehouses", json={"name": "Rpt WH"}).json()["id"]
        pid = client.post("/api/inventory/products", json={"name": "Rpt Widget", "cost_method": "weighted_average"}).json()["id"]
        client.post("/api/inventory/movements", json={"product_id": pid, "warehouse_id": wh, "date": "2036-01-01",
            "movement_type": "receipt", "quantity": "10", "unit_cost": "5"})
        client.post("/api/inventory/movements", json={"product_id": pid, "warehouse_id": wh, "date": "2036-01-05",
            "movement_type": "issue", "quantity": "4"})
        buys = client.get("/api/reports/product-purchases", params={"start": "2036-01-01", "end": "2036-12-31"}).json()
        prow = next(r for r in buys["rows"] if r["product_id"] == pid)
        assert D(prow["quantity"]) == Decimal("10.0000") and D(prow["value"]) == Decimal("50.00")
        sales = client.get("/api/reports/product-sales", params={"start": "2036-01-01", "end": "2036-12-31"}).json()
        srow = next(r for r in sales["rows"] if r["product_id"] == pid)
        assert D(srow["quantity"]) == Decimal("4.0000") and D(srow["value"]) == Decimal("20.00")  # 4 @ WAC 5


def test_ct_and_product_exports():
    with TestClient(app) as client:
        assert client.get("/api/export/ct-computation", params={"format": "pdf"}).content[:5] == b"%PDF-"
        assert client.get("/api/export/product-sales", params={"format": "xlsx"}).content[:2] == b"PK"
        assert client.get("/api/export/inventory-valuation", params={"format": "csv"}).status_code == 200
