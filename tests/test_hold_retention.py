"""Hold retention on an already-posted invoice/bill (move AR→Retention Receivable / AP→Retention
Payable after the fact). 2069 dates."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app


def test_hold_retention_on_invoice():
    with TestClient(app) as client:
        cid = client.post("/api/sales/customers", json={"name": "Hold Ret Co"}).json()["id"]
        inv = client.post("/api/sales/invoices", json={
            "customer_id": cid, "date": "2069-01-05",
            "lines": [{"description": "x", "quantity": "1", "unit_price": "1000", "vat_rate": "0.05"}],
        }).json()
        grand = Decimal(inv["grand_total"])                       # 1050
        assert not inv["retention_applicable"]

        r = client.post(f"/api/sales/invoices/{inv['id']}/hold-retention",
                        json={"amount": "100", "date": "2069-01-06", "to_bank": False})
        assert r.status_code == 200, r.text
        b = r.json()
        assert b["retention_applicable"] and Decimal(b["retention_amount"]) == Decimal("100.00")
        # Collectible balance dropped by the held retention.
        assert Decimal(b["balance_due"]) == grand - Decimal("100")
        assert Decimal(b["retention_outstanding"]) == Decimal("100.00")
        # Audit recorded.
        assert any(a["reason"] == "Retention held" for a in
                   client.get(f"/api/sales/invoices/{inv['id']}/audit").json())

        # Cannot hold more than the collectible balance.
        over = client.post(f"/api/sales/invoices/{inv['id']}/hold-retention",
                           json={"amount": "999999", "date": "2069-01-06", "to_bank": False})
        assert over.status_code == 400


def test_hold_retention_on_bill():
    with TestClient(app) as client:
        vid = client.post("/api/purchases/vendors", json={"name": "Hold Ret Vendor"}).json()["id"]
        acc = {a["code"]: a["id"] for a in client.get("/api/accounts").json()}
        exp = acc.get("5090") or next(a["id"] for a in client.get("/api/accounts").json()
                                      if a["code"].startswith("50"))
        bill = client.post("/api/purchases/bills", json={
            "vendor_id": vid, "date": "2069-02-01",
            "lines": [{"description": "svc", "quantity": "1", "unit_price": "2000",
                       "vat_rate": "0.05", "expense_account_id": exp}],
        }).json()
        grand = Decimal(bill["grand_total"])                      # 2100
        r = client.post(f"/api/purchases/bills/{bill['id']}/hold-retention",
                        json={"amount": "300", "date": "2069-02-02", "to_bank": False})
        assert r.status_code == 200, r.text
        assert Decimal(r.json()["balance_due"]) == grand - Decimal("300")
        assert Decimal(r.json()["retention_outstanding"]) == Decimal("300.00")
