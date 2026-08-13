"""Spec: TRN + structured address on Customer/Vendor masters, TRN-format validation,
missing-field warnings, and Fixed-Asset→Vendor linkage that pulls TRN + address. 2053 dates."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_customer_structured_address_and_trn_validation():
    with TestClient(app) as client:
        # Valid 15-digit TRN + billing/shipping split persist and echo back.
        c = client.post("/api/sales/customers", json={
            "name": "Bilingual Co", "trn": "100200300400500",
            "contact_name": "Aisha", "billing_address": "Bay Square, Dubai",
            "shipping_address": "JAFZA Warehouse 7",
        })
        assert c.status_code == 200, c.text
        body = c.json()
        assert body["billing_address"] == "Bay Square, Dubai"
        assert body["shipping_address"] == "JAFZA Warehouse 7"
        assert body["contact_name"] == "Aisha"
        assert body["warnings"] == []  # TRN + address present → no warnings

        # Invalid TRN format is rejected.
        bad = client.post("/api/sales/customers", json={"name": "Bad TRN", "trn": "123"})
        assert bad.status_code == 400
        assert "15-digit" in bad.text

        # Missing TRN → non-blocking warning surfaced.
        warn = client.post("/api/sales/customers", json={"name": "No TRN Co"}).json()
        assert any("TRN" in w for w in warn["warnings"])


def test_vendor_billing_and_contact():
    with TestClient(app) as client:
        v = client.post("/api/purchases/vendors", json={
            "name": "Supply LLC", "trn": "999888777666555",
            "contact_name": "Omar", "billing_address": "DIP, Dubai",
        }).json()
        assert v["billing_address"] == "DIP, Dubai" and v["contact_name"] == "Omar"
        assert v["warnings"] == []


def test_asset_pulls_vendor_trn_and_address():
    with TestClient(app) as client:
        vid = client.post("/api/purchases/vendors", json={
            "name": "Asset Vendor", "trn": "112233445566778",
            "billing_address": "Al Quoz, Dubai",
        }).json()["id"]
        a = client.post("/api/assets", json={
            "name": "Server Rack", "category": "IT Equipment", "vendor_id": vid,
            "purchase_date": "2053-01-10", "purchase_cost": "10000", "vat_amount": "500",
            "invoice_number": "SUP-77",
        })
        assert a.status_code == 200, a.text
        body = a.json()
        assert body["vendor_id"] == vid
        assert body["vendor_trn"] == "112233445566778"
        assert body["vendor_address"] == "Al Quoz, Dubai"
        assert body["total_cost"] == "10500.00"  # gross = net + VAT
        assert body["warnings"] == []

        # Unknown vendor is rejected.
        bad = client.post("/api/assets", json={
            "name": "Ghost", "vendor_id": "nope", "purchase_date": "2053-01-10",
            "purchase_cost": "1",
        })
        assert bad.status_code == 400
