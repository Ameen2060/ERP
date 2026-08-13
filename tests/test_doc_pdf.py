"""Branded per-document PDFs (invoice, customer/vendor credit note, bill) carry the org
header and the document details. Uses 2040 dates to avoid the CT report window."""

from __future__ import annotations

import io

from fastapi.testclient import TestClient

from app.main import app


def _text(pdf_bytes: bytes) -> str:
    import pdfplumber
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return "\n".join((p.extract_text() or "") for p in pdf.pages)


def _codes(client):
    return {a["code"]: a["id"] for a in client.get("/api/accounts").json()}


def test_invoice_and_credit_note_pdfs():
    with TestClient(app) as client:
        client.put("/api/organization", json={"name": "PDF Test Co", "trn": "100200300400500",
                                               "vat_registered": True, "vat_return_frequency": "quarterly"})
        cid = client.post("/api/sales/customers", json={"name": "PDF Cust", "trn": "999888777666555"}).json()["id"]
        inv = client.post("/api/sales/invoices", json={
            "customer_id": cid, "date": "2040-01-01",
            "lines": [{"description": "Consulting", "quantity": "2", "unit_price": "500", "vat_rate": "0.05"}]}).json()
        r = client.get(f"/api/sales/invoices/{inv['id']}/pdf")
        assert r.status_code == 200 and r.content[:4] == b"%PDF"
        t = _text(r.content)
        assert "TAX INVOICE" in t and inv["number"] in t
        assert "PDF Test Co" in t and "100200300400500" in t   # org header + TRN
        assert "Consulting" in t

        cn = client.post("/api/sales/credit-notes", json={
            "customer_id": cid, "date": "2040-01-05", "reason": "Return",
            "lines": [{"description": "Return item", "quantity": "1", "unit_price": "100", "vat_rate": "0.05"}]}).json()
        rc = client.get(f"/api/sales/credit-notes/{cn['id']}/pdf")
        assert rc.status_code == 200 and rc.content[:4] == b"%PDF"
        tc = _text(rc.content)
        assert "CREDIT NOTE" in tc and cn["number"] in tc and "Return" in tc


def test_payment_receipt_pdf_and_cn_shows_party_trn():
    with TestClient(app) as client:
        client.put("/api/organization", json={"name": "Rcpt Co", "trn": "100200300400500",
                                               "vat_registered": True, "vat_return_frequency": "quarterly"})
        cid = client.post("/api/sales/customers",
                          json={"name": "Rcpt Cust", "trn": "555444333222111", "address": "Abu Dhabi"}).json()["id"]
        inv = client.post("/api/sales/invoices", json={
            "customer_id": cid, "date": "2042-01-01",
            "lines": [{"description": "svc", "quantity": "1", "unit_price": "1000", "vat_rate": "0"}]}).json()
        pay = client.post("/api/sales/payments",
                          json={"invoice_id": inv["id"], "date": "2042-01-05", "amount": "400", "reference": "RC-1"}).json()
        r = client.get(f"/api/sales/payments/{pay['id']}/receipt")
        assert r.status_code == 200 and r.content[:4] == b"%PDF"
        t = _text(r.content)
        assert "PAYMENT RECEIPT" in t and "Rcpt Cust" in t and "400.00" in t
        # credit-note PDF now carries the customer TRN + address
        cn = client.post("/api/sales/credit-notes", json={
            "customer_id": cid, "date": "2042-01-06", "reason": "Adj",
            "lines": [{"description": "adj", "quantity": "1", "unit_price": "50", "vat_rate": "0"}]}).json()
        ct = _text(client.get(f"/api/sales/credit-notes/{cn['id']}/pdf").content)
        assert "555444333222111" in ct and "Abu Dhabi" in ct


def test_bill_and_vendor_cn_pdfs():
    with TestClient(app) as client:
        a = _codes(client)
        vid = client.post("/api/purchases/vendors", json={"name": "PDF Vend"}).json()["id"]
        bill = client.post("/api/purchases/bills", json={
            "vendor_id": vid, "date": "2040-01-01",
            "lines": [{"description": "Supplies", "quantity": "1", "unit_price": "1000", "vat_rate": "0.05",
                       "expense_account_id": a["5090"]}]}).json()
        assert client.get(f"/api/purchases/bills/{bill['id']}/pdf").status_code == 200
        cn = client.post("/api/purchases/credit-notes", json={
            "vendor_id": vid, "date": "2040-01-05", "reason": "Damaged",
            "lines": [{"description": "Damaged", "quantity": "1", "unit_price": "100", "vat_rate": "0.05",
                       "expense_account_id": a["5090"]}]}).json()
        r = client.get(f"/api/purchases/credit-notes/{cn['id']}/pdf")
        assert r.status_code == 200 and r.content[:4] == b"%PDF"
        assert "VENDOR CREDIT NOTE" in _text(r.content)
