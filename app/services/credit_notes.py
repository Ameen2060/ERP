"""Customer & vendor credit notes and their application against invoices/bills.

  Customer CN posted   Dr Sales Returns/Revenue + Dr Output VAT / Cr Accounts Receivable
  Vendor CN posted     Dr Accounts Payable / Cr Expense·Inventory·Asset + Cr Input VAT

Posting reduces the receivable/payable and reverses revenue/cost + VAT. 'Application' is an
allocation of the resulting credit against a specific invoice/bill (no further GL entry — the
GL already moved at posting); it reduces that document's outstanding balance and the credit
note's unapplied balance. Unapplied balance = grand total − Σ applications.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .. import constants as C
from ..models import (
    Account,
    CreditNoteApplication,
    Customer,
    CustomerCreditNote,
    CustomerCreditNoteLine,
    SalesInvoice,
    Vendor,
    VendorBill,
    VendorCreditNote,
    VendorCreditNoteLine,
)
from ..schemas import JournalEntryIn, JournalLineIn, q
from . import calc, ledger


def _retention_acct_id(db: Session, cn, code: str) -> str:
    if cn.retention_account_id:
        return cn.retention_account_id
    return _acc_by_code(db, code).id

ZERO = Decimal("0.00")


class CreditNoteError(ValueError):
    """Domain error → HTTP 400."""


def _acc_by_code(db: Session, code: str) -> Account:
    a = db.execute(select(Account).where(Account.code == code)).scalar_one_or_none()
    if not a:
        raise CreditNoteError(f"Required account '{code}' is missing — seed the Chart of Accounts first.")
    return a


def _leaf(db: Session, account_id: str, label: str) -> Account:
    a = db.get(Account, account_id)
    if not a:
        raise CreditNoteError(f"{label} account not found.")
    if a.is_group:
        raise CreditNoteError(f"{label} account '{a.code}' is a group account.")
    return a


def _applied(db: Session, cn_type: str, cn_id: str) -> Decimal:
    total = db.execute(select(func.coalesce(func.sum(CreditNoteApplication.amount), 0)).where(
        CreditNoteApplication.cn_type == cn_type, CreditNoteApplication.cn_id == cn_id)).scalar()
    return q(total or 0)


def _next_num(db: Session, model, prefix: str) -> str:
    n = db.execute(select(func.count(model.id))).scalar() or 0
    return f"{prefix}-{n + 1:04d}"


def _lines_totals(data_lines):
    net = ZERO
    vat = ZERO
    parsed = []
    for i, ln in enumerate(data_lines):
        lnet = q(Decimal(str(ln.quantity)) * Decimal(str(ln.unit_price)))
        lvat = q(lnet * Decimal(str(ln.vat_rate)))
        parsed.append((i, ln, lnet, lvat))
        net += lnet
        vat += lvat
    return parsed, q(net), q(vat)


# ── Customer credit notes ─────────────────────────────────────────────────────────────────
def _cust_out(db: Session, cn: CustomerCreditNote) -> dict:
    cust = db.get(Customer, cn.customer_id)
    inv = db.get(SalesInvoice, cn.invoice_id) if cn.invoice_id else None
    codes = {a.id: a.code for a in db.execute(
        select(Account).where(Account.id.in_({l.revenue_account_id for l in cn.lines}))).scalars()} if cn.lines else {}
    applied = _applied(db, "customer", cn.id)
    return {
        "id": cn.id, "number": cn.number, "customer_id": cn.customer_id,
        "customer_name": cust.name if cust else None, "invoice_id": cn.invoice_id,
        "invoice_number": inv.number if inv else None, "date": str(cn.date), "reason": cn.reason,
        "description": cn.description, "currency": cn.currency, "project": cn.project,
        "contract_reference": cn.contract_reference, "net_total": str(cn.net_total),
        "vat_total": str(cn.vat_total), "grand_total": str(cn.grand_total),
        "applied": str(applied), "unapplied": str(q(cn.grand_total - applied)),
        "status": cn.status, "journal_entry_id": cn.journal_entry_id, "notes": cn.notes,
        "retention_applicable": cn.retention_applicable, "retention_amount": str(cn.retention_amount),
        "lines": [{"description": l.description, "quantity": str(l.quantity), "unit_price": str(l.unit_price),
                   "vat_rate": str(l.vat_rate), "revenue_account_id": l.revenue_account_id,
                   "revenue_account_code": codes.get(l.revenue_account_id),
                   "net_amount": str(l.net_amount), "vat_amount": str(l.vat_amount),
                   "line_total": str(l.line_total)} for l in cn.lines],
        "created_at": cn.created_at.isoformat() if cn.created_at else None,
    }


def _get_cust(db: Session, cn_id: str) -> CustomerCreditNote:
    cn = db.execute(select(CustomerCreditNote).where(CustomerCreditNote.id == cn_id)
                    .options(selectinload(CustomerCreditNote.lines))).scalar_one_or_none()
    if not cn:
        raise CreditNoteError("Customer credit note not found.")
    return cn


def create_customer_cn(db: Session, data) -> dict:
    if not db.get(Customer, data.customer_id):
        raise CreditNoteError("Customer not found.")
    if not data.reason:
        raise CreditNoteError("A reason for the credit is required.")
    if not data.lines:
        raise CreditNoteError("At least one line is required.")
    if data.invoice_id:
        inv = db.get(SalesInvoice, data.invoice_id)
        if not inv or inv.customer_id != data.customer_id:
            raise CreditNoteError("Linked invoice not found for this customer.")
    default_rev = _acc_by_code(db, C.CODE_SALES)
    parsed, net, vat = _lines_totals(data.lines)
    grand = q(net + vat)
    if data.invoice_id:
        inv = db.get(SalesInvoice, data.invoice_id)
        if grand > Decimal(inv.grand_total):
            raise CreditNoteError(f"Credit note {grand} exceeds the linked invoice total {inv.grand_total}.")
    cn = CustomerCreditNote(
        number=_next_num(db, CustomerCreditNote, "CN"), customer_id=data.customer_id,
        invoice_id=data.invoice_id, date=data.date, reason=data.reason, description=data.description,
        currency=data.currency, project=data.project, contract_reference=data.contract_reference,
        net_total=net, vat_total=vat, grand_total=grand, status="draft", notes=data.notes)
    if getattr(data, "retention_applicable", False):
        rs = calc.document_summary(subtotal=net, vat_amount=vat, retention_basis=data.retention_basis,
                                   retention_percent=data.retention_percent, retention_amount=data.retention_amount)
        ret = rs["retention"]
        if ret <= 0:
            raise CreditNoteError("Retention is enabled but computes to zero.")
        if ret > grand:
            raise CreditNoteError("Retention cannot exceed the credit note amount.")
        cn.retention_applicable = True
        cn.retention_basis = data.retention_basis
        cn.retention_percent = data.retention_percent
        cn.retention_amount = ret
        cn.retention_account_id = data.retention_account_id
    for i, ln, lnet, lvat in parsed:
        rev = _leaf(db, ln.revenue_account_id, "Revenue") if ln.revenue_account_id else default_rev
        cn.lines.append(CustomerCreditNoteLine(
            ordinal=i, description=ln.description, quantity=ln.quantity, unit_price=ln.unit_price,
            vat_rate=ln.vat_rate, revenue_account_id=rev.id, net_amount=lnet, vat_amount=lvat,
            line_total=q(lnet + lvat)))
    db.add(cn)
    db.flush()
    if getattr(data, "auto_post", True):
        _post_cust_je(db, cn)
        cn.status = "posted"
    db.commit()
    db.refresh(cn)
    return _cust_out(db, cn)


def _post_cust_je(db: Session, cn: CustomerCreditNote) -> None:
    ar = _acc_by_code(db, C.CODE_AR)
    vat_out = _acc_by_code(db, C.CODE_VAT_OUTPUT)
    rev_by: dict[str, Decimal] = {}
    for l in cn.lines:
        rev_by[l.revenue_account_id] = q(rev_by.get(l.revenue_account_id, ZERO) + l.net_amount)
    lines = [JournalLineIn(account_id=aid, debit=amt) for aid, amt in rev_by.items()]
    if cn.vat_total > 0:
        lines.append(JournalLineIn(account_id=vat_out.id, debit=cn.vat_total))
    retention = q(cn.retention_amount) if cn.retention_applicable else ZERO
    if retention > 0:
        lines.append(JournalLineIn(account_id=_retention_acct_id(db, cn, C.CODE_RETENTION_RECEIVABLE),
                                   credit=retention))
    lines.append(JournalLineIn(account_id=ar.id, credit=q(cn.grand_total - retention)))
    entry = ledger.create_journal_entry(db, JournalEntryIn(
        date=cn.date, memo=f"Customer credit note {cn.number}", reference=cn.number, source="sales",
        currency="AED", lines=lines, auto_post=True))
    cn.journal_entry_id = entry.id


def post_customer_cn(db: Session, cn_id: str) -> dict:
    cn = _get_cust(db, cn_id)
    if cn.status != "draft":
        raise CreditNoteError(f"Only draft credit notes can be posted (status is '{cn.status}').")
    _post_cust_je(db, cn)
    cn.status = "posted"
    db.commit()
    db.refresh(cn)
    return _cust_out(db, cn)


def void_customer_cn(db: Session, cn_id: str) -> dict:
    cn = _get_cust(db, cn_id)
    if _applied(db, "customer", cn.id) > 0:
        raise CreditNoteError("Cannot void a credit note that has been applied — reverse the applications first.")
    if cn.journal_entry_id:
        ledger.void_entry(db, cn.journal_entry_id)
    cn.status = "void"
    db.commit()
    db.refresh(cn)
    return _cust_out(db, cn)


def list_customer_cn(db: Session, customer_id: str | None = None) -> list[dict]:
    stmt = select(CustomerCreditNote).order_by(CustomerCreditNote.date.desc())
    if customer_id:
        stmt = stmt.where(CustomerCreditNote.customer_id == customer_id)
    return [_cust_out(db, cn) for cn in db.execute(stmt.options(selectinload(CustomerCreditNote.lines))).scalars()]


def get_customer_cn(db: Session, cn_id: str) -> dict:
    return _cust_out(db, _get_cust(db, cn_id))


def update_customer_cn(db: Session, cn_id: str, data, actor: str | None = None,
                       reason: str | None = None) -> dict:
    """Edit a customer credit note via reverse-and-repost (id + number preserved). Blocked once
    the note has been applied, and in a locked period."""
    from . import audit, system_settings
    cn = _get_cust(db, cn_id)
    if cn.status == "void":
        raise CreditNoteError("Cannot edit a voided credit note.")
    if _applied(db, "customer", cn.id) > 0:
        raise CreditNoteError("This credit note has been applied — reverse the applications before editing.")
    if not db.get(Customer, data.customer_id):
        raise CreditNoteError("Customer not found.")
    if not data.reason:
        raise CreditNoteError("A reason for the credit is required.")
    if not data.lines:
        raise CreditNoteError("At least one line is required.")
    system_settings.assert_period_open(db, cn.date, "edit")
    system_settings.assert_period_open(db, data.date, "edit")
    default_rev = _acc_by_code(db, C.CODE_SALES)
    parsed, net, vat = _lines_totals(data.lines)
    grand = q(net + vat)
    prev_status = cn.status
    before = {"date": str(cn.date), "net_total": str(cn.net_total), "vat_total": str(cn.vat_total),
              "grand_total": str(cn.grand_total), "lines": len(cn.lines)}
    if cn.journal_entry_id:
        ledger.void_entry(db, cn.journal_entry_id)
        cn.journal_entry_id = None
    cn.customer_id = data.customer_id
    cn.invoice_id = data.invoice_id
    cn.date = data.date
    cn.reason = data.reason
    cn.description = data.description
    cn.currency = data.currency
    cn.project = data.project
    cn.contract_reference = data.contract_reference
    cn.net_total = net
    cn.vat_total = vat
    cn.grand_total = grand
    cn.retention_applicable = False
    cn.retention_amount = ZERO
    if getattr(data, "retention_applicable", False):
        rs = calc.document_summary(subtotal=net, vat_amount=vat, retention_basis=data.retention_basis,
                                   retention_percent=data.retention_percent, retention_amount=data.retention_amount)
        ret = rs["retention"]
        if ret <= 0 or ret > grand:
            raise CreditNoteError("Retention is invalid for this credit note amount.")
        cn.retention_applicable = True
        cn.retention_basis = data.retention_basis
        cn.retention_percent = data.retention_percent
        cn.retention_amount = ret
        cn.retention_account_id = data.retention_account_id
    cn.lines.clear()
    db.flush()
    for i, ln, lnet, lvat in parsed:
        rev = _leaf(db, ln.revenue_account_id, "Revenue") if ln.revenue_account_id else default_rev
        cn.lines.append(CustomerCreditNoteLine(
            ordinal=i, description=ln.description, quantity=ln.quantity, unit_price=ln.unit_price,
            vat_rate=ln.vat_rate, revenue_account_id=rev.id, net_amount=lnet, vat_amount=lvat,
            line_total=q(lnet + lvat)))
    if prev_status == "posted":
        _post_cust_je(db, cn)
        cn.status = "posted"
    after = {"date": str(cn.date), "net_total": str(cn.net_total), "vat_total": str(cn.vat_total),
             "grand_total": str(cn.grand_total), "lines": len(parsed)}
    audit.record_txn_audit(db, entity_type="customer_cn", entity_id=cn.id, doc_number=cn.number,
                           actor=actor, action="edit", reason=reason, prev_status=prev_status,
                           new_status=cn.status, changes=audit.diff(before, after))
    db.commit()
    db.refresh(cn)
    return _cust_out(db, cn)


def customer_cn_audit(db: Session, cn_id: str) -> list[dict]:
    from . import audit
    return audit.list_txn_audit(db, "customer_cn", cn_id)


_CN_META = ("reason", "description", "project", "contract_reference", "notes")


def update_customer_cn_meta(db: Session, cn_id: str, data, actor: str | None = None,
                            reason: str | None = None) -> dict:
    """Edit a customer credit note's non-financial fields (reason/description/project/notes)
    without touching lines, amounts or the ledger — safe even when the note has been applied."""
    from . import audit
    cn = _get_cust(db, cn_id)
    if cn.status == "void":
        raise CreditNoteError("Cannot edit a voided credit note.")
    payload = data.model_dump(exclude_unset=True) if hasattr(data, "model_dump") else dict(data)
    before = {f: str(getattr(cn, f, None)) for f in _CN_META}
    for f in _CN_META:
        if f in payload:
            setattr(cn, f, payload[f])
    changes = audit.diff(before, {f: str(getattr(cn, f, None)) for f in _CN_META})
    audit.record_txn_audit(db, entity_type="customer_cn", entity_id=cn.id, doc_number=cn.number,
                           actor=actor, action="edit", reason=reason or "Edited details",
                           prev_status=cn.status, new_status=cn.status, changes=changes)
    db.commit()
    db.refresh(cn)
    return _cust_out(db, cn)


def update_vendor_cn_meta(db: Session, cn_id: str, data, actor: str | None = None,
                          reason: str | None = None) -> dict:
    """Edit a vendor credit note's non-financial fields without touching amounts/ledger."""
    from . import audit
    cn = _get_vend(db, cn_id)
    if cn.status == "void":
        raise CreditNoteError("Cannot edit a voided credit note.")
    payload = data.model_dump(exclude_unset=True) if hasattr(data, "model_dump") else dict(data)
    fields = _CN_META + ("vendor_ref",)
    before = {f: str(getattr(cn, f, None)) for f in fields}
    for f in fields:
        if f in payload:
            setattr(cn, f, payload[f])
    changes = audit.diff(before, {f: str(getattr(cn, f, None)) for f in fields})
    audit.record_txn_audit(db, entity_type="vendor_cn", entity_id=cn.id, doc_number=cn.number,
                           actor=actor, action="edit", reason=reason or "Edited details",
                           prev_status=cn.status, new_status=cn.status, changes=changes)
    db.commit()
    db.refresh(cn)
    return _vend_out(db, cn)


