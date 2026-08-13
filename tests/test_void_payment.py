"""Void (reverse) customer receipts and vendor payments: restores the invoice/bill outstanding
balance, reverses the GL entry, and audits. 2061 dates."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app


def test_void_customer_receipt_restores_balance():
    with TestClient(app) as client:
        cid = client.post("/api/sales/customers", json={"name": "Void Pmt Co"}).json()["id"]
        inv = client.post("/api/sales/invoices", json={
            "customer_id": cid, "date": "2061-01-10",
            "lines": [{"description": "x", "quantity": "1", "unit_price": "1000", "vat_rate": "0.05"}],
        }).json()
        grand = Decimal(inv["grand_total"])
        pay = client.post("/api/sales/payments", json={
            "invoice_id": inv["id"], "date": "2061-01-12", "amount": "400"}).json()
        after = client.get(f"/api/sales/invoices/{inv['id']}").json()
        assert Decimal(after["balance_due"]) == grand - Decimal("400") and after["status"] == "partial"

        v = client.post(f"/api/sales/payments/{pay['id']}/void?reason=Duplicate+receipt")
        assert v.status_code == 200, v.text
        restored = client.get(f"/api/sales/invoices/{inv['id']}").json()
        assert Decimal(restored["balance_due"]) == grand and restored["status"] == "posted"
        # Payment JE reversed.
        assert client.get(f"/api/journal-entries/{pay['journal_entry_id']}").json()["status"] == "void"
        # Payment removed from the invoice's payment list.
        assert client.get(f"/api/sales/invoices/{inv['id']}/payments").json() == []


def test_void_vendor_payment_restores_balance():
    with TestClient(app) as client:
        vid = client.post("/api/purchases/vendors", json={"name": "Void Pay Vendor"}).json()["id"]
        acc = {a["code"]: a["id"] for a in client.get("/api/accounts").json()}
        exp = acc.get("5090") or next(a["id"] for a in client.get("/api/accounts").json()
                                      if a["code"].startswith("50"))
        bill = client.post("/api/purchases/bills", json={
            "vendor_id": vid, "date": "2061-02-01",
            "lines": [{"description": "x", "quantity": "1", "unit_price": "1000",
                       "vat_rate": "0.05", "expense_account_id": exp}],
        }).json()
        grand = Decimal(bill["grand_total"])
        pay = client.post("/api/purchases/payments", json={
            "bill_id": bill["id"], "date": "2061-02-03", "amount": "500"}).json()
        assert Decimal(client.get(f"/api/purchases/bills/{bill['id']}").json()["balance_due"]) == grand - Decimal("500")
        v = client.post(f"/api/purchases/payments/{pay['id']}/void")
        assert v.status_code == 200, v.text
        assert Decimal(client.get(f"/api/purchases/bills/{bill['id']}").json()["balance_due"]) == grand
