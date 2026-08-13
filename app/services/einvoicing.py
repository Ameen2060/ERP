"""UAE E-Invoicing — a provisional, configuration-driven compliance layer.

This module implements the *engine, lifecycle, validation, archive, audit, dashboard and
service-provider architecture* for UAE e-invoicing. It is deliberately layered ON TOP of the
already-posted accounting transactions: it reads invoices/credit-notes/bills/advances and never
posts to the General Ledger itself, so submitting or resubmitting an e-invoice can never create
a duplicate accounting entry — the source transaction remains the single source of the GL/VAT
records.

IMPORTANT — regulatory posture (see the project's compliance safeguards):
  * The concrete UAE MoF/FTA technical specification (the PINT AE / Peppol-based schema, the
    mandatory field list, the Accredited Service Provider APIs and the QR/security requirements)
    is NOT hard-coded here. Those inputs live in ``EInvoiceConfig`` as versioned configuration
    (schema id/version, applicable transaction types, mandatory fields, ruleset source/date) so
    they can be updated when the official requirements change, without touching this engine or
    any historical transaction.
  * ``system_validation_passed`` ("System Validation Passed") is reported strictly separately
    from ``regulatory_confirmed`` ("Regulatory Compliance Confirmed"). Passing the internal
    validator NEVER means the document is legally compliant.
  * Until each rule/schema has been verified against the latest official UAE documentation by a
    qualified SME, ``provisional`` stays True and every record carries the notice
    "Provisional — requires UAE tax/e-invoicing SME validation."

The Accredited Service Provider integration is modular (a provider adapter registry). The
default ``manual`` provider performs NO external submission and never confirms regulatory
compliance — it records the document as pending external submission through an approved ASP.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Customer,
    CustomerAdvance,
    CustomerCreditNote,
    EInvoice,
    EInvoiceConfig,
    EInvoiceEvent,
    Organization,
    SalesInvoice,
    Vendor,
    VendorAdvance,
    VendorBill,
    VendorCreditNote,
    VatTreatment,
)
from . import organization
from .validation import validate_trn

PROVISIONAL_NOTICE = "Provisional — requires UAE tax/e-invoicing SME validation."

# Human-readable status labels for the full lifecycle required by the spec.
STATUS_LABEL = {
    "draft": "Draft",
    "validating": "Validating",
    "validation_failed": "Validation Failed",
    "ready": "Ready for Submission",
    "submitted": "Submitted",
    "accepted": "Accepted",
    "rejected": "Rejected",
    "cancelled": "Cancelled",
    "corrected": "Corrected / Replaced",
    "error": "Error",
    "pending": "Pending",
}

# Applicability + document-type metadata per accounting source type. `applicable_types` in the
# config decides which of these are actually turned on.
SOURCE_META = {
    "sales_invoice":    {"direction": "outbound", "doc_type": "invoice",     "label": "Sales Invoice"},
    "customer_cn":      {"direction": "outbound", "doc_type": "credit_note", "label": "Sales Credit Note"},
    "customer_advance": {"direction": "outbound", "doc_type": "prepayment",  "label": "Customer Advance"},
    "vendor_bill":      {"direction": "inbound",  "doc_type": "invoice",     "label": "Vendor Bill"},
    "vendor_cn":        {"direction": "inbound",  "doc_type": "credit_note", "label": "Vendor Credit Note"},
    "vendor_advance":   {"direction": "inbound",  "doc_type": "prepayment",  "label": "Vendor Advance"},
}

# Default provisional mandatory-field set. This is a NEUTRAL, spec-agnostic starting point — it
# must be replaced/confirmed against the official UAE mandatory field list by an SME. Field keys
# reference the structured payload built by `build_payload`.
DEFAULT_REQUIRED_FIELDS = [
    "supplier.name", "supplier.trn", "buyer.name",
    "invoice.number", "invoice.issue_date", "invoice.currency", "invoice.type_code",
    "lines", "totals.net", "totals.vat", "totals.gross",
]

_CONFIG_ID = "einv"


class EInvoiceError(ValueError):
    """Domain error → HTTP 400."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _d(v) -> Decimal:
    return Decimal(str(v or 0))


def _q(v) -> Decimal:
    return _d(v).quantize(Decimal("0.01"))


# ── Configuration ─────────────────────────────────────────────────────────────────────────────
def ensure_config(db: Session) -> EInvoiceConfig:
    cfg = db.get(EInvoiceConfig, _CONFIG_ID)
    if not cfg:
        cfg = EInvoiceConfig(
            id=_CONFIG_ID, enabled=False, provider="manual",
            applicable_types="sales_invoice,customer_cn",
            required_fields_json=json.dumps(DEFAULT_REQUIRED_FIELDS),
            ruleset_source="UAE MoF/FTA e-invoicing documentation (to be loaded by SME)",
            ruleset_date=None, provisional=True,
        )
        db.add(cfg)
        db.commit()
        db.refresh(cfg)
    return cfg


def get_config(db: Session) -> EInvoiceConfig:
    return ensure_config(db)


def applicable_types(cfg: EInvoiceConfig) -> list[str]:
    return [t.strip() for t in (cfg.applicable_types or "").split(",") if t.strip()]


def required_fields(cfg: EInvoiceConfig) -> list[str]:
    try:
        v = json.loads(cfg.required_fields_json or "[]")
        return v if isinstance(v, list) else DEFAULT_REQUIRED_FIELDS
    except (ValueError, TypeError):
        return DEFAULT_REQUIRED_FIELDS


def is_applicable(cfg: EInvoiceConfig, source_type: str) -> bool:
    return source_type in applicable_types(cfg)


