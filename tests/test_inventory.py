"""Inventory: weighted-average and FIFO issue costing, GL postings (Dr COGS / Cr Inventory),
valuation reconciling to the GL Inventory account, low-stock alerts, and negative-stock guard."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app


def D(x) -> Decimal:
    return Decimal(str(x))


def _wh(client) -> str:
    return client.post("/api/inventory/warehouses", json={"name": "Main WH"}).json()["id"]


def _product(client, method="weighted_average", **kw) -> str:
    return client.post("/api/inventory/products", json={"name": "Widget", "cost_method": method, **kw}).json()["id"]


def _move(client, pid, wh, mtype, qty, **kw):
    return client.post("/api/inventory/movements", json={
        "product_id": pid, "warehouse_id": wh, "date": "2025-01-10", "movement_type": mtype,
        "quantity": str(qty), **kw})


def test_weighted_average_costing_and_gl():
    with TestClient(app) as client:
        wh = _wh(client)
        pid = _product(client, "weighted_average")
        # Receive 10 @ 10 and 10 @ 20 → avg 15.
        _move(client, pid, wh, "receipt", 10, unit_cost="10")
        _move(client, pid, wh, "receipt", 10, unit_cost="20")
        # Issue 5 → cost 5 * 15 = 75.
        issue = _move(client, pid, wh, "issue", 5)
        assert issue.status_code == 200, issue.text
        assert D(issue.json()["total_cost"]) == Decimal("-75.00")
        # GL: Dr COGS 75 / Cr Inventory 75.
        je = client.get(f"/api/journal-entries/{issue.json()['journal_entry_id']}").json()
        by = {ln["account_code"]: ln for ln in je["lines"]}
        assert D(by["5060"]["debit"]) == Decimal("75.00")
        assert D(by["1200"]["credit"]) == Decimal("75.00")
        # On hand 15 @ avg 15 → value 225.
        p = client.get(f"/api/inventory/products/{pid}").json()
        assert D(p["on_hand"]) == Decimal("15.0000")
        assert D(p["stock_value"]) == Decimal("225.00")
        assert D(p["avg_cost"]) == Decimal("15.00")


def test_fifo_costing():
    with TestClient(app) as client:
        wh = _wh(client)
        pid = _product(client, "fifo")
        _move(client, pid, wh, "receipt", 10, unit_cost="10")  # layer 1
        _move(client, pid, wh, "receipt", 10, unit_cost="20")  # layer 2
        # Issue 15 → 10@10 + 5@20 = 100 + 100 = 200.
        issue = _move(client, pid, wh, "issue", 15)
        assert D(issue.json()["total_cost"]) == Decimal("-200.00")
        p = client.get(f"/api/inventory/products/{pid}").json()
        assert D(p["on_hand"]) == Decimal("5.0000")     # 5 left from layer 2
        assert D(p["stock_value"]) == Decimal("100.00")  # 5 @ 20


def test_valuation_reconciles_to_gl_and_negative_guard():
    with TestClient(app) as client:
        wh = _wh(client)
        pid = _product(client, "weighted_average")
        _move(client, pid, wh, "receipt", 100, unit_cost="5")
        val = client.get("/api/inventory/valuation").json()
        assert val["in_sync"] is True
        assert D(val["total_value"]) == D(val["gl_inventory_balance"])
        # Cannot issue more than on hand.
        bad = _move(client, pid, wh, "issue", 200)
        assert bad.status_code == 400 and "on hand" in bad.json()["detail"]


def test_low_stock_alert_and_service_guard():
    with TestClient(app) as client:
        wh = _wh(client)
        pid = _product(client, "weighted_average", reorder_level="20")
        _move(client, pid, wh, "receipt", 15, unit_cost="5")  # below reorder 20
        low = client.get("/api/inventory/low-stock").json()
        row = next(r for r in low if r["product_id"] == pid)
        assert D(row["shortfall"]) == Decimal("5.0000")
        # A service item has no stock movements.
        svc = _product(client, "weighted_average", name="Consulting", type="service")
        r = _move(client, svc, wh, "receipt", 1, unit_cost="100")
        assert r.status_code == 400
