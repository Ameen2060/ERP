"""Customer & vendor advances and their application/recovery against invoices/bills.

  Customer advance received   Dr Bank·Cash / Cr Customer Advances (2160, liability)
  Apply to sales invoice       Dr Customer Advances / Cr Accounts Receivable
  Vendor advance paid          Dr Vendor Advances (1170, asset) / Cr Bank·Cash
  Apply to vendor bill         Dr Accounts Payable / Cr Vendor Advances

The available balance of an advance is amount − Σ(applications) — never stored as a duplicate.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import constants as C
from ..models import (
    Account,
    AdvanceApplication,
    Customer,
    CustomerAdvance,
    SalesInvoice,
    Vendor,
    VendorAdvance,
    VendorBill,
)
from ..schemas import JournalEntryIn, JournalLineIn, q
from . import ledger, system_settings

ZERO = Decimal("0.00")


class AdvanceError(ValueError):
    """Domain error → HTTP 400."""


def _split_vat(gross: Decimal, rate: Decimal) -> tuple[Decimal, Decimal]:
    """Split a VAT-inclusive gross into (net, vat) at the given rate."""
    net = q(Decimal(gross) / (Decimal(1) + Decimal(rate)))
    return net, q(Decimal(gross) - net)


def _acc_by_code(db: Session, code: str) -> Account:
    a = db.execute(select(Account).where(Account.code == code)).scalar_one_or_none()
    if not a:
        raise AdvanceError(f"Required account '{code}' is missing — seed the Chart of Accounts first.")
    return a


def _leaf(db: Session, account_id: str, label: str) -> Account:
    a = db.get(Account, account_id)
    if not a:
        raise AdvanceError(f"{label} account not found.")
    if a.is_group:
        raise AdvanceError(f"{label} account '{a.code}' is a group account.")
    return a


def _applied(db: Session, advance_type: str, advance_id: str) -> Decimal:
    total = db.execute(select(func.coalesce(func.sum(AdvanceApplication.amount), 0)).where(
        AdvanceApplication.advance_type == advance_type,
        AdvanceApplication.advance_id == advance_id)).scalar()
    return q(total or 0)


def _available(db: Session, advance_type: str, adv) -> Decimal:
    return q(Decimal(adv.amount) - _applied(db, advance_type, adv.id))


# ── Customer advances ───────────────────────────────────────────────────────────────────────
def _cust_out(db: Session, a: CustomerAdvance) -> dict:
    cust = db.get(Customer, a.customer_id)
    return {
        "id": a.id, "number": a.number, "customer_id": a.customer_id,
        "customer_name": cust.name if cust else None, "date": str(a.date), "reference": a.reference,
        "currency": a.currency, "amount": str(a.amount), "applied": str(_applied(db, "customer", a.id)),
        "available": str(_available(db, "customer", a)), "project": a.project,
        "contract_reference": a.contract_reference, "status": a.status,
        "vat_applicable": a.vat_applicable, "vat_rate": str(a.vat_rate),
        "vat_amount": str(a.vat_amount), "net_amount": str(a.net_amount),
        "tax_point_date": str(a.tax_point_date) if a.tax_point_date else None,
        "requires_sme_validation": bool(a.vat_applicable),
        "journal_entry_id": a.journal_entry_id, "notes": a.notes,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


def _next_num(db: Session, model, prefix: str) -> str:
    n = db.execute(select(func.count(model.id))).scalar() or 0
    return f"{prefix}-{n + 1:04d}"


def create_customer_advance(db: Session, data) -> dict:
    if not db.get(Customer, data.customer_id):
        raise AdvanceError("Customer not found.")
    if Decimal(data.amount) <= 0:
        raise AdvanceError("Advance amount must be positive.")
    deposit = _leaf(db, data.deposit_account_id, "Deposit") if data.deposit_account_id else _acc_by_code(db, C.CODE_BANK)
    adv_acct = _leaf(db, data.advance_account_id, "Advance") if data.advance_account_id else _acc_by_code(db, C.CODE_CUSTOMER_ADVANCES)
    gross = q(data.amount)
    vat_applicable = bool(getattr(data, "vat_applicable", False))
    if vat_applicable and not system_settings.advance_vat_allowed(db):
        raise AdvanceError("VAT on advances is OFF — enable it in System Settings (requires UAE VAT SME validation).")
    rate = Decimal(str(getattr(data, "vat_rate", 0) or 0))
    net, vat = _split_vat(gross, rate) if (vat_applicable and rate > 0) else (gross, ZERO)
    a = CustomerAdvance(
        number=_next_num(db, CustomerAdvance, "ADV-C"), customer_id=data.customer_id, date=data.date,
        reference=data.reference, currency=data.currency, amount=gross,
        vat_applicable=vat_applicable, vat_rate=rate, vat_amount=vat, net_amount=net,
        tax_point_date=(data.date if vat_applicable else None),
        deposit_account_id=deposit.id, advance_account_id=adv_acct.id, project=data.project,
        contract_reference=data.contract_reference, notes=data.notes, status="posted")
    db.add(a)
    db.flush()
    # Advance receipt: Dr Bank(gross) / Cr Customer Advances(net) [+ Cr Output VAT(vat) if VAT-on-advance]
    lines = [JournalLineIn(account_id=deposit.id, debit=gross),
             JournalLineIn(account_id=adv_acct.id, credit=net)]
    if vat > 0:
        lines.append(JournalLineIn(account_id=_acc_by_code(db, C.CODE_VAT_OUTPUT).id, credit=vat))
    entry = ledger.create_journal_entry(db, JournalEntryIn(
        date=a.date, memo=f"Customer advance {a.number}" + (" (VAT on advance)" if vat > 0 else ""),
        reference=a.reference or a.number, source="sales", currency="AED", lines=lines, auto_post=True))
    a.journal_entry_id = entry.id
    db.commit()
    db.refresh(a)
    return _cust_out(db, a)


def _rebuild_customer_advance_je(db: Session, a: CustomerAdvance) -> None:
    deposit = db.get(Account, a.deposit_account_id)
    adv_acct = db.get(Account, a.advance_account_id)
    lines = [JournalLineIn(account_id=deposit.id, debit=Decimal(a.amount)),
             JournalLineIn(account_id=adv_acct.id, credit=Decimal(a.net_amount))]
    if Decimal(a.vat_amount) > 0:
        lines.append(JournalLineIn(account_id=_acc_by_code(db, C.CODE_VAT_OUTPUT).id, credit=Decimal(a.vat_amount)))
    entry = ledger.create_journal_entry(db, JournalEntryIn(
        date=a.date, memo=f"Customer advance {a.number}" + (" (VAT on advance)" if Decimal(a.vat_amount) > 0 else ""),
        reference=a.reference or a.number, source="sales", currency="AED", lines=lines, auto_post=True))
    a.journal_entry_id = entry.id


def update_customer_advance(db: Session, advance_id: str, data, actor: str | None = None,
                            reason: str | None = None) -> dict:
    """Edit a customer advance via reverse-and-repost of the receipt entry (number preserved).
    Blocked once any part is applied (unapply first) and in a locked period."""
    from . import audit
    a = db.get(CustomerAdvance, advance_id)
    if not a or a.status != "posted":
        raise AdvanceError("Customer advance not found.")
    if _applied(db, "customer", a.id) > 0:
        raise AdvanceError("This advance has been applied — unapply it before editing.")
    system_settings.assert_period_open(db, a.date, "edit")
    system_settings.assert_period_open(db, data.date, "edit")
    if Decimal(data.amount) <= 0:
        raise AdvanceError("Advance amount must be positive.")
    vat_applicable = bool(getattr(data, "vat_applicable", False))
    if vat_applicable and not system_settings.advance_vat_allowed(db):
        raise AdvanceError("VAT on advances is OFF — enable it in System Settings (requires SME validation).")
    before = {"date": str(a.date), "amount": str(a.amount), "reference": a.reference,
              "vat_applicable": str(a.vat_applicable), "vat_amount": str(a.vat_amount)}
    if a.journal_entry_id:
        ledger.void_entry(db, a.journal_entry_id)
        a.journal_entry_id = None
    gross = q(data.amount)
    rate = Decimal(str(getattr(data, "vat_rate", 0) or 0))
    net, vat = _split_vat(gross, rate) if (vat_applicable and rate > 0) else (gross, ZERO)
    if getattr(data, "customer_id", None):
        a.customer_id = data.customer_id
    a.date = data.date
    a.reference = data.reference
    a.currency = data.currency
    a.amount = gross
    a.vat_applicable = vat_applicable
    a.vat_rate = rate
    a.vat_amount = vat
    a.net_amount = net
    a.tax_point_date = data.date if vat_applicable else None
    if getattr(data, "deposit_account_id", None):
        a.deposit_account_id = _leaf(db, data.deposit_account_id, "Deposit").id
    a.project = data.project
    a.contract_reference = data.contract_reference
    a.notes = data.notes
    _rebuild_customer_advance_je(db, a)
    after = {"date": str(a.date), "amount": str(a.amount), "reference": a.reference,
             "vat_applicable": str(a.vat_applicable), "vat_amount": str(a.vat_amount)}
    audit.record_txn_audit(db, entity_type="customer_advance", entity_id=a.id, doc_number=a.number,
                           actor=actor, action="edit", reason=reason, prev_status="posted",
                           new_status="posted", changes=audit.diff(before, after))
    db.commit()
    db.refresh(a)
    return _cust_out(db, a)


def list_customer_advances(db: Session, customer_id: str | None = None, only_available: bool = False) -> list[dict]:
    stmt = select(CustomerAdvance).where(CustomerAdvance.status == "posted").order_by(CustomerAdvance.date.desc())
    if customer_id:
        stmt = stmt.where(CustomerAdvance.customer_id == customer_id)
    out = [_cust_out(db, a) for a in db.execute(stmt).scalars()]
    return [o for o in out if Decimal(o["available"]) > 0] if only_available else out


# ── Vendor advances ─────────────────────────────────────────────────────────────────────────
def _vend_out(db: Session, a: VendorAdvance) -> dict:
    ven = db.get(Vendor, a.vendor_id)
    return {
        "id": a.id, "number": a.number, "vendor_id": a.vendor_id,
        "vendor_name": ven.name if ven else None, "date": str(a.date), "reference": a.reference,
        "currency": a.currency, "amount": str(a.amount), "applied": str(_applied(db, "vendor", a.id)),
        "available": str(_available(db, "vendor", a)), "project": a.project,
        "contract_reference": a.contract_reference, "status": a.status,
        "vat_applicable": a.vat_applicable, "vat_rate": str(a.vat_rate),
        "vat_amount": str(a.vat_amount), "net_amount": str(a.net_amount),
        "tax_point_date": str(a.tax_point_date) if a.tax_point_date else None,
        "requires_sme_validation": bool(a.vat_applicable),
        "journal_entry_id": a.journal_entry_id, "notes": a.notes,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


def create_vendor_advance(db: Session, data) -> dict:
    if not db.get(Vendor, data.vendor_id):
        raise AdvanceError("Vendor not found.")
    if Decimal(data.amount) <= 0:
        raise AdvanceError("Advance amount must be positive.")
    pay = _leaf(db, data.payment_account_id, "Payment") if data.payment_account_id else _acc_by_code(db, C.CODE_BANK)
    adv_acct = _leaf(db, data.advance_account_id, "Advance") if data.advance_account_id else _acc_by_code(db, C.CODE_VENDOR_ADVANCES)
    gross = q(data.amount)
    vat_applicable = bool(getattr(data, "vat_applicable", False))
    if vat_applicable and not system_settings.advance_vat_allowed(db):
        raise AdvanceError("VAT on advances is OFF — enable it in System Settings (requires UAE VAT SME validation).")
    rate = Decimal(str(getattr(data, "vat_rate", 0) or 0))
    net, vat = _split_vat(gross, rate) if (vat_applicable and rate > 0) else (gross, ZERO)
    a = VendorAdvance(
        number=_next_num(db, VendorAdvance, "ADV-V"), vendor_id=data.vendor_id, date=data.date,
        reference=data.reference, currency=data.currency, amount=gross,
        vat_applicable=vat_applicable, vat_rate=rate, vat_amount=vat, net_amount=net,
        tax_point_date=(data.date if vat_applicable else None),
        payment_account_id=pay.id, advance_account_id=adv_acct.id, project=data.project,
        contract_reference=data.contract_reference, notes=data.notes, status="posted")
    db.add(a)
    db.flush()
    # Advance paid: Dr Vendor Advances(net) [+ Dr Input VAT(vat)] / Cr Bank(gross)
    lines = [JournalLineIn(account_id=adv_acct.id, debit=net)]
    if vat > 0:
        lines.append(JournalLineIn(account_id=_acc_by_code(db, C.CODE_VAT_INPUT).id, debit=vat))
    lines.append(JournalLineIn(account_id=pay.id, credit=gross))
    entry = ledger.create_journal_entry(db, JournalEntryIn(
        date=a.date, memo=f"Vendor advance {a.number}" + (" (VAT on advance)" if vat > 0 else ""),
        reference=a.reference or a.number, source="purchase", currency="AED", lines=lines, auto_post=True))
    a.journal_entry_id = entry.id
    db.commit()
    db.refresh(a)
    return _vend_out(db, a)


def _rebuild_vendor_advance_je(db: Session, a: VendorAdvance) -> None:
    pay = db.get(Account, a.payment_account_id)
    adv_acct = db.get(Account, a.advance_account_id)
    lines = [JournalLineIn(account_id=adv_acct.id, debit=Decimal(a.net_amount))]
    if Decimal(a.vat_amount) > 0:
        lines.append(JournalLineIn(account_id=_acc_by_code(db, C.CODE_VAT_INPUT).id, debit=Decimal(a.vat_amount)))
    lines.append(JournalLineIn(account_id=pay.id, credit=Decimal(a.amount)))
    entry = ledger.create_journal_entry(db, JournalEntryIn(
        date=a.date, memo=f"Vendor advance {a.number}" + (" (VAT on advance)" if Decimal(a.vat_amount) > 0 else ""),
        reference=a.reference or a.number, source="purchase", currency="AED", lines=lines, auto_post=True))
    a.journal_entry_id = entry.id


def update_vendor_advance(db: Session, advance_id: str, data, actor: str | None = None,
                          reason: str | None = None) -> dict:
    """Edit a vendor advance via reverse-and-repost of the payment entry (number preserved).
    Blocked once applied (unapply first) and in a locked period."""
    from . import audit
    a = db.get(VendorAdvance, advance_id)
    if not a or a.status != "posted":
        raise AdvanceError("Vendor advance not found.")
    if _applied(db, "vendor", a.id) > 0:
        raise AdvanceError("This advance has been applied — unapply it before editing.")
    system_settings.assert_period_open(db, a.date, "edit")
    system_settings.assert_period_open(db, data.date, "edit")
    if Decimal(data.amount) <= 0:
        raise AdvanceError("Advance amount must be positive.")
    vat_applicable = bool(getattr(data, "vat_applicable", False))
    if vat_applicable and not system_settings.advance_vat_allowed(db):
        raise AdvanceError("VAT on advances is OFF — enable it in System Settings (requires SME validation).")
    before = {"date": str(a.date), "amount": str(a.amount), "reference": a.reference,
              "vat_applicable": str(a.vat_applicable), "vat_amount": str(a.vat_amount)}
    if a.journal_entry_id:
        ledger.void_entry(db, a.journal_entry_id)
        a.journal_entry_id = None
    gross = q(data.amount)
    rate = Decimal(str(getattr(data, "vat_rate", 0) or 0))
    net, vat = _split_vat(gross, rate) if (vat_applicable and rate > 0) else (gross, ZERO)
    if getattr(data, "vendor_id", None):
        a.vendor_id = data.vendor_id
    a.date = data.date
    a.reference = data.reference
    a.currency = data.currency
    a.amount = gross
    a.vat_applicable = vat_applicable
    a.vat_rate = rate
    a.vat_amount = vat
    a.net_amount = net
    a.tax_point_date = data.date if vat_applicable else None
    if getattr(data, "payment_account_id", None):
        a.payment_account_id = _leaf(db, data.payment_account_id, "Payment").id
    a.project = data.project
    a.contract_reference = data.contract_reference
    a.notes = data.notes
    _rebuild_vendor_advance_je(db, a)
    after = {"date": str(a.date), "amount": str(a.amount), "reference": a.reference,
             "vat_applicable": str(a.vat_applicable), "vat_amount": str(a.vat_amount)}
    audit.record_txn_audit(db, entity_type="vendor_advance", entity_id=a.id, doc_number=a.number,
                           actor=actor, action="edit", reason=reason, prev_status="posted",
                           new_status="posted", changes=audit.diff(before, after))
    db.commit()
    db.refresh(a)
    return _vend_out(db, a)


def get_customer_advance(db: Session, advance_id: str) -> dict:
    a = db.get(CustomerAdvance, advance_id)
    if not a:
        raise AdvanceError("Customer advance not found.")
    return _cust_out(db, a)


def get_vendor_advance(db: Session, advance_id: str) -> dict:
    a = db.get(VendorAdvance, advance_id)
    if not a:
        raise AdvanceError("Vendor advance not found.")
    return _vend_out(db, a)


def list_vendor_advances(db: Session, vendor_id: str | None = None, only_available: bool = False) -> list[dict]:
    stmt = select(VendorAdvance).where(VendorAdvance.status == "posted").order_by(VendorAdvance.date.desc())
    if vendor_id:
        stmt = stmt.where(VendorAdvance.vendor_id == vendor_id)
    out = [_vend_out(db, a) for a in db.execute(stmt).scalars()]
    return [o for o in out if Decimal(o["available"]) > 0] if only_available else out


# ── Application / recovery ────────────────────────────────────────────────────────────────
def _target_outstanding(doc) -> Decimal:
    retention_out = Decimal(doc.retention_amount) - Decimal(doc.retention_released)
    collectible = Decimal(doc.grand_total) - retention_out
    return q(collectible - Decimal(doc.amount_paid))


def apply_advance(db: Session, data, actor: str | None = None) -> dict:
    atype = data.advance_type
    if atype not in ("customer", "vendor"):
        raise AdvanceError("advance_type must be 'customer' or 'vendor'.")
    amt = q(data.amount)
    if amt <= 0:
        raise AdvanceError("Application amount must be positive.")

    if atype == "customer":
        adv = db.get(CustomerAdvance, data.advance_id)
        if not adv or adv.status != "posted":
            raise AdvanceError("Customer advance not found.")
        inv = db.get(SalesInvoice, data.target_id)
        if not inv or inv.status not in ("posted", "partial"):
            raise AdvanceError("Target invoice not found or not posted.")
        if inv.customer_id != adv.customer_id:
            raise AdvanceError("Advance and invoice belong to different customers.")
        available = _available(db, "customer", adv)
        if amt > available:
            raise AdvanceError(f"Recovery {amt} exceeds available advance balance {available}.")
        outstanding = _target_outstanding(inv)
        if amt > outstanding:
            raise AdvanceError(f"Recovery {amt} exceeds the invoice outstanding {outstanding}.")
        adv_acct = adv.advance_account_id or _acc_by_code(db, C.CODE_CUSTOMER_ADVANCES).id
        ar = _acc_by_code(db, C.CODE_AR)
        # Reduce AR by the gross applied. If the advance carried VAT at receipt, reverse that
        # advance-stage output VAT here so it isn't double-counted against the invoice's own VAT.
        if adv.vat_applicable and Decimal(adv.vat_rate) > 0:
            net, vat = _split_vat(amt, Decimal(adv.vat_rate))
            lines = [JournalLineIn(account_id=adv_acct, debit=net),
                     JournalLineIn(account_id=_acc_by_code(db, C.CODE_VAT_OUTPUT).id, debit=vat),
                     JournalLineIn(account_id=ar.id, credit=amt)]
        else:
            lines = [JournalLineIn(account_id=adv_acct, debit=amt),
                     JournalLineIn(account_id=ar.id, credit=amt)]
        entry = ledger.create_journal_entry(db, JournalEntryIn(
            date=data.date, memo=f"Advance {adv.number} applied to {inv.number}",
            reference=adv.number, source="sales", currency="AED", lines=lines, auto_post=True))
        inv.amount_paid = q(Decimal(inv.amount_paid) + amt)
        collectible = q(Decimal(inv.grand_total) - (Decimal(inv.retention_amount) - Decimal(inv.retention_released)))
        inv.status = "paid" if inv.amount_paid >= collectible else "partial"
        target_num = inv.number
    else:
        adv = db.get(VendorAdvance, data.advance_id)
        if not adv or adv.status != "posted":
            raise AdvanceError("Vendor advance not found.")
        bill = db.get(VendorBill, data.target_id)
        if not bill or bill.status not in ("posted", "partial"):
            raise AdvanceError("Target bill not found or not posted.")
        if bill.vendor_id != adv.vendor_id:
            raise AdvanceError("Advance and bill belong to different vendors.")
        available = _available(db, "vendor", adv)
        if amt > available:
            raise AdvanceError(f"Application {amt} exceeds available advance balance {available}.")
        outstanding = _target_outstanding(bill)
        if amt > outstanding:
            raise AdvanceError(f"Application {amt} exceeds the bill outstanding {outstanding}.")
        adv_acct = adv.advance_account_id or _acc_by_code(db, C.CODE_VENDOR_ADVANCES).id
        ap = _acc_by_code(db, C.CODE_AP)
        if adv.vat_applicable and Decimal(adv.vat_rate) > 0:
            net, vat = _split_vat(amt, Decimal(adv.vat_rate))
            lines = [JournalLineIn(account_id=ap.id, debit=amt),
                     JournalLineIn(account_id=adv_acct, credit=net),
                     JournalLineIn(account_id=_acc_by_code(db, C.CODE_VAT_INPUT).id, credit=vat)]
        else:
            lines = [JournalLineIn(account_id=ap.id, debit=amt),
                     JournalLineIn(account_id=adv_acct, credit=amt)]
        entry = ledger.create_journal_entry(db, JournalEntryIn(
            date=data.date, memo=f"Advance {adv.number} applied to bill {bill.number}",
            reference=adv.number, source="purchase", currency="AED", lines=lines, auto_post=True))
        bill.amount_paid = q(Decimal(bill.amount_paid) + amt)
        payable = q(Decimal(bill.grand_total) - (Decimal(bill.retention_amount) - Decimal(bill.retention_released)))
        bill.status = "paid" if bill.amount_paid >= payable else "partial"
        target_num = bill.number

    app = AdvanceApplication(
        advance_type=atype, advance_id=adv.id,
        target_type="sales_invoice" if atype == "customer" else "vendor_bill",
        target_id=data.target_id, date=data.date, amount=amt, journal_entry_id=entry.id)
    db.add(app)
    from . import audit
    audit.record_txn_audit(db, entity_type=("customer_advance" if atype == "customer" else "vendor_advance"),
                           entity_id=adv.id, doc_number=adv.number, actor=actor, action="apply",
                           reason=f"Applied {amt} to {target_num}", prev_status="posted", new_status="posted",
                           changes=[{"field": "applied_to", "old": None, "new": target_num},
                                    {"field": "amount", "old": None, "new": str(amt)}])
    db.commit()
    return {"ok": True, "advance_id": adv.id, "target": target_num, "amount": str(amt),
            "remaining_advance": str(_available(db, atype, adv))}


def unapply_advance(db: Session, application_id: str, actor: str | None = None,
                    reason: str | None = None) -> dict:
    """Reverse an advance application (unapplication / reallocation). Voids the application's
    journal entry, restores the target's outstanding balance and the advance's available
    balance, and records an audit entry. Does NOT touch the original advance receipt/payment."""
    from . import audit
    app = db.get(AdvanceApplication, application_id)
    if not app:
        raise AdvanceError("Advance application not found.")
    system_settings.assert_period_open(db, app.date, "reverse")
    if app.journal_entry_id:
        ledger.void_entry(db, app.journal_entry_id)   # reverse the GL effect of the application
    amt = q(app.amount)
    if app.target_type == "sales_invoice":
        doc = db.get(SalesInvoice, app.target_id)
        adv = db.get(CustomerAdvance, app.advance_id)
        num = doc.number if doc else app.target_id
    else:
        doc = db.get(VendorBill, app.target_id)
        adv = db.get(VendorAdvance, app.advance_id)
        num = doc.number if doc else app.target_id
    if doc:
        doc.amount_paid = q(max(Decimal(doc.amount_paid) - amt, ZERO))
        collectible = q(Decimal(doc.grand_total) - (Decimal(doc.retention_amount) - Decimal(doc.retention_released)))
        doc.status = "paid" if doc.amount_paid >= collectible else ("partial" if doc.amount_paid > 0 else "posted")
    audit.record_txn_audit(db, entity_type=("customer_advance" if app.advance_type == "customer" else "vendor_advance"),
                           entity_id=app.advance_id, doc_number=(adv.number if adv else None), actor=actor,
                           action="reverse", reason=reason or f"Unapplied {amt} from {num}",
                           prev_status="posted", new_status="posted",
                           changes=[{"field": "unapplied_from", "old": num, "new": None},
                                    {"field": "amount", "old": str(amt), "new": None}])
    db.delete(app)
    db.commit()
    return {"ok": True, "advance_id": app.advance_id, "target": num, "amount": str(amt),
            "remaining_advance": str(_available(db, app.advance_type, adv)) if adv else "0.00"}


def list_applications(db: Session, advance_type: str, advance_id: str) -> list[dict]:
    apps = db.execute(select(AdvanceApplication).where(
        AdvanceApplication.advance_type == advance_type,
        AdvanceApplication.advance_id == advance_id).order_by(AdvanceApplication.date)).scalars()
    out = []
    for a in apps:
        if a.target_type == "sales_invoice":
            doc = db.get(SalesInvoice, a.target_id); num = doc.number if doc else a.target_id
        else:
            doc = db.get(VendorBill, a.target_id); num = doc.number if doc else a.target_id
        out.append({"id": a.id, "date": str(a.date), "amount": str(a.amount),
                    "target_type": a.target_type, "target_id": a.target_id, "target_number": num,
                    "journal_entry_id": a.journal_entry_id})
    return out


def applications_for_target(db: Session, target_type: str, target_id: str) -> list[dict]:
    """Advances applied to a given invoice/bill — powers the Invoice/Bill → Advance drill-down."""
    apps = db.execute(select(AdvanceApplication).where(
        AdvanceApplication.target_type == target_type,
        AdvanceApplication.target_id == target_id).order_by(AdvanceApplication.date)).scalars()
    out = []
    for a in apps:
        if a.advance_type == "customer":
            adv = db.get(CustomerAdvance, a.advance_id)
        else:
            adv = db.get(VendorAdvance, a.advance_id)
        out.append({"id": a.id, "date": str(a.date), "amount": str(a.amount),
                    "advance_type": a.advance_type, "advance_id": a.advance_id,
                    "advance_number": adv.number if adv else a.advance_id,
                    "journal_entry_id": a.journal_entry_id})
    return out


def advance_audit(db: Session, side: str, advance_id: str) -> list[dict]:
    from . import audit
    et = "customer_advance" if side == "customer" else "vendor_advance"
    return audit.list_txn_audit(db, et, advance_id)


# ── Reports ───────────────────────────────────────────────────────────────────────────────
def advance_report(db: Session, side: str = "customer") -> dict:
    advs = (list_customer_advances(db) if side == "customer" else list_vendor_advances(db))
    total = round(sum(float(a["amount"]) for a in advs), 2)
    applied = round(sum(float(a["applied"]) for a in advs), 2)
    available = round(sum(float(a["available"]) for a in advs), 2)
    return {"side": side, "rows": advs, "total": total, "applied": applied, "available": available,
            "unapplied_count": sum(1 for a in advs if float(a["available"]) > 0)}


def advance_application_details(db: Session, side: str = "customer") -> dict:
    """Every advance application for the side, with advance# + target# for drill-down."""
    advs = (list_customer_advances(db) if side == "customer" else list_vendor_advances(db))
    party_key = "customer_name" if side == "customer" else "vendor_name"
    rows = []
    for adv in advs:
        for app in list_applications(db, side, adv["id"]):
            rows.append({"advance_number": adv["number"], "advance_id": adv["id"],
                         "party": adv.get(party_key), "date": app["date"], "amount": app["amount"],
                         "target_type": app["target_type"], "target_id": app["target_id"],
                         "target_number": app["target_number"], "journal_entry_id": app["journal_entry_id"]})
    rows.sort(key=lambda r: r["date"])
    return {"side": side, "rows": rows, "total": round(sum(float(r["amount"]) for r in rows), 2),
            "count": len(rows)}


def advance_aging(db: Session, side: str = "customer", as_of=None) -> dict:
    """Outstanding (unapplied) advances bucketed by age — current/31-60/61-90/90+."""
    from datetime import date as _D
    ref = as_of or _D.today()
    advs = [a for a in (list_customer_advances(db) if side == "customer" else list_vendor_advances(db))
            if float(a["available"]) > 0]
    buckets = {"current": 0.0, "31_60": 0.0, "61_90": 0.0, "over_90": 0.0}
    for a in advs:
        try:
            d = _D.fromisoformat(a["date"])
            days = (ref - d).days
        except Exception:  # noqa: BLE001
            days = 0
        avail = float(a["available"])
        key = "current" if days <= 30 else ("31_60" if days <= 60 else ("61_90" if days <= 90 else "over_90"))
        buckets[key] += avail
        a["age_days"] = days
        a["bucket"] = key
    return {"side": side, "as_of": str(ref), "rows": advs,
            "buckets": {k: round(v, 2) for k, v in buckets.items()},
            "total_outstanding": round(sum(float(a["available"]) for a in advs), 2)}