def provider_config(cfg: EInvoiceConfig) -> dict:
    try:
        v = json.loads(cfg.provider_config_json or "{}")
        return v if isinstance(v, dict) else {}
    except (ValueError, TypeError):
        return {}


def config_out(db: Session) -> dict:
    cfg = get_config(db)
    return {
        "enabled": cfg.enabled, "environment": cfg.environment, "provider": cfg.provider,
        "provider_config": provider_config(cfg),
        "provider_labels": {k: getattr(v, "name", k) for k, v in PROVIDERS.items()},
        "schema_id": cfg.schema_id, "schema_version": cfg.schema_version,
        "applicable_types": applicable_types(cfg),
        "applicable_type_labels": {k: v["label"] for k, v in SOURCE_META.items()},
        "required_fields": required_fields(cfg),
        "ruleset_version": cfg.ruleset_version, "ruleset_source": cfg.ruleset_source,
        "ruleset_date": cfg.ruleset_date, "provisional": cfg.provisional,
        "require_buyer_trn_b2b": cfg.require_buyer_trn_b2b,
        "providers": list(PROVIDERS.keys()),
        "ruleset_validated": not cfg.provisional and bool(cfg.sme_validated_at),
        "sme": {"firm": cfg.sme_firm, "validator": cfg.sme_validator,
                "validated_at": _iso(cfg.sme_validated_at), "note": cfg.sme_note},
        "updated_at": _iso(cfg.updated_at),
        "compliance_notice": PROVISIONAL_NOTICE if cfg.provisional else None,
        "regulatory_compliance_confirmed": False,  # only an external ASP/FTA confirmation sets this per-document
    }


# Config keys whose change invalidates a prior SME ruleset validation (rules/schema changed).
_RULE_KEYS = ("schema_id", "schema_version", "ruleset_version", "ruleset_source", "ruleset_date",
              "applicable_types", "required_fields", "require_buyer_trn_b2b")


def update_config(db: Session, data: dict, actor: str = "local") -> dict:
    cfg = get_config(db)
    rule_change = any(k in data for k in _RULE_KEYS)
    if "enabled" in data:
        cfg.enabled = bool(data["enabled"])
    if data.get("environment") in ("sandbox", "production"):
        cfg.environment = data["environment"]
    if data.get("provider"):
        if data["provider"] not in PROVIDERS:
            raise EInvoiceError(f"Unknown service provider adapter '{data['provider']}'.")
        cfg.provider = data["provider"]
    if "provider_config" in data and isinstance(data["provider_config"], dict):
        # Store non-secret connection settings only (endpoint/participant/environment). Secrets
        # (API keys) must come from environment/secret store — never persisted here in clear.
        safe = {k: v for k, v in data["provider_config"].items()
                if k.lower() not in ("password", "secret", "api_key", "apikey", "token", "client_secret")}
        cfg.provider_config_json = json.dumps(safe)
    if "schema_id" in data and data["schema_id"]:
        cfg.schema_id = str(data["schema_id"])[:48]
    if "schema_version" in data and data["schema_version"]:
        cfg.schema_version = str(data["schema_version"])[:24]
    if "applicable_types" in data and isinstance(data["applicable_types"], list):
        bad = [t for t in data["applicable_types"] if t not in SOURCE_META]
        if bad:
            raise EInvoiceError(f"Unknown transaction type(s): {', '.join(bad)}.")
        cfg.applicable_types = ",".join(data["applicable_types"])
    if "required_fields" in data and isinstance(data["required_fields"], list):
        cfg.required_fields_json = json.dumps([str(f) for f in data["required_fields"]])
    if "ruleset_version" in data and data["ruleset_version"]:
        cfg.ruleset_version = str(data["ruleset_version"])[:32]
    if "ruleset_source" in data:
        cfg.ruleset_source = (data["ruleset_source"] or None)
    if "ruleset_date" in data:
        cfg.ruleset_date = (data["ruleset_date"] or None)
    if "provisional" in data:
        cfg.provisional = bool(data["provisional"])
    if "require_buyer_trn_b2b" in data:
        cfg.require_buyer_trn_b2b = bool(data["require_buyer_trn_b2b"])
    # A change to any rule/schema key invalidates a prior SME sign-off and re-arms PROVISIONAL.
    if rule_change and cfg.sme_validated_at:
        cfg.provisional = True
        cfg.sme_firm = cfg.sme_validator = cfg.sme_note = None
        cfg.sme_validated_at = None
    cfg.updated_at = _now()
    db.commit()
    return config_out(db)


def validate_ruleset(db: Session, firm: str, validator: str | None = None,
                     note: str | None = None, actor: str = "local") -> dict:
    """Record an SME sign-off of the ACTIVE e-invoicing ruleset/schema (e.g. by a tax advisory
    firm such as Deloitte). Clears the PROVISIONAL flag so newly generated e-invoices are no
    longer marked provisional. Existing e-invoices keep their original stamp."""
    if not (firm and firm.strip()):
        raise EInvoiceError("The validating SME firm name is required.")
    cfg = get_config(db)
    cfg.sme_firm = firm.strip()
    cfg.sme_validator = (validator or "").strip() or None
    cfg.sme_note = (note or "").strip() or None
    cfg.sme_validated_at = _now()
    cfg.provisional = False
    cfg.updated_at = _now()
    db.commit()
    return config_out(db)


def revoke_ruleset_validation(db: Session, actor: str = "local") -> dict:
    """Withdraw a ruleset SME sign-off — back to PROVISIONAL."""
    cfg = get_config(db)
    cfg.sme_firm = cfg.sme_validator = cfg.sme_note = None
    cfg.sme_validated_at = None
    cfg.provisional = True
    cfg.updated_at = _now()
    db.commit()
    return config_out(db)


