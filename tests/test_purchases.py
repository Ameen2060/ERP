"""Purchases/AP: bill posting produces the correct GL entry, payments reduce AP + advance
status, void reverses the GL, and AP aging buckets correctly."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app


def D(x) -> Decimal:
    return Decimal(str(x))


def _acct(client, code):
    return next(a["id"] for a in client.get("/api/accounts").json() if a["code"] == code)


def _vendor(client) -> str:
    return client.post("/api/purchases/vendors", json={"name": "Gulf Supplies LLC", "trn": "100999888777666", "payment_terms": "Net 30"}).json()["id"]


def _bill_payload(client, vid, **kw) -> dict:
    return {
        "vendor_id": vid, "date": "2025-01-10", "due_date": "2025-02-09", "vendor_ref": "SUP-555",
        "lines": [
            {"description": "Office rent", "quantity": "1", "unit_price": "1000", "vat_rate": "0.05", "expense_account_id": _acct(client, "5020")},
            {"description": "Stationery", "quantity": "1", "unit_price": "200", "vat_rate": "0.05", "expense_account_id": _acct(client, "5090")},
        ], **kw,
    }


def test_post_bill_creates_balanced_gl_entry():
    with TestClient(app) as client:
        vid = _vendor(client)
        bill = client.post("/api/purchases/bills", json=_bill_payload(client, vid)).json()
        assert bill["status"] == "posted"
        assert D(bill["net_total"]) == Decimal("1200.00")
        assert D(bill["vat_total"]) == Decimal("60.00")
        assert D(bill["grand_total"]) == Decimal("1260.00")
        je = client.get(f"/api/journal-entries/{bill['journal_entry_id']}").json()
        assert je["source"] == "purchase"
        by_code = {ln["account_code"]: ln for ln in je["lines"]}
        assert D(by_code["5020"]["debit"]) == Decimal("1000.00")
        assert D(by_code["5090"]["debit"]) == Decimal("200.00")
        assert D(by_code["1300"]["debit"]) == Decimal("60.00")   # Input VAT
        assert D(by_code["2010"]["credit"]) == Decimal("1260.00")  # Accounts Payable


def test_pay_bill_flow_and_overpay_guard():
    with TestClient(app) as client:
        vid = _vendor(client)
        bill = client.post("/api/purchases/bills", json=_bill_payload(client, vid)).json()
        client.post("/api/purchases/payments", json={"bill_id": bill["id"], "date": "2025-01-20", "amount": "1000"})
        got = client.get(f"/api/purchases/bills/{bill['id']}").json()
        assert got["status"] == "partial" and D(got["balance_due"]) == Decimal("260.00")
        client.post("/api/purchases/payments", json={"bill_id": bill["id"], "date": "2025-01-25", "amount": "260"})
        assert client.get(f"/api/purchases/bills/{bill['id']}").json()["status"] == "paid"
        over = client.post("/api/purchases/payments", json={"bill_id": bill["id"], "date": "2025-01-26", "amount": "1"})
        assert over.status_code == 400


def test_void_bill_reverses_gl():
    with TestClient(app) as client:
        vid = _vendor(client)
        bill = client.post("/api/purchases/bills", json=_bill_payload(client, vid)).json()
        je_id = bill["journal_entry_id"]
        client.post(f"/api/purchases/bills/{bill['id']}/void")
        assert client.get(f"/api/journal-entries/{je_id}").json()["status"] == "void"


def test_ap_aging_buckets():
    with TestClient(app) as client:
        vid = _vendor(client)
        client.post("/api/purchases/bills", json=_bill_payload(client, vid))  # due 2025-02-09
        aging = client.get("/api/purchases/aging", params={"as_of": "2025-06-20"}).json()  # ~131 days
        row = next(r for r in aging["rows"] if r["vendor_id"] == vid)
        assert D(row["d120_plus"]) == Decimal("1260.00")
        assert D(row["overdue"]) == Decimal("1260.00")
        assert len(row["lines"]) == 1  # bill-level drill-down
