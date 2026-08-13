"""Retention (holdback) on sales invoices and vendor bills: split posting to Retention
Receivable/Payable, payment cap excludes held retention, release action, and the report."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def _codes(client):
    return {a["code"]: a["id"] for a in client.get("/api/accounts").json()}


def test_sales_invoice_retention_posting_and_payment_cap():
    with TestClient(app) as client:
        cid = client.post("/api/sales/customers", json={"name": "Retention Cust"}).json()["id"]
        inv = client.post("/api/sales/invoices", json={
            "customer_id": cid, "date": "2038-06-01",
            "lines": [{"description": "works", "quantity": "1", "unit_price": "100000", "vat_rate": "0.05"}],
            "retention_applicable": True, "retention_basis": "net", "retention_percent": "10",
            "retention_release_date": "2038-12-01",
        })
        assert inv.status_code == 200, inv.text
        d = inv.json()
        assert d["grand_total"] == "105000.00"
        assert d["retention_amount"] == "10000.00"
        assert d["retention_outstanding"] == "10000.00"
        assert d["balance_due"] == "95000.00"          # grand - retention held
        # journal: Retention Receivable 10k debit, AR 95k debit, Sales 100k cr, VAT 5k cr
        je = client.get(f"/api/journal-entries/{d['journal_entry_id']}").json()
        by = {ln["account_code"]: ln for ln in je["lines"]}
        assert by["1150"]["debit"] == "10000.00"       # retention receivable
        assert by["1100"]["debit"] == "95000.00"       # AR
        assert by["4010"]["credit"] == "100000.00"
        assert by["2100"]["credit"] == "5000.00"
        # cannot pay more than the collectible (95,000)
        over = client.post("/api/sales/payments", json={"invoice_id": d["id"], "date": "2038-06-10", "amount": "95000.01"})
        assert over.status_code == 400
        ok = client.post("/api/sales/payments", json={"invoice_id": d["id"], "date": "2038-06-10", "amount": "95000.00"})
        assert ok.status_code == 200


def test_sales_retention_release_to_receivable():
    with TestClient(app) as client:
        cid = client.post("/api/sales/customers", json={"name": "Rel Cust"}).json()["id"]
        d = client.post("/api/sales/invoices", json={
            "customer_id": cid, "date": "2038-06-01",
            "lines": [{"description": "works", "quantity": "1", "unit_price": "100000", "vat_rate": "0"}],
            "retention_applicable": True, "retention_basis": "net", "retention_percent": "10",
        }).json()
        assert d["retention_outstanding"] == "10000.00"
        rel = client.post(f"/api/sales/invoices/{d['id']}/release-retention",
                          json={"amount": "10000", "date": "2038-12-01", "to_bank": False})
        assert rel.status_code == 200, rel.text
        r = rel.json()
        assert r["retention_released"] == "10000.00" and r["retention_outstanding"] == "0.00"
        # over-release rejected
        again = client.post(f"/api/sales/invoices/{d['id']}/release-retention",
                            json={"amount": "1", "date": "2038-12-02", "to_bank": False})
        assert again.status_code == 400


def test_vendor_bill_retention_posting():
    with TestClient(app) as client:
        a = _codes(client)
        vid = client.post("/api/purchases/vendors", json={"name": "Retention Vend"}).json()["id"]
        bill = client.post("/api/purchases/bills", json={
            "vendor_id": vid, "date": "2038-06-01",
            "lines": [{"description": "subcontract", "quantity": "1", "unit_price": "100000",
                       "vat_rate": "0.05", "expense_account_id": a["5090"]}],
            "retention_applicable": True, "retention_basis": "net", "retention_percent": "10",
        })
        assert bill.status_code == 200, bill.text
        d = bill.json()
        assert d["retention_amount"] == "10000.00" and d["balance_due"] == "95000.00"
        je = client.get(f"/api/journal-entries/{d['journal_entry_id']}").json()
        by = {ln["account_code"]: ln for ln in je["lines"]}
        assert by["2150"]["credit"] == "10000.00"      # retention payable
        assert by["2010"]["credit"] == "95000.00"      # AP


def test_retention_over_limit_rejected_and_report():
    with TestClient(app) as client:
        cid = client.post("/api/sales/customers", json={"name": "Over Cust"}).json()["id"]
        bad = client.post("/api/sales/invoices", json={
            "customer_id": cid, "date": "2038-06-01",
            "lines": [{"description": "x", "quantity": "1", "unit_price": "1000", "vat_rate": "0"}],
            "retention_applicable": True, "retention_basis": "amount", "retention_amount": "5000",
        })
        assert bad.status_code == 400  # retention 5000 > grand 1000
        rep = client.get("/api/reports/retention", params={"side": "customer"}).json()
        assert "rows" in rep and "total_outstanding" in rep and rep["account"] == "Retention Receivable"