# ── VAT classification (driven by the configurable VAT-treatment master, not hard-coded) ────────
def _vat_category(db: Session, code: str | None, rate) -> str:
    if code:
        t = db.execute(select(VatTreatment).where(VatTreatment.code == code)).scalar_one_or_none()
        if t and t.kind:
            return t.kind
    return "standard" if _d(rate) > 0 else "zero"


# ── Party / organization payload blocks ─────────────────────────────────────────────────────────
def _supplier_block(db: Session, org: Organization) -> dict:
    return {
        "name": org.legal_name or org.name or None,
        "trn": org.trn or None,
        "address": org.address or None,
        "country": org.country or "AE",
        "trade_license": org.trade_license or None,
        "einvoice_scheme": org.einvoice_scheme or None,
        "einvoice_id": org.einvoice_id or None,
        "bank_iban": org.bank_iban or None,
    }


def _party_block(party) -> dict:
    if party is None:
        return {}
    return {
        "name": party.name or None,
        "trn": getattr(party, "trn", None),
        "address": getattr(party, "billing_address", None) or getattr(party, "address", None),
        "country": getattr(party, "country", None) or "AE",
        "class": getattr(party, "party_type", None) or "b2b",
        "tax_status": getattr(party, "tax_status", None) or "unknown",
        "einvoice_scheme": getattr(party, "einvoice_scheme", None),
        "einvoice_id": getattr(party, "einvoice_id", None),
    }


def _line_block(db: Session, ln, currency: str) -> dict:
    qty = _d(ln.quantity)
    unit = _d(ln.unit_price)
    net = _d(ln.net_amount)
    gross_before_disc = (qty * unit).quantize(Decimal("0.01"))
    discount = gross_before_disc - net
    if discount < 0:
        discount = Decimal(0)
    code = getattr(ln, "vat_treatment", None)
    return {
        "description": ln.description or "",
        "quantity": str(qty),
        "unit_price": str(unit),
        "discount": str(_q(discount)),
        "net_amount": str(_q(net)),
        "vat_rate": str(_d(ln.vat_rate)),
        "vat_treatment": code,
        "vat_category": _vat_category(db, code, ln.vat_rate),
        "vat_amount": str(_q(ln.vat_amount)),
        "line_total": str(_q(ln.line_total)),
        "currency": currency,
    }


