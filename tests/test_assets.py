"""Fixed Assets: acquisition posting, straight-line depreciation run + GL entries, schedule,
and disposal with gain/loss."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app


def D(x) -> Decimal:
    return Decimal(str(x))


def _asset_payload(**kw) -> dict:
    return {
        "name": "Delivery Van", "category": "Vehicles", "purchase_date": "2025-01-15",
        "purchase_cost": "12000", "useful_life_months": 12, "method": "straight_line",
        "residual_value": "0", **kw,
    }


def test_create_asset_with_acquisition_posting():
    with TestClient(app) as client:
        a = client.post("/api/assets", json=_asset_payload(auto_post_acquisition=True, acquisition_credit_code="1020")).json()
        assert a["asset_code"].startswith("FA-")
        assert D(a["net_book_value"]) == Decimal("12000.00")
        assert D(a["total_cost"]) == Decimal("12000.00")
        acq = [t for t in a["transactions"] if t["type"] == "acquisition"]
        assert acq and acq[0]["journal_entry_id"]
        je = client.get(f"/api/journal-entries/{acq[0]['journal_entry_id']}").json()
        by_code = {ln["account_code"]: ln for ln in je["lines"]}
        assert D(by_code["1500"]["debit"]) == Decimal("12000.00")  # Fixed Assets
        assert D(by_code["1020"]["credit"]) == Decimal("12000.00")  # Bank


def test_depreciation_schedule_and_run():
    with TestClient(app) as client:
        a = client.post("/api/assets", json=_asset_payload()).json()
        sched = client.get(f"/api/assets/{a['id']}/schedule").json()
        assert len(sched) == 12
        assert D(sched[0]["depreciation"]) == Decimal("1000.00")
        assert D(sched[-1]["net_book_value"]) == Decimal("0.00")

        run = client.post("/api/assets/depreciation/run", json={"as_of": "2025-03-31"}).json()
        assert run["periods_posted"] >= 3  # this asset contributes 3 (Jan/Feb/Mar)
        got = client.get(f"/api/assets/{a['id']}").json()
        assert D(got["accumulated_depreciation"]) == Decimal("3000.00")
        assert D(got["net_book_value"]) == Decimal("9000.00")

        # Running again for the same period posts nothing new for this asset.
        got2 = client.get(f"/api/assets/{a['id']}").json()
        client.post("/api/assets/depreciation/run", json={"as_of": "2025-03-31"})
        assert D(client.get(f"/api/assets/{a['id']}").json()["accumulated_depreciation"]) == D(got2["accumulated_depreciation"])


def test_dispose_asset_with_gain():
    with TestClient(app) as client:
        a = client.post("/api/assets", json=_asset_payload()).json()
        client.post("/api/assets/depreciation/run", json={"as_of": "2025-03-31"})  # accum 3000, NBV 9000
        disp = client.post(f"/api/assets/{a['id']}/dispose", json={
            "disposal_date": "2025-04-01", "proceeds": "10000", "proceeds_account_code": "1020"}).json()
        assert disp["status"] == "disposed"
        assert D(disp["disposal_gain_loss"]) == Decimal("1000.00")  # 10000 - NBV 9000
        je_id = next(t["journal_entry_id"] for t in disp["transactions"] if t["type"] == "disposal")
        je = client.get(f"/api/journal-entries/{je_id}").json()
        by_code = {ln["account_code"]: ln for ln in je["lines"]}
        assert D(by_code["1500"]["credit"]) == Decimal("12000.00")   # remove cost
        assert D(by_code["1510"]["debit"]) == Decimal("3000.00")     # remove accum dep
        assert D(by_code["1020"]["debit"]) == Decimal("10000.00")    # proceeds
        assert D(by_code["4080"]["credit"]) == Decimal("1000.00")    # gain


def test_asset_register_and_dashboard():
    with TestClient(app) as client:
        client.post("/api/assets", json=_asset_payload(name="Laptop", category="IT", purchase_cost="6000"))
        reg = client.get("/api/assets/register").json()
        assert reg["count"] >= 1 and D(reg["total_cost"]) >= Decimal("6000.00")
        dash = client.get("/api/assets/dashboard").json()
        assert dash["asset_count"] >= 1
        assert "by_category" in dash and "net_book_value" in dash
