"""Advance drill-down, unapplication/reversal, audit, and application-detail/aging reports.
Confirms no double-counting of AR and that unapply restores balances. 2060 dates."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app


def _mk_customer_invoice(client, unit="1000"):
    cid = client.post("/api/sales/customers", json={"name": "Adv DD Co"}).json()["id"]
    inv = client.post("/api/sales/invoices", json={
        "customer_id": cid, "date": "2060-01-10",
        "lines": [{"description": "svc", "quantity": "1", "unit_price": unit, "vat_rate": "0.05"}],
    }).json()
    return cid, inv


def test_customer_advance_apply_drilldown_and_unapply():
    with TestClient(app) as client:
        cid, inv = _mk_customer_invoice(client)
        grand = Decimal(inv["grand_total"])                       # 1050
        adv = client.post("/api/sales/advances", json={
            "customer_id": cid, "date": "2060-01-05", "amount": "600"}).json()
        assert adv["journal_entry_id"]                            # advance → receipt JE drill-down

        ap = client.post("/api/advances/apply", json={
            "advance_type": "customer", "advance_id": adv["id"],
            "target_id": inv["id"], "amount": "400", "date": "2060-01-11"})
        assert ap.status_code == 200, ap.text

        # Invoice → advances applied drill-down.
        applied = client.get(f"/api/targets/sales_invoice/{inv['id']}/advances").json()
        assert len(applied) == 1 and applied[0]["advance_number"] == adv["number"]
        assert Decimal(applied[0]["amount"]) == Decimal("400.00")
        appid = applied[0]["id"]

        # Balances reduced, no double AR: invoice balance = grand − 400.
        inv2 = client.get(f"/api/sales/invoices/{inv['id']}").json()
        assert Decimal(inv2["balance_due"]) == grand - Decimal("400")
        assert Decimal(client.get("/api/sales/advances").json()[0]["available"]) == Decimal("200.00")

        # Audit trail recorded the application.
        aud = client.get(f"/api/advances/customer/{adv['id']}/audit").json()
        assert any(a["action"] == "apply" for a in aud)

        # Unapply → restores both balances and logs a reversal.
        un = client.post(f"/api/advances/applications/{appid}/unapply?reason=Wrong+invoice")
        assert un.status_code == 200, un.text
        assert Decimal(client.get(f"/api/sales/invoices/{inv['id']}").json()["balance_due"]) == grand
        assert Decimal(client.get("/api/sales/advances").json()[0]["available"]) == Decimal("600.00")
        assert client.get(f"/api/targets/sales_invoice/{inv['id']}/advances").json() == []
        assert any(a["action"] == "reverse" for a in client.get(f"/api/advances/customer/{adv['id']}/audit").json())


def test_advance_reports():
    with TestClient(app) as client:
        cid, inv = _mk_customer_invoice(client, unit="2000")
        adv = client.post("/api/sales/advances", json={
            "customer_id": cid, "date": "2060-02-01", "amount": "500"}).json()
        client.post("/api/advances/apply", json={
            "advance_type": "customer", "advance_id": adv["id"],
            "target_id": inv["id"], "amount": "300", "date": "2060-02-02"})

        details = client.get("/api/reports/advance-applications?side=customer").json()
        assert any(r["advance_number"] == adv["number"] and r["target_number"] == inv["number"]
                   for r in details["rows"])

        aging = client.get("/api/reports/advance-aging?side=customer&as_of=2060-02-15").json()
        assert Decimal(str(aging["total_outstanding"])) >= Decimal("200")  # 500 − 300 still unapplied
        assert set(aging["buckets"]) == {"current", "31_60", "61_90", "over_90"}


def test_vendor_advance_target_drilldown():
    with TestClient(app) as client:
        vid = client.post("/api/purchases/vendors", json={"name": "Adv DD Vendor"}).json()["id"]
        acc = {a["code"]: a["id"] for a in client.get("/api/accounts").json()}
        exp = acc.get("5090") or next(a["id"] for a in client.get("/api/accounts").json()
                                      if a["code"].startswith("50"))
        bill = client.post("/api/purchases/bills", json={
            "vendor_id": vid, "date": "2060-03-01",
            "lines": [{"description": "x", "quantity": "1", "unit_price": "1000",
                       "vat_rate": "0.05", "expense_account_id": exp}],
        }).json()
        adv = client.post("/api/purchases/advances", json={
            "vendor_id": vid, "date": "2060-02-20", "amount": "400"}).json()
        client.post("/api/advances/apply", json={
            "advance_type": "vendor", "advance_id": adv["id"],
            "target_id": bill["id"], "amount": "400", "date": "2060-03-02"})
        applied = client.get(f"/api/targets/vendor_bill/{bill['id']}/advances").json()
        assert len(applied) == 1 and Decimal(applied[0]["amount"]) == Decimal("400.00")
