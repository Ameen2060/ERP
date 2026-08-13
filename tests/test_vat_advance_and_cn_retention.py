"""Spec A gaps: retention on credit notes (split to Retention Receivable/Payable) and
configurable VAT-on-advances (tax point at receipt) with no double-VAT on recovery.
2046 dates to isolate from the CT report window."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def _codes(client):
    return {a["code"]: a["id"] for a in client.get("/api/accounts").json()}


def _je(client, jid):
    return {ln["account_code"]: ln for ln in client.get(f"/api/journal-entries/{jid}").json()["lines"]}


def _enable_advance_vat(client):
    client.post("/api/system-settings/advance-vat/transition", params={"action": "disable"})
    for act in ("request_review", "approve", "enable"):
        r = client.post("/api/system-settings/advance-vat/transition", params={"action": act})
        assert r.status_code == 200, r.text


def _disable_advance_vat(client):
    client.post("/api/system-settings/advance-vat/transition", params={"action": "disable"})


def test_customer_credit_note_retention_split():
    with TestClient(app) as client:
        cid = client.post("/api/sales/customers", json={"name": "CNR Cust"}).json()["id"]
        cn = client.post("/api/sales/credit-notes", json={
            "customer_id": cid, "date": "2046-01-05", "reason": "Retention adj",
            "lines": [{"description": "x", "quantity": "1", "unit_price": "10000", "vat_rate": "0"}],
            "retention_applicable": True, "retention_basis": "net", "retention_percent": "10"}).json()
        assert cn["retention_amount"] == "1000.00"
        by = _je(client, cn["journal_entry_id"])
        assert by["4010"]["debit"] == "10000.00"     # revenue reversed
        assert by["1150"]["credit"] == "1000.00"      # retention receivable reduced
        assert by["1100"]["credit"] == "9000.00"      # AR reduced by grand - retention


def test_vendor_credit_note_retention_split():
    with TestClient(app) as client:
        a = _codes(client)
        vid = client.post("/api/purchases/vendors", json={"name": "CNR Vend"}).json()["id"]
        cn = client.post("/api/purchases/credit-notes", json={
            "vendor_id": vid, "date": "2046-01-05", "reason": "ret",
            "lines": [{"description": "x", "quantity": "1", "unit_price": "10000", "vat_rate": "0",
                       "expense_account_id": a["5090"]}],
            "retention_applicable": True, "retention_basis": "net", "retention_percent": "10"}).json()
        by = _je(client, cn["journal_entry_id"])
        assert by["2010"]["debit"] == "9000.00"       # AP reduced by grand - retention
        assert by["2150"]["debit"] == "1000.00"       # retention payable reduced
        assert by["5090"]["credit"] == "10000.00"     # expense reversed


def test_advance_vat_gate_and_provisional_return():
    with TestClient(app) as client:
        _disable_advance_vat(client)
        cid = client.post("/api/sales/customers", json={"name": "Gate Cust"}).json()["id"]
        # OFF by default → VAT-applicable advance is blocked
        blocked = client.post("/api/sales/advances", json={
            "customer_id": cid, "date": "2046-03-01", "amount": "1050",
            "vat_applicable": True, "vat_rate": "0.05"})
        assert blocked.status_code == 400 and "off" in blocked.json()["detail"].lower()
        # drive the SME workflow to enabled
        _enable_advance_vat(client)
        s = client.get("/api/system-settings").json()
        assert s["advance_vat_status"] == "enabled" and s["advance_vat_filing_ready"] is True
        _disable_advance_vat(client)   # leave OFF for other tests


def test_customer_advance_vat_and_no_double_vat_on_recovery():
    with TestClient(app) as client:
        _enable_advance_vat(client)
        cid = client.post("/api/sales/customers", json={"name": "AdvVat Cust"}).json()["id"]
        adv = client.post("/api/sales/advances", json={
            "customer_id": cid, "date": "2046-02-01", "amount": "10500",
            "vat_applicable": True, "vat_rate": "0.05"}).json()
        assert adv["net_amount"] == "10000.00" and adv["vat_amount"] == "500.00"
        assert adv["requires_sme_validation"] is True
        by = _je(client, adv["journal_entry_id"])
        assert by["1020"]["debit"] == "10500.00"      # bank gross in
        assert by["2160"]["credit"] == "10000.00"     # customer advances (net)
        assert by["2100"]["credit"] == "500.00"       # output VAT recognised at receipt
        # recover against an invoice — the advance VAT is reversed so it isn't double counted
        inv = client.post("/api/sales/invoices", json={
            "customer_id": cid, "date": "2046-02-05",
            "lines": [{"description": "job", "quantity": "1", "unit_price": "20000", "vat_rate": "0.05"}]}).json()
        rec = client.post("/api/advances/apply", json={
            "advance_type": "customer", "advance_id": adv["id"], "target_id": inv["id"],
            "amount": "10500", "date": "2046-02-06"})
        assert rec.status_code == 200, rec.text
        aje = client.get(f"/api/advances/customer/{adv['id']}/applications").json()
        by2 = _je(client, aje[0]["journal_entry_id"])
        assert by2["2160"]["debit"] == "10000.00"     # clear advance (net)
        assert by2["2100"]["debit"] == "500.00"       # reverse advance-stage output VAT
        assert by2["1100"]["credit"] == "10500.00"    # AR reduced by gross applied


def test_vendor_advance_vat_posting():
    with TestClient(app) as client:
        _enable_advance_vat(client)
        a = _codes(client)
        vid = client.post("/api/purchases/vendors", json={"name": "AdvVat Vend"}).json()["id"]
        adv = client.post("/api/purchases/advances", json={
            "vendor_id": vid, "date": "2046-02-01", "amount": "10500",
            "vat_applicable": True, "vat_rate": "0.05"}).json()
        by = _je(client, adv["journal_entry_id"])
        assert by["1170"]["debit"] == "10000.00"      # vendor advances (net)
        assert by["1300"]["debit"] == "500.00"        # input VAT recognised
        assert by["1020"]["credit"] == "10500.00"     # bank gross out