# ── Structured e-invoice payload builder (per source transaction) ────────────────────────────────
def build_payload(db: Session, source_type: str, source_id: str) -> dict:
    """Build the structured electronic-invoice representation for a posted transaction.

    NOTE: the field names/shape here are a NEUTRAL structured model, deliberately not asserted to
    match the official UAE schema. The mapping to the official PINT AE / Peppol schema is applied
    by the service-provider adapter using the configured, version-controlled schema."""
    if source_type not in SOURCE_META:
        raise EInvoiceError(f"Unsupported source type '{source_type}'.")
    org = organization.get_org(db)
    meta = SOURCE_META[source_type]
    supplier = _supplier_block(db, org)

    payload: dict = {
        "schema": {"id": None, "version": None, "provisional": True,
                   "notice": PROVISIONAL_NOTICE},
        "document": {"source_type": source_type, "direction": meta["direction"],
                     "type_code": meta["doc_type"]},
        "supplier": supplier,
        "buyer": {},
        "invoice": {},
        "lines": [],
        "totals": {},
        "vat_breakdown": [],
        "references": {},
        "payment": {},
    }

    if source_type == "sales_invoice":
        inv = db.get(SalesInvoice, source_id)
        if not inv:
            raise EInvoiceError("Sales invoice not found.")
        cust = db.get(Customer, inv.customer_id)
        payload["buyer"] = _party_block(cust)
        payload["invoice"] = {
            "number": inv.number, "issue_date": str(inv.date),
            "supply_date": str(inv.date), "due_date": str(inv.due_date) if inv.due_date else None,
            "currency": inv.currency, "type_code": "invoice",
            "exchange_rate": str(_d(inv.exchange_rate)),
        }
        payload["lines"] = [_line_block(db, ln, inv.currency) for ln in inv.lines]
        payload["totals"] = {"net": str(_q(inv.net_total)), "vat": str(_q(inv.vat_total)),
                             "gross": str(_q(inv.grand_total))}
        payload["payment"] = {"amount_paid": str(_q(inv.amount_paid)),
                              "outstanding": str(_q(_d(inv.grand_total) - _d(inv.amount_paid)))}
        payload["references"] = {"contract": inv.contract_reference, "project": inv.project}

    elif source_type == "customer_cn":
        cn = db.get(CustomerCreditNote, source_id)
        if not cn:
            raise EInvoiceError("Customer credit note not found.")
        cust = db.get(Customer, cn.customer_id)
        payload["buyer"] = _party_block(cust)
        orig_num = None
        if cn.invoice_id:
            inv = db.get(SalesInvoice, cn.invoice_id)
            orig_num = inv.number if inv else None
        payload["invoice"] = {
            "number": cn.number, "issue_date": str(cn.date), "supply_date": str(cn.date),
            "currency": cn.currency, "type_code": "credit_note", "reason": cn.reason,
        }
        payload["lines"] = [_line_block(db, ln, cn.currency) for ln in cn.lines]
        payload["totals"] = {"net": str(_q(cn.net_total)), "vat": str(_q(cn.vat_total)),
                             "gross": str(_q(cn.grand_total))}
        payload["references"] = {"original_invoice": orig_num, "original_invoice_id": cn.invoice_id,
                                 "contract": cn.contract_reference}

    elif source_type == "customer_advance":
        adv = db.get(CustomerAdvance, source_id)
        if not adv:
            raise EInvoiceError("Customer advance not found.")
        cust = db.get(Customer, adv.customer_id)
        payload["buyer"] = _party_block(cust)
        payload["invoice"] = {
            "number": adv.number, "issue_date": str(adv.date),
            "supply_date": str(adv.tax_point_date or adv.date), "currency": adv.currency,
            "type_code": "prepayment",
        }
        payload["lines"] = [{
            "description": "Advance / prepayment received", "quantity": "1",
            "unit_price": str(_q(adv.net_amount if adv.vat_applicable else adv.amount)),
            "discount": "0.00",
            "net_amount": str(_q(adv.net_amount if adv.vat_applicable else adv.amount)),
            "vat_rate": str(_d(adv.vat_rate)),
            "vat_treatment": "SR" if adv.vat_applicable else None,
            "vat_category": "standard" if adv.vat_applicable else "out_of_scope",
            "vat_amount": str(_q(adv.vat_amount)),
            "line_total": str(_q(adv.amount)), "currency": adv.currency,
        }]
        payload["totals"] = {
            "net": str(_q(adv.net_amount if adv.vat_applicable else adv.amount)),
            "vat": str(_q(adv.vat_amount)), "gross": str(_q(adv.amount))}
        payload["references"] = {"contract": adv.contract_reference, "project": adv.project}

    elif source_type == "vendor_bill":
        bill = db.get(VendorBill, source_id)
        if not bill:
            raise EInvoiceError("Vendor bill not found.")
        vendor = db.get(Vendor, bill.vendor_id)
        # For inbound documents the vendor is the supplier and the organization is the buyer.
        payload["supplier"] = _party_block(vendor)
        payload["buyer"] = _supplier_block(db, org)
        payload["invoice"] = {
            "number": bill.number, "vendor_ref": bill.vendor_ref, "issue_date": str(bill.date),
            "supply_date": str(bill.date), "currency": bill.currency, "type_code": "invoice",
        }
        payload["lines"] = [_line_block(db, ln, bill.currency) for ln in bill.lines]
        payload["totals"] = {"net": str(_q(bill.net_total)), "vat": str(_q(bill.vat_total)),
                             "gross": str(_q(bill.grand_total))}
        payload["references"] = {"contract": bill.contract_reference, "project": bill.project}

    elif source_type == "vendor_cn":
        cn = db.get(VendorCreditNote, source_id)
        if not cn:
            raise EInvoiceError("Vendor credit note not found.")
        vendor = db.get(Vendor, cn.vendor_id)
        payload["supplier"] = _party_block(vendor)
        payload["buyer"] = _supplier_block(db, org)
        orig_num = None
        if cn.bill_id:
            bill = db.get(VendorBill, cn.bill_id)
            orig_num = bill.number if bill else None
        payload["invoice"] = {
            "number": cn.number, "vendor_ref": cn.vendor_ref, "issue_date": str(cn.date),
            "supply_date": str(cn.date), "currency": cn.currency, "type_code": "credit_note",
            "reason": cn.reason,
        }
        payload["lines"] = [_line_block(db, ln, cn.currency) for ln in cn.lines]
        payload["totals"] = {"net": str(_q(cn.net_total)), "vat": str(_q(cn.vat_total)),
                             "gross": str(_q(cn.grand_total))}
        payload["references"] = {"original_bill": orig_num, "original_bill_id": cn.bill_id,
                                 "contract": cn.contract_reference}

    # VAT breakdown grouped by category/rate.
    groups: dict = {}
    for ln in payload["lines"]:
        key = (ln.get("vat_category"), ln.get("vat_rate"))
        g = groups.setdefault(key, {"category": key[0], "rate": key[1],
                                    "net": Decimal(0), "vat": Decimal(0)})
        g["net"] += _d(ln["net_amount"])
        g["vat"] += _d(ln["vat_amount"])
    payload["vat_breakdown"] = [
        {"category": g["category"], "rate": g["rate"], "net": str(_q(g["net"])),
         "vat": str(_q(g["vat"]))} for g in groups.values()]
    return payload


