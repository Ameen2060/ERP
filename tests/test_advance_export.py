"""Print / PDF / Excel + archive for customer & vendor advances. 2073 dates."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_customer_advance_pdf_excel_archive():
    with TestClient(app) as client:
        cid = client.post("/api/sales/customers", json={"name": "Adv Export Co"}).json()["id"]
        adv = client.post("/api/sales/advances", json={
            "customer_id": cid, "date": "2073-01-05", "amount": "5000"}).json()
        pdf = client.get(f"/api/sales/advances/{adv['id']}/pdf")
        assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
        xl = client.get(f"/api/documents/customer_advance/{adv['id']}/excel")
        assert xl.status_code == 200 and xl.content[:2] == b"PK"
        arch = client.post(f"/api/documents/customer_advance/{adv['id']}/archive-pdf")
        assert arch.status_code == 200


def test_vendor_advance_pdf_excel():
    with TestClient(app) as client:
        vid = client.post("/api/purchases/vendors", json={"name": "Adv Export Vendor"}).json()["id"]
        adv = client.post("/api/purchases/advances", json={
            "vendor_id": vid, "date": "2073-02-01", "amount": "50"}).json()
        assert client.get(f"/api/purchases/advances/{adv['id']}/pdf").content[:4] == b"%PDF"
        assert client.get(f"/api/documents/vendor_advance/{adv['id']}/excel").content[:2] == b"PK"
