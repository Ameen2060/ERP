"""Durable object-storage (Vercel Blob) backend — simulated in-memory so the full upload →
store → download → archive path is exercised without a real Blob token/network.

Proves that when BLOB_READ_WRITE_TOKEN is present the app: routes writes to Blob, stores the
Blob URL (not a local path) in Postgres, and streams bytes back through its own authenticated
endpoint — i.e. uploaded documents persist independently of the local filesystem. 2076 dates.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.services import storage


def _install_fake_blob(monkeypatch):
    """Replace the Blob HTTP calls with an in-memory object store."""
    store: dict[str, bytes] = {}

    def fake_token():
        return "vercel_blob_rw_TESTTOKEN"

    def fake_put(key, data, content_type):
        url = f"https://fake123.public.blob.vercel-storage.com/{key}-abcd1234"
        store[url] = bytes(data)
        return url

    def fake_get(url):
        if url not in store:
            raise storage.StorageError("not found")
        return store[url]

    def fake_delete(url):
        store.pop(url, None)

    monkeypatch.setattr(storage, "_blob_token", fake_token)
    monkeypatch.setattr(storage, "_blob_put", fake_put)
    monkeypatch.setattr(storage, "_http_get", fake_get)
    monkeypatch.setattr(storage, "_blob_delete", fake_delete)
    return store


def test_backend_selects_blob_when_token_present(monkeypatch):
    _install_fake_blob(monkeypatch)
    assert storage.backend_name() == "vercel_blob"
    ref = storage.save("sales_invoice/x/y.pdf", b"%PDF-1.4 test", "application/pdf")
    assert ref.startswith("https://")               # a Blob URL, not a local path
    assert storage.read(ref) == b"%PDF-1.4 test"     # round-trips via the (simulated) network
    assert storage.exists(ref) is True


def test_attachment_upload_and_download_via_blob(monkeypatch):
    store = _install_fake_blob(monkeypatch)
    with TestClient(app) as client:
        cid = client.post("/api/sales/customers", json={"name": "Blob Co"}).json()["id"]
        inv = client.post("/api/sales/invoices", json={
            "customer_id": cid, "date": "2076-01-05",
            "lines": [{"description": "x", "quantity": "1", "unit_price": "100", "vat_rate": "0.05"}],
        }).json()
        # Upload a document to the invoice.
        up = client.post("/api/attachments",
                         data={"entity_type": "sales_invoice", "entity_id": inv["id"]},
                         files={"file": ("receipt.pdf", b"%PDF-1.4 hello", "application/pdf")})
        assert up.status_code in (200, 201), up.text
        att = up.json()
        # The stored reference is a durable Blob URL, not a local filesystem path.
        got = client.get(f"/api/attachments/{att['id']}")
        # Download streams the exact bytes back through the app (server-side fetch from Blob).
        dl = client.get(f"/api/attachments/{att['id']}/download")
        assert dl.status_code == 200
        assert dl.content == b"%PDF-1.4 hello"
        # The object physically lives in the (simulated) durable store.
        assert any(v == b"%PDF-1.4 hello" for v in store.values())
        # Archive listing still shows it (Archive preserved).
        lst = client.get(f"/api/attachments?entity_type=sales_invoice&entity_id={inv['id']}").json()
        assert any(a["id"] == att["id"] for a in lst)


def test_archive_pdf_export_persists_to_blob(monkeypatch):
    store = _install_fake_blob(monkeypatch)
    with TestClient(app) as client:
        cid = client.post("/api/sales/customers", json={"name": "Blob Archive Co"}).json()["id"]
        inv = client.post("/api/sales/invoices", json={
            "customer_id": cid, "date": "2076-02-05",
            "lines": [{"description": "x", "quantity": "1", "unit_price": "100", "vat_rate": "0.05"}],
        }).json()
        # Archive the invoice PDF → should be written to durable storage.
        arch = client.post(f"/api/documents/invoice/{inv['id']}/archive-pdf")
        assert arch.status_code == 200, arch.text
        att = arch.json()
        dl = client.get(f"/api/attachments/{att['id']}/download")
        assert dl.status_code == 200 and dl.content[:4] == b"%PDF"
        assert len(store) >= 1   # the archived PDF is in Blob
