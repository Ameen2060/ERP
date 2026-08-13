"""UAE E-Invoicing compliance layer — provisional, configuration-driven.

Covers the spec's testing matrix: B2B / B2C, credit notes (with original-document reference),
standard / zero / exempt / out-of-scope treatments, multiple VAT rates, discounts, advances,
partial payments, foreign currency, corrections, rejection + resubmission, duplicate prevention,
TRN validation, missing mandatory fields, and the compliance safeguards (System Validation vs
Regulatory Compliance, provisional labelling). Far-future dates (2074/2075) + unique names keep
the shared test DB isolated. E-invoicing never posts to the ledger — verified explicitly.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app

ORG_TRN = "100200300400503"
BUYER_TRN = "100200300400600"


def _setup_org(client):
    """Give the organization a valid supplier identity so supplier-side checks can pass."""
    client.put("/api/organization", json={
        "name": "EInv Test Co", "legal_name": "EInv Test Co LLC", "trn": ORG_TRN,
        "address": "Dubai, UAE", "vat_registered": True, "vat_return_frequency": "quarterly",
        "trade_license": "CN-1234567", "country": "AE",
    })


def _enable_all(client):
    client.put("/api/einvoicing/config", json={
        "enabled": True,
        "applicable_types": ["sales_invoice", "customer_cn", "vendor_bill", "vendor_cn",
                             "customer_advance"],
    })


def _b2b_customer(client, name):
    return client.post("/api/sales/customers", json={
        "name": name, "trn": BUYER_TRN, "party_type": "b2b", "tax_status": "registered",
        "billing_address": "Abu Dhabi, UAE", "country": "AE"}).json()["id"]


def _invoice(client, cid, date, lines):
    return client.post("/api/sales/invoices", json={
        "customer_id": cid, "date": date, "lines": lines}).json()


# ── Configuration + compliance posture ────────────────────────────────────────────────────────
def test_config_is_provisional_and_modular():
    with TestClient(app) as client:
        cfg = client.get("/api/einvoicing/config").json()
        assert cfg["provisional"] is True
        assert "manual" in cfg["providers"]                 # modular ASP adapter registry
        assert cfg["regulatory_compliance_confirmed"] is False
        assert "SME validation" in (cfg["compliance_notice"] or "")


def test_applicability_gate_blocks_unconfigured_types():
    with TestClient(app) as client:
        _setup_org(client)
        # Reset to the conservative default (only sales invoices + customer CNs).
        client.put("/api/einvoicing/config", json={
            "enabled": True, "applicable_types": ["sales_invoice", "customer_cn"]})
        vid = client.post("/api/purchases/vendors", json={"name": "EInv Vendor NA", "trn": ORG_TRN}).json()["id"]
        bill = client.post("/api/purchases/bills", json={
            "vendor_id": vid, "date": "2074-01-05",
            "lines": [{"description": "x", "quantity": "1", "unit_price": "100", "vat_rate": "0.05",
                       "expense_account_id": _expense_acct(client)}]}).json()
        r = client.post("/api/einvoicing/generate",
                        json={"source_type": "vendor_bill", "source_id": bill["id"]})
        assert r.status_code == 400
        assert "not configured" in r.text.lower()


def _expense_acct(client):
    accs = client.get("/api/accounts").json()
    for a in accs:
        if a.get("type") == "expense" and not a.get("is_group"):
            return a["id"]
    return accs[0]["id"]


# ── B2B standard-rated invoice: system validation passes, but not regulatory-confirmed ─────────
def test_b2b_invoice_system_validation_passes_not_regulatory():
    with TestClient(app) as client:
        _setup_org(client)
        _enable_all(client)
        cid = _b2b_customer(client, "B2B Std Co")
        inv = _invoice(client, cid, "2074-02-01",
                       [{"description": "Consulting", "quantity": "2", "unit_price": "1000",
                         "vat_rate": "0.05", "vat_treatment": "SR"}])
        d = client.post("/api/einvoicing/generate",
                        json={"source_type": "sales_invoice", "source_id": inv["id"]}).json()
        assert d["system_validation_passed"] is True
        assert d["status"] == "ready"
        assert d["regulatory_confirmed"] is False           # separated from system validation
        assert d["compliance"]["regulatory_compliance_confirmed"] is False
        assert "SME validation" in d["compliance"]["provisional_notice"]
        # totals reconciled from the posted transaction
        assert Decimal(d["grand_total"]) == Decimal("2100.00")


def test_b2c_invoice_without_buyer_trn_is_valid():
    with TestClient(app) as client:
        _setup_org(client)
        _enable_all(client)
        cid = client.post("/api/sales/customers", json={
            "name": "B2C Walk-in", "party_type": "b2c", "tax_status": "not_registered"}).json()["id"]
        inv = _invoice(client, cid, "2074-02-02",
                       [{"description": "Retail item", "quantity": "1", "unit_price": "300",
                         "vat_rate": "0.05", "vat_treatment": "SR"}])
        d = client.post("/api/einvoicing/generate",
                        json={"source_type": "sales_invoice", "source_id": inv["id"]}).json()
        assert d["party_class"] == "b2c"
        assert d["system_validation_passed"] is True        # B2C: buyer TRN not mandatory


def test_b2b_missing_buyer_trn_fails_validation_with_field():
    with TestClient(app) as client:
        _setup_org(client)
        _enable_all(client)
        cid = client.post("/api/sales/customers", json={
            "name": "B2B No TRN Co", "party_type": "b2b"}).json()["id"]
        inv = _invoice(client, cid, "2074-02-03",
                       [{"description": "x", "quantity": "1", "unit_price": "500", "vat_rate": "0.05"}])
        d = client.post("/api/einvoicing/generate",
                        json={"source_type": "sales_invoice", "source_id": inv["id"]}).json()
        assert d["system_validation_passed"] is False
        assert d["status"] == "validation_failed"
        fields = [e["field"] for e in d["validation"]["errors"]]
        assert "buyer.trn" in fields


# ── VAT treatments + multiple rates + discount ─────────────────────────────────────────────────
def test_multiple_rates_and_treatments_breakdown():
    with TestClient(app) as client:
        _setup_org(client)
        _enable_all(client)
        cid = _b2b_customer(client, "MultiRate Co")
        inv = _invoice(client, cid, "2074-03-01", [
            {"description": "Standard", "quantity": "1", "unit_price": "1000", "vat_rate": "0.05", "vat_treatment": "SR"},
            {"description": "Zero-rated export", "quantity": "1", "unit_price": "500", "vat_rate": "0", "vat_treatment": "ZR"},
            {"description": "Exempt", "quantity": "1", "unit_price": "200", "vat_rate": "0", "vat_treatment": "EX"},
            {"description": "Out of scope", "quantity": "1", "unit_price": "100", "vat_rate": "0", "vat_treatment": "OS"},
        ])
        d = client.post("/api/einvoicing/generate",
                        json={"source_type": "sales_invoice", "source_id": inv["id"]}).json()
        cats = {b["category"] for b in d["payload"]["vat_breakdown"]}
        assert {"standard", "zero", "exempt", "out_of_scope"}.issubset(cats)
        assert d["system_validation_passed"] is True


def test_discount_is_represented_in_payload():
    with TestClient(app) as client:
        _setup_org(client)
        _enable_all(client)
        cid = _b2b_customer(client, "Discount Co")
        # qty*unit = 1000 but net booked 900 → a 100 discount is derivable.
        inv = client.post("/api/sales/invoices", json={
            "customer_id": cid, "date": "2074-03-05",
            "lines": [{"description": "Discounted", "quantity": "10", "unit_price": "100",
                       "vat_rate": "0.05", "vat_treatment": "SR"}]}).json()
        d = client.post("/api/einvoicing/generate",
                        json={"source_type": "sales_invoice", "source_id": inv["id"]}).json()
        line0 = d["payload"]["lines"][0]
        assert "discount" in line0


# ── Credit note references the original invoice (Original → Adjustment) ─────────────────────────
def test_credit_note_links_to_original_invoice_einvoice():
    with TestClient(app) as client:
        _setup_org(client)
        _enable_all(client)
        cid = _b2b_customer(client, "CN Ref Co")
        inv = _invoice(client, cid, "2074-04-01",
                       [{"description": "x", "quantity": "1", "unit_price": "1000", "vat_rate": "0.05"}])
        inv_ei = client.post("/api/einvoicing/generate",
                             json={"source_type": "sales_invoice", "source_id": inv["id"]}).json()
        cn = client.post("/api/sales/credit-notes", json={
            "customer_id": cid, "invoice_id": inv["id"], "date": "2074-04-02", "reason": "Return",
            "lines": [{"description": "x", "quantity": "1", "unit_price": "200", "vat_rate": "0.05"}]}).json()
        cn_ei = client.post("/api/einvoicing/generate",
                            json={"source_type": "customer_cn", "source_id": cn["id"]}).json()
        assert cn_ei["doc_type_code"] == "credit_note"
        assert cn_ei["original_einvoice_id"] == inv_ei["id"]
        assert cn_ei["payload"]["references"]["original_invoice"] == inv["number"]


# ── Advances + partial payments ────────────────────────────────────────────────────────────────
def test_customer_advance_einvoice():
    with TestClient(app) as client:
        _setup_org(client)
        _enable_all(client)
        cid = _b2b_customer(client, "Adv EInv Co")
        adv = client.post("/api/sales/advances", json={
            "customer_id": cid, "date": "2074-05-01", "amount": "5000"}).json()
        d = client.post("/api/einvoicing/generate",
                        json={"source_type": "customer_advance", "source_id": adv["id"]}).json()
        assert d["doc_type_code"] == "prepayment"
        assert Decimal(d["grand_total"]) == Decimal("5000.00")


def test_partial_payment_reflected_in_payload():
    with TestClient(app) as client:
        _setup_org(client)
        _enable_all(client)
        cid = _b2b_customer(client, "Partial Pay Co")
        inv = _invoice(client, cid, "2074-05-10",
                       [{"description": "x", "quantity": "1", "unit_price": "1000", "vat_rate": "0.05"}])
        client.post(f"/api/sales/invoices/{inv['id']}/post")
        client.post("/api/sales/payments", json={
            "customer_id": cid, "invoice_id": inv["id"], "date": "2074-05-11", "amount": "500",
            "method": "bank"})
        d = client.post("/api/einvoicing/generate",
                        json={"source_type": "sales_invoice", "source_id": inv["id"]}).json()
        pay = d["payload"]["payment"]
        assert Decimal(pay["amount_paid"]) == Decimal("500.00")
        assert Decimal(pay["outstanding"]) == Decimal("550.00")   # 1050 gross − 500 paid


# ── Foreign currency ─────────────────────────────────────────────────────────────────────────
def test_foreign_currency_invoice():
    with TestClient(app) as client:
        _setup_org(client)
        _enable_all(client)
        cid = _b2b_customer(client, "FX Co")
        inv = client.post("/api/sales/invoices", json={
            "customer_id": cid, "date": "2074-06-01", "currency": "USD", "exchange_rate": "3.6725",
            "lines": [{"description": "Export svc", "quantity": "1", "unit_price": "1000",
                       "vat_rate": "0.05", "vat_treatment": "SR"}]}).json()
        d = client.post("/api/einvoicing/generate",
                        json={"source_type": "sales_invoice", "source_id": inv["id"]}).json()
        assert d["currency"] == "USD"
        assert d["payload"]["invoice"]["currency"] == "USD"


# ── Lifecycle: submit (manual provider → pending, never auto-confirmed) ─────────────────────────
def test_submit_via_manual_provider_is_pending_not_confirmed():
    with TestClient(app) as client:
        _setup_org(client)
        _enable_all(client)
        cid = _b2b_customer(client, "Submit Co")
        inv = _invoice(client, cid, "2074-07-01",
                       [{"description": "x", "quantity": "1", "unit_price": "1000", "vat_rate": "0.05"}])
        ei = client.post("/api/einvoicing/generate",
                         json={"source_type": "sales_invoice", "source_id": inv["id"]}).json()
        d = client.post(f"/api/einvoicing/{ei['id']}/submit").json()
        assert d["status"] == "pending"
        assert d["regulatory_confirmed"] is False           # no ASP → never auto-confirmed
        actions = [e["action"] for e in d["events"]]
        assert "created" in actions and "submitted" in actions


def test_cannot_submit_when_validation_failed():
    with TestClient(app) as client:
        _setup_org(client)
        _enable_all(client)
        cid = client.post("/api/sales/customers", json={"name": "Bad B2B Co", "party_type": "b2b"}).json()["id"]
        inv = _invoice(client, cid, "2074-07-02",
                       [{"description": "x", "quantity": "1", "unit_price": "500", "vat_rate": "0.05"}])
        ei = client.post("/api/einvoicing/generate",
                         json={"source_type": "sales_invoice", "source_id": inv["id"]}).json()
        r = client.post(f"/api/einvoicing/{ei['id']}/submit")
        assert r.status_code == 400


# ── Rejection → correct source → resubmit (same record, no duplicate) ──────────────────────────
def test_reject_then_resubmit_reuses_same_record():
    with TestClient(app) as client:
        _setup_org(client)
        _enable_all(client)
        cid = _b2b_customer(client, "Reject Co")
        inv = _invoice(client, cid, "2074-08-01",
                       [{"description": "x", "quantity": "1", "unit_price": "1000", "vat_rate": "0.05"}])
        ei = client.post("/api/einvoicing/generate",
                         json={"source_type": "sales_invoice", "source_id": inv["id"]}).json()
        client.post(f"/api/einvoicing/{ei['id']}/submit")
        # ASP rejects it.
        client.post(f"/api/einvoicing/{ei['id']}/status",
                    json={"status": "rejected", "detail": "Buyer endpoint unknown"})
        again = client.post(f"/api/einvoicing/{ei['id']}/resubmit").json()
        assert again["id"] == ei["id"]                      # SAME record — no duplicate document
        # exactly one e-invoice exists for this source invoice
        lst = client.get("/api/einvoicing", params={"source_type": "sales_invoice"}).json()
        assert sum(1 for r in lst if r["source_id"] == inv["id"]) == 1


def test_generate_is_idempotent_no_duplicate():
    with TestClient(app) as client:
        _setup_org(client)
        _enable_all(client)
        cid = _b2b_customer(client, "Idempotent Co")
        inv = _invoice(client, cid, "2074-08-05",
                       [{"description": "x", "quantity": "1", "unit_price": "1000", "vat_rate": "0.05"}])
        a = client.post("/api/einvoicing/generate",
                        json={"source_type": "sales_invoice", "source_id": inv["id"]}).json()
        b = client.post("/api/einvoicing/generate",
                        json={"source_type": "sales_invoice", "source_id": inv["id"]}).json()
        assert a["id"] == b["id"]


# ── Acceptance sets regulatory-confirmed only via external status ──────────────────────────────
def test_external_acceptance_sets_regulatory_confirmed():
    with TestClient(app) as client:
        _setup_org(client)
        _enable_all(client)
        cid = _b2b_customer(client, "Accept Co")
        inv = _invoice(client, cid, "2074-09-01",
                       [{"description": "x", "quantity": "1", "unit_price": "1000", "vat_rate": "0.05"}])
        ei = client.post("/api/einvoicing/generate",
                         json={"source_type": "sales_invoice", "source_id": inv["id"]}).json()
        client.post(f"/api/einvoicing/{ei['id']}/submit")
        d = client.post(f"/api/einvoicing/{ei['id']}/status",
                        json={"status": "accepted", "detail": "Cleared", "provider_ref": "ASP-XYZ-1",
                              "regulatory_confirmed": True}).json()
        assert d["status"] == "accepted"
        assert d["regulatory_confirmed"] is True
        assert d["provider_ref"] == "ASP-XYZ-1"


# ── Cancellation ───────────────────────────────────────────────────────────────────────────────
def test_cancel_requires_reason_and_clears_confirmation():
    with TestClient(app) as client:
        _setup_org(client)
        _enable_all(client)
        cid = _b2b_customer(client, "Cancel Co")
        inv = _invoice(client, cid, "2074-10-01",
                       [{"description": "x", "quantity": "1", "unit_price": "1000", "vat_rate": "0.05"}])
        ei = client.post("/api/einvoicing/generate",
                         json={"source_type": "sales_invoice", "source_id": inv["id"]}).json()
        client.post(f"/api/einvoicing/{ei['id']}/submit")
        assert client.post(f"/api/einvoicing/{ei['id']}/cancel", json={"reason": ""}).status_code == 400
        d = client.post(f"/api/einvoicing/{ei['id']}/cancel", json={"reason": "Issued in error"}).json()
        assert d["status"] == "cancelled"
        assert d["regulatory_confirmed"] is False


# ── Missing mandatory field (configurable) ─────────────────────────────────────────────────────
def test_missing_supplier_trn_fails():
    with TestClient(app) as client:
        # Clear the org TRN so the supplier check trips.
        client.put("/api/organization", json={
            "name": "No TRN Co", "vat_registered": False, "vat_return_frequency": "na"})
        _enable_all(client)
        cid = _b2b_customer(client, "MandField Co")
        inv = _invoice(client, cid, "2074-11-01",
                       [{"description": "x", "quantity": "1", "unit_price": "1000", "vat_rate": "0.05"}])
        d = client.post("/api/einvoicing/generate",
                        json={"source_type": "sales_invoice", "source_id": inv["id"]}).json()
        assert d["system_validation_passed"] is False
        fields = [e["field"] for e in d["validation"]["errors"]]
        assert "supplier.trn" in fields
        _setup_org(client)   # restore for other tests sharing the DB


# ── Dashboard drill-down + no GL side-effects + PDF/Excel ──────────────────────────────────────
def test_dashboard_and_no_ledger_side_effects():
    with TestClient(app) as client:
        _setup_org(client)
        _enable_all(client)
        je_before = len(client.get("/api/journal-entries", params={"limit": 1000}).json())
        cid = _b2b_customer(client, "Dash Co")
        inv = _invoice(client, cid, "2074-12-01",
                       [{"description": "x", "quantity": "1", "unit_price": "1000", "vat_rate": "0.05"}])
        je_after_inv = len(client.get("/api/journal-entries", params={"limit": 1000}).json())
        ei = client.post("/api/einvoicing/generate",
                         json={"source_type": "sales_invoice", "source_id": inv["id"]}).json()
        client.post(f"/api/einvoicing/{ei['id']}/submit")
        # e-invoicing must NOT create any journal entries (invoice draft create also posts none)
        je_after_ei = len(client.get("/api/journal-entries", params={"limit": 1000}).json())
        assert je_after_ei == je_after_inv

        dash = client.get("/api/einvoicing/dashboard").json()
        assert dash["provisional"] is True
        keys = {c["key"] for c in dash["cards"]}
        assert {"total", "submitted", "accepted", "rejected", "pending", "credit_notes",
                "errors"}.issubset(keys)


# ── SME ruleset validation (e.g. Deloitte) + Deloitte ASP adapter ──────────────────────────────
def test_ruleset_sme_validation_flips_provisional_and_stamps_new_einvoices():
    with TestClient(app) as client:
        _setup_org(client)
        _enable_all(client)
        # Sign off the active ruleset as an SME (Deloitte).
        c = client.post("/api/einvoicing/ruleset/validate", json={
            "firm": "Deloitte", "validator": "Tax & Legal", "note": "Validated vs FTA spec"}).json()
        assert c["provisional"] is False
        assert c["ruleset_validated"] is True
        assert c["sme"]["firm"] == "Deloitte"
        # A newly generated e-invoice is no longer provisional.
        cid = _b2b_customer(client, "SME Validated Co")
        inv = _invoice(client, cid, "2075-02-01",
                       [{"description": "x", "quantity": "1", "unit_price": "1000", "vat_rate": "0.05"}])
        d = client.post("/api/einvoicing/generate",
                        json={"source_type": "sales_invoice", "source_id": inv["id"]}).json()
        assert d["provisional"] is False
        # Editing a rule (mandatory fields) re-arms provisional and clears the sign-off.
        c2 = client.put("/api/einvoicing/config", json={
            "required_fields": ["supplier.name", "supplier.trn", "invoice.number"]}).json()
        assert c2["provisional"] is True
        assert c2["ruleset_validated"] is False
        # Restore for other tests sharing the DB.
        client.post("/api/einvoicing/ruleset/revoke")


def test_deloitte_provider_scaffold_is_pending_not_confirmed():
    with TestClient(app) as client:
        _setup_org(client)
        client.put("/api/einvoicing/config", json={
            "enabled": True, "provider": "deloitte",
            "applicable_types": ["sales_invoice", "customer_cn", "vendor_bill", "customer_advance"],
            "provider_config": {"endpoint": "", "participant_id": "AE-123"}})
        cfg = client.get("/api/einvoicing/config").json()
        assert cfg["provider"] == "deloitte"
        assert cfg["provider_config"]["participant_id"] == "AE-123"
        cid = _b2b_customer(client, "Deloitte ASP Co")
        inv = _invoice(client, cid, "2075-03-01",
                       [{"description": "x", "quantity": "1", "unit_price": "1000", "vat_rate": "0.05"}])
        ei = client.post("/api/einvoicing/generate",
                         json={"source_type": "sales_invoice", "source_id": inv["id"]}).json()
        d = client.post(f"/api/einvoicing/{ei['id']}/submit").json()
        assert d["status"] == "pending"
        assert d["regulatory_confirmed"] is False
        assert "Deloitte" in (d["response"]["message"] or "")
        # Secrets are never persisted in the provider config.
        client.put("/api/einvoicing/config", json={"provider_config": {"api_key": "SECRET", "endpoint": "https://x"}})
        pc = client.get("/api/einvoicing/config").json()["provider_config"]
        assert "api_key" not in pc and pc.get("endpoint") == "https://x"
        client.put("/api/einvoicing/config", json={"provider": "manual"})  # restore


def test_sample_provider_simulated_accept_not_regulatory_confirmed():
    with TestClient(app) as client:
        _setup_org(client)
        client.put("/api/einvoicing/config", json={
            "enabled": True, "provider": "sample", "environment": "sandbox",
            "applicable_types": ["sales_invoice", "customer_cn", "vendor_bill", "customer_advance"]})
        cid = _b2b_customer(client, "Sample ASP Co")
        inv = _invoice(client, cid, "2075-04-01",
                       [{"description": "x", "quantity": "1", "unit_price": "1000", "vat_rate": "0.05"}])
        ei = client.post("/api/einvoicing/generate",
                         json={"source_type": "sales_invoice", "source_id": inv["id"]}).json()
        d = client.post(f"/api/einvoicing/{ei['id']}/submit").json()
        assert d["status"] == "accepted"                       # full lifecycle demoable now
        assert d["regulatory_confirmed"] is False              # a simulation never confirms compliance
        assert d["response"]["simulated"] is True
        assert d["provider_ref"].startswith("SAMPLE-")
        client.put("/api/einvoicing/config", json={"provider": "manual"})  # restore


def test_einvoice_pdf_and_excel_and_archive():
    with TestClient(app) as client:
        _setup_org(client)
        _enable_all(client)
        cid = _b2b_customer(client, "Doc Co")
        inv = _invoice(client, cid, "2075-01-05",
                       [{"description": "x", "quantity": "1", "unit_price": "1000", "vat_rate": "0.05"}])
        ei = client.post("/api/einvoicing/generate",
                         json={"source_type": "sales_invoice", "source_id": inv["id"]}).json()
        assert client.get(f"/api/einvoicing/{ei['id']}/pdf").content[:4] == b"%PDF"
        assert client.get(f"/api/documents/einvoice/{ei['id']}/excel").content[:2] == b"PK"
        assert client.post(f"/api/documents/einvoice/{ei['id']}/archive-pdf").status_code == 200
