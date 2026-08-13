"""Edit Customer / Vendor / Bank profiles: update endpoints, TRN + required-field validation,
duplicate-name guard, field-level audit trail, and preservation of posted transactions. 2055 dates."""

from __future__ import annotations

import secrets
from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app


def _uniq(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(3)}"


def test_edit_customer_with_audit_and_no_transaction_change():
    with TestClient(app) as client:
        nm = _uniq("Editable Co")
        c = client.post("/api/sales/customers", json={
            "name": nm, "trn": "100200300400500", "billing_address": "Old St",
            "payment_terms": "Net 30", "credit_limit": "10000"}).json()
        cid = c["id"]
        # Raise an invoice — its posted GL must not change when the customer is later edited.
        inv = client.post("/api/sales/invoices", json={
            "customer_id": cid, "date": "2055-01-05",
            "lines": [{"description": "x", "quantity": "1", "unit_price": "1000", "vat_rate": "0.05"}],
        }).json()
        grand_before = inv["grand_total"]

        # Edit several fields.
        up = client.put(f"/api/sales/customers/{cid}", json={
            "name": "Editable Co", "trn": "999888777666555", "billing_address": "New Tower",
            "payment_terms": "Net 60", "credit_limit": "25000", "contact_name": "Sara"})
        assert up.status_code == 200, up.text
        body = up.json()
        assert body["trn"] == "999888777666555"
        assert body["billing_address"] == "New Tower"
        assert body["payment_terms"] == "Net 60"
        assert Decimal(body["credit_limit"]) == Decimal("25000")

        # Invoice total unchanged (historical transaction preserved).
        assert client.get(f"/api/sales/invoices/{inv['id']}").json()["grand_total"] == grand_before

        # Audit trail records the field-level diff + a create entry.
        audit = client.get(f"/api/sales/customers/{cid}/audit").json()
        actions = [a["action"] for a in audit]
        assert "create" in actions and "update" in actions
        upd = next(a for a in audit if a["action"] == "update")
        changed = {ch["field"] for ch in upd["changes"]}
        assert {"trn", "billing_address", "payment_terms"} <= changed

        # Invalid TRN rejected on edit.
        bad = client.put(f"/api/sales/customers/{cid}", json={"name": "Editable Co", "trn": "123"})
        assert bad.status_code == 400


def test_edit_vendor_and_duplicate_guard():
    with TestClient(app) as client:
        one, two = _uniq("Vendor One"), _uniq("Vendor Two")
        client.post("/api/purchases/vendors", json={"name": one})
        vid = client.post("/api/purchases/vendors", json={"name": two}).json()["id"]
        # Rename Vendor Two → Vendor One should fail (duplicate name).
        dup = client.put(f"/api/purchases/vendors/{vid}", json={"name": one})
        assert dup.status_code == 400
        # Valid edit works + audited.
        ok = client.put(f"/api/purchases/vendors/{vid}", json={
            "name": two, "trn": "111222333444555", "billing_address": "Deira"})
        assert ok.status_code == 200 and ok.json()["trn"] == "111222333444555"
        assert any(a["action"] == "update" for a in
                   client.get(f"/api/purchases/vendors/{vid}/audit").json())


def test_edit_bank_preserves_gl_and_audits():
    with TestClient(app) as client:
        nm = _uniq("Main Bank")
        ba = client.post("/api/banking/accounts", json={
            "name": nm, "bank_name": "ADCB", "account_number": "111"}).json()
        bid, gl = ba["id"], ba["gl_account_id"]
        up = client.put(f"/api/banking/accounts/{bid}", json={
            "name": nm, "bank_name": "ADCB", "account_name": "Company LLC",
            "account_number": "222333", "iban": "AE070331234567890123456", "swift": "ADCBAEAA",
            "branch": "Business Bay", "currency": "AED", "is_active": True})
        assert up.status_code == 200, up.text
        body = up.json()
        assert body["gl_account_id"] == gl          # GL link immutable → history preserved
        assert body["iban"] == "AE070331234567890123456"
        assert body["swift"] == "ADCBAEAA" and body["branch"] == "Business Bay"
        audit = client.get(f"/api/banking/accounts/{bid}/audit").json()
        changed = {ch["field"] for a in audit if a["action"] == "update" for ch in a["changes"]}
        assert {"account_number", "iban", "swift"} <= changed
