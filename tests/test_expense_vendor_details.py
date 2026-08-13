"""Expense enhancement: vendor TRN + address auto-populate from the Vendor Master into the
expense record and its PDF voucher. 2051 dates."""

from __future__ import annotations

import io

from fastapi.testclient import TestClient

from app.main import app


def test_expense_carries_vendor_trn_and_address():
    with TestClient(app) as client:
        accs = {a["code"]: a["id"] for a in client.get("/api/accounts").json()}
        vid = client.post("/api/purchases/vendors",
                          json={"name": "TRN Vendor", "trn": "100200300400999",
                                "address": "Jebel Ali, Dubai"}).json()["id"]
        exp = client.post("/api/expenses", json={
            "date": "2051-01-01", "category": "Freight", "vendor_id": vid,
            "expense_account_id": accs["5090"], "payment_account_id": accs["1020"],
            "paid_directly": True, "net_amount": "1000", "vat_rate": "0.05"}).json()
        assert exp["vendor_trn"] == "100200300400999" and exp["vendor_address"] == "Jebel Ali, Dubai"
        pdf = client.get(f"/api/expenses/{exp['id']}/pdf")
        assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
        import pdfplumber
        with pdfplumber.open(io.BytesIO(pdf.content)) as p:
            t = "\n".join((pg.extract_text() or "") for pg in p.pages)
        assert "100200300400999" in t and "Jebel Ali" in t
