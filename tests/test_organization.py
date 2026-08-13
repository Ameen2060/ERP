"""Organization profile + VAT configuration: defaults, validation (TRN, financial year,
VAT frequency), persistence, and logo upload/serve/remove."""

from __future__ import annotations

import io

from fastapi.testclient import TestClient

from app.main import app


def _base(**over):
    d = {"name": "Acme Trading LLC", "trn": "100200300400003", "vat_registered": True,
         "vat_return_frequency": "quarterly", "financial_year_start": "2026-01-01",
         "financial_year_end": "2026-12-31", "address": "Dubai, UAE", "base_currency": "AED"}
    d.update(over)
    return d


def test_default_profile_exists():
    with TestClient(app) as client:
        p = client.get("/api/organization").json()
        assert "name" in p and "vat_return_frequency" in p and p["vat_registered"] in (True, False)


def test_update_and_persist():
    with TestClient(app) as client:
        r = client.put("/api/organization", json=_base())
        assert r.status_code == 200, r.text
        p = r.json()
        assert p["name"] == "Acme Trading LLC" and p["trn"] == "100200300400003"
        assert p["vat_return_frequency"] == "quarterly"
        # persisted on re-fetch
        assert client.get("/api/organization").json()["name"] == "Acme Trading LLC"


def test_trn_must_be_15_digits():
    with TestClient(app) as client:
        assert client.put("/api/organization", json=_base(trn="123")).status_code == 400
        assert client.put("/api/organization", json=_base(trn="12345678901234X")).status_code == 400


def test_financial_year_order():
    with TestClient(app) as client:
        bad = client.put("/api/organization", json=_base(financial_year_start="2026-12-31",
                                                          financial_year_end="2026-01-01"))
        assert bad.status_code == 400


def test_vat_registered_requires_frequency_and_trn():
    with TestClient(app) as client:
        assert client.put("/api/organization", json=_base(vat_return_frequency="na")).status_code == 400
        assert client.put("/api/organization", json=_base(trn=None)).status_code == 400
        # not registered → frequency forced to na, no TRN needed
        ok = client.put("/api/organization", json=_base(vat_registered=False, vat_return_frequency="na", trn=None))
        assert ok.status_code == 200 and ok.json()["vat_return_frequency"] == "na"


def test_name_required():
    with TestClient(app) as client:
        assert client.put("/api/organization", json=_base(name="   ")).status_code == 400


def test_logo_upload_serve_remove():
    with TestClient(app) as client:
        client.put("/api/organization", json=_base())
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
        up = client.post("/api/organization/logo",
                         files={"file": ("logo.png", io.BytesIO(png), "image/png")})
        assert up.status_code == 200 and up.json()["has_logo"] is True
        got = client.get("/api/organization/logo")
        assert got.status_code == 200 and got.content[:4] == b"\x89PNG"
        # bad type rejected
        bad = client.post("/api/organization/logo",
                          files={"file": ("logo.txt", io.BytesIO(b"x"), "text/plain")})
        assert bad.status_code == 400
        rm = client.delete("/api/organization/logo")
        assert rm.status_code == 200 and rm.json()["has_logo"] is False