# ── Vendor credit notes ─────────────────────────────────────────────────────────────────────
def _vend_out(db: Session, cn: VendorCreditNote) -> dict:
    ven = db.get(Vendor, cn.vendor_id)
    bill = db.get(VendorBill, cn.bill_id) if cn.bill_id else None
    codes = {a.id: a.code for a in db.execute(
        select(Account).where(Account.id.in_({l.expense_account_id for l in cn.lines}))).scalars()} if cn.lines else {}
    applied = _applied(db, "vendor", cn.id)
    return {
        "id": cn.id, "number": cn.number, "vendor_id": cn.vendor_id,
        "vendor_name": ven.name if ven else None, "bill_id": cn.bill_id,
        "bill_number": bill.number if bill else None, "vendor_ref": cn.vendor_ref, "date": str(cn.date),
        "reason": cn.reason, "description": cn.description, "currency": cn.currency, "project": cn.project,
        "contract_reference": cn.contract_reference, "net_total": str(cn.net_total),
        "vat_total": str(cn.vat_total), "grand_total": str(cn.grand_total),
        "applied": str(applied), "unapplied": str(q(cn.grand_total - applied)),
        "status": cn.status, "journal_entry_id": cn.journal_entry_id, "notes": cn.notes,
        "retention_applicable": cn.retention_applicable, "retention_amount": str(cn.retention_amount),
        "lines": [{"description": l.description, "quantity": str(l.quantity), "unit_price": str(l.unit_price),
                   "vat_rate": str(l.vat_rate), "expense_account_id": l.expense_account_id,
                   "expense_account_code": codes.get(l.expense_account_id),
                   "net_amount": str(l.net_amount), "vat_amount": str(l.vat_amount),
                   "line_total": str(l.line_total)} for l in cn.lines],
        "created_at": cn.created_at.isoformat() if cn.created_at else None,
    }