# ── Internal ("System") validation — NOT a regulatory-compliance statement ─────────────────────
def _get_nested(payload: dict, dotted: str):
    cur = payload
    for part in dotted.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def validate_payload(db: Session, cfg: EInvoiceConfig, payload: dict) -> dict:
    """Run the configured internal checks over a structured payload. Returns
    {passed, errors[], warnings[]}. This is 'System Validation' only."""
    errors: list[dict] = []
    warnings: list[dict] = []

    def err(field, code, message, hint=None):
        errors.append({"field": field, "code": code, "message": message, "hint": hint,
                       "severity": "error"})

    def warn(field, message):
        warnings.append({"field": field, "message": message, "severity": "warning"})

    # 1) Mandatory field presence (configurable list).
    for f in required_fields(cfg):
        val = _get_nested(payload, f)
        if val is None or (isinstance(val, (list, str, dict)) and len(val) == 0):
            err(f, "missing_field", f"Required field '{f}' is missing or empty.",
                "Complete the source transaction / master data.")

    supplier = payload.get("supplier") or {}
    buyer = payload.get("buyer") or {}
    inv = payload.get("invoice") or {}
    totals = payload.get("totals") or {}
    lines = payload.get("lines") or []

    # 2) TRN format checks (supplier always; buyer when B2B and required).
    try:
        validate_trn(supplier.get("trn"), required=True, label="Supplier TRN")
    except Exception as e:  # noqa: BLE001
        err("supplier.trn", "invalid_trn", str(e))
    if buyer:
        try:
            buyer_required = cfg.require_buyer_trn_b2b and (buyer.get("class") == "b2b")
            validate_trn(buyer.get("trn"), required=buyer_required, label="Buyer TRN")
        except Exception as e:  # noqa: BLE001
            err("buyer.trn", "invalid_trn", str(e))
        if buyer.get("class") == "b2c" and buyer.get("trn"):
            warn("buyer.trn", "Buyer is classified B2C but carries a TRN — verify classification.")

    # 3) Currency.
    if inv.get("currency") and len(str(inv.get("currency"))) != 3:
        err("invoice.currency", "invalid_currency", "Currency must be a 3-letter ISO code.")

    # 4) Line-level arithmetic + totals reconciliation.
    net_sum = Decimal(0)
    vat_sum = Decimal(0)
    for i, ln in enumerate(lines):
        qty = _d(ln.get("quantity"))
        if qty == 0:
            err(f"lines[{i}].quantity", "zero_quantity", "Line quantity cannot be zero.")
        net_sum += _d(ln.get("net_amount"))
        vat_sum += _d(ln.get("vat_amount"))
        # VAT amount vs rate × net (allow rounding tolerance).
        expected = (_d(ln.get("net_amount")) * _d(ln.get("vat_rate"))).quantize(Decimal("0.01"))
        if abs(expected - _d(ln.get("vat_amount"))) > Decimal("0.02"):
            warn(f"lines[{i}].vat_amount",
                 f"Line VAT {ln.get('vat_amount')} differs from rate×net ({expected}).")
    if totals:
        if abs(net_sum - _d(totals.get("net"))) > Decimal("0.02"):
            err("totals.net", "totals_mismatch",
                f"Sum of line net ({_q(net_sum)}) does not match invoice net ({totals.get('net')}).")
        if abs(vat_sum - _d(totals.get("vat"))) > Decimal("0.02"):
            err("totals.vat", "totals_mismatch",
                f"Sum of line VAT ({_q(vat_sum)}) does not match invoice VAT ({totals.get('vat')}).")
        gross = _d(totals.get("net")) + _d(totals.get("vat"))
        if abs(gross - _d(totals.get("gross"))) > Decimal("0.02"):
            err("totals.gross", "gross_mismatch",
                f"Gross ({totals.get('gross')}) must equal net + VAT ({_q(gross)}).")

    # 5) Credit / debit note must reference the original document.
    if inv.get("type_code") in ("credit_note", "debit_note"):
        refs = payload.get("references") or {}
        if not (refs.get("original_invoice") or refs.get("original_bill")):
            warn("references.original_invoice",
                 "Credit/debit note does not reference an original invoice — required for a valid correction.")

    return {"passed": len(errors) == 0, "errors": errors, "warnings": warnings}


# ── Service-provider adapter registry (modular ASP integration) ─────────────────────────────────
class ManualProvider:
    """Default adapter: performs NO external submission. It records the document as pending
    submission through an approved Accredited Service Provider, and NEVER confirms regulatory
    compliance. Swap this for a real ASP adapter without changing the engine."""

    key = "manual"
    name = "Manual / No ASP connected"

    def submit(self, cfg: EInvoiceConfig, ei: EInvoice, payload: dict) -> dict:
        return {
            "status": "pending",
            "provider_ref": None,
            "regulatory_confirmed": False,
            "message": ("No accredited service provider is connected. The structured e-invoice "
                        "has been prepared and marked pending external submission/clearance "
                        "through a UAE-approved ASP. " + PROVISIONAL_NOTICE),
        }


class DeloitteProvider:
    """Accredited Service Provider adapter for Deloitte — SCAFFOLD.

    The connection settings (endpoint, participant id, environment) are read from the config's
    provider_config; API credentials must come from environment/secret store, never persisted in
    clear. Until Deloitte's actual e-invoicing API specification (endpoints, auth scheme, the
    exact schema/field mapping they require) is supplied, this adapter performs NO live network
    call and NEVER confirms regulatory compliance — it prepares the document and marks it pending.

    To go live, implement the marked TODO: map `payload` to Deloitte's required schema, POST it
    to `endpoint`, and translate their response into {status, provider_ref, regulatory_confirmed,
    message}."""

    key = "deloitte"
    name = "Deloitte (Accredited Service Provider)"

    def submit(self, cfg: EInvoiceConfig, ei: EInvoice, payload: dict) -> dict:
        pc = provider_config(cfg)
        endpoint = pc.get("endpoint")
        # TODO(live): build Deloitte's schema payload from `payload`, authenticate with the
        # credential from the secret store, POST to `endpoint`, and parse the response.
        if not endpoint:
            return {
                "status": "pending", "provider_ref": None, "regulatory_confirmed": False,
                "message": ("Deloitte ASP adapter selected but not connected — set the Deloitte "
                            "e-invoicing endpoint + participant id in E-Invoice Settings and "
                            "supply API credentials via the secret store to enable live "
                            "submission. Document prepared and marked pending. " + PROVISIONAL_NOTICE),
            }
        return {
            "status": "pending", "provider_ref": None, "regulatory_confirmed": False,
            "message": (f"Deloitte ASP endpoint configured ({endpoint}) but the live submission "
                        "call is not yet implemented — awaiting Deloitte's API specification and "
                        "credentials. Document prepared and marked pending. " + PROVISIONAL_NOTICE),
        }


