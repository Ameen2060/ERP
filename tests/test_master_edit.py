"""Edit Product & Employee masters with field-level audit, uniqueness guards, and preservation
of historical transactions (stock movements / posted payslips). 2056 dates."""

from __future__ import annotations

import secrets

from fastapi.testclient import TestClient

from app.main import app


def _uniq(p: str) -> str:
    return f"{p}-{secrets.token_hex(3)}"


def test_edit_product_locks_cost_method_after_movements_and_audits():
    with TestClient(app) as client:
        sku = _uniq("SKU")
        wid = client.post("/api/inventory/warehouses", json={"name": _uniq("WH")}).json()["id"]
        p = client.post("/api/inventory/products", json={
            "sku": sku, "name": "Widget", "cost_method": "weighted_average",
            "sales_price": "100", "purchase_cost": "60", "reorder_level": "5"}).json()
        pid = p["id"]

        # Edit descriptive/pricing fields.
        up = client.put(f"/api/inventory/products/{pid}", json={
            "sku": sku, "name": "Widget Pro", "cost_method": "weighted_average",
            "sales_price": "120", "purchase_cost": "70", "reorder_level": "10",
            "track_inventory": True, "is_active": True})
        assert up.status_code == 200, up.text
        assert up.json()["name"] == "Widget Pro" and up.json()["sales_price"] == "120.00"

        # Audit captured the changes.
        changed = {c["field"] for a in client.get(f"/api/inventory/products/{pid}/audit").json()
                   if a["action"] == "update" for c in a["changes"]}
        assert {"name", "sales_price", "reorder_level"} <= changed

        # Record a stock movement, then costing-method change must be rejected.
        client.post("/api/inventory/movements", json={
            "product_id": pid, "warehouse_id": wid, "movement_type": "receipt",
            "date": "2056-01-05", "quantity": "10", "unit_cost": "70"})
        locked = client.put(f"/api/inventory/products/{pid}", json={
            "sku": sku, "name": "Widget Pro", "cost_method": "fifo",
            "sales_price": "120", "purchase_cost": "70", "reorder_level": "10"})
        assert locked.status_code == 400
        assert "method" in locked.text.lower()


def test_edit_employee_preserves_posted_payslip_and_audits():
    with TestClient(app) as client:
        code = _uniq("EMP")
        e = client.post("/api/payroll/employees", json={
            "code": code, "name": "Ali", "basic_salary": "10000",
            "housing_allowance": "2000", "join_date": "2056-01-01"}).json()
        eid = e["id"]

        # Run + post payroll → payslip freezes the salary at that time.
        run = client.post("/api/payroll/runs", json={
            "period_label": "2056-02", "pay_date": "2056-02-28", "accrue_eosb": False}).json()
        client.post(f"/api/payroll/runs/{run['id']}/post")
        gross_before = run["gross_total"]

        # Edit the employee's salary structure afterward.
        up = client.put(f"/api/payroll/employees/{eid}", json={
            "code": code, "name": "Ali Hassan", "basic_salary": "12000",
            "housing_allowance": "2500", "join_date": "2056-01-01", "is_active": True})
        assert up.status_code == 200, up.text
        assert up.json()["name"] == "Ali Hassan"

        # The already-posted run total is unchanged (historical payroll preserved).
        assert client.get(f"/api/payroll/runs/{run['id']}").json()["gross_total"] == gross_before

        # Audit recorded the field changes.
        changed = {c["field"] for a in client.get(f"/api/payroll/employees/{eid}/audit").json()
                   if a["action"] == "update" for c in a["changes"]}
        assert {"name", "basic_salary"} <= changed
