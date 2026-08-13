"""Multi-currency: currencies + rates, foreign-currency invoices posting base (AED) amounts,
and realized FX gain/loss on payment at a different rate."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app


def D(x) -> Decimal:
    return Decimal(str(x))


def test_base_currency_seeded_and_add_currency_with_rate():
    with TestClient(app) as client:
        codes = {c["code"] for c in client.get("/api/currencies").json()}
        assert "AED" in codes  # base seeded on startup
        assert client.post("/api/currencies", json={"code": "USD", "name": "US Dollar", "symbol": "$"}).status_code == 200
        r = client.post("/api/currencies/rates", json={"currency_code": "USD", "date": "2028-01-01", "rate": "3.60"})
        assert r.status_code == 200 and D(r.json()["rate"]) == Decimal("3.60")
        # Base currency rejects a rate.
        assert client.post("/api/currencies/rates", json={"currency_code": "AED", "date": "2028-01-01", "rate": "2"}).status_code == 400


def test_foreign_invoice_posts_base_amounts():
    with TestClient(app) as client:
        client.post("/api/currencies", json={"code": "USD", "name": "US Dollar"})
        cid = client.post("/api/sales/customers", json={"name": "USD Cust"}).json()["id"]
        inv = client.post("/api/sales/invoices", json={
            "customer_id": cid, "date": "2028-03-01", "currency": "USD", "exchange_rate": "3.60",
            "lines": [{"description": "Svc", "quantity": "1", "unit_price": "1000", "vat_rate": "0.05"}]}).json()
        assert inv["currency"] == "USD"
        assert D(inv["grand_total"]) == Decimal("1050.00")          # in USD
        assert D(inv["exchange_rate"]) == Decimal("3.60")
        assert D(inv["base_grand_total"]) == Decimal("3780.00")     # AED
        # GL posts base AED amounts.
        je = client.get(f"/api/journal-entries/{inv['journal_entry_id']}").json()
        by = {ln["account_code"]: ln for ln in je["lines"]}
        assert D(by["1100"]["debit"]) == Decimal("3780.00")   # AR (AED)
        assert D(by["4010"]["credit"]) == Decimal("3600.00")  # Sales (AED)
        assert D(by["2100"]["credit"]) == Decimal("180.00")   # VAT (AED)


def test_realized_fx_gain_on_payment():
    with TestClient(app) as client:
        client.post("/api/currencies", json={"code": "USD", "name": "US Dollar"})
        cid = client.post("/api/sales/customers", json={"name": "USD Cust 2"}).json()["id"]
        inv = client.post("/api/sales/invoices", json={
            "customer_id": cid, "date": "2028-03-01", "currency": "USD", "exchange_rate": "3.60",
            "lines": [{"description": "Svc", "quantity": "1", "unit_price": "1000", "vat_rate": "0.05"}]}).json()
        # Pay the full 1050 USD when the rate has risen to 3.70 → realized FX gain 105 AED.
        p = client.post("/api/sales/payments", json={
            "invoice_id": inv["id"], "date": "2028-04-01", "amount": "1050", "exchange_rate": "3.70"})
        assert p.status_code == 200, p.text
        got = client.get(f"/api/sales/invoices/{inv['id']}").json()
        assert got["status"] == "paid"
        je = client.get(f"/api/journal-entries/{p.json()['journal_entry_id']}").json()
        by = {ln["account_code"]: ln for ln in je["lines"]}
        assert D(by["1020"]["debit"]) == Decimal("3885.00")   # Bank received (1050 * 3.70)
        assert D(by["1100"]["credit"]) == Decimal("3780.00")  # AR relieved at invoice rate (1050 * 3.60)
        assert D(by["4085"]["credit"]) == Decimal("105.00")   # realized FX gain


def test_rate_lookup_when_not_supplied():
    with TestClient(app) as client:
        client.post("/api/currencies", json={"code": "EUR", "name": "Euro"})
        client.post("/api/currencies/rates", json={"currency_code": "EUR", "date": "2028-01-01", "rate": "4.00"})
        cid = client.post("/api/sales/customers", json={"name": "EUR Cust"}).json()["id"]
        # No exchange_rate supplied → uses the latest rate on/before the invoice date.
        inv = client.post("/api/sales/invoices", json={
            "customer_id": cid, "date": "2028-06-01", "currency": "EUR",
            "lines": [{"description": "X", "quantity": "1", "unit_price": "100", "vat_rate": "0"}]}).json()
        assert D(inv["exchange_rate"]) == Decimal("4.00")
        assert D(inv["base_grand_total"]) == Decimal("400.00")