class SampleProvider:
    """TEMPORARY sandbox provider — simulates the ASP/network round-trip so the full lifecycle
    (submit → accepted) can be exercised before a live Accredited Service Provider is connected.

    It makes NO external call. Its 'accepted' response is explicitly flagged ``simulated`` and,
    critically, does NOT set regulatory_confirmed — a sandbox simulation is never a real
    regulatory/FTA confirmation. Replace with a live adapter (e.g. Deloitte) for production."""

    key = "sample"
    name = "Sample (temporary sandbox — SIMULATED, not a real ASP)"

    def submit(self, cfg: EInvoiceConfig, ei: EInvoice, payload: dict) -> dict:
        return {
            "status": "accepted",
            "provider_ref": f"SAMPLE-{ei.id[:8].upper()}",
            "regulatory_confirmed": False,   # a simulation NEVER confirms real compliance
            "simulated": True,
            "message": ("SIMULATED sandbox acceptance via the temporary Sample provider — NOT a "
                        "real ASP/FTA submission and NOT a regulatory-compliance confirmation. "
                        "Switch to the live Deloitte adapter once its API spec is available. "
                        + PROVISIONAL_NOTICE),
        }


PROVIDERS: dict[str, object] = {ManualProvider.key: ManualProvider(),
                                SampleProvider.key: SampleProvider(),
                                DeloitteProvider.key: DeloitteProvider()}


def register_provider(adapter) -> None:
    PROVIDERS[adapter.key] = adapter


def _provider(cfg: EInvoiceConfig):
    return PROVIDERS.get(cfg.provider) or PROVIDERS["manual"]


# ── Audit trail ─────────────────────────────────────────────────────────────────────────────────
def _log(db: Session, ei: EInvoice, action: str, frm: str | None, to: str | None,
         actor: str, detail: str | None = None) -> None:
    db.add(EInvoiceEvent(einvoice_id=ei.id, action=action, from_status=frm, to_status=to,
                         actor=actor or "local", detail=detail))


# ── Generation / lifecycle ──────────────────────────────────────────────────────────────────────
def _existing(db: Session, source_type: str, source_id: str) -> EInvoice | None:
    return db.execute(
        select(EInvoice).where(EInvoice.source_type == source_type,
                               EInvoice.source_id == source_id)
    ).scalars().first()


def _apply_validation(db: Session, cfg: EInvoiceConfig, ei: EInvoice, payload: dict) -> dict:
    result = validate_payload(db, cfg, payload)
    ei.validation_json = json.dumps(result)
    ei.system_validation_passed = result["passed"]
    ei.status = "ready" if result["passed"] else "validation_failed"
    return result


def generate(db: Session, source_type: str, source_id: str, actor: str = "local") -> EInvoice:
    """Create-or-refresh the single e-invoice for a source transaction, build its structured
    payload and run system validation. Never creates a duplicate (idempotent per source doc),
    and never posts to the ledger."""
    cfg = get_config(db)
    if not is_applicable(cfg, source_type):
        raise EInvoiceError(
            f"{SOURCE_META.get(source_type, {}).get('label', source_type)} is not configured as "
            "e-invoiceable under the active UAE e-invoicing rules.")
    payload = build_payload(db, source_type, source_id)
    payload["schema"]["id"] = cfg.schema_id
    payload["schema"]["version"] = cfg.schema_version
    payload["schema"]["provisional"] = cfg.provisional

    meta = SOURCE_META[source_type]
    inv = payload.get("invoice") or {}
    ei = _existing(db, source_type, source_id)
    creating = ei is None
    if creating:
        ei = EInvoice(source_type=source_type, source_id=source_id, direction=meta["direction"],
                      doc_type_code=meta["doc_type"])
        db.add(ei)
    # Do not regenerate a locked/accepted record's identity — but allow revalidation.
    ei.doc_number = inv.get("number")
    ei.party_name = (payload.get("buyer") or {}).get("name") if meta["direction"] == "outbound" \
        else (payload.get("supplier") or {}).get("name")
    ei.party_trn = (payload.get("buyer") or {}).get("trn") if meta["direction"] == "outbound" \
        else (payload.get("supplier") or {}).get("trn")
    ei.party_class = (payload.get("buyer") or {}).get("class")
    ei.currency = inv.get("currency") or "AED"
    tot = payload.get("totals") or {}
    ei.net_total, ei.vat_total, ei.grand_total = _q(tot.get("net")), _q(tot.get("vat")), _q(tot.get("gross"))
    ei.payload_json = json.dumps(payload)
    ei.provider = cfg.provider
    ei.provisional = cfg.provisional
    ei.schema_id, ei.schema_version, ei.ruleset_version = cfg.schema_id, cfg.schema_version, cfg.ruleset_version

    # Link a customer credit note to the original invoice's e-invoice, if present.
    refs = payload.get("references") or {}
    if refs.get("original_invoice_id"):
        orig = _existing(db, "sales_invoice", refs["original_invoice_id"])
        ei.original_einvoice_id = orig.id if orig else None
    elif refs.get("original_bill_id"):
        orig = _existing(db, "vendor_bill", refs["original_bill_id"])
        ei.original_einvoice_id = orig.id if orig else None

    frm = None if creating else ei.status
    _apply_validation(db, cfg, ei, payload)
    db.flush()
    _log(db, ei, "created" if creating else "revalidated", frm, ei.status, actor,
         detail=f"System validation {'passed' if ei.system_validation_passed else 'failed'}.")
    db.commit()
    db.refresh(ei)
    return ei


def revalidate(db: Session, ei_id: str, actor: str = "local") -> EInvoice:
    ei = _get(db, ei_id)
    if ei.status in ("submitted", "accepted"):
        raise EInvoiceError("Cannot revalidate a submitted/accepted e-invoice — cancel or correct it first.")
    return generate(db, ei.source_type, ei.source_id, actor=actor)


