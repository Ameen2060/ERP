"""Non-financial 'edit details' on a settled invoice/bill (payments present) — changes detail
fields without touching amounts/GL, and is audited. 2071 dates."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app


def test_edit_paid_invoice_details():
    with TestClient(app) as client:
        cid = client.post("/api/sales/customers", json={"name": "Details Co"}).json()["id"]
        inv = client.post("/api/sales/invoices", json={
            "customer_id": cid, "date": "2071-01-05", "due_date": "2071-02-04",
            "lines": [{"description": "x", "quantity": "1", "unit_price": "1000", "vat_rate": "0.05"}],
        }).json()
        # Partly pay it → full edit is blocked, details edit still allowed.
        client.post("/api/sales/payments", json={"invoice_id": inv["id"], "date": "2071-01-10", "amount": "500"})
        grand = Decimal(inv["grand_total"])

        up = client.put(f"/api/sales/invoices/{inv['id']}/details?reason=New+PO", json={
            "due_date": "2071-03-01", "project": "PRJ-9", "salesperson": "Sara", "notes": "PO#123"})
        assert up.status_code == 200, up.text
        b = up.json()
        assert b["due_date"] == "2071-03-01"
        # Amounts untouched.
        assert Decimal(b["grand_total"]) == grand and Decimal(b["amount_paid"]) == Decimal("500")
        # Audited, with the field-level diff (project/salesperson recorded there).
        aud = client.get(f"/api/sales/invoices/{inv['id']}/audit").json()
        edit = next(a for a in aud if a["reason"] == "New PO")
        changed = {c["field"] for c in edit["changes"]}
        assert {"due_date", "project", "salesperson"} <= changed


def test_edit_paid_bill_details():
    with TestClient(app) as client:
        vid = client.post("/api/purchases/vendors", json={"name": "Details Vendor"}).json()["id"]
        acc = {a["code"]: a["id"] for a in client.get("/api/accounts").json()}
        exp = acc.get("5090") or next(a["id"] for a in client.get("/api/accounts").json() if a["code"].startswith("50"))
        bill = client.post("/api/purchases/bills", json={
            "vendor_id": vid, "date": "2071-02-01",
            "lines": [{"description": "svc", "quantity": "1", "unit_price": "800", "vat_rate": "0.05", "expense_account_id": exp}],
        }).json()
        client.post("/api/purchases/payments", json={"bill_id": bill["id"], "date": "2071-02-05", "amount": "200"})
        up = client.put(f"/api/purchases/bills/{bill['id']}/details?reason=Ref+fix", json={
            "vendor_ref": "SUP-INV-77", "project": "PRJ-2", "notes": "late"})
        assert up.status_code == 200 and up.json()["vendor_ref"] == "SUP-INV-77"
        assert Decimal(up.json()["grand_total"]) == Decimal(bill["grand_total"])
