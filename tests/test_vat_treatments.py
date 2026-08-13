"""UAE VAT Treatment master: seeded standard treatments, compute (exclusive/inclusive),
extensibility (add a custom treatment), treatment stored on invoice lines, and the
VAT-by-treatment report. 2048 dates."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_seeded_standard_treatments():
    with TestClient(app) as client:
        codes = {t["code"]: t for t in client.get("/api/vat-treatments").json()}
        for c in ("SR", "ZR", "EX", "OS", "RC"):
            assert c in codes
        assert codes["SR"]["rate"] == "0.0500" and codes["SR"]["kind"] == "standard"
        assert codes["ZR"]["kind"] == "zero" and codes["EX"]["kind"] == "exempt"
        assert codes["OS"]["kind"] == "out_of_scope" and codes["RC"]["kind"] == "reverse_charge"
        assert codes["EX"]["taxable"] is False and codes["OS"]["taxable"] is False


def test_compute_exclusive_and_inclusive():
    with TestClient(app) as client:
        ex = client.post("/api/vat-treatments/compute", json={"code": "SR", "amount": "1000"}).json()
        assert ex["net"] == "1000.00" and ex["vat"] == "50.00" and ex["gross"] == "1050.00"
        inc = client.post("/api/vat-treatments/compute", json={"code": "SR", "amount": "1050", "inclusive": True}).json()
        assert inc["net"] == "1000.00" and inc["vat"] == "50.00"
        rc = client.post("/api/vat-treatments/compute", json={"code": "RC", "amount": "1000"}).json()
        assert rc["reverse_charge"] is True


def test_create_custom_treatment_and_validation():
    with TestClient(app) as client:
        r = client.post("/api/vat-treatments", json={"code": "TR5", "name": "Tourism 5%",
                                                     "kind": "standard", "rate": "0.05"})
        assert r.status_code == 200, r.text
        assert client.post("/api/vat-treatments", json={"code": "TR5", "name": "dup", "rate": "0.05"}).status_code == 400
        assert client.post("/api/vat-treatments", json={"code": "BAD", "name": "x", "rate": "2"}).status_code == 400


def test_treatment_stored_on_invoice_and_report():
    with TestClient(app) as client:
        cid = client.post("/api/sales/customers", json={"name": "VT Cust"}).json()["id"]
        inv = client.post("/api/sales/invoices", json={
            "customer_id": cid, "date": "2048-01-01",
            "lines": [{"description": "std", "quantity": "1", "unit_price": "1000", "vat_rate": "0.05"},
                      {"description": "zero", "quantity": "1", "unit_price": "500", "vat_rate": "0",
                       "vat_treatment": "ZR"}]}).json()
        tr = {l["description"]: l["vat_treatment"] for l in inv["lines"]}
        assert tr["std"] == "SR" and tr["zero"] == "ZR"          # derived + explicit
        rep = client.get("/api/reports/vat-by-treatment", params={"start": "2048-01-01", "end": "2048-12-31"}).json()
        byc = {r["code"]: r for r in rep["rows"]}
        assert byc["SR"]["output_vat"] >= 50 and "ZR" in byc
