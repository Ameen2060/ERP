"""Customer & vendor advances: posting, available-balance tracking, application/recovery
against invoices/bills, and guards (over-apply, exceeds outstanding, wrong party)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def _codes(client):
    return {a["code"]: a["id"] for a in client.get("/api/accounts").json()}


def _invoice(client, cid, amount="100000"):
    return client.post("/api/sales/invoices", json={
        "customer_id": cid, "date": "2038-07-01",
        "lines": [{"description": "svc", "quantity": "1", "unit_price": amount, "vat_rate": "0"}],
    }).json()


def test_customer_advance_posting_and_recovery():
    with TestClient(app) as client:
        a = _codes(client)
        cid = client.post("/api/sales/customers", json={"name": "Adv Cust"}).json()["id"]
        adv = client.post("/api/sales/advances", json={
            "customer_id": cid, "date": "2038-06-01", "amount": "50000", "deposit_account_id": a["1020"]})
        assert adv.status_code == 200, adv.text
        d = adv.json()
        assert d["amount"] == "50000.00" and d["available"] == "50000.00"
        # posting: Dr Bank / Cr Customer Advances (2160)
        je = client.get(f"/api/journal-entries/{d['journal_entry_id']}").json()
        by = {ln["account_code"]: ln for ln in je["lines"]}
        assert by["1020"]["debit"] == "50000.00" and by["2160"]["credit"] == "50000.00"

        inv = _invoice(client, cid, "100000")
        rec = client.post("/api/advances/apply", json={
            "advance_type": "customer", "advance_id": d["id"], "target_id": inv["id"],
            "amount": "30000", "date": "2038-07-02"})
        assert rec.status_code == 200, rec.text
        assert rec.json()["remaining_advance"] == "20000.00"
        # invoice now shows 30k paid via advance → balance 70k
        inv2 = client.get(f"/api/sales/invoices/{inv['id']}").json()
        assert inv2["amount_paid"] == "30000.00" and inv2["balance_due"] == "70000.00"
        # recovery JE: Dr Customer Advances / Cr AR
        aje = client.get(f"/api/advances/customer/{d['id']}/applications").json()
        je2 = client.get(f"/api/journal-entries/{aje[0]['journal_entry_id']}").json()
        by2 = {ln["account_code"]: ln for ln in je2["lines"]}
        assert by2["2160"]["debit"] == "30000.00" and by2["1100"]["credit"] == "30000.00"


def test_cannot_over_recover_advance():
    with TestClient(app) as client:
        cid = client.post("/api/sales/customers", json={"name": "Over Adv"}).json()["id"]
        d = client.post("/api/sales/advances", json={"customer_id": cid, "date": "2038-06-01", "amount": "10000"}).json()
        inv = _invoice(client, cid, "100000")
        over = client.post("/api/advances/apply", json={
            "advance_type": "customer", "advance_id": d["id"], "target_id": inv["id"],
            "amount": "10000.01", "date": "2038-07-02"})
        assert over.status_code == 400 and "exceeds available" in over.json()["detail"].lower()


def test_recovery_cannot_exceed_invoice_outstanding():
    with TestClient(app) as client:
        cid = client.post("/api/sales/customers", json={"name": "Small Inv"}).json()["id"]
        d = client.post("/api/sales/advances", json={"customer_id": cid, "date": "2038-06-01", "amount": "50000"}).json()
        inv = _invoice(client, cid, "1000")
        bad = client.post("/api/advances/apply", json={
            "advance_type": "customer", "advance_id": d["id"], "target_id": inv["id"],
            "amount": "2000", "date": "2038-07-02"})
        assert bad.status_code == 400 and "outstanding" in bad.json()["detail"].lower()


def test_vendor_advance_and_application():
    with TestClient(app) as client:
        a = _codes(client)
        vid = client.post("/api/purchases/vendors", json={"name": "Adv Vend"}).json()["id"]
        d = client.post("/api/purchases/advances", json={
            "vendor_id": vid, "date": "2038-06-01", "amount": "50000", "payment_account_id": a["1020"]}).json()
        # Dr Vendor Advances (1170) / Cr Bank
        je = client.get(f"/api/journal-entries/{d['journal_entry_id']}").json()
        by = {ln["account_code"]: ln for ln in je["lines"]}
        assert by["1170"]["debit"] == "50000.00" and by["1020"]["credit"] == "50000.00"
        bill = client.post("/api/purchases/bills", json={
            "vendor_id": vid, "date": "2038-07-01",
            "lines": [{"description": "x", "quantity": "1", "unit_price": "100000", "vat_rate": "0",
                       "expense_account_id": a["5090"]}]}).json()
        rec = client.post("/api/advances/apply", json={
            "advance_type": "vendor", "advance_id": d["id"], "target_id": bill["id"],
            "amount": "30000", "date": "2038-07-02"})
        assert rec.status_code == 200, rec.text
        bill2 = client.get(f"/api/purchases/bills/{bill['id']}").json()
        assert bill2["amount_paid"] == "30000.00" and bill2["balance_due"] == "70000.00"
        # Dr AP / Cr Vendor Advances
        aje = client.get(f"/api/advances/vendor/{d['id']}/applications").json()
        je2 = client.get(f"/api/journal-entries/{aje[0]['journal_entry_id']}").json()
        by2 = {ln["account_code"]: ln for ln in je2["lines"]}
        assert by2["2010"]["debit"] == "30000.00" and by2["1170"]["credit"] == "30000.00"


def test_advance_report():
    with TestClient(app) as client:
        cid = client.post("/api/sales/customers", json={"name": "Rep Adv"}).json()["id"]
        client.post("/api/sales/advances", json={"customer_id": cid, "date": "2038-06-01", "amount": "5000"})
        rep = client.get("/api/reports/advances", params={"side": "customer"}).json()
        assert rep["side"] == "customer" and rep["total"] >= 5000 and "available" in rep