def _get_vend(db: Session, cn_id: str) -> VendorCreditNote:
    cn = db.execute(select(VendorCreditNote).where(VendorCreditNote.id == cn_id)
                    .options(selectinload(VendorCreditNote.lines))).scalar_one_or_none()
    if not cn:
        raise CreditNoteError("Vendor credit note not found.")
    return cn


def create_vendor_cn(db: Session, data) -> dict:
    if not db.get(Vendor, data.vendor_id):
        raise CreditNoteError("Vendor not found.")
    if not data.reason:
        raise CreditNoteError("A reason for the credit is required.")
    if not data.lines:
        raise CreditNoteError("At least one line is required.")
    if data.bill_id:
        bill = db.get(VendorBill, data.bill_id)
        if not bill or bill.vendor_id != data.vendor_id:
            raise CreditNoteError("Linked bill not found for this vendor.")
    parsed, net, vat = _lines_totals(data.lines)
    grand = q(net + vat)
    if data.bill_id:
        bill = db.get(VendorBill, data.bill_id)
        if grand > Decimal(bill.grand_total):
            raise CreditNoteError(f"Credit note {grand} exceeds the linked bill total {bill.grand_total}.")
    cn = VendorCreditNote(
        number=_next_num(db, VendorCreditNote, "VCN"), vendor_id=data.vendor_id, bill_id=data.bill_id,
        vendor_ref=data.vendor_ref, date=data.date, reason=data.reason, description=data.description,
        currency=data.currency, project=data.project, contract_reference=data.contract_reference,
        net_total=net, vat_total=vat, grand_total=grand, status="draft", notes=data.notes)
    if getattr(data, "retention_applicable", False):
        rs = calc.document_summary(subtotal=net, vat_amount=vat, retention_basis=data.retention_basis,
                                   retention_percent=data.retention_percent, retention_amount=data.retention_amount)
        ret = rs["retention"]
        if ret <= 0:
            raise CreditNoteError("Retention is enabled but computes to zero.")
        if ret > grand:
            raise CreditNoteError("Retention cannot exceed the credit note amount.")
        cn.retention_applicable = True
        cn.retention_basis = data.retention_basis
        cn.retention_percent = data.retention_percent
        cn.retention_amount = ret
        cn.retention_account_id = data.retention_account_id
    for i, ln, lnet, lvat in parsed:
        acc = _leaf(db, ln.expense_account_id, "Expense")
        cn.lines.append(VendorCreditNoteLine(
            ordinal=i, description=ln.description, quantity=ln.quantity, unit_price=ln.unit_price,
            vat_rate=ln.vat_rate, expense_account_id=acc.id, net_amount=lnet, vat_amount=lvat,
            line_total=q(lnet + lvat)))
    db.add(cn)
    db.flush()
    if getattr(data, "auto_post", True):
        _post_vend_je(db, cn)
        cn.status = "posted"
    db.commit()
    db.refresh(cn)
    return _vend_out(db, cn)


