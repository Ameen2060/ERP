"""Edit Sales Invoice + Vendor Bill via reverse-and-repost: recalculation, id/number preserved,
audit trail, and the guard that blocks editing once the document is settled. 2058 dates."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app


def test_edit_invoice_recalcs_and_audits():
    with TestClient(app) as client:
        cid = client.post("/api/sales/customers", json={"name": "Edit Inv Co"}).json()["id"]
        inv = client.post("/api/sales/invoices", json={
            "customer_id": cid, "date": "2058-01-10",
            "lines": [{"description": "A", "quantity": "1", "unit_price": "1000", "vat_rate": "0.05"}],
        }).json()
        iid, num = inv["id"], inv["number"]
        old_je = inv["journal_entry_id"]

        up = client.put(f"/api/sales/invoices/{iid}?reason=Add+line", json={
            "customer_id": cid, "date": "2058-01-10",
            "lines": [
                {"description": "A", "quantity": "2", "unit_price": "1000", "vat_rate": "0.05"},
                {"description": "B", "quantity": "1", "unit_price": "500", "vat_rate": "0"},
            ],
        })
        assert up.status_code == 200, up.text
        b = up.json()
        assert b["id"] == iid and b["number"] == num                 # identity preserved
        assert Decimal(b["net_total"]) == Decimal("2500.00")         # 2000 + 500
        assert Decimal(b["vat_total"]) == Decimal("100.00")          # 5% of 2000 only
        assert Decimal(b["grand_total"]) == Decimal("2600.00")
        assert b["journal_entry_id"] != old_je                       # reposted
        assert client.get(f"/api/journal-entries/{old_je}").json()["status"] == "void"

        audit = client.get(f"/api/sales/invoices/{iid}/audit").json()
        assert audit[0]["reason"] == "Add line"
        assert {"grand_total", "net_total"} <= {c["field"] for c in audit[0]["changes"]}


def test_cannot_edit_paid_invoice():
    with TestClient(app) as client:
        cid = client.post("/api/sales/customers", json={"name": "Paid Inv Co"}).json()["id"]
        inv = client.post("/api/sales/invoices", json={
            "customer_id": cid, "date": "2058-02-01",
            "lines": [{"description": "x", "quantity": "1", "unit_price": "1000", "vat_rate": "0.05"}],
        }).json()
        client.post("/api/sales/payments", json={
            "invoice_id": inv["id"], "date": "2058-02-05", "amount": "500"})
        blocked = client.put(f"/api/sales/invoices/{inv['id']}?reason=x", json={
            "customer_id": cid, "date": "2058-02-01",
            "lines": [{"description": "x", "quantity": "1", "unit_price": "1200", "vat_rate": "0.05"}],
        })
        assert blocked.status_code == 400 and "reverse" in blocked.text.lower()


def test_edit_bill_recalcs_and_audits():
    with TestClient(app) as client:
        vid = client.post("/api/purchases/vendors", json={"name": "Edit Bill Vendor"}).json()["id"]
        acc = {a["code"]: a["id"] for a in client.get("/api/accounts").json()}
        exp = acc.get("5090") or next(a["id"] for a in client.get("/api/accounts").json()
                                      if a["code"].startswith("50"))
        bill = client.post("/api/purchases/bills", json={
            "vendor_id": vid, "date": "2058-03-01",
            "lines": [{"description": "svc", "quantity": "1", "unit_price": "800",
                       "vat_rate": "0.05", "expense_account_id": exp}],
        }).json()
        bid, num = bill["id"], bill["number"]
        up = client.put(f"/api/purchases/bills/{bid}?reason=Price+fix", json={
            "vendor_id": vid, "date": "2058-03-01",
            "lines": [{"description": "svc", "quantity": "1", "unit_price": "1000",
                       "vat_rate": "0.05", "expense_account_id": exp}],
        })
        assert up.status_code == 200, up.text
        b = up.json()
        assert b["id"] == bid and b["number"] == num
        assert Decimal(b["net_total"]) == Decimal("1000.00")
        assert Decimal(b["vat_total"]) == Decimal("50.00")
        assert client.get(f"/api/purchases/bills/{bid}/audit").json()[0]["reason"] == "Price fix"
