"""Open + edit a customer/vendor advance: GET single, PUT reverse-repost with audit, and the
guard blocking edit once applied. 2063 dates."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app


def test_get_and_edit_customer_advance():
    with TestClient(app) as client:
        cid = client.post("/api/sales/customers", json={"name": "Adv Edit Co"}).json()["id"]
        adv = client.post("/api/sales/advances", json={
            "customer_id": cid, "date": "2063-01-05", "amount": "500", "reference": "RCT-1"}).json()
        aid = adv["id"]
        old_je = adv["journal_entry_id"]

        # GET single (open detail).
        got = client.get(f"/api/sales/advances/{aid}")
        assert got.status_code == 200 and got.json()["number"] == adv["number"]

        # Edit amount + reference.
        up = client.put(f"/api/sales/advances/{aid}?reason=Correct+amount", json={
            "customer_id": cid, "date": "2063-01-05", "amount": "800", "reference": "RCT-1b"})
        assert up.status_code == 200, up.text
        b = up.json()
        assert b["number"] == adv["number"] and Decimal(b["amount"]) == Decimal("800.00")
        assert Decimal(b["available"]) == Decimal("800.00")
        assert b["journal_entry_id"] != old_je
        assert client.get(f"/api/journal-entries/{old_je}").json()["status"] == "void"
        assert any(a["action"] == "edit" for a in client.get(f"/api/advances/customer/{aid}/audit").json())


def test_cannot_edit_applied_advance():
    with TestClient(app) as client:
        cid = client.post("/api/sales/customers", json={"name": "Adv Applied Co"}).json()["id"]
        inv = client.post("/api/sales/invoices", json={
            "customer_id": cid, "date": "2063-02-01",
            "lines": [{"description": "x", "quantity": "1", "unit_price": "1000", "vat_rate": "0.05"}],
        }).json()
        adv = client.post("/api/sales/advances", json={
            "customer_id": cid, "date": "2063-02-02", "amount": "400"}).json()
        client.post("/api/advances/apply", json={
            "advance_type": "customer", "advance_id": adv["id"], "target_id": inv["id"],
            "amount": "300", "date": "2063-02-03"})
        blocked = client.put(f"/api/sales/advances/{adv['id']}?reason=x", json={
            "customer_id": cid, "date": "2063-02-02", "amount": "600"})
        assert blocked.status_code == 400 and "applied" in blocked.text.lower()


def test_edit_vendor_advance():
    with TestClient(app) as client:
        vid = client.post("/api/purchases/vendors", json={"name": "Adv Edit Vendor"}).json()["id"]
        adv = client.post("/api/purchases/advances", json={
            "vendor_id": vid, "date": "2063-03-01", "amount": "300"}).json()
        up = client.put(f"/api/purchases/advances/{adv['id']}?reason=Fix", json={
            "vendor_id": vid, "date": "2063-03-01", "amount": "450"})
        assert up.status_code == 200 and Decimal(up.json()["amount"]) == Decimal("450.00")
