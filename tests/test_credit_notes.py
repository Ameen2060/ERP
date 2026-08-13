"""Customer & vendor credit notes: posting (reduces AR/AP + reverses revenue/cost + VAT),
application to invoices/bills, and guards. Uses 2039 dates to avoid the CT report's window."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def _codes(client):
    return {a["code"]: a["id"] for a in client.get("/api/accounts").json()}


def _invoice(client, cid, amount="100000"):
    return client.post("/api/sales/invoices", json={
        "customer_id": cid, "date": "2039-01-01",
        "lines": [{"description": "svc", "quantity": "1", "unit_price": amount, "vat_rate": "0.05"}],
    }).json()


def test_customer_cn_posting_reduces_ar_revenue_vat():
    with TestClient(app) as client:
        cid = client.post("/api/sales/customers", json={"name": "CN Cust"}).json()["id"]
        cn = client.post("/api/sales/credit-notes", json={
            "customer_id": cid, "date": "2039-01-05", "reason": "Goods returned",
            "lines": [{"description": "return", "quantity": "1", "unit_price": "10000", "vat_rate": "0.05"}]})
        assert cn.status_code == 200, cn.text
        d = cn.json()
        assert d["grand_total"] == "10500.00" and d["status"] == "posted" and d["unapplied"] == "10500.00"
        je = client.get(f"/api/journal-entries/{d['journal_entry_id']}").json()
        by = {ln["account_code"]: ln for ln in je["lines"]}
        assert by["4010"]["debit"] == "10000.00"   # revenue reversed
        assert by["2100"]["debit"] == "500.00"      # output VAT reversed
        assert by["1100"]["credit"] == "10500.00"   # AR reduced


def test_customer_cn_reason_required():
    with TestClient(app) as client:
        cid = client.post("/api/sales/customers", json={"name": "No Reason"}).json()["id"]
        r = client.post("/api/sales/credit-notes", json={
            "customer_id": cid, "date": "2039-01-05", "reason": "",
            "lines": [{"description": "x", "quantity": "1", "unit_price": "100", "vat_rate": "0"}]})
        assert r.status_code == 400


def test_customer_cn_application_reduces_invoice_balance():
    with TestClient(app) as client:
        cid = client.post("/api/sales/customers", json={"name": "Apply Cust"}).json()["id"]
        inv = _invoice(client, cid, "100000")          # grand 105,000
        cn = client.post("/api/sales/credit-notes", json={
            "customer_id": cid, "date": "2039-01-06", "reason": "Discount", "invoice_id": inv["id"],
            "lines": [{"description": "adj", "quantity": "1", "unit_price": "10000", "vat_rate": "0"}]}).json()
        assert cn["unapplied"] == "10000.00"
        ap = client.post("/api/credit-notes/apply", json={
            "cn_type": "customer", "cn_id": cn["id"], "target_id": inv["id"],
            "amount": "10000", "date": "2039-01-07"})
        assert ap.status_code == 200, ap.text
        assert ap.json()["remaining_credit"] == "0.00"
        inv2 = client.get(f"/api/sales/invoices/{inv['id']}").json()
        assert inv2["balance_due"] == "95000.00"       # 105,000 - 10,000 credit applied
        # over-apply beyond remaining credit rejected
        over = client.post("/api/credit-notes/apply", json={
            "cn_type": "customer", "cn_id": cn["id"], "target_id": inv["id"],
            "amount": "1", "date": "2039-01-08"})
        assert over.status_code == 400


def test_cn_cannot_exceed_linked_invoice():
    with TestClient(app) as client:
        cid = client.post("/api/sales/customers", json={"name": "Big CN"}).json()["id"]
        inv = _invoice(client, cid, "1000")            # grand 1,050
        r = client.post("/api/sales/credit-notes", json={
            "customer_id": cid, "date": "2039-01-06", "reason": "Too big", "invoice_id": inv["id"],
            "lines": [{"description": "adj", "quantity": "1", "unit_price": "5000", "vat_rate": "0"}]})
        assert r.status_code == 400


def test_vendor_cn_posting_and_application():
    with TestClient(app) as client:
        a = _codes(client)
        vid = client.post("/api/purchases/vendors", json={"name": "CN Vend"}).json()["id"]
        bill = client.post("/api/purchases/bills", json={
            "vendor_id": vid, "date": "2039-01-01",
            "lines": [{"description": "goods", "quantity": "1", "unit_price": "100000", "vat_rate": "0.05",
                       "expense_account_id": a["5090"]}]}).json()
        cn = client.post("/api/purchases/credit-notes", json={
            "vendor_id": vid, "date": "2039-01-05", "reason": "Returned goods", "bill_id": bill["id"],
            "lines": [{"description": "ret", "quantity": "1", "unit_price": "10000", "vat_rate": "0.05",
                       "expense_account_id": a["5090"]}]}).json()
        assert cn["grand_total"] == "10500.00"
        je = client.get(f"/api/journal-entries/{cn['journal_entry_id']}").json()
        by = {ln["account_code"]: ln for ln in je["lines"]}
        assert by["2010"]["debit"] == "10500.00"    # AP reduced
        assert by["5090"]["credit"] == "10000.00"   # expense reversed
        assert by["1300"]["credit"] == "500.00"     # input VAT reversed
        ap = client.post("/api/credit-notes/apply", json={
            "cn_type": "vendor", "cn_id": cn["id"], "target_id": bill["id"],
            "amount": "10500", "date": "2039-01-07"})
        assert ap.status_code == 200, ap.text
        bill2 = client.get(f"/api/purchases/bills/{bill['id']}").json()
        assert bill2["balance_due"] == "94500.00"   # 105,000 - 10,500


def test_credit_note_report():
    with TestClient(app) as client:
        cid = client.post("/api/sales/customers", json={"name": "Rep CN"}).json()["id"]
        client.post("/api/sales/credit-notes", json={
            "customer_id": cid, "date": "2039-02-01", "reason": "x",
            "lines": [{"description": "r", "quantity": "1", "unit_price": "1000", "vat_rate": "0"}]})
        rep = client.get("/api/reports/credit-notes", params={"side": "customer"}).json()
        assert rep["side"] == "customer" and rep["total"] >= 1000 and "unapplied" in rep
