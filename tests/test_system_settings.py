"""System settings: defaults, validation, and that the configurable default sales account
actually drives invoice posting (nothing hard-coded). 2047 dates."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_defaults_and_validation():
    with TestClient(app) as client:
        s = client.get("/api/system-settings").json()
        assert s["decimal_places"] == 2 and float(s["default_vat_rate"]) == 0.05
        assert client.put("/api/system-settings", json={"decimal_places": 5}).status_code == 400
        assert client.put("/api/system-settings", json={"default_sales_code": "9999"}).status_code == 400
        assert client.put("/api/system-settings", json={"default_vat_rate": "2"}).status_code == 400


def test_default_sales_account_drives_invoice_posting():
    with TestClient(app) as client:
        # point the default sales account at 4020 (Service Revenue)
        r = client.put("/api/system-settings", json={"default_sales_code": "4020", "decimal_places": 2,
                                                     "default_vat_rate": "0.05"})
        assert r.status_code == 200 and r.json()["default_sales_code"] == "4020"
        cid = client.post("/api/sales/customers", json={"name": "SS Cust"}).json()["id"]
        inv = client.post("/api/sales/invoices", json={          # no revenue_account_id on the line
            "customer_id": cid, "date": "2047-01-01",
            "lines": [{"description": "svc", "quantity": "1", "unit_price": "1000", "vat_rate": "0"}]}).json()
        je = client.get(f"/api/journal-entries/{inv['journal_entry_id']}").json()
        codes = {ln["account_code"] for ln in je["lines"]}
        assert "4020" in codes and "4010" not in codes          # posted to the configured default
        # reset so other suites aren't affected
        client.put("/api/system-settings", json={"default_sales_code": None})