def submit(db: Session, ei_id: str, actor: str = "local") -> EInvoice:
    cfg = get_config(db)
    ei = _get(db, ei_id)
    if not ei.system_validation_passed:
        raise EInvoiceError("System validation must pass before an e-invoice can be submitted.")
    if ei.status in ("submitted", "accepted"):
        raise EInvoiceError("This e-invoice has already been submitted.")
    if ei.status == "cancelled":
        raise EInvoiceError("A cancelled e-invoice cannot be submitted — generate a replacement.")
    payload = json.loads(ei.payload_json or "{}")
    frm = ei.status
    try:
        resp = _provider(cfg).submit(cfg, ei, payload)
    except Exception as e:  # noqa: BLE001 — provider failures must not crash the app
        ei.status = "error"
        ei.response_json = json.dumps({"error": str(e)})
        _log(db, ei, "error", frm, "error", actor, detail=f"Provider error: {e}")
        db.commit()
        db.refresh(ei)
        return ei
    ei.response_json = json.dumps(resp)
    ei.provider_ref = resp.get("provider_ref")
    ei.submitted_at = _now()
    # The default/manual provider returns 'pending'; a real ASP may return submitted/accepted.
    new_status = resp.get("status") or "submitted"
    if new_status not in STATUS_LABEL:
        new_status = "submitted"
    ei.status = new_status
    ei.regulatory_confirmed = bool(resp.get("regulatory_confirmed", False))
    if new_status == "accepted":
        ei.accepted_at = _now()
    _log(db, ei, "submitted", frm, ei.status, actor, detail=resp.get("message"))
    db.commit()
    db.refresh(ei)
    return ei


def record_provider_status(db: Session, ei_id: str, status: str, detail: str | None = None,
                           provider_ref: str | None = None, regulatory_confirmed: bool | None = None,
                           actor: str = "local") -> EInvoice:
    """Record an acceptance/rejection/status update returned by the ASP / UAE network.

    This is how an external confirmation flows in. Setting status 'accepted' with
    regulatory_confirmed=True is the ONLY path that marks the document regulatory-confirmed —
    the engine never self-certifies."""
    ei = _get(db, ei_id)
    if status not in STATUS_LABEL:
        raise EInvoiceError(f"Unknown status '{status}'.")
    frm = ei.status
    ei.status = status
    if provider_ref:
        ei.provider_ref = provider_ref
    if regulatory_confirmed is not None:
        ei.regulatory_confirmed = bool(regulatory_confirmed)
    if status == "accepted":
        ei.accepted_at = _now()
        if regulatory_confirmed is None:
            ei.regulatory_confirmed = True
    if status in ("rejected", "cancelled", "validation_failed"):
        ei.regulatory_confirmed = False
    ei.response_json = json.dumps({"status": status, "detail": detail, "provider_ref": provider_ref})
    _log(db, ei, status, frm, status, actor, detail=detail)
    db.commit()
    db.refresh(ei)
    return ei


def cancel(db: Session, ei_id: str, reason: str, actor: str = "local") -> EInvoice:
    ei = _get(db, ei_id)
    if not (reason and reason.strip()):
        raise EInvoiceError("A cancellation reason is required.")
    if ei.status == "cancelled":
        raise EInvoiceError("This e-invoice is already cancelled.")
    frm = ei.status
    ei.status = "cancelled"
    ei.regulatory_confirmed = False
    _log(db, ei, "cancelled", frm, "cancelled", actor, detail=reason.strip())
    db.commit()
    db.refresh(ei)
    return ei


def resubmit(db: Session, ei_id: str, actor: str = "local") -> EInvoice:
    """Correct-in-place resubmission after fixing the source transaction. Reuses the SAME record
    (no duplicate document), rebuilds the payload, revalidates, then submits when valid."""
    ei = _get(db, ei_id)
    if ei.status not in ("rejected", "validation_failed", "error", "ready", "pending"):
        raise EInvoiceError(f"An e-invoice in status '{ei.status}' cannot be resubmitted.")
    ei = generate(db, ei.source_type, ei.source_id, actor=actor)  # rebuild + revalidate same record
    _log(db, ei, "resubmitted", ei.status, ei.status, actor,
         detail="Source transaction corrected; document rebuilt for resubmission.")
    db.commit()
    if ei.system_validation_passed:
        return submit(db, ei.id, actor=actor)
    db.refresh(ei)
    return ei


def mark_corrected(db: Session, ei_id: str, replacement_source_type: str,
                   replacement_source_id: str, actor: str = "local") -> EInvoice:
    """Mark an accepted e-invoice as corrected/replaced by a NEW document (e.g. a credit note or
    a replacement invoice), preserving the Original → Adjustment relationship."""
    ei = _get(db, ei_id)
    frm = ei.status
    ei.status = "corrected"
    repl = generate(db, replacement_source_type, replacement_source_id, actor=actor)
    repl.replaces_einvoice_id = ei.id
    repl.original_einvoice_id = repl.original_einvoice_id or ei.id
    _log(db, ei, "corrected", frm, "corrected", actor,
         detail=f"Replaced by {repl.source_type}:{repl.doc_number}.")
    db.commit()
    db.refresh(ei)
    return ei


# ── Queries / serialization ─────────────────────────────────────────────────────────────────────
def _get(db: Session, ei_id: str) -> EInvoice:
    ei = db.get(EInvoice, ei_id)
    if not ei:
        raise EInvoiceError("E-invoice not found.")
    return ei


def get(db: Session, ei_id: str) -> EInvoice:
    return _get(db, ei_id)