def _post_vend_je(db: Session, cn: VendorCreditNote) -> None:
    ap = _acc_by_code(db, C.CODE_AP)
    vat_in = _acc_by_code(db, C.CODE_VAT_INPUT)
    exp_by: dict[str, Decimal] = {}
    for l in cn.lines:
        exp_by[l.expense_account_id] = q(exp_by.get(l.expense_account_id, ZERO) + l.net_amount)
    retention = q(cn.retention_amount) if cn.retention_applicable else ZERO
    lines = [JournalLineIn(account_id=ap.id, debit=q(cn.grand_total - retention))]
    if retention > 0:
        lines.append(JournalLineIn(account_id=_retention_acct_id(db, cn, C.CODE_RETENTION_PAYABLE),
                                   debit=retention))
    for aid, amt in exp_by.items():
        lines.append(JournalLineIn(account_id=aid, credit=amt))
    if cn.vat_total > 0:
        lines.append(JournalLineIn(account_id=vat_in.id, credit=cn.vat_total))
    entry = ledger.create_journal_entry(db, JournalEntryIn(
        date=cn.date, memo=f"Vendor credit note {cn.number}", reference=cn.vendor_ref or cn.number,
        source="purchase", currency="AED", lines=lines, auto_post=True))
    cn.journal_entry_id = entry.id


