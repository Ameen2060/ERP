"""Journal-entry & expense PDFs, and the centralized Archive-to-attachments engine. 2049 dates."""

from __future__ import annotations

import io

from fastapi.testclient import TestClient

from app.main import app


def _text(b: bytes) -> str:
    import pdfplumber
    with pdfplumber.open(io.BytesIO(b)) as pdf:
        return "\n".join((p.extract_text() or "") for p in pdf.pages)


def _accts(client):
    return [a["id"] for a in client.get("/api/accounts").json() if not a["is_group"]]


def test_journal_entry_pdf_shows_balanced():
    with TestClient(app) as client:
        a, b = _accts(client)[:2]
        je = client.post("/api/journal-entries", json={
            "date": "2049-01-01", "memo": "PDF JE", "lines": [
                {"account_id": a, "debit": "500"}, {"account_id": b, "credit": "500"}]}).json()
        r = client.get(f"/api/journal-entries/{je['id']}/pdf")
        assert r.status_code == 200 and r.content[:4] == b"%PDF"
        t = _text(r.content)
        assert "JOURNAL ENTRY" in t and "BALANCED" in t


def test_expense_pdf_and_archive_to_attachments():
    with TestClient(app) as client:
        accs = {x["code"]: x["id"] for x in client.get("/api/accounts").json()}
        exp = client.post("/api/expenses", json={
            "date": "2049-02-01", "category": "Travel", "expense_account_id": accs["5090"],
            "payment_account_id": accs["1020"], "paid_directly": True,
            "net_amount": "300", "vat_rate": "0.05"}).json()
        pdf = client.get(f"/api/expenses/{exp['id']}/pdf")
        assert pdf.status_code == 200 and "EXPENSE VOUCHER" in _text(pdf.content)
        # archive the PDF → it lands in the expense's attachments with history
        arch = client.post(f"/api/documents/expense/{exp['id']}/archive-pdf")
        assert arch.status_code == 200, arch.text
        att = arch.json()
        assert att["entity_type"] == "expense" and att["file_ext"] == "pdf"
        lst = client.get("/api/attachments", params={"entity_type": "expense", "entity_id": exp["id"]}).json()
        assert any(x["id"] == att["id"] for x in lst)
        # history/events recorded
        ev = client.get(f"/api/attachments/{att['id']}/events").json()
        assert any(e["action"] == "uploaded" for e in ev)


def test_archive_unknown_kind_rejected():
    with TestClient(app) as client:
        assert client.post("/api/documents/nope/x/archive-pdf").status_code == 400
