"""Global search across entities + project portfolio report/export. 2067 dates."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app


def test_global_search_finds_entities():
    with TestClient(app) as client:
        cid = client.post("/api/sales/customers", json={"name": "Searchable Widgets LLC"}).json()["id"]
        inv = client.post("/api/sales/invoices", json={
            "customer_id": cid, "date": "2067-01-05",
            "lines": [{"description": "x", "quantity": "1", "unit_price": "1000", "vat_rate": "0.05"}],
        }).json()

        # Find the customer by name fragment.
        d = client.get("/api/search?q=Searchable").json()
        cust_grp = next(g for g in d["groups"] if g["type"] == "customer")
        hit = next(h for h in cust_grp["hits"] if h["id"] == cid)
        assert hit["open"]["fn"] == "openCustomer" and hit["open"]["args"] == [cid]

        # Find the invoice by its number.
        d2 = client.get(f"/api/search?q={inv['number']}").json()
        inv_grp = next(g for g in d2["groups"] if g["type"] == "invoice")
        assert any(h["id"] == inv["id"] and h["open"]["fn"] == "openInv" for h in inv_grp["hits"])

        # Too-short query returns nothing.
        assert client.get("/api/search?q=a").json()["groups"] == []


def test_project_portfolio_report_and_export():
    with TestClient(app) as client:
        code = "PORT-1"
        client.post("/api/projects", json={"code": code, "name": "Portfolio Test", "contract_value": "100000"})
        cid = client.post("/api/sales/customers", json={"name": "Port Cust"}).json()["id"]
        client.post("/api/sales/invoices", json={
            "customer_id": cid, "date": "2067-02-01", "project": code,
            "lines": [{"description": "svc", "quantity": "1", "unit_price": "5000", "vat_rate": "0.05"}],
        })
        rep = client.get("/api/reports/projects").json()
        row = next(r for r in rep["rows"] if r["code"] == code)
        assert Decimal(str(row["revenue"])) == Decimal("5000")
        assert Decimal(str(row["contract_value"])) == Decimal("100000")
        # Export works.
        x = client.get("/api/export/projects?format=xlsx")
        assert x.status_code == 200 and x.content[:2] == b"PK"
