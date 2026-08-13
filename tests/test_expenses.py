"""Direct Expenses: direct-pay vs pay-later journal entries, VAT handling, reports, validation,
and the shared calculation engine."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app
from app.services import calc


def _acc(client):
    accts = {a["code"]: a["id"] for a in client.get("/api/accounts").json()}
    return accts


def test_calc_engine_breakdown():
    s = calc.document_summary(subtotal="1000", vat_rate="0.05",
                              retention_basis="net", retention_percent="10", advance_recovery="100")
    assert str(s["taxable"]) == "1000.00"
    assert str(s["vat"]) == "50.00"
    assert str(s["gross"]) == "1050.00"
    assert str(s["retention"]) == "100.00"          # 10% of net 1000
    assert str(s["advance_recovery"]) == "100.00"
    assert str(s["net_due"]) == "850.00"            # 1050 - 100 - 100


def test_direct_expense_paid_by_bank_with_vat():
    with TestClient(app) as client:
        a = _acc(client)
        r = client.post("/api/expenses", json={
            "date": "2038-02-01", "reference": "EXP-TST-1", "category": "Rent",
            "expense_account_id": a["5020"], "payment_account_id": a["1020"],
            "payment_method": "bank", "paid_directly": True,
            "net_amount": "1000.00", "vat_rate": "0.05",
        })
        assert r.status_code == 200, r.text
        e = r.json()
        assert e["status"] == "posted" and e["vat_amount"] == "50.00" and e["total_amount"] == "1050.00"
        je = client.get(f"/api/journal-entries/{e['journal_entry_id']}").json()
        by = {ln["account_code"]: ln for ln in je["lines"]}
        assert by["5020"]["debit"] == "1000.00"       # expense
        assert by["1300"]["debit"] == "50.00"         # input VAT
        assert by["1020"]["credit"] == "1050.00"      # bank out


def test_direct_expense_by_cash_no_vat():
    with TestClient(app) as client:
        a = _acc(client)
        e = client.post("/api/expenses", json={
            "date": "2038-02-02", "category": "Sundry", "expense_account_id": a["5090"],
            "payment_account_id": a["1010"], "payment_method": "cash", "paid_directly": True,
            "net_amount": "300.00", "vat_rate": "0",
        }).json()
        assert e["vat_amount"] == "0.00" and e["total_amount"] == "300.00"
        je = client.get(f"/api/journal-entries/{e['journal_entry_id']}").json()
        codes = {ln["account_code"] for ln in je["lines"]}
        assert "1300" not in codes                    # no VAT line
        assert "1010" in codes                        # cash out


def test_pay_later_books_payable():
    with TestClient(app) as client:
        a = _acc(client)
        e = client.post("/api/expenses", json={
            "date": "2038-02-03", "category": "Utilities", "expense_account_id": a["5030"],
            "paid_directly": False, "net_amount": "500.00", "vat_rate": "0.05",
        }).json()
        assert e["paid_directly"] is False
        je = client.get(f"/api/journal-entries/{e['journal_entry_id']}").json()
        by = {ln["account_code"]: ln for ln in je["lines"]}
        assert by["2010"]["credit"] == "525.00"       # accounts payable


def test_direct_payment_requires_payment_account():
    with TestClient(app) as client:
        a = _acc(client)
        r = client.post("/api/expenses", json={
            "date": "2038-02-04", "expense_account_id": a["5090"], "paid_directly": True,
            "net_amount": "100.00", "vat_rate": "0",
        })
        assert r.status_code == 400 and "payment account" in r.json()["detail"].lower()


def test_expense_report_by_category():
    with TestClient(app) as client:
        a = _acc(client)
        for cat, acc in [("Marketing", "5040"), ("Marketing", "5040"), ("Rent", "5020")]:
            client.post("/api/expenses", json={
                "date": "2038-03-01", "category": cat, "expense_account_id": a[acc],
                "payment_account_id": a["1020"], "paid_directly": True,
                "net_amount": "200.00", "vat_rate": "0",
            })
        rep = client.get("/api/expenses/report", params={"group_by": "category"}).json()
        groups = {r["group"]: r for r in rep["rows"]}
        assert groups["Marketing"]["count"] >= 2
        assert rep["total_gross"] >= 600