def post_vendor_cn(db: Session, cn_id: str) -> dict:
    cn = _get_vend(db, cn_id)
    if cn.status != "draft":
        raise CreditNoteError(f"Only draft credit notes can be posted (status is '{cn.status}').")
    _post_vend_je(db, cn)
    cn.status = "posted"
    db.commit()
    db.refresh(cn)
    return _vend_out(db, cn)


def void_vendor_cn(db: Session, cn_id: str) -> dict:
    cn = _get_vend(db, cn_id)
    if _applied(db, "vendor", cn.id) > 0:
        raise CreditNoteError("Cannot void a credit note that has been applied — reverse the applications first.")
    if cn.journal_entry_id:
        ledger.void_entry(db, cn.journal_entry_id)
    cn.status = "void"
    db.commit()
    db.refresh(cn)
    return _vend_out(db, cn)


def list_vendor_cn(db: Session, vendor_id: str | None = None) -> list[dict]:
    stmt = select(VendorCreditNote).order_by(VendorCreditNote.date.desc())
    if vendor_id:
        stmt = stmt.where(VendorCreditNote.vendor_id == vendor_id)
    return [_vend_out(db, cn) for cn in db.execute(stmt.options(selectinload(VendorCreditNote.lines))).scalars()]


def get_vendor_cn(db: Session, cn_id: str) -> dict:
    return _vend_out(db, _get_vend(db, cn_id))


