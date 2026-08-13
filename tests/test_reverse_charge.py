"""Reverse-charge (RC) on vendor bills: self-assess both input and output VAT, pay vendor net
only, and reflect RC in the VAT return output. 2050 dates."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def _codes(client):
    return {a["code"]: a["id"] for a in client.get("/api/accounts").json()}


def test_reverse_charge_bill_dual_posts_and_pays_net():
    with TestClient(app) as client:
        a = _codes(client)
        vid = client.post("/api/purchases/vendors", json={"name": "RC Vend"}).json()["id"]
        bill = client.post("/api/purchases/bills", json={
            "vendor_id": vid, "date": "2050-01-01",
            "lines": [{"description": "imported service", "quantity": "1", "unit_price": "10000",
                       "vat_rate": "0.05", "vat_treatment": "RC", "expense_account_id": a["5090"]}]}).json()
        # vendor is owed NET only (RC VAT is self-assessed, not paid to vendor)
        assert bill["grand_total"] == "10500.00" and bill["balance_due"] == "10000.00"
        je = client.get(f"/api/journal-entries/{bill['journal_entry_id']}").json()
        by = {}
        for ln in je["lines"]:
            by.setdefault(ln["account_code"], {"d": 0.0, "c": 0.0})
            by[ln["account_code"]]["d"] += float(ln["debit"]); by[ln["account_code"]]["c"] += float(ln["credit"])
        assert by["5090"]["d"] == 10000.0          # expense (net)
        assert by["1300"]["d"] == 500.0            # input VAT (recoverable, RC)
        assert by["2100"]["c"] == 500.0            # output VAT (self-assessed, RC)
        assert by["2010"]["c"] == 10000.0          # AP = net only


def test_reverse_charge_in_vat_return_output():
    with TestClient(app) as client:
        a = _codes(client)
        vid = client.post("/api/purchases/vendors", json={"name": "RC Vend2"}).json()["id"]
        client.post("/api/purchases/bills", json={
            "vendor_id": vid, "date": "2050-06-01",
            "lines": [{"description": "svc", "quantity": "1", "unit_price": "20000", "vat_rate": "0.05",
                       "vat_treatment": "RC", "expense_account_id": a["5090"]}]})
        vr = client.get("/api/reports/vat-return", params={"start": "2050-01-01", "end": "2050-12-31"}).json()
        # RC VAT (1,000 on 20,000) appears on BOTH sides → net VAT effect zero for this bill
        assert float(vr["total_output_vat"]) >= 1000 and float(vr["total_input_vat"]) >= 1000
        assert vr["reconciled"] is True
