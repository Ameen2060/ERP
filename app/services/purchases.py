"""Purchases / Accounts Payable service: vendors, supplier bills, bill payments.

Posting rules (balanced double-entry, source='purchase'):

  Post bill      Dr Expense / Inventory / Fixed-Asset account(s) (net, per line)
                 Dr VAT Recoverable (input VAT)
                 Cr Accounts Payable (grand total)

  Pay bill       Dr Accounts Payable (amount)
                 Cr Bank / Cash (amount)
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .. import constants as C
from ..models import Account, BillPayment, Vendor, VendorBill, VendorBillLine
from ..schemas import (
    BillIn,
    BillLineOut,
    BillOut,
    BillPaymentIn,
    BillPaymentOut,
    BillSummary,
    JournalEntryIn,
    JournalLineIn,
    VendorIn,
    VendorOut,
    q,
)
from . import audit, calc, ledger, validation

ZERO = Decimal(0)


class PurchaseError(ValueError):
    """Domain error → HTTP 400."""


def _account_by_code(db: Session, code: str) -> Account:
    acct = db.execute(select(Account).where(Account.code == code)).scalar_one_or_none()
    if not acct:
        raise PurchaseError(f"Required account '{code}' is missing — seed the Chart of Accounts first.")
    return acct


def _leaf_or_error(db: Session, account_id: str) -> Account:
    a = db.get(Account, account_id)
    if not a:
        raise PurchaseError("Account not found.")
    if a.is_group:
        raise PurchaseError(f"Account '{a.code}' is a group account and cannot be posted to.")
    return a


def _retention_account(db: Session, bill: VendorBill) -> Account:
    if bill.retention_account_id:
        return _leaf_or_error(db, bill.retention_account_id)
    return _account_by_code(db, C.CODE_RETENTION_PAYABLE)


# ── Vendors ─────────────────────────────────────────────────────────────────────────────
_VENDOR_FIELDS = ("name", "trn", "email", "phone", "contact_name", "address",
                  "billing_address", "currency", "payment_terms", "country", "tax_status",
                  "party_type", "einvoice_scheme", "einvoice_id", "notes")


def _vendor_out(v: Vendor) -> VendorOut:
    billing = v.billing_address or v.address
    return VendorOut(
        id=v.id, name=v.name, trn=v.trn, email=v.email, phone=v.phone,
        contact_name=v.contact_name, address=v.address, billing_address=v.billing_address,
        currency=v.currency, payment_terms=v.payment_terms,
        country=v.country or "AE", tax_status=v.tax_status or "unknown",
        party_type=v.party_type or "b2b", einvoice_scheme=v.einvoice_scheme,
        einvoice_id=v.einvoice_id, notes=v.notes, is_active=v.is_active,
        warnings=validation.party_warnings(v.trn, billing, party="Vendor"),
        created_at=v.created_at.isoformat() if v.created_at else None,
    )


def _dup_vendor_name(db: Session, name: str, exclude_id: str | None = None) -> bool:
    stmt = select(Vendor).where(func.lower(Vendor.name) == name.strip().lower())
    if exclude_id:
        stmt = stmt.where(Vendor.id != exclude_id)
    return db.execute(stmt).scalars().first() is not None


def create_vendor(db: Session, data: VendorIn) -> VendorOut:
    payload = data.model_dump()
    payload["trn"] = validation.validate_trn(payload.get("trn"), label="Vendor TRN")
    if not (payload.get("name") or "").strip():
        raise PurchaseError("Vendor name is required.")
    v = Vendor(**payload)
    db.add(v)
    db.flush()
    audit.record_profile_change(db, entity_type="vendor", entity_id=v.id, entity_label=v.name,
                                actor=None, changes=[], action="create")
    db.commit()
    db.refresh(v)
    return _vendor_out(v)


def update_vendor(db: Session, vendor_id: str, data: VendorIn,
                  actor: str | None = None) -> VendorOut:
    """Edit a vendor profile without changing posted bills / credit notes / payments (they
    reference the vendor by id). Writes a field-level audit trail."""
    v = db.get(Vendor, vendor_id)
    if not v:
        raise PurchaseError("Vendor not found.")
    payload = data.model_dump()
    name = (payload.get("name") or "").strip()
    if not name:
        raise PurchaseError("Vendor name is required.")
    payload["trn"] = validation.validate_trn(payload.get("trn"), label="Vendor TRN")
    if _dup_vendor_name(db, name, exclude_id=vendor_id):
        raise PurchaseError("Another vendor already uses that name.")
    before = {f: getattr(v, f, None) for f in _VENDOR_FIELDS}
    for f in _VENDOR_FIELDS:
        if f in payload:
            setattr(v, f, payload[f])
    changes = audit.diff(before, {f: getattr(v, f, None) for f in _VENDOR_FIELDS})
    audit.record_profile_change(db, entity_type="vendor", entity_id=v.id, entity_label=v.name,
                                actor=actor, changes=changes)
    db.commit()
    db.refresh(v)
    return _vendor_out(v)


def vendor_audit(db: Session, vendor_id: str) -> list[dict]:
    return audit.list_profile_audit(db, "vendor", vendor_id)


def list_vendors(db: Session, active_only: bool = True) -> list[VendorOut]:
    stmt = select(Vendor).order_by(Vendor.name)
    if active_only:
        stmt = stmt.where(Vendor.is_active.is_(True))
    return [_vendor_out(v) for v in db.execute(stmt).scalars()]


def get_vendor(db: Session, vendor_id: str) -> VendorOut:
    v = db.get(Vendor, vendor_id)
    if not v:
        raise PurchaseError("Vendor not found.")
    return _vendor_out(v)


# ── Bills ───────────────────────────────────────────────────────────────────────────────
def _next_bill_number(db: Session) -> str:
    count = db.execute(select(func.count(VendorBill.id))).scalar() or 0
    return f"BILL-{count + 1:04d}"


def _bill_out(db: Session, bill: VendorBill) -> BillOut:
    accts = {
        a.id: a
        for a in db.execute(select(Account).where(Account.id.in_({ln.expense_account_id for ln in bill.lines}))).scalars()
    } if bill.lines else {}
    vendor = db.get(Vendor, bill.vendor_id)
    lines = [
        BillLineOut(
            id=ln.id, description=ln.description, quantity=ln.quantity, unit_price=ln.unit_price,
            vat_rate=ln.vat_rate, expense_account_id=ln.expense_account_id,
            expense_account_code=accts[ln.expense_account_id].code if ln.expense_account_id in accts else None,
            expense_account_name=accts[ln.expense_account_id].name if ln.expense_account_id in accts else None,
            net_amount=ln.net_amount, vat_amount=ln.vat_amount, line_total=ln.line_total,
        )
        for ln in bill.lines
    ]
    return BillOut(
        id=bill.id, number=bill.number, vendor_ref=bill.vendor_ref, vendor_id=bill.vendor_id,
        vendor_name=vendor.name if vendor else None, vendor_trn=vendor.trn if vendor else None,
        date=bill.date, due_date=bill.due_date, payment_terms=bill.payment_terms, currency=bill.currency,
        status=bill.status, net_total=bill.net_total, vat_total=bill.vat_total, grand_total=bill.grand_total,
        amount_paid=bill.amount_paid,
        balance_due=q(bill.grand_total - (bill.retention_amount - bill.retention_released) - _rc_vat(bill) - bill.amount_paid),
        journal_entry_id=bill.journal_entry_id, notes=bill.notes, project=bill.project,
        department=bill.department, cost_center=bill.cost_center, expense_category=bill.expense_category,
        lines=lines,
        retention_applicable=bill.retention_applicable, retention_basis=bill.retention_basis,
        retention_amount=q(bill.retention_amount), retention_released=q(bill.retention_released),
        retention_outstanding=q(bill.retention_amount - bill.retention_released),
        retention_reference=bill.retention_reference, retention_release_date=bill.retention_release_date,
        contract_reference=bill.contract_reference,
        created_at=bill.created_at.isoformat() if bill.created_at else None,
    )


def _get_bill(db: Session, bill_id: str) -> VendorBill:
    bill = db.execute(
        select(VendorBill).where(VendorBill.id == bill_id).options(selectinload(VendorBill.lines))
    ).scalar_one_or_none()
    if not bill:
        raise PurchaseError("Bill not found.")
    return bill


def create_bill(db: Session, data: BillIn) -> BillOut:
    vendor = db.get(Vendor, data.vendor_id)
    if not vendor:
        raise PurchaseError("Vendor not found.")
    bill = VendorBill(
        number=_next_bill_number(db), vendor_ref=data.vendor_ref, vendor_id=data.vendor_id,
        date=data.date, due_date=data.due_date, payment_terms=data.payment_terms or vendor.payment_terms,
        currency=data.currency, status="draft", notes=data.notes, project=data.project,
        department=data.department, cost_center=data.cost_center, expense_category=data.expense_category,
    )
    net_total = ZERO
    vat_total = ZERO
    for i, ln in enumerate(data.lines):
        acct = db.get(Account, ln.expense_account_id)
        if not acct:
            raise PurchaseError(f"Account '{ln.expense_account_id}' not found.")
        if acct.is_group:
            raise PurchaseError(f"Account '{acct.code}' is a group account and cannot be posted to.")
        net = q(ln.quantity * ln.unit_price)
        vat = q(net * ln.vat_rate)
        bill.lines.append(VendorBillLine(
            ordinal=i, description=ln.description, quantity=ln.quantity, unit_price=ln.unit_price,
            vat_rate=ln.vat_rate, vat_treatment=(ln.vat_treatment or ("SR" if ln.vat_rate > 0 else "ZR")),
            expense_account_id=ln.expense_account_id, net_amount=net,
            vat_amount=vat, line_total=q(net + vat),
        ))
        net_total += net
        vat_total += vat
    bill.net_total = q(net_total)
    bill.vat_total = q(vat_total)
    bill.grand_total = q(net_total + vat_total)
    if data.retention_applicable:
        rs = calc.document_summary(
            subtotal=bill.net_total, vat_amount=bill.vat_total, retention_basis=data.retention_basis,
            retention_percent=data.retention_percent, retention_amount=data.retention_amount)
        ret = rs["retention"]
        if ret <= 0:
            raise PurchaseError("Retention is enabled but computes to zero — set a percent or amount.")
        if ret > bill.grand_total:
            raise PurchaseError("Retention cannot exceed the bill amount.")
        if data.retention_account_id:
            _leaf_or_error(db, data.retention_account_id)
        bill.retention_applicable = True
        bill.retention_basis = data.retention_basis
        bill.retention_percent = data.retention_percent
        bill.retention_amount = ret
        bill.retention_reference = data.retention_reference
        bill.retention_release_date = data.retention_release_date
        bill.retention_account_id = data.retention_account_id
        bill.contract_reference = data.contract_reference
    db.add(bill)
    db.flush()
    if data.auto_post:
        _post_bill_je(db, bill)
        bill.status = "posted"
    db.commit()
    db.refresh(bill)
    return _bill_out(db, bill)


def _rc_vat(bill: VendorBill) -> Decimal:
    """VAT on reverse-charge (RC) lines — self-assessed, NOT payable to the vendor."""
    return q(sum((ln.vat_amount for ln in bill.lines if (ln.vat_treatment or "") == "RC"), ZERO))


def _post_bill_je(db: Session, bill: VendorBill) -> None:
    ap = _account_by_code(db, C.CODE_AP)
    vat_in = _account_by_code(db, C.CODE_VAT_INPUT)
    expense_by_account: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for ln in bill.lines:
        expense_by_account[ln.expense_account_id] += ln.net_amount
    lines = [JournalLineIn(account_id=acct_id, debit=q(net)) for acct_id, net in expense_by_account.items()]
    rc_vat = _rc_vat(bill)
    normal_vat = q(bill.vat_total - rc_vat)
    # Input VAT: normal lines are recoverable input; RC lines self-assess BOTH input and output VAT
    # (net-zero cash effect) — the vendor is paid net only.
    if normal_vat > 0:
        lines.append(JournalLineIn(account_id=vat_in.id, debit=normal_vat))
    if rc_vat > 0:
        vat_out = _account_by_code(db, C.CODE_VAT_OUTPUT)
        lines.append(JournalLineIn(account_id=vat_in.id, debit=rc_vat))     # recoverable input (RC)
        lines.append(JournalLineIn(account_id=vat_out.id, credit=rc_vat))   # self-assessed output (RC)
    # Split the credit: hold back retention as Retention Payable, the rest to AP (excl. RC VAT).
    retention = q(bill.retention_amount) if bill.retention_applicable else ZERO
    if retention > 0:
        lines.append(JournalLineIn(account_id=_retention_account(db, bill).id, credit=retention))
    lines.append(JournalLineIn(account_id=ap.id, credit=q(bill.grand_total - retention - rc_vat)))
    entry = ledger.create_journal_entry(
        db,
        JournalEntryIn(
            date=bill.date, memo=f"Vendor bill {bill.number}", reference=bill.vendor_ref or bill.number,
            source="purchase", currency=bill.currency, lines=lines, auto_post=True,
        ),
    )
    bill.journal_entry_id = entry.id


def post_bill(db: Session, bill_id: str) -> BillOut:
    bill = _get_bill(db, bill_id)
    if bill.status != "draft":
        raise PurchaseError(f"Only draft bills can be posted (status is '{bill.status}').")
    _post_bill_je(db, bill)
    bill.status = "posted"
    db.commit()
    db.refresh(bill)
    return _bill_out(db, bill)


def void_bill(db: Session, bill_id: str) -> BillOut:
    bill = _get_bill(db, bill_id)
    if bill.amount_paid > 0:
        raise PurchaseError("Cannot void a bill with payments — void the payments first.")
    if bill.journal_entry_id:
        ledger.void_entry(db, bill.journal_entry_id)
    bill.status = "void"
    db.commit()
    db.refresh(bill)
    return _bill_out(db, bill)


def update_bill(db: Session, bill_id: str, data, actor: str | None = None,
                reason: str | None = None) -> BillOut:
    """Edit a vendor bill via reverse-and-repost (id + number preserved). Blocked when the bill
    has payments/allocations or released retention, and in a locked period."""
    from . import audit, system_settings
    bill = _get_bill(db, bill_id)
    if bill.status == "void":
        raise PurchaseError("Cannot edit a voided bill.")
    if Decimal(bill.amount_paid) > 0:
        raise PurchaseError("This bill has payments/allocations applied — reverse them before editing.")
    if Decimal(bill.retention_released or 0) > 0:
        raise PurchaseError("Retention has been released on this bill — reverse it before editing.")
    vendor = db.get(Vendor, data.vendor_id)
    if not vendor:
        raise PurchaseError("Vendor not found.")
    system_settings.assert_period_open(db, bill.date, "edit")
    system_settings.assert_period_open(db, data.date, "edit")

    prev_status = bill.status
    before = {"date": str(bill.date), "vendor_id": bill.vendor_id, "net_total": str(bill.net_total),
              "vat_total": str(bill.vat_total), "grand_total": str(bill.grand_total),
              "retention_amount": str(bill.retention_amount), "lines": len(bill.lines)}
    if bill.journal_entry_id:
        ledger.void_entry(db, bill.journal_entry_id)
        bill.journal_entry_id = None

    bill.vendor_id = data.vendor_id
    bill.vendor_ref = data.vendor_ref
    bill.date = data.date
    bill.due_date = data.due_date
    bill.payment_terms = data.payment_terms or vendor.payment_terms
    bill.currency = data.currency
    bill.notes = data.notes
    bill.project = data.project
    bill.department = data.department
    bill.cost_center = data.cost_center
    bill.expense_category = data.expense_category
    bill.lines.clear()
    db.flush()
    net_total = vat_total = ZERO
    for i, ln in enumerate(data.lines):
        acct = db.get(Account, ln.expense_account_id)
        if not acct or acct.is_group:
            raise PurchaseError(f"Account '{ln.expense_account_id}' is invalid.")
        net = q(ln.quantity * ln.unit_price)
        vat = q(net * ln.vat_rate)
        bill.lines.append(VendorBillLine(
            ordinal=i, description=ln.description, quantity=ln.quantity, unit_price=ln.unit_price,
            vat_rate=ln.vat_rate, vat_treatment=(ln.vat_treatment or ("SR" if ln.vat_rate > 0 else "ZR")),
            expense_account_id=ln.expense_account_id, net_amount=net, vat_amount=vat, line_total=q(net + vat)))
        net_total += net
        vat_total += vat
    bill.net_total = q(net_total)
    bill.vat_total = q(vat_total)
    bill.grand_total = q(net_total + vat_total)
    bill.retention_applicable = False
    bill.retention_amount = ZERO
    if data.retention_applicable:
        rs = calc.document_summary(subtotal=bill.net_total, vat_amount=bill.vat_total,
                                   retention_basis=data.retention_basis,
                                   retention_percent=data.retention_percent,
                                   retention_amount=data.retention_amount)
        ret = rs["retention"]
        if ret <= 0:
            raise PurchaseError("Retention is enabled but computes to zero — set a percent or amount.")
        if ret > bill.grand_total:
            raise PurchaseError("Retention cannot exceed the bill amount.")
        bill.retention_applicable = True
        bill.retention_basis = data.retention_basis
        bill.retention_percent = data.retention_percent
        bill.retention_amount = ret
        bill.retention_reference = data.retention_reference
        bill.retention_release_date = data.retention_release_date
        bill.retention_account_id = data.retention_account_id
        bill.contract_reference = data.contract_reference
    if prev_status == "posted":
        _post_bill_je(db, bill)
        bill.status = "posted"
    after = {"date": str(bill.date), "vendor_id": bill.vendor_id, "net_total": str(bill.net_total),
             "vat_total": str(bill.vat_total), "grand_total": str(bill.grand_total),
             "retention_amount": str(bill.retention_amount), "lines": len(bill.lines)}
    audit.record_txn_audit(db, entity_type="bill", entity_id=bill.id, doc_number=bill.number,
                           actor=actor, action="edit", reason=reason, prev_status=prev_status,
                           new_status=bill.status, changes=audit.diff(before, after))
    db.commit()
    db.refresh(bill)
    return _bill_out(db, bill)


def bill_audit(db: Session, bill_id: str) -> list[dict]:
    from . import audit
    return audit.list_txn_audit(db, "bill", bill_id)


_BILL_META = ("due_date", "vendor_ref", "payment_terms", "department", "project", "cost_center",
              "expense_category", "notes")


def update_bill_meta(db: Session, bill_id: str, data, actor: str | None = None,
                     reason: str | None = None) -> BillOut:
    """Edit a bill's NON-financial detail fields without touching lines/amounts/ledger — safe
    even when the bill is paid/settled."""
    from . import audit
    bill = _get_bill(db, bill_id)
    if bill.status == "void":
        raise PurchaseError("Cannot edit a voided bill.")
    payload = data.model_dump(exclude_unset=True) if hasattr(data, "model_dump") else dict(data)
    before = {f: str(getattr(bill, f, None)) for f in _BILL_META}
    for f in _BILL_META:
        if f in payload:
            setattr(bill, f, payload[f])
    changes = audit.diff(before, {f: str(getattr(bill, f, None)) for f in _BILL_META})
    audit.record_txn_audit(db, entity_type="bill", entity_id=bill.id, doc_number=bill.number,
                           actor=actor, action="edit", reason=reason or "Edited details",
                           prev_status=bill.status, new_status=bill.status, changes=changes)
    db.commit()
    db.refresh(bill)
    return _bill_out(db, bill)


def hold_retention(db: Session, bill_id: str, data, actor: str | None = None) -> BillOut:
    """Hold retention on an already-posted bill: move an amount from AP into Retention Payable
    (Dr AP / Cr Retention Payable), reducing the currently payable balance."""
    from . import audit
    bill = _get_bill(db, bill_id)
    if bill.status not in ("posted", "partial"):
        raise PurchaseError("Retention can only be held on a posted bill.")
    amt = q(data.amount)
    if amt <= 0:
        raise PurchaseError("Retention amount must be positive.")
    payable = q(bill.grand_total - (bill.retention_amount - bill.retention_released) - _rc_vat(bill))
    balance = q(payable - bill.amount_paid)
    if amt > balance:
        raise PurchaseError(f"Retention {amt} exceeds the payable balance {balance}.")
    ret_acct = _retention_account(db, bill)
    ap = _account_by_code(db, C.CODE_AP)
    entry = ledger.create_journal_entry(db, JournalEntryIn(
        date=data.date, memo=f"Retention held — {bill.number}", reference=bill.vendor_ref or bill.number,
        source="purchase", currency="AED", lines=[JournalLineIn(account_id=ap.id, debit=q(amt)),
                               JournalLineIn(account_id=ret_acct.id, credit=q(amt))], auto_post=True))
    prev = q(bill.retention_amount)
    bill.retention_applicable = True
    bill.retention_amount = q(prev + amt)
    if not bill.retention_basis:
        bill.retention_basis = "amount"
    payable = q(bill.grand_total - (bill.retention_amount - bill.retention_released) - _rc_vat(bill))
    bill.status = "paid" if bill.amount_paid >= payable else ("partial" if bill.amount_paid > 0 else "posted")
    audit.record_txn_audit(db, entity_type="bill", entity_id=bill.id, doc_number=bill.number,
                           actor=actor, action="edit", reason="Retention held",
                           prev_status="posted", new_status=bill.status,
                           changes=[{"field": "retention_amount", "old": str(prev), "new": str(bill.retention_amount)}])
    db.commit()
    db.refresh(bill)
    return _bill_out(db, bill)


def release_retention(db: Session, bill_id: str, data) -> BillOut:
    """Release held vendor retention: move it from Retention Payable back to AP (to_bank=False)
    or pay it out of bank directly (to_bank=True)."""
    bill = _get_bill(db, bill_id)
    if not bill.retention_applicable or bill.retention_amount <= 0:
        raise PurchaseError("This bill has no retention to release.")
    outstanding = q(bill.retention_amount - bill.retention_released)
    amt = q(data.amount)
    if amt <= 0:
        raise PurchaseError("Release amount must be positive.")
    if amt > outstanding:
        raise PurchaseError(f"Release {amt} exceeds outstanding retention {outstanding}.")
    ret_acct = _retention_account(db, bill)
    if data.to_bank:
        target = _account_by_code(db, C.CODE_BANK)
        memo = f"Retention released & paid — bill {bill.number}"
    else:
        target = _account_by_code(db, C.CODE_AP)
        memo = f"Retention released to payable — bill {bill.number}"
    entry = ledger.create_journal_entry(db, JournalEntryIn(
        date=data.date, memo=memo, reference=data.reference or bill.number, source="purchase",
        currency=bill.currency, lines=[JournalLineIn(account_id=ret_acct.id, debit=amt),
                                       JournalLineIn(account_id=target.id, credit=amt)], auto_post=True))
    bill.retention_released = q(bill.retention_released + amt)
    if data.to_bank:
        bill.amount_paid = q(bill.amount_paid + amt)
        payable = q(bill.grand_total - (bill.retention_amount - bill.retention_released))
        bill.status = "paid" if bill.amount_paid >= payable else "partial"
    db.commit()
    db.refresh(bill)
    return _bill_out(db, bill)


def list_bills(db: Session, vendor_id: str | None = None, status: str | None = None) -> list[BillSummary]:
    stmt = select(VendorBill).order_by(VendorBill.number.desc())
    if vendor_id:
        stmt = stmt.where(VendorBill.vendor_id == vendor_id)
    if status:
        stmt = stmt.where(VendorBill.status == status)
    names = {v.id: v.name for v in db.execute(select(Vendor)).scalars()}
    return [
        BillSummary(
            id=b.id, number=b.number, vendor_ref=b.vendor_ref, vendor_id=b.vendor_id,
            vendor_name=names.get(b.vendor_id), date=b.date, due_date=b.due_date, status=b.status,
            grand_total=b.grand_total, amount_paid=b.amount_paid, balance_due=q(b.grand_total - b.amount_paid),
        )
        for b in db.execute(stmt).scalars()
    ]


def get_bill(db: Session, bill_id: str) -> BillOut:
    return _bill_out(db, _get_bill(db, bill_id))


# ── Payments ────────────────────────────────────────────────────────────────────────────
def pay_bill(db: Session, data: BillPaymentIn) -> BillPaymentOut:
    bill = _get_bill(db, data.bill_id)
    if bill.status == "void":
        raise PurchaseError("Cannot pay a voided bill.")
    if bill.status == "draft":
        raise PurchaseError("Post the bill before paying it.")
    retention_outstanding = q(bill.retention_amount - bill.retention_released)
    payable = q(bill.grand_total - retention_outstanding - _rc_vat(bill))
    balance = q(payable - bill.amount_paid)
    if data.amount > balance:
        raise PurchaseError(f"Payment {data.amount} exceeds the outstanding balance {balance}.")
    pay_acct = (
        db.get(Account, data.payment_account_id) if data.payment_account_id
        else _account_by_code(db, C.CODE_BANK)
    )
    if not pay_acct:
        raise PurchaseError("Payment account not found.")
    ap = _account_by_code(db, C.CODE_AP)
    entry = ledger.create_journal_entry(
        db,
        JournalEntryIn(
            date=data.date, memo=f"Payment for bill {bill.number}", reference=data.reference or bill.number,
            source="purchase", currency=bill.currency,
            lines=[
                JournalLineIn(account_id=ap.id, debit=q(data.amount)),
                JournalLineIn(account_id=pay_acct.id, credit=q(data.amount)),
            ],
            auto_post=True,
        ),
    )
    pay = BillPayment(
        vendor_id=bill.vendor_id, bill_id=bill.id, date=data.date, amount=q(data.amount),
        method=data.method, payment_account_id=pay_acct.id, reference=data.reference,
        journal_entry_id=entry.id,
    )
    db.add(pay)
    bill.amount_paid = q(bill.amount_paid + data.amount)
    bill.status = "paid" if bill.amount_paid >= payable else "partial"
    db.commit()
    db.refresh(pay)
    return BillPaymentOut(
        id=pay.id, vendor_id=pay.vendor_id, bill_id=pay.bill_id, date=pay.date, amount=pay.amount,
        method=pay.method, payment_account_id=pay.payment_account_id, reference=pay.reference,
        journal_entry_id=pay.journal_entry_id, created_at=pay.created_at.isoformat() if pay.created_at else None,
    )


def void_bill_payment(db: Session, payment_id: str, actor: str | None = None,
                      reason: str | None = None) -> dict:
    """Reverse a vendor payment: void its journal entry, restore the bill's outstanding balance,
    and audit. Blocked in a locked period."""
    from . import audit, system_settings
    from ..models import BillPayment
    pay = db.get(BillPayment, payment_id)
    if not pay:
        raise PurchaseError("Payment not found.")
    system_settings.assert_period_open(db, pay.date, "reverse")
    bill = _get_bill(db, pay.bill_id)
    if pay.journal_entry_id:
        ledger.void_entry(db, pay.journal_entry_id)
    if bill:
        bill.amount_paid = q(max(Decimal(bill.amount_paid) - Decimal(pay.amount), ZERO))
        payable = q(Decimal(bill.grand_total) - (Decimal(bill.retention_amount) - Decimal(bill.retention_released)) - _rc_vat(bill))
        bill.status = "paid" if bill.amount_paid >= payable else ("partial" if bill.amount_paid > 0 else "posted")
    audit.record_txn_audit(db, entity_type="vendor_payment", entity_id=pay.id,
                           doc_number=(bill.number if bill else None), actor=actor, action="reverse",
                           reason=reason or "Payment voided", prev_status="posted", new_status="void",
                           changes=[{"field": "amount", "old": str(pay.amount), "new": None}])
    db.delete(pay)
    db.commit()
    return {"ok": True, "bill_id": pay.bill_id, "amount": str(pay.amount)}
