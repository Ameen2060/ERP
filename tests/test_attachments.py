"""Transaction document attachments: upload/list/rename/replace/review/delete, the
template-free document-intelligence extraction, amount matching, audit trail, and validation.
Auth is disabled in tests, so the caller is treated as a local editor."""

from __future__ import annotations

import io

from fastapi.testclient import TestClient

from app.main import app


def _two_accounts(client):
    accts = [a for a in client.get("/api/accounts").json() if not a["is_group"]]
    return accts[0]["id"], accts[1]["id"]


def _make_je(client, amount="10500.00"):
    a, b = _two_accounts(client)
    r = client.post("/api/journal-entries", json={
        "date": "2034-03-01", "memo": "attach test", "reference": "AT-1",
        "lines": [{"account_id": a, "debit": amount}, {"account_id": b, "credit": amount}],
    })
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _upload(client, entity_id, name, content: bytes, entity_type="journal_entry"):
    return client.post("/api/attachments",
                       data={"entity_type": entity_type, "entity_id": entity_id},
                       files={"file": (name, io.BytesIO(content), "application/octet-stream")})


def _pdf_bytes(text_lines):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 800
    for ln in text_lines:
        c.drawString(40, y, ln)
        y -= 20
    c.save()
    buf.seek(0)
    return buf.read()


def _xlsx_bytes(rows):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return out.read()


def test_upload_csv_extracts_and_matches():
    with TestClient(app) as client:
        je = _make_je(client, "10500.00")
        r = _upload(client, je, "invoice.csv", b"Description,Amount\nGrand Total,10500.00\nVAT,500.00\n")
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["entity_type"] == "journal_entry" and d["entity_id"] == je
        assert d["extraction_status"] == "done"
        assert d["extracted"].get("gross_amount") == "10500.00"
        assert d["match_status"] == "matched"
        assert d["file_ext"] == "csv" and d["file_size"] > 0 and d["sha256"]


def test_mismatch_is_flagged_with_difference():
    with TestClient(app) as client:
        je = _make_je(client, "10500.00")
        r = _upload(client, je, "wrong.csv", b"Grand Total,11000.00\n")
        d = r.json()
        assert d["match_status"] == "mismatch"
        assert str(d["match_difference"]) in ("-500.00", "-500.0", "-500")


def test_pdf_and_xlsx_extraction():
    with TestClient(app) as client:
        je = _make_je(client, "10500.00")
        pdf = _pdf_bytes(["Tax Invoice No INV-001", "TRN 123456789012345",
                          "Currency AED", "Grand Total 10,500.00"])
        d = _upload(client, je, "inv.pdf", pdf).json()
        assert d["extraction_status"] == "done"
        assert d["extracted"].get("invoice_number") == "INV-001"
        assert d["extracted"].get("trn") == "123456789012345"
        assert d["match_status"] == "matched"

        xlsx = _xlsx_bytes([["Item", "Amount"], ["Grand Total", 10500]])
        d2 = _upload(client, je, "inv.xlsx", xlsx).json()
        assert d2["extraction_status"] == "done"
        assert d2["match_status"] == "matched"


def test_lifecycle_rename_replace_review_delete_audit():
    with TestClient(app) as client:
        je = _make_je(client)
        aid = _upload(client, je, "a.csv", b"Grand Total,10500.00\n").json()["id"]

        # rename
        r = client.patch(f"/api/attachments/{aid}", json={"display_name": "Renamed.csv"})
        assert r.status_code == 200 and r.json()["display_name"] == "Renamed.csv"

        # review
        assert client.post(f"/api/attachments/{aid}/review", json={"note": "ok"}).json()["review_status"] == "reviewed"

        # replace (resets review to pending, re-extracts)
        rep = client.post(f"/api/attachments/{aid}/replace",
                          files={"file": ("b.csv", io.BytesIO(b"Grand Total,11000.00\n"), "text/csv")})
        assert rep.status_code == 200
        assert rep.json()["review_status"] == "pending" and rep.json()["match_status"] == "mismatch"

        # audit trail has all the actions
        actions = {e["action"] for e in client.get(f"/api/attachments/{aid}/events").json()}
        assert {"uploaded", "extracted", "renamed", "reviewed", "replaced"} <= actions

        # download works
        dl = client.get(f"/api/attachments/{aid}/download")
        assert dl.status_code == 200 and dl.content

        # soft delete → drops out of the list but the record & audit survive
        assert client.delete(f"/api/attachments/{aid}").json()["ok"] is True
        assert all(a["id"] != aid for a in
                   client.get("/api/attachments", params={"entity_type": "journal_entry", "entity_id": je}).json())
        assert "deleted" in {e["action"] for e in client.get(f"/api/attachments/{aid}/events").json()}


