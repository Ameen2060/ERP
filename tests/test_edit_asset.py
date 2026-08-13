"""Edit a fixed asset's acquisition via reverse-and-repost: recalc, audit, and the guard that
blocks editing once depreciation has been booked. 2062 dates."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app


def _acc(client):
    return {a["code"]: a["id"] for a in client.get("/api/accounts").json()}


def test_edit_asset_reposts_acquisition_and_audits():
    with TestClient(app) as client:
        a = client.post("/api/assets", json={
            "name": "Laptop", "category": "IT", "purchase_date": "2062-01-05",
            "purchase_cost": "5000", "vat_amount": "250", "useful_life_months": 36,
            "auto_post_acquisition": True, "acquisition_credit_code": "1020"}).json()
        aid = a["id"]
        acq_je = a["transactions"][0]["journal_entry_id"]
        assert acq_je

        up = client.put(f"/api/assets/{aid}?reason=Correct+cost", json={
            "name": "Laptop Pro", "category": "IT", "purchase_date": "2062-01-05",
            "purchase_cost": "6000", "vat_amount": "300", "useful_life_months": 36,
            "auto_post_acquisition": True, "acquisition_credit_code": "1020"})
        assert up.status_code == 200, up.text
        b = up.json()
        assert b["id"] == aid and b["asset_code"] == a["asset_code"]      # identity preserved
        assert Decimal(b["purchase_cost"]) == Decimal("6000.00")
        assert Decimal(b["total_cost"]) == Decimal("6300.00")            # recalc net+VAT
        # Original acquisition JE reversed, a fresh one posted.
        assert client.get(f"/api/journal-entries/{acq_je}").json()["status"] == "void"
        new_je = b["transactions"][0]["journal_entry_id"]
        assert new_je and new_je != acq_je
        assert Decimal(client.get(f"/api/journal-entries/{new_je}").json()["total"]) == Decimal("6000.00")

        aud = client.get(f"/api/assets/{aid}/audit").json()
        assert aud[0]["reason"] == "Correct cost"
        assert {"purchase_cost", "name"} <= {c["field"] for c in aud[0]["changes"]}


def test_cannot_edit_asset_after_depreciation():
    with TestClient(app) as client:
        a = client.post("/api/assets", json={
            "name": "Machine", "category": "Plant", "purchase_date": "2062-02-01",
            "in_service_date": "2062-02-01", "purchase_cost": "3600", "useful_life_months": 36,
            "auto_post_acquisition": True, "acquisition_credit_code": "1020"}).json()
        # Book depreciation up to a later date.
        client.post("/api/assets/depreciation/run", json={"as_of": "2062-05-31"})
        blocked = client.put(f"/api/assets/{a['id']}?reason=x", json={
            "name": "Machine", "category": "Plant", "purchase_date": "2062-02-01",
            "purchase_cost": "4000", "useful_life_months": 36})
        assert blocked.status_code == 400 and "depreciation" in blocked.text.lower()
