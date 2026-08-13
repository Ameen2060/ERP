"""Sales: invoice posting produces the correct GL entry, payments reduce AR + advance
status, void reverses the GL, and AR aging buckets correctly."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app


def D(x) -> Decimal:
    return Decimal(str(x))


def _customer(client) -> str:
    return client.post("/api/sales/customers", json={"name": "Palm Retail LLC"}).json()["id"]


def _payload(cid, **kw) -> dict:
    return {
        "customer_id": cid, "date": "2025-01-10", "due_date": "2025-02-09",
        "lines": [
            {"description": "Widgets", "quantity": "2", "unit_price": "500", "vat_rate": "0.05"},
            {"description": "Delivery", "quantity": "1", "unit_price": "200", "vat_rate": "0"},
        ], **kw,
    }


def test_post_invoice_creates_balanced_gl_entry():
    with TestClient(app) as client:
        inv = client.post("/api/sales/invoices", json=_payload(_customer(client))).json()
        assert inv["status"] == "posted"
        assert D(inv["net_total"]) == Decimal("1200.00")
        assert D(inv["vat_total"]) == Decimal("50.00")
        assert D(inv["grand_total"]) == Decimal("1250.00")
        je = client.get(f"/api/journal-entries/{inv['journal_entry_id']}").json()
        by_code = {ln["account_code"]: ln for ln in je["lines"]}
        assert D(by_code["1100"]["debit"]) == Decimal("1250.00")
        assert D(by_code["4010"]["credit"]) == Decimal("1200.00")
        assert D(by_code["2100"]["credit"]) == Decimal("50.00")


def test_payment_flow_and_overpay_guard():
    with TestClient(app) as client:
        inv = client.post("/api/sales/invoices", json=_payload(_customer(client))).json()
        client.post("/api/sales/payments", json={"invoice_id": inv["id"], "date": "2025-01-20", "amount": "1000"})
        got = client.get(f"/api/sales/invoices/{inv['id']}").json()
        assert got["status"] == "partial" and D(got["balance_due"]) == Decimal("250.00")
        client.post("/api/sales/payments", json={"invoice_id": inv["id"], "date": "2025-01-25", "amount": "250"})
        assert client.get(f"/api/sales/invoices/{inv['id']}").json()["status"] == "paid"
        over = client.post("/api/sales/payments", json={"invoice_id": inv["id"], "date": "2025-01-26", "amount": "1"})
        assert over.status_code == 400


def test_draft_then_post_and_void():
    with TestClient(app) as client:
        cid = _customer(client)
        draft = client.post("/api/sales/invoices", json=_payload(cid, auto_post=False)).json()
        assert draft["status"] == "draft" and draft["journal_entry_id"] is None
        posted = client.post(f"/api/sales/invoices/{draft['id']}/post").json()
        assert posted["status"] == "posted" and posted["journal_entry_id"]
        other = client.post("/api/sales/invoices", json=_payload(cid)).json()
        je_id = other["journal_entry_id"]
        client.post(f"/api/sales/invoices/{other['id']}/void")
        assert client.get(f"/api/journal-entries/{je_id}").json()["status"] == "void"


def test_ar_aging_buckets():
    with TestClient(app) as client:
        cid = _customer(client)
        client.post("/api/sales/invoices", json=_payload(cid))
        aging = client.get("/api/sales/aging", params={"as_of": "2025-04-01"}).json()
        row = next(r for r in aging["rows"] if r["customer_id"] == cid)
        assert D(row["d31_60"]) == Decimal("1250.00")