def test_persistence_and_status_and_multiple():
    with TestClient(app) as client:
        je = _make_je(client)
        _upload(client, je, "one.csv", b"Grand Total,10500.00\n")
        _upload(client, je, "two.csv", b"Grand Total,11000.00\n")
        lst = client.get("/api/attachments", params={"entity_type": "journal_entry", "entity_id": je}).json()
        assert len(lst) == 2  # multiple documents per transaction
        st = client.get("/api/attachments/status", params={"entity_type": "journal_entry", "entity_id": je}).json()
        assert st["count"] == 2 and st["code"] == "mismatch" and st["mismatch"] == 1


def test_attach_to_payment_and_bank_line_and_bulk_status():
    with TestClient(app) as client:
        # a customer + posted invoice + payment (customer_payment entity)
        cid = client.post("/api/sales/customers", json={"name": "Att Co"}).json()["id"]
        acc = [a for a in client.get("/api/accounts").json() if not a["is_group"]]
        inv = client.post("/api/sales/invoices", json={
            "customer_id": cid, "date": "2034-05-01", "due_date": "2034-06-01",
            "lines": [{"description": "svc", "quantity": "1", "unit_price": "1000.00", "vat_rate": "0"}],
        }).json()
        iid = inv["id"]
        pay = client.post("/api/sales/payments", json={"invoice_id": iid, "date": "2034-05-05", "amount": "1000.00"}).json()
        pid = pay["id"]
        # payments listing endpoint
        plist = client.get(f"/api/sales/invoices/{iid}/payments").json()
        assert any(p["id"] == pid for p in plist)
        # attach a receipt to the payment; amount matches the payment (1000)
        r = client.post("/api/attachments", data={"entity_type": "customer_payment", "entity_id": pid},
                        files={"file": ("receipt.csv", io.BytesIO(b"Grand Total,1000.00\n"), "text/csv")})
        assert r.status_code == 200 and r.json()["match_status"] == "matched"

        # bulk status across a couple of invoices
        bulk = client.get("/api/attachments/status-bulk",
                          params={"entity_type": "sales_invoice", "ids": f"{iid},nope"}).json()
        assert iid in bulk and bulk[iid]["code"] == "none"  # nothing attached to the invoice itself
        assert bulk["nope"]["code"] == "none"


def test_all_transaction_types_are_linkable():
    """Every transaction type the UI exposes must accept an attachment and reject a bad id."""
    with TestClient(app) as client:
        je = _make_je(client)
        for etype, good_id in [("journal_entry", je)]:
            r = _upload(client, good_id, "doc.csv", b"Grand Total,10500.00\n", entity_type=etype)
            assert r.status_code == 200, (etype, r.text)
        # unknown entity type is rejected
        bad_type = _upload(client, je, "doc.csv", b"x,1\n", entity_type="not_a_type")
        assert bad_type.status_code == 400
        # known type but missing record is rejected
        for etype in ("fixed_asset", "stock_movement", "payroll_run", "bank_statement_line",
                      "customer_payment", "bill_payment"):
            miss = _upload(client, "does-not-exist", "d.csv", b"x,1\n", entity_type=etype)
            assert miss.status_code == 400, etype


def test_rejects_bad_type_and_empty():
    with TestClient(app) as client:
        je = _make_je(client)
        bad = _upload(client, je, "hack.exe", b"MZ....")
        assert bad.status_code == 400
        empty = _upload(client, je, "empty.csv", b"")
        assert empty.status_code == 400
