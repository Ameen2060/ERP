"""Recurring / subscription invoicing: plan creates real sales invoices on schedule, advances
the next-run date, respects max occurrences, and run-due generates for all due plans. 2068 dates."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app


def _plan(client, start="2068-01-01", freq="monthly", maxo=None):
    cid = client.post("/api/sales/customers", json={"name": "Sub Co"}).json()["id"]
    body = {"name": "Monthly retainer", "customer_id": cid, "frequency": freq,
            "start_date": start, "max_occurrences": maxo,
            "lines": [{"description": "Retainer", "quantity": "1", "unit_price": "1000", "vat_rate": "0.05"}]}
    return client.post("/api/recurring", json=body).json(), cid


def test_generate_advances_schedule_and_creates_invoice():
    with TestClient(app) as client:
        p, cid = _plan(client)
        assert p["next_run_date"] == "2068-01-01"
        g = client.post(f"/api/recurring/{p['id']}/generate").json()
        assert g["invoice_number"] and Decimal(g["amount"]) == Decimal("1050.00")  # 1000 + 5% VAT
        assert g["next_run_date"] == "2068-02-01"                                   # advanced one month
        # The generated invoice is a real posted sales invoice for the customer.
        inv = client.get(f"/api/sales/invoices/{g['invoice_id']}").json()
        assert inv["customer_id"] == cid and inv["status"] in ("posted", "partial")
        # Recorded in the plan's run history.
        runs = client.get(f"/api/recurring/{p['id']}/runs").json()
        assert len(runs) == 1 and runs[0]["invoice_number"] == g["invoice_number"]


def test_max_occurrences_deactivates():
    with TestClient(app) as client:
        p, _ = _plan(client, start="2068-03-01", maxo=2)
        client.post(f"/api/recurring/{p['id']}/generate")
        g2 = client.post(f"/api/recurring/{p['id']}/generate").json()
        assert g2["active"] is False                     # hit max → auto-deactivated
        # Further generation is blocked.
        assert client.post(f"/api/recurring/{p['id']}/generate").status_code == 400


def test_run_due_catch_up():
    with TestClient(app) as client:
        p, _ = _plan(client, start="2068-06-01", freq="monthly")
        # Catch-up from June through Aug 15 → 3 invoices (Jun, Jul, Aug).
        r = client.post(f"/api/recurring/run?as_of=2068-08-15&catch_up=true").json()
        mine = [g for g in r["generated"] if g["plan_id"] == p["id"]]
        assert len(mine) == 3
        assert client.get(f"/api/recurring/{p['id']}").json()["next_run_date"] == "2068-09-01"
