"""Cross-module reports: AR aging (6 buckets + risk), invoice-by-vendor, sales-by-customer,
and Excel/CSV export."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app


def D(x) -> Decimal:
    return Decimal(str(x))


def _acct(client, code):
    return next(a["id"] for a in client.get("/api/accounts").json() if a["code"] == code)


def test_ar_aging_six_buckets_and_risk():
    with TestClient(app) as client:
        cid = client.post("/api/sales/customers", json={"name": "Risky Customer LLC"}).json()["id"]
        # Due long ago → lands in 120+ → high risk.
        client.post("/api/sales/invoices", json={
            "customer_id": cid, "date": "2025-01-01", "due_date": "2025-01-31",
            "lines": [{"description": "X", "quantity": "1", "unit_price": "1000", "vat_rate": "0"}]})
        rep = client.get("/api/sales/aging", params={"as_of": "2025-07-01"}).json()
        row = next(r for r in rep["rows"] if r["customer_id"] == cid)
        assert D(row["d120_plus"]) == Decimal("1000.00")
        assert row["risk"] == "high"
        assert "d91_120" in row and len(row["lines"]) == 1


def test_invoice_by_vendor_report():
    with TestClient(app) as client:
        vid = client.post("/api/purchases/vendors", json={"name": "Report Vendor", "trn": "100000000000001"}).json()["id"]
        client.post("/api/purchases/bills", json={
            "vendor_id": vid, "date": "2026-03-05", "due_date": "2026-04-04",
            "lines": [{"description": "Svc", "quantity": "1", "unit_price": "5000", "vat_rate": "0.05",
                       "expense_account_id": _acct(client, "5090")}]})
        rep = client.get("/api/reports/invoice-by-vendor", params={"group_by": "vendor", "start": "2026-01-01", "end": "2026-12-31"}).json()
        row = next(r for r in rep["rows"] if r["party_id"] == vid)
        assert D(row["net"]) == Decimal("5000.00") and D(row["vat"]) == Decimal("250.00")
        assert row["party_trn"] == "100000000000001"
        grp = next(g for g in rep["groups"] if g["key"] == vid)
        assert D(grp["gross"]) == Decimal("5250.00")


def test_sales_by_customer_report_grouping():
    with TestClient(app) as client:
        cid = client.post("/api/sales/customers", json={"name": "Group Customer"}).json()["id"]
        client.post("/api/sales/invoices", json={
            "customer_id": cid, "date": "2026-05-10", "sales_category": "Consulting",
            "lines": [{"description": "Y", "quantity": "1", "unit_price": "3000", "vat_rate": "0.05"}]})
        rep = client.get("/api/reports/sales-by-customer", params={"group_by": "month", "start": "2026-01-01", "end": "2026-12-31"}).json()
        assert any(g["key"] == "2026-05" for g in rep["groups"])
        assert D(rep["total_net"]) >= Decimal("3000.00")


def test_excel_and_csv_export():
    with TestClient(app) as client:
        client.post("/api/sales/customers", json={"name": "Export Cust"})
        xlsx = client.get("/api/export/ar-aging", params={"format": "xlsx"})
        assert xlsx.status_code == 200
        assert "spreadsheetml" in xlsx.headers["content-type"]
        assert xlsx.content[:2] == b"PK"  # xlsx is a zip
        csv = client.get("/api/export/ap-aging", params={"format": "csv"})
        assert csv.status_code == 200 and "text/csv" in csv.headers["content-type"]
        assert "Accounts Payable Aging" in csv.text
