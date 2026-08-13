"""Corporate Tax 'Provisional → Requires SME Validation' workflow.

Covers the full lifecycle, the filing-block, sign-off gating, rejection, and that reopening
a validated computation invalidates the prior validation. Uses a far-future period so the
shared test DB doesn't leak other tests' postings into the computation."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

PERIOD = {"period_start": "2035-01-01", "period_end": "2035-12-31"}


def _create(client) -> dict:
    r = client.post("/api/ct/reviews", json={**PERIOD, "prepared_by": "prep"})
    assert r.status_code == 200, r.text
    return r.json()


def _sign_all(client, review: dict) -> dict:
    out = review
    for it in review["items"]:
        if it["requires_signoff"] and not it["signed_off"]:
            r = client.post(f"/api/ct/reviews/{review['id']}/items/{it['id']}/signoff",
                            json={"signed_off": True, "note": "ok"})
            assert r.status_code == 200, r.text
            out = r.json()
    return out


def test_create_starts_draft_and_blocks_filing():
    with TestClient(app) as client:
        rv = _create(client)
        assert rv["status"] == "draft"
        assert rv["can_file"] is False
        assert "submit" in rv["allowed_actions"]
        # every non-rate line generated a sign-off item
        assert rv["signoff_total"] >= 5 and rv["signed_count"] == 0
        # audit trail opened
        assert any(e["action"] == "created" for e in rv["events"])


def test_full_lifecycle_to_validated():
    with TestClient(app) as client:
        rv = _create(client)
        rid = rv["id"]

        # draft → provisional
        rv = client.post(f"/api/ct/reviews/{rid}/submit", json={}).json()
        assert rv["status"] == "provisional"

        # can't mark reviewed until every required line is signed off
        early = client.post(f"/api/ct/reviews/{rid}/mark-reviewed", json={})
        assert early.status_code == 400

        rv = _sign_all(client, rv)
        assert rv["all_signed_off"] is True
        assert rv["signed_count"] == rv["signoff_total"]

        rv = client.post(f"/api/ct/reviews/{rid}/mark-reviewed", json={}).json()
        assert rv["status"] == "sme_reviewed"

        # validation requires a named SME
        no_name = client.post(f"/api/ct/reviews/{rid}/validate", json={})
        assert no_name.status_code == 400

        rv = client.post(f"/api/ct/reviews/{rid}/validate",
                         json={"sme_name": "Jane Tax FCA", "note": "reviewed in full"}).json()
        assert rv["status"] == "validated"
        assert rv["can_file"] is True
        assert rv["sme_name"] == "Jane Tax FCA"
        assert rv["validated_by"] and rv["validated_at"]
        assert any(e["action"] == "validated" for e in rv["events"])


def test_final_export_blocked_until_validated():
    with TestClient(app) as client:
        rid = _create(client)["id"]
        blocked = client.get(f"/api/ct/reviews/{rid}/export?format=pdf&final=true")
        assert blocked.status_code == 400
        # provisional (non-final) export is always allowed and is a PDF
        prov = client.get(f"/api/ct/reviews/{rid}/export?format=pdf")
        assert prov.status_code == 200
        assert prov.content[:4] == b"%PDF"


def test_reject_requires_reason():
    with TestClient(app) as client:
        rid = _create(client)["id"]
        client.post(f"/api/ct/reviews/{rid}/submit", json={})
        assert client.post(f"/api/ct/reviews/{rid}/reject", json={}).status_code == 400
        ok = client.post(f"/api/ct/reviews/{rid}/reject", json={"note": "add-backs missing"})
        assert ok.status_code == 200 and ok.json()["status"] == "rejected"


def test_reopen_validated_invalidates():
    with TestClient(app) as client:
        rv = _create(client)
        rid = rv["id"]
        client.post(f"/api/ct/reviews/{rid}/submit", json={})
        rv = _sign_all(client, client.get(f"/api/ct/reviews/{rid}").json())
        client.post(f"/api/ct/reviews/{rid}/mark-reviewed", json={})
        client.post(f"/api/ct/reviews/{rid}/validate", json={"sme_name": "SME"})

        rv = client.post(f"/api/ct/reviews/{rid}/reopen", json={"note": "figures changed"}).json()
        assert rv["status"] == "draft"
        assert rv["can_file"] is False
        assert rv["validated_by"] is None and rv["sme_name"] is None
        assert rv["signed_count"] == 0          # sign-offs cleared
        assert any(e["action"] == "reopened" for e in rv["events"])


def test_kpis_endpoint():
    with TestClient(app) as client:
        _create(client)
        k = client.get("/api/ct/reviews/kpis").json()
        assert k["total"] >= 1
        assert "by_status" in k and "validated_ct_payable" in k
