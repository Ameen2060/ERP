"""Drill-down engine: every KPI's rows reconcile to the dashboard figure, and rows link to
real source records (journal entries / invoices)."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app


def D(x) -> Decimal:
    return Decimal(str(x))


def test_keys_listed():
    with TestClient(app) as client:
        keys = {k["key"] for k in client.get("/api/drilldown").json()}
        for expected in ("cash", "bank", "accounts_receivable", "accounts_payable", "revenue_ytd",
                         "vat_payable", "outstanding_invoices", "net_profit_ytd", "gross_profit_ytd"):
            assert expected in keys


def test_account_kpi_drilldowns_reconcile():
    with TestClient(app) as client:
        cid = client.post("/api/sales/customers", json={"name": "Drill Cust"}).json()["id"]
        client.post("/api/sales/invoices", json={
            "customer_id": cid, "date": "2025-05-01", "due_date": "2025-05-31",
            "lines": [{"description": "X", "quantity": "1", "unit_price": "1000", "vat_rate": "0.05"}]})
        dash = client.get("/api/dashboard").json()
        for key in ("accounts_receivable", "vat_payable", "cash", "bank", "accounts_payable"):
            d = client.get(f"/api/drilldown/{key}").json()
            assert d["reconciles"] is True, f"{key} did not reconcile: {d}"
            assert D(d["computed_total"]) == D(d["kpi_value"]) == D(dash[key])
            # Every row links to a source record.
            assert all(r["link"] and r["link"]["type"] == "journal" for r in d["rows"])


def test_outstanding_invoices_drilldown_lists_invoice():
    with TestClient(app) as client:
        cid = client.post("/api/sales/customers", json={"name": "Outstanding Cust"}).json()["id"]
        inv = client.post("/api/sales/invoices", json={
            "customer_id": cid, "date": "2025-06-01", "due_date": "2025-06-30",
            "lines": [{"description": "Y", "quantity": "1", "unit_price": "500", "vat_rate": "0"}]}).json()
        d = client.get("/api/drilldown/outstanding_invoices").json()
        assert d["kind"] == "count" and d["reconciles"] is True
        numbers = [r["cells"]["number"] for r in d["rows"]]
        assert inv["number"] in numbers
        row = next(r for r in d["rows"] if r["cells"]["number"] == inv["number"])
        assert row["link"]["type"] == "invoice" and row["link"]["id"] == inv["id"]


def test_unknown_key_404():
    with TestClient(app) as client:
        assert client.get("/api/drilldown/not_a_kpi").status_code == 404