def update_vendor_cn(db: Session, cn_id: str, data, actor: str | None = None,
                     reason: str | None = None) -> dict:
    """Edit a vendor credit note via reverse-and-repost (id + number preserved). Blocked once
    applied, and in a locked period."""
    from . import audit, system_settings
    cn = _get_vend(db, cn_id)
    if cn.status == "void":
        raise CreditNoteError("Cannot edit a voided credit note.")
    if _applied(db, "vendor", cn.id) > 0:
        raise CreditNoteError("This credit note has been applied — reverse the applications before editing.")
    if not db.get(Vendor, data.vendor_id):
        raise CreditNoteError("Vendor not found.")
    if not data.reason:
        raise CreditNoteError("A reason for the credit is required.")
    if not data.lines:
        raise CreditNoteError("At least one line is required.")
    system_settings.assert_period_open(db, cn.date, "edit")
    system_settings.assert_period_open(db, data.date, "edit")
    parsed, net, vat = _lines_totals(data.lines)
    grand = q(net + vat)
    prev_status = cn.status
    before = {"date": str(cn.date), "net_total": str(cn.net_total), "vat_total": str(cn.vat_total),
              "grand_total": str(cn.grand_total), "lines": len(cn.lines)}
    if cn.journal_entry_id:
        ledger.void_entry(db, cn.journal_entry_id)
        cn.journal_entry_id = None
    cn.vendor_id = data.vendor_id
    cn.bill_id = data.bill_id
    cn.vendor_ref = data.vendor_ref
    cn.date = data.date
    cn.reason = data.reason
    cn.description = data.description
    cn.currency = data.currency
    cn.project = data.project
    cn.contract_reference = data.contract_reference
    cn.net_total = net
    cn.vat_total = vat
    cn.grand_total = grand
    cn.retention_applicable = False
    cn.retention_amount = ZERO
    if getattr(data, "retention_applicable", False):
        rs = calc.document_summary(subtotal=net, vat_amount=vat, retention_basis=data.retention_basis,
                                   retention_percent=data.retention_percent, retention_amount=data.retention_amount)
        ret = rs["retention"]
        if ret <= 0 or ret > grand:
            raise CreditNoteError("Retention is invalid for this credit note amount.")
        cn.retention_applicable = True
        cn.retention_basis = data.retention_basis
        cn.retention_percent = data.retention_percent
        cn.retention_amount = ret
        cn.retention_account_id = data.retention_account_id
    cn.lines.clear()
    db.flush()
    for i, ln, lnet, lvat in parsed:
        acc = _leaf(db, ln.expense_account_id, "Expense")
        cn.lines.append(VendorCreditNoteLine(
            ordinal=i, description=ln.description, quantity=ln.quantity, unit_price=ln.unit_price,
            vat_rate=ln.vat_rate, expense_account_id=acc.id, net_amount=lnet, vat_amount=lvat,
            line_total=q(lnet + lvat)))
    if prev_status == "posted":
        _post_vend_je(db, cn)
        cn.status = "posted"
    after = {"date": str(cn.date), "net_total": str(cn.net_total), "vat_total": str(cn.vat_total),
             "grand_total": str(cn.grand_total), "lines": len(parsed)}
    audit.record_txn_audit(db, entity_type="vendor_cn", entity_id=cn.id, doc_number=cn.number,
                           actor=actor, action="edit", reason=reason, prev_status=prev_status,
                           new_status=cn.status, changes=audit.diff(before, after))
    db.commit()
    db.refresh(cn)
    return _vend_out(db, cn)


