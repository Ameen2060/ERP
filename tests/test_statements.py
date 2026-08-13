"""Customer & Vendor statements: opening balance, running balance, closing reconciles;
and their Excel/PDF export."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app


def D(x) -> Decimal:
    return Decimal(str(x))


def _acct(client, code):
    return next(a["id"] for a in client.get("/api/accounts").json() if a["code"] == code)


def test_customer_statement_running_balance():
    with TestClient(app) as client:
        cid = client.post("/api/sales/customers", json={"name": "Stmt Cust", "trn": "100999000111222"}).json()["id"]
        # Two invoices then a partial receipt, all in 2030 to isolate.
        i1 = client.post("/api/sales/invoices", json={"customer_id": cid, "date": "2030-01-05",
            "lines": [{"description": "A", "quantity": "1", "unit_price": "1000", "vat_rate": "0"}]}).json()
        client.post("/api/sales/invoices", json={"customer_id": cid, "date": "2030-02-05",
            "lines": [{"description": "B", "quantity": "1", "unit_price": "500", "vat_rate": "0"}]})
        client.post("/api/sales/payments", json={"invoice_id": i1["id"], "date": "2030-02-10", "amount": "600"})

        st = client.get("/api/reports/customer-statement", params={"customer_id": cid, "start": "2030-01-01", "end": "2030-12-31"}).json()
        assert st["party_trn"] == "100999000111222"
        assert D(st["opening_balance"]) == Decimal("0.00")
        # Debits 1500 (two invoices), credits 600 → closing 900.
        assert D(st["total_debit"]) == Decimal("1500.00")
        assert D(st["total_credit"]) == Decimal("600.00")
        assert D(st["closing_balance"]) == Decimal("900.00")
        # Running balance on the last line equals the closing balance (reconciles).
        assert D(st["lines"][-1]["balance"]) == Decimal("900.00")
        # Each line links to its source.
        assert st["lines"][0]["link"] in ("invoice", "journal")


def test_customer_statement_opening_balance_from_prior_period():
    with TestClient(app) as client:
        cid = client.post("/api/sales/customers", json={"name": "Opening Cust"}).json()["id"]
        client.post("/api/sales/invoices", json={"customer_id": cid, "date": "2031-01-01",
            "lines": [{"description": "Prior", "quantity": "1", "unit_price": "800", "vat_rate": "0"}]})
        # Statement starting AFTER that invoice → it becomes the opening balance.
        st = client.get("/api/reports/customer-statement", params={"customer_id": cid, "start": "2031-06-01", "end": "2031-12-31"}).json()
        assert D(st["opening_balance"]) == Decimal("800.00")
        assert st["lines"] == []


def test_vendor_statement_and_exports():
    with TestClient(app) as client:
        vid = client.post("/api/purchases/vendors", json={"name": "Stmt Vendor"}).json()["id"]
        client.post("/api/purchases/bills", json={"vendor_id": vid, "date": "2030-03-01",
            "lines": [{"description": "X", "quantity": "1", "unit_price": "2000", "vat_rate": "0",
                       "expense_account_id": _acct(client, "5090")}]})
        st = client.get("/api/reports/vendor-statement", params={"vendor_id": vid}).json()
        assert D(st["closing_balance"]) == Decimal("2000.00") and st["kind"] == "vendor"
        # Exports.
        assert client.get("/api/export/vendor-statement", params={"format": "pdf", "vendor_id": vid}).content[:5] == b"%PDF-"
        cid = client.post("/api/sales/customers", json={"name": "Stmt Export Cust"}).json()["id"]
        assert client.get("/api/export/customer-statement", params={"format": "xlsx", "customer_id": cid}).content[:2] == b"PK"
        # Missing id → 400.
        assert client.get("/api/export/customer-statement", params={"format": "pdf"}).status_code == 400
