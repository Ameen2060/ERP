"""UAE FTA VAT201 return report + PDF/Excel export across reports."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app


def D(x) -> Decimal:
    return Decimal(str(x))


def _acct(client, code):
    return next(a["id"] for a in client.get("/api/accounts").json() if a["code"] == code)


def test_vat201_boxes_and_reconciliation():
    with TestClient(app) as client:
        # A dated window in 2027 isolates this scenario from other tests' postings.
        cid = client.post("/api/sales/customers", json={"name": "VAT Cust"}).json()["id"]
        client.post("/api/sales/invoices", json={
            "customer_id": cid, "date": "2027-01-10",
            "lines": [{"description": "Std", "quantity": "1", "unit_price": "1000", "vat_rate": "0.05"},
                      {"description": "Zero", "quantity": "1", "unit_price": "300", "vat_rate": "0"}]})
        vid = client.post("/api/purchases/vendors", json={"name": "VAT Vendor"}).json()["id"]
        client.post("/api/purchases/bills", json={
            "vendor_id": vid, "date": "2027-01-12",
            "lines": [{"description": "Exp", "quantity": "1", "unit_price": "500", "vat_rate": "0.05",
                       "expense_account_id": _acct(client, "5020")}]})

        r = client.get("/api/reports/vat-return", params={"start": "2027-01-01", "end": "2027-12-31"})
        assert r.status_code == 200, r.text
        v = r.json()
        boxes = {b["box"]: b for b in v["boxes"]}
        assert D(boxes["1"]["amount"]) == Decimal("1000.00") and D(boxes["1"]["vat"]) == Decimal("50.00")
        assert D(boxes["4"]["amount"]) == Decimal("300.00")           # zero-rated
        assert D(boxes["9"]["amount"]) == Decimal("500.00") and D(boxes["9"]["vat"]) == Decimal("25.00")
        assert D(boxes["12"]["vat"]) == Decimal("50.00")              # output tax
        assert D(boxes["13"]["vat"]) == Decimal("25.00")              # input tax
        assert D(v["net_vat_due"]) == Decimal("25.00") and v["is_refund"] is False
        # Ties to the GL VAT accounts for the period.
        assert v["reconciled"] is True
        assert D(v["gl_output_vat"]) == Decimal("50.00") and D(v["gl_input_vat"]) == Decimal("25.00")


def test_exports_pdf_excel_csv():
    with TestClient(app) as client:
        client.post("/api/sales/customers", json={"name": "Exp Cust"})
        # VAT return as PDF.
        pdf = client.get("/api/export/vat-return", params={"format": "pdf", "start": "2027-01-01", "end": "2027-12-31"})
        assert pdf.status_code == 200
        assert pdf.headers["content-type"] == "application/pdf"
        assert pdf.content[:5] == b"%PDF-"
        # Financial statement as PDF.
        pl = client.get("/api/export/income-statement", params={"format": "pdf"})
        assert pl.status_code == 200 and pl.content[:5] == b"%PDF-"
        # Aging still exports xlsx + csv.
        assert client.get("/api/export/ar-aging", params={"format": "xlsx"}).content[:2] == b"PK"
        assert "Accounts Payable Aging" in client.get("/api/export/ap-aging", params={"format": "csv"}).text


def test_balance_sheet_pdf_and_asset_register_pdf():
    with TestClient(app) as client:
        assert client.get("/api/export/balance-sheet", params={"format": "pdf"}).content[:5] == b"%PDF-"
        assert client.get("/api/export/asset-register", params={"format": "pdf"}).content[:5] == b"%PDF-"
        assert client.get("/api/export/cash-flow", params={"format": "pdf"}).content[:5] == b"%PDF-"