def vendor_cn_audit(db: Session, cn_id: str) -> list[dict]:
    from . import audit
    return audit.list_txn_audit(db, "vendor_cn", cn_id)


# ── Application ───────────────────────────────────────────────────────────────────────────
def _target_outstanding(doc) -> Decimal:
    retention_out = Decimal(doc.retention_amount) - Decimal(doc.retention_released)
    collectible = Decimal(doc.grand_total) - retention_out
    return q(collectible - Decimal(doc.amount_paid))


def apply_credit_note(db: Session, data) -> dict:
    ctype = data.cn_type
    if ctype not in ("customer", "vendor"):
        raise CreditNoteError("cn_type must be 'customer' or 'vendor'.")
    amt = q(data.amount)
    if amt <= 0:
        raise CreditNoteError("Application amount must be positive.")

    if ctype == "customer":
        cn = _get_cust(db, data.cn_id)
        if cn.status != "posted":
            raise CreditNoteError("Only a posted credit note can be applied.")
        inv = db.get(SalesInvoice, data.target_id)
        if not inv or inv.status not in ("posted", "partial"):
            raise CreditNoteError("Target invoice not found or not open.")
        if inv.customer_id != cn.customer_id:
            raise CreditNoteError("Credit note and invoice belong to different customers.")
        doc = inv
        retention_out = Decimal(inv.retention_amount) - Decimal(inv.retention_released)
        collectible = q(Decimal(inv.grand_total) - retention_out)
        target_num = inv.number
    else:
        cn = _get_vend(db, data.cn_id)
        if cn.status != "posted":
            raise CreditNoteError("Only a posted credit note can be applied.")
        bill = db.get(VendorBill, data.target_id)
        if not bill or bill.status not in ("posted", "partial"):
            raise CreditNoteError("Target bill not found or not open.")
        if bill.vendor_id != cn.vendor_id:
            raise CreditNoteError("Credit note and bill belong to different vendors.")
        doc = bill
        retention_out = Decimal(bill.retention_amount) - Decimal(bill.retention_released)
        collectible = q(Decimal(bill.grand_total) - retention_out)
        target_num = bill.number

    unapplied = q(Decimal(cn.grand_total) - _applied(db, ctype, cn.id))
    if amt > unapplied:
        raise CreditNoteError(f"Application {amt} exceeds the unapplied credit balance {unapplied}.")
    outstanding = _target_outstanding(doc)
    if amt > outstanding:
        raise CreditNoteError(f"Application {amt} exceeds the document outstanding {outstanding}.")

    # Allocation only — the GL already moved when the credit note was posted.
    doc.amount_paid = q(Decimal(doc.amount_paid) + amt)
    doc.status = "paid" if doc.amount_paid >= collectible else "partial"
    db.add(CreditNoteApplication(
        cn_type=ctype, cn_id=cn.id,
        target_type="sales_invoice" if ctype == "customer" else "vendor_bill",
        target_id=data.target_id, date=data.date, amount=amt))
    db.commit()
    return {"ok": True, "cn_id": cn.id, "target": target_num, "amount": str(amt),
            "remaining_credit": str(q(Decimal(cn.grand_total) - _applied(db, ctype, cn.id)))}


