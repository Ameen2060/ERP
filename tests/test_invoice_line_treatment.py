"""Invoice-line VAT treatment parity with bills: a sales-invoice line stores the chosen
VAT treatment code immutably (SR/ZR/EX/OS), and by-treatment reporting sees it. 2052 dates."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_invoice_line_persists_vat_treatment():
    with TestClient(app) as client:
        cid = client.post("/api/sales/customers", json={"name": "Treat Co"}).json()["id"]
        inv = client.post("/api/sales/invoices", json={
            "customer_id": cid, "date": "2052-03-01",
            "lines": [
                {"description": "Std goods", "quantity": "1", "unit_price": "1000",
                 "vat_rate": "0.05", "vat_treatment": "SR"},
                {"description": "Exports", "quantity": "1", "unit_price": "500",
                 "vat_rate": "0", "vat_treatment": "ZR"},
            ],
        }).json()
        assert inv.get("id"), inv
        got = client.get(f"/api/sales/invoices/{inv['id']}").json()
        treatments = sorted(ln["vat_treatment"] for ln in got["lines"])
        assert treatments == ["SR", "ZR"], treatments
