"""Document layout templates: CRUD, default selection, live preview, and that a template's
section toggles / bank details / footer actually change the generated invoice PDF. 2041 dates."""

from __future__ import annotations

import io

from fastapi.testclient import TestClient

from app.main import app


def _text(b: bytes) -> str:
    import pdfplumber
    with pdfplumber.open(io.BytesIO(b)) as pdf:
        return "\n".join((p.extract_text() or "") for p in pdf.pages)


def _invoice(client):
    cid = client.post("/api/sales/customers", json={"name": "Tmpl Cust"}).json()["id"]
    return client.post("/api/sales/invoices", json={
        "customer_id": cid, "date": "2041-01-01",
        "lines": [{"description": "Work", "quantity": "1", "unit_price": "1000", "vat_rate": "0.05"}]}).json()


def test_template_crud_and_validation():
    with TestClient(app) as client:
        r = client.post("/api/document-templates", json={
            "name": "Bank layout", "doc_type": "invoice", "bank_details": "IBAN AE-TEST-123",
            "footer_notes": "Payment within 30 days.", "sections": {"totals": True}})
        assert r.status_code == 200, r.text
        tid = r.json()["id"]
        assert any(t["id"] == tid for t in client.get("/api/document-templates").json())
        # bad page size rejected
        assert client.post("/api/document-templates", json={"name": "x", "page_size": "A2"}).status_code == 400
        # delete
        assert client.delete(f"/api/document-templates/{tid}").json()["ok"] is True


def test_preview_renders_pdf():
    with TestClient(app) as client:
        p = client.post("/api/document-templates/preview", json={
            "name": "P", "doc_type": "invoice", "bank_details": "IBAN PREVIEW"})
        assert p.status_code == 200 and p.content[:4] == b"%PDF"
        assert "IBAN PREVIEW" in _text(p.content)


def test_template_changes_invoice_pdf():
    with TestClient(app) as client:
        inv = _invoice(client)
        t = client.post("/api/document-templates", json={
            "name": "No totals + bank", "doc_type": "invoice",
            "sections": {"totals": False}, "bank_details": "IBAN AE99 BANKLINE",
            "footer_notes": "FOOTERMARK"}).json()
        pdf = client.get(f"/api/sales/invoices/{inv['id']}/pdf", params={"template_id": t["id"]})
        assert pdf.status_code == 200
        txt = _text(pdf.content)
        assert "IBAN AE99 BANKLINE" in txt and "FOOTERMARK" in txt
        assert "Balance due" not in txt          # totals section hidden by the template


def test_default_template_applied_without_id():
    with TestClient(app) as client:
        inv = _invoice(client)
        t = client.post("/api/document-templates", json={
            "name": "Default bank", "doc_type": "invoice", "is_default": True,
            "bank_details": "IBAN DEFAULT9"}).json()
        assert t["is_default"] is True
        pdf = client.get(f"/api/sales/invoices/{inv['id']}/pdf")   # no template_id → default
        assert "IBAN DEFAULT9" in _text(pdf.content)
        # clean up default so it doesn't affect other suites' PDFs
        client.delete(f"/api/document-templates/{t['id']}")