def list_applications(db: Session, cn_type: str, cn_id: str) -> list[dict]:
    apps = db.execute(select(CreditNoteApplication).where(
        CreditNoteApplication.cn_type == cn_type, CreditNoteApplication.cn_id == cn_id)
        .order_by(CreditNoteApplication.date)).scalars()
    out = []
    for a in apps:
        doc = (db.get(SalesInvoice, a.target_id) if a.target_type == "sales_invoice"
               else db.get(VendorBill, a.target_id))
        out.append({"id": a.id, "date": str(a.date), "amount": str(a.amount),
                    "target_type": a.target_type, "target_number": doc.number if doc else a.target_id})
    return out


# ── Reports ───────────────────────────────────────────────────────────────────────────────
def credit_note_report(db: Session, side: str = "customer") -> dict:
    rows = list_customer_cn(db) if side == "customer" else list_vendor_cn(db)
    rows = [r for r in rows if r["status"] != "void"]
    total = round(sum(float(r["grand_total"]) for r in rows), 2)
    applied = round(sum(float(r["applied"]) for r in rows), 2)
    unapplied = round(sum(float(r["unapplied"]) for r in rows), 2)
    vat = round(sum(float(r["vat_total"]) for r in rows), 2)
    return {"side": side, "rows": rows, "total": total, "applied": applied,
            "unapplied": unapplied, "vat": vat, "count": len(rows)}
