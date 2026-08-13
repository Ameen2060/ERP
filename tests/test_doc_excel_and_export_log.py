"""Per-transaction Excel export for every document kind + export audit log. 2070 dates."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def _accounts(client):
    return {a["code"]: a["id"] for a in client.get("/api/accounts").json()}


def test_document_excel_for_all_kinds():
    with TestClient(app) as client:
        acc = _accounts(client)
        cid = client.post("/api/sales/customers", json={"name": "XL Co"}).json()["id"]
        vid = client.post("/api/purchases/vendors", json={"name": "XL Vendor"}).json()["id"]
        exp_acc = acc.get("5090") or next(a["id"] for a in client.get("/api/accounts").json() if a["code"].startswith("50"))

        inv = client.post("/api/sales/invoices", json={"customer_id": cid, "date": "2070-01-05",
            "lines": [{"description": "svc", "quantity": "2", "unit_price": "500", "vat_rate": "0.05"}]}).json()
        bill = client.post("/api/purchases/bills", json={"vendor_id": vid, "date": "2070-01-06",
            "lines": [{"description": "buy", "quantity": "1", "unit_price": "800", "vat_rate": "0.05", "expense_account_id": exp_acc}]}).json()
        ccn = client.post("/api/sales/credit-notes", json={"customer_id": cid, "date": "2070-01-07", "reason": "adj",
            "lines": [{"description": "x", "quantity": "1", "unit_price": "100", "vat_rate": "0.05"}]}).json()
        expd = client.post("/api/expenses", json={"date": "2070-01-08", "category": "Misc",
            "expense_account_id": exp_acc, "payment_account_id": acc["1020"], "paid_directly": True,
            "net_amount": "300", "vat_rate": "0.05"}).json()
        je = client.post("/api/journal-entries", json={"date": "2070-01-09", "memo": "m",
            "lines": [{"account_id": acc["1010"], "debit": "50"}, {"account_id": acc["4010"], "credit": "50"}]}).json()

        for kind, did in [("invoice", inv["id"]), ("bill", bill["id"]), ("customer_cn", ccn["id"]),
                          ("expense", expd["id"]), ("journal", je["id"])]:
            r = client.get(f"/api/documents/{kind}/{did}/excel")
            assert r.status_code == 200 and r.content[:2] == b"PK", kind


def test_export_log_records_activity():
    with TestClient(app) as client:
        # A report export and a document export should both be logged.
        client.get("/api/export/trial-balance?format=xlsx")
        acc = _accounts(client)
        je = client.post("/api/journal-entries", json={"date": "2070-02-01", "memo": "log",
            "lines": [{"account_id": acc["1010"], "debit": "10"}, {"account_id": acc["4010"], "credit": "10"}]}).json()
        client.get(f"/api/documents/journal/{je['id']}/excel")
        log = client.get("/api/export-log").json()
        refs = {r["ref"] for r in log}
        assert "trial-balance" in refs
        assert any(r["ref"].startswith("journal/") for r in log)
        assert any(r["fmt"] == "xlsx" for r in log)
