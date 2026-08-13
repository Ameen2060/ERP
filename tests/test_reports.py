"""Financial statements: P&L gross/net profit, balance sheet balances, dashboard KPIs."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app


def D(x) -> Decimal:
    return Decimal(str(x))


def _post(client, date_, lines, source="manual"):
    r = client.post("/api/journal-entries", json={"date": date_, "source": source, "lines": lines})
    assert r.status_code == 200, r.text


def test_income_statement_gross_and_net_profit():
    with TestClient(app) as client:
        accts = {a["code"]: a for a in client.get("/api/accounts").json()}
        cash = accts["1010"]["id"]
        rev = client.post("/api/accounts", json={"code": "4901", "name": "R", "type": "income"}).json()["id"]
        cogs = client.post("/api/accounts", json={"code": "5901", "name": "C", "type": "expense", "is_cost_of_sales": True}).json()["id"]
        opex = client.post("/api/accounts", json={"code": "5902", "name": "O", "type": "expense"}).json()["id"]
        _post(client, "2099-06-15", [{"account_id": cash, "debit": "1000"}, {"account_id": rev, "credit": "1000"}])
        _post(client, "2099-06-16", [{"account_id": cogs, "debit": "400"}, {"account_id": cash, "credit": "400"}])
        _post(client, "2099-06-17", [{"account_id": opex, "debit": "100"}, {"account_id": cash, "credit": "100"}])
        pl = client.get("/api/reports/income-statement", params={"start": "2099-01-01", "end": "2099-12-31"}).json()
        assert D(pl["total_income"]) == Decimal("1000.00")
        assert D(pl["gross_profit"]) == Decimal("600.00")
        assert D(pl["net_profit"]) == Decimal("500.00")


def test_balance_sheet_balances():
    with TestClient(app) as client:
        accts = {a["code"]: a for a in client.get("/api/accounts").json()}
        _post(client, "2099-01-02", [
            {"account_id": accts["1010"]["id"], "debit": "50000"},
            {"account_id": accts["3010"]["id"], "credit": "50000"}])
        bs = client.get("/api/reports/balance-sheet").json()
        assert bs["balanced"] is True
        assert D(bs["total_assets"]) == D(bs["total_liabilities"]) + D(bs["total_equity"])


def test_dashboard_kpis():
    with TestClient(app) as client:
        d = client.get("/api/dashboard").json()
        for key in ("cash", "bank", "accounts_receivable", "revenue_ytd", "net_profit_ytd", "vat_payable"):
            assert key in d
        assert isinstance(d["outstanding_invoices"], int)
