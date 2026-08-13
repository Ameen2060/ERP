"""Details-only edit of an applied credit note (amounts locked). 2072 dates."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app


def test_edit_applied_customer_cn_details():
    with TestClient(app) as client:
        cid = client.post("/api/sales/customers", json={"name": "CN Details Co"}).json()["id"]
        inv = client.post("/api/sales/invoices", json={"customer_id": cid, "date": "2072-01-05",
            "lines": [{"description": "x", "quantity": "1", "unit_price": "1000", "vat_rate": "0.05"}]}).json()
        cn = client.post("/api/sales/credit-notes", json={"customer_id": cid, "date": "2072-01-06",
            "reason": "Return", "lines": [{"description": "x", "quantity": "1", "unit_price": "200", "vat_rate": "0.05"}]}).json()
        client.post("/api/credit-notes/apply", json={"cn_type": "customer", "cn_id": cn["id"],
            "target_id": inv["id"], "amount": "210", "date": "2072-01-07"})
        # Full edit blocked (applied); details edit allowed.
        full = client.put(f"/api/sales/credit-notes/{cn['id']}?reason=x", json={"customer_id": cid,
            "date": "2072-01-06", "reason": "Return", "lines": [{"description": "x", "quantity": "1", "unit_price": "300", "vat_rate": "0.05"}]})
        assert full.status_code == 400
        d = client.put(f"/api/sales/credit-notes/{cn['id']}/details?reason=Reclassify",
                       json={"reason": "Goods returned - damaged", "project": "PRJ-7", "notes": "batch 9"})
        assert d.status_code == 200, d.text
        b = d.json()
        assert b["reason"] == "Goods returned - damaged"
        assert Decimal(b["grand_total"]) == Decimal(cn["grand_total"])   # amounts untouched
        assert any(a["reason"] == "Reclassify" for a in client.get(f"/api/sales/credit-notes/{cn['id']}/audit").json())
