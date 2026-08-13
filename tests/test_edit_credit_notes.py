"""Edit Customer & Vendor Credit Notes via reverse-and-repost: recalculation, id/number
preserved, audit trail, and the guard blocking edits once the note is applied. 2059 dates."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app


def test_edit_customer_credit_note():
    with TestClient(app) as client:
        cid = client.post("/api/sales/customers", json={"name": "CN Edit Co"}).json()["id"]
        cn = client.post("/api/sales/credit-notes", json={
            "customer_id": cid, "date": "2059-01-05", "reason": "Return",
            "lines": [{"description": "goods", "quantity": "1", "unit_price": "1000", "vat_rate": "0.05"}],
        }).json()
        cnid, num = cn["id"], cn["number"]
        up = client.put(f"/api/sales/credit-notes/{cnid}?reason=Fix+qty", json={
            "customer_id": cid, "date": "2059-01-05", "reason": "Return",
            "lines": [{"description": "goods", "quantity": "3", "unit_price": "1000", "vat_rate": "0.05"}],
        })
        assert up.status_code == 200, up.text
        b = up.json()
        assert b["id"] == cnid and b["number"] == num
        assert Decimal(b["grand_total"]) == Decimal("3150.00")   # 3000 + 5% VAT
        assert client.get(f"/api/sales/credit-notes/{cnid}/audit").json()[0]["reason"] == "Fix qty"


def test_cannot_edit_applied_credit_note():
    with TestClient(app) as client:
        cid = client.post("/api/sales/customers", json={"name": "CN Applied Co"}).json()["id"]
        inv = client.post("/api/sales/invoices", json={
            "customer_id": cid, "date": "2059-02-01",
            "lines": [{"description": "x", "quantity": "1", "unit_price": "1000", "vat_rate": "0.05"}],
        }).json()
        cn = client.post("/api/sales/credit-notes", json={
            "customer_id": cid, "date": "2059-02-02", "reason": "Adj",
            "lines": [{"description": "x", "quantity": "1", "unit_price": "200", "vat_rate": "0.05"}],
        }).json()
        ap = client.post("/api/credit-notes/apply", json={
            "cn_type": "customer", "cn_id": cn["id"],
            "target_id": inv["id"], "amount": "210", "date": "2059-02-03"})
        assert ap.status_code == 200, ap.text
        blocked = client.put(f"/api/sales/credit-notes/{cn['id']}?reason=x", json={
            "customer_id": cid, "date": "2059-02-02", "reason": "Adj",
            "lines": [{"description": "x", "quantity": "1", "unit_price": "300", "vat_rate": "0.05"}],
        })
        assert blocked.status_code == 400 and "applied" in blocked.text.lower()


def test_edit_vendor_credit_note():
    with TestClient(app) as client:
        vid = client.post("/api/purchases/vendors", json={"name": "VCN Edit Vendor"}).json()["id"]
        acc = {a["code"]: a["id"] for a in client.get("/api/accounts").json()}
        exp = acc.get("5090") or next(a["id"] for a in client.get("/api/accounts").json()
                                      if a["code"].startswith("50"))
        cn = client.post("/api/purchases/credit-notes", json={
            "vendor_id": vid, "date": "2059-03-01", "reason": "Return",
            "lines": [{"description": "x", "quantity": "1", "unit_price": "500",
                       "vat_rate": "0.05", "expense_account_id": exp}],
        }).json()
        up = client.put(f"/api/purchases/credit-notes/{cn['id']}?reason=Adjust", json={
            "vendor_id": vid, "date": "2059-03-01", "reason": "Return",
            "lines": [{"description": "x", "quantity": "1", "unit_price": "700",
                       "vat_rate": "0.05", "expense_account_id": exp}],
        })
        assert up.status_code == 200, up.text
        assert Decimal(up.json()["grand_total"]) == Decimal("735.00")
        assert client.get(f"/api/purchases/credit-notes/{cn['id']}/audit").json()[0]["reason"] == "Adjust"
