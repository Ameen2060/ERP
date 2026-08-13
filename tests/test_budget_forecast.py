"""Budget run-rate forecast + budget/advance exports. 2065 dates."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app


def _company_budget(client, year=2065):
    acc = {a["code"]: a["id"] for a in client.get("/api/accounts").json()}
    exp = acc.get("5090") or next(a["id"] for a in client.get("/api/accounts").json() if a["code"].startswith("50"))
    b = client.post("/api/budgets", json={
        "name": "FC Test", "fiscal_year": year, "scope": "company",
        "lines": [{"account_id": exp, "month": 0, "amount": "12000"}]}).json()
    return b, exp


def test_forecast_run_rate_projection():
    with TestClient(app) as client:
        b, exp = _company_budget(client)
        acc = {a["code"]: a["id"] for a in client.get("/api/accounts").json()}
        # Post 1,000 of actual expense in the fiscal year.
        client.post("/api/journal-entries", json={
            "date": "2065-01-31", "memo": "exp",
            "lines": [{"account_id": exp, "debit": "1000"}, {"account_id": acc["1020"], "credit": "1000"}]})
        # 1 month elapsed → project 1000 × 12 = 12,000.
        f = client.get(f"/api/budgets/{b['id']}/forecast?months_elapsed=1").json()
        assert Decimal(str(f["total_actual_ytd"])) == Decimal("1000")
        assert Decimal(str(f["total_projected"])) == Decimal("12000")
        assert Decimal(str(f["projected_variance"])) == Decimal("0")
        # 2 months elapsed → 1000 × 6 = 6,000 projected.
        f2 = client.get(f"/api/budgets/{b['id']}/forecast?months_elapsed=2").json()
        assert Decimal(str(f2["total_projected"])) == Decimal("6000")


def test_budget_and_advance_exports():
    with TestClient(app) as client:
        b, _ = _company_budget(client, year=2066)
        for report in ("budget-vs-actual", "budget-forecast"):
            r = client.get(f"/api/export/{report}?format=xlsx&budget_id={b['id']}")
            assert r.status_code == 200 and r.content[:2] == b"PK", report
        for report in ("advance-applications", "advance-aging"):
            r = client.get(f"/api/export/{report}?format=csv&side=customer")
            assert r.status_code == 200, report
        # Missing budget_id → 400.
        assert client.get("/api/export/budget-vs-actual?format=csv").status_code == 400