def _allowed_actions(ei: EInvoice) -> list[str]:
    s = ei.status
    acts: list[str] = []
    if s in ("draft", "validation_failed", "ready", "error", "pending", "rejected"):
        acts.append("revalidate")
    if ei.system_validation_passed and s in ("ready", "pending", "rejected", "error", "validation_failed"):
        acts.append("submit")
    if s in ("rejected", "validation_failed", "error"):
        acts.append("resubmit")
    if s in ("submitted", "accepted", "pending"):
        acts.append("cancel")
    if s == "accepted":
        acts.append("correct")
    return acts


def summary(ei: EInvoice) -> dict:
    return {
        "id": ei.id, "source_type": ei.source_type, "source_id": ei.source_id,
        "source_label": SOURCE_META.get(ei.source_type, {}).get("label", ei.source_type),
        "doc_number": ei.doc_number, "direction": ei.direction, "doc_type_code": ei.doc_type_code,
        "party_name": ei.party_name, "party_trn": ei.party_trn, "party_class": ei.party_class,
        "currency": ei.currency, "net_total": str(ei.net_total), "vat_total": str(ei.vat_total),
        "grand_total": str(ei.grand_total), "status": ei.status,
        "status_label": STATUS_LABEL.get(ei.status, ei.status),
        "system_validation_passed": ei.system_validation_passed,
        "regulatory_confirmed": ei.regulatory_confirmed, "provisional": ei.provisional,
        "provider": ei.provider, "provider_ref": ei.provider_ref,
        "schema_id": ei.schema_id, "schema_version": ei.schema_version,
        "original_einvoice_id": ei.original_einvoice_id, "replaces_einvoice_id": ei.replaces_einvoice_id,
        "submitted_at": _iso(ei.submitted_at), "accepted_at": _iso(ei.accepted_at),
        "created_at": _iso(ei.created_at), "updated_at": _iso(ei.updated_at),
    }


def detail(db: Session, ei: EInvoice) -> dict:
    base = summary(ei)
    base["payload"] = json.loads(ei.payload_json) if ei.payload_json else None
    base["validation"] = json.loads(ei.validation_json) if ei.validation_json else None
    base["response"] = json.loads(ei.response_json) if ei.response_json else None
    base["allowed_actions"] = _allowed_actions(ei)
    base["compliance"] = {
        "system_validation_passed": ei.system_validation_passed,
        "regulatory_compliance_confirmed": ei.regulatory_confirmed,
        "provisional_notice": PROVISIONAL_NOTICE if ei.provisional else None,
    }
    base["events"] = [
        {"at": _iso(e.at), "actor": e.actor, "action": e.action,
         "from_status": e.from_status, "to_status": e.to_status, "detail": e.detail}
        for e in ei.events]
    return base


def list_einvoices(db: Session, status: str | None = None, source_type: str | None = None,
                   limit: int = 500) -> list[dict]:
    stmt = select(EInvoice).order_by(EInvoice.created_at.desc())
    if status:
        stmt = stmt.where(EInvoice.status == status)
    if source_type:
        stmt = stmt.where(EInvoice.source_type == source_type)
    return [summary(ei) for ei in db.execute(stmt.limit(limit)).scalars()]


# ── Dashboard (every number drills down to the underlying e-invoices) ───────────────────────────
def dashboard(db: Session) -> dict:
    rows = list(db.execute(select(EInvoice)).scalars())
    by_status: dict[str, int] = {}
    for ei in rows:
        by_status[ei.status] = by_status.get(ei.status, 0) + 1
    credit_notes = sum(1 for ei in rows if ei.doc_type_code == "credit_note")
    outstanding_errors = sum(
        len((json.loads(ei.validation_json).get("errors") or [])) if ei.validation_json else 0
        for ei in rows if ei.status in ("validation_failed", "error", "rejected"))
    cfg = get_config(db)
    cards = [
        {"key": "total", "label": "Total e-invoices", "value": len(rows), "filter": None},
        {"key": "submitted", "label": "Submitted", "value": by_status.get("submitted", 0), "filter": "submitted"},
        {"key": "accepted", "label": "Accepted", "value": by_status.get("accepted", 0), "filter": "accepted"},
        {"key": "rejected", "label": "Rejected", "value": by_status.get("rejected", 0), "filter": "rejected"},
        {"key": "pending", "label": "Pending", "value": by_status.get("pending", 0)
            + by_status.get("submitted", 0), "filter": "pending"},
        {"key": "validation_failed", "label": "Failed validation",
            "value": by_status.get("validation_failed", 0), "filter": "validation_failed"},
        {"key": "cancelled", "label": "Cancelled", "value": by_status.get("cancelled", 0), "filter": "cancelled"},
        {"key": "credit_notes", "label": "Credit notes", "value": credit_notes, "filter": None},
        {"key": "errors", "label": "Outstanding errors", "value": outstanding_errors, "filter": "error"},
    ]
    return {
        "cards": cards,
        "by_status": by_status,
        "enabled": cfg.enabled,
        "provider": cfg.provider,
        "environment": cfg.environment,
        "provisional": cfg.provisional,
        "compliance_notice": PROVISIONAL_NOTICE if cfg.provisional else None,
        "ruleset_validated": not cfg.provisional and bool(cfg.sme_validated_at),
        "sme": {"firm": cfg.sme_firm, "validator": cfg.sme_validator,
                "validated_at": _iso(cfg.sme_validated_at)},
        "schema": {"id": cfg.schema_id, "version": cfg.schema_version},
        "ruleset": {"version": cfg.ruleset_version, "source": cfg.ruleset_source,
                    "date": cfg.ruleset_date},
    }
