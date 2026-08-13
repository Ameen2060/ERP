"""VAT201 boxes are driven by each line's VAT Treatment code (configurable master), not a
rate heuristic: standard→Box 1/9, zero→Box 4, exempt→Box 5, reverse-charge→Box 3/10. 2054 dates."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app

Y = "2054"
START, END = f"{Y}-01-01", f"{Y}-12-31"


def _box(boxes, num):
    return next(b for b in boxes if b["box"] == num)


def test_boxes_mapped_from_treatment_codes():
    with TestClient(app) as client:
        cid = client.post("/api/sales/customers", json={"name": "VAT201 Co"}).json()["id"]
        # Sales: standard (Box 1), zero (Box 4), exempt (Box 5).
        client.post("/api/sales/invoices", json={
            "customer_id": cid, "date": f"{Y}-02-01",
            "lines": [
                {"description": "Std", "quantity": "1", "unit_price": "1000", "vat_rate": "0.05", "vat_treatment": "SR"},
                {"description": "Zero", "quantity": "1", "unit_price": "500", "vat_rate": "0", "vat_treatment": "ZR"},
                {"description": "Exempt", "quantity": "1", "unit_price": "300", "vat_rate": "0", "vat_treatment": "EX"},
            ],
        })
        # Expenses: standard bill (Box 9) + reverse-charge bill (Box 3 + Box 10).
        vid = client.post("/api/purchases/vendors", json={"name": "VAT201 Vendor"}).json()["id"]
        accs = {a["code"]: a["id"] for a in client.get("/api/accounts").json()}
        exp_acc = accs.get("5090") or next(a["id"] for a in client.get("/api/accounts").json()
                                           if a["code"].startswith("50"))
        client.post("/api/purchases/bills", json={
            "vendor_id": vid, "date": f"{Y}-02-10",
            "lines": [{"description": "Std exp", "quantity": "1", "unit_price": "2000",
                       "vat_rate": "0.05", "vat_treatment": "SR", "expense_account_id": exp_acc}],
        })
        client.post("/api/purchases/bills", json={
            "vendor_id": vid, "date": f"{Y}-02-11",
            "lines": [{"description": "RC import", "quantity": "1", "unit_price": "4000",
                       "vat_rate": "0.05", "vat_treatment": "RC", "expense_account_id": exp_acc}],
        })

        r = client.get(f"/api/reports/vat-return?start={START}&end={END}").json()
        boxes = r["boxes"]

        assert Decimal(_box(boxes, "1")["amount"]) == Decimal("1000.00")
        assert Decimal(_box(boxes, "1")["vat"]) == Decimal("50.00")
        assert Decimal(_box(boxes, "4")["amount"]) == Decimal("500.00")
        assert Decimal(_box(boxes, "5")["amount"]) == Decimal("300.00")
        # Standard expense net in Box 9.
        assert Decimal(_box(boxes, "9")["amount"]) == Decimal("2000.00")
        # Reverse charge: Box 3 (output self-assessed) + Box 10 (recoverable input), VAT = 200.
        assert Decimal(_box(boxes, "3")["vat"]) == Decimal("200.00")
        assert Decimal(_box(boxes, "10")["amount"]) == Decimal("4000.00")
        assert Decimal(_box(boxes, "10")["vat"]) == Decimal("200.00")
        # RC is net-zero: output total includes RC, input total includes RC.
        assert Decimal(_box(boxes, "12")["vat"]) == Decimal("250.00")   # 50 std + 200 RC output
        assert Decimal(_box(boxes, "13")["vat"]) == Decimal("300.00")   # 100 std exp + 200 RC input
