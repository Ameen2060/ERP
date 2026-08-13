"""Sales service: customers, tax invoices, payments, AR aging. Every posted invoice and
payment writes a balanced journal entry via the ledger service."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .. import constants as C
from ..models import Account, Customer, CustomerPayment, SalesInvoice, SalesInvoiceLine
from ..schemas import (
    CustomerIn,
    CustomerOut,
    InvoiceIn,
    InvoiceLineOut,
    InvoiceOut,
    InvoiceSummary,
    JournalEntryIn,
    JournalLineIn,
    PaymentIn,
    PaymentOut,
    q,
)
from . import audit, calc, currency, ledger, validation

ZERO = Decimal(0)


class SalesError(ValueError):
    """Domain error → HTTP 400."""


def _account_by_code(db: Session, code: str) -> Account:
    acct = db.execute(select(Account).where(Account.code == code)).scalar_one_or_none()
    if not acct:
        raise SalesError(f"Required account '{code}' is missing — seed the Chart of Accounts first.")
    return acct


def _leaf_or_error(db: Session, account_id: str) -> Account:
    a = db.get(Account, account_id)
    if not a:
        raise SalesError("Account not found.")
    if a.is_group:
        raise SalesError(f"Account '{a.code}' is a group account and cannot be posted to.")
    return a


def _retention_account(db: Session, inv_or_bill) -> Account:
    if inv_or_bill.retention_account_id:
        return _leaf_or_error(db, inv_or_bill.retention_account_id)
    return _account_by_code(db, C.CODE_RETENTION_RECEIVABLE)


# ── Customers ───────────────────────────────────────────────────────────────────────────
_CUSTOMER_FIELDS = ("name", "trn", "email", "phone", "contact_name", "address",
                    "billing_address", "shipping_address", "payment_terms", "credit_limit",
                    "currency", "country", "tax_status", "party_type", "einvoice_scheme",
                    "einvoice_id", "notes")


def _customer_out(c: Customer) -> CustomerOut:
    billing = c.billing_address or c.address
    return CustomerOut(
        id=c.id, name=c.name, trn=c.trn, email=c.email, phone=c.phone,
        contact_name=c.contact_name, address=c.address, billing_address=c.billing_address,
        shipping_address=c.shipping_address, payment_terms=c.payment_terms,
        credit_limit=Decimal(c.credit_limit or 0), currency=c.currency,
        country=c.country or "AE", tax_status=c.tax_status or "unknown",
        party_type=c.party_type or "b2b", einvoice_scheme=c.einvoice_scheme,
        einvoice_id=c.einvoice_id, notes=c.notes,
        is_active=c.is_active,
        warnings=validation.party_warnings(c.trn, billing, party="Customer"),
        created_at=c.created_at.isoformat() if c.created_at else None,
    )


def _snapshot(obj, fields) -> dict:
    return {f: getattr(obj, f, None) for f in fields}


def _dup_name(db: Session, model, name: str, exclude_id: str | None = None) -> bool:
    stmt = select(model).where(func.lower(model.name) == name.strip().lower())
    if exclude_id:
        stmt = stmt.where(model.id != exclude_id)
    return db.execute(stmt).scalars().first() is not None


def create_customer(db: Session, data: CustomerIn) -> CustomerOut:
    payload = data.model_dump()
    payload["trn"] = validation.validate_trn(payload.get("trn"), label="Customer TRN")
    if not (payload.get("name") or "").strip():
        raise SalesError("Customer name is required.")
    c = Customer(**payload)
    db.add(c)
    db.flush()
    audit.record_profile_change(db, entity_type="customer", entity_id=c.id, entity_label=c.name,
                                actor=None, changes=[], action="create")
    db.commit()
    db.refresh(c)
    return _customer_out(c)


def update_customer(db: Session, customer_id: str, data: CustomerIn,
                    actor: str | None = None) -> CustomerOut:
    """Edit a customer profile. Historical transactions are NOT touched (they reference the
    customer by id and render current master data by design). Writes a field-level audit."""
    c = db.get(Customer, customer_id)
    if not c:
        raise SalesError("Customer not found.")
    payload = data.model_dump()
    name = (payload.get("name") or "").strip()
    if not name:
        raise SalesError("Customer name is required.")
    payload["trn"] = validation.validate_trn(payload.get("trn"), label="Customer TRN")
    if _dup_name(db, Customer, name, exclude_id=customer_id):
        raise SalesError("Another customer already uses that name.")
    before = _snapshot(c, _CUSTOMER_FIELDS)
    for f in _CUSTOMER_FIELDS:
        if f in payload:
            setattr(c, f, payload[f])
    changes = audit.diff(before, _snapshot(c, _CUSTOMER_FIELDS))
    audit.record_profile_change(db, entity_type="customer", entity_id=c.id, entity_label=c.name,
                                actor=actor, changes=changes)
    db.commit()
    db.refresh(c)
    return _customer_out(c)


def customer_audit(db: Session, customer_id: str) -> list[dict]:
    return audit.list_profile_audit(db, "customer", customer_id)


def list_customers(db: Session, active_only: bool = True) -> list[CustomerOut]:
    stmt = select(Customer).order_by(Customer.name)
    if active_only:
        stmt = stmt.where(Customer.is_active.is_(True))
    return [_customer_out(c) for c in db.execute(stmt).scalars()]


def get_customer(db: Session, customer_id: str) -> CustomerOut:
    c = db.get(Customer, customer_id)
    if not c:
        raise SalesError("Customer not found.")
    return _customer_out(c)


# ── Invoices ────────────────────────────────────────────────────────────────────────────
def _next_invoice_number(db: Session) -> str:
    count = db.execute(select(func.count(SalesInvoice.id))).scalar() or 0
    return f"INV-{count + 1:04d}"


def _invoice_out(db: Session, inv: SalesInvoice) -> InvoiceOut:
    acct_codes = {
        a.id: a.code
        for a in db.execute(select(Account).where(Account.id.in_({ln.revenue_account_id for ln in inv.lines}))).scalars()
    } if inv.lines else {}
    customer = db.get(Customer, inv.customer_id)
    lines = [
        InvoiceLineOut(
            id=ln.id, description=ln.description, quantity=ln.quantity, unit_price=ln.unit_price,
            vat_rate=ln.vat_rate, vat_treatment=ln.vat_treatment, revenue_account_id=ln.revenue_account_id,
            revenue_account_code=acct_codes.get(ln.revenue_account_id),
            net_amount=ln.net_amount, vat_amount=ln.vat_amount, line_total=ln.line_total,
        )
        for ln in inv.lines
    ]
    return InvoiceOut(
        id=inv.id, number=inv.number, customer_id=inv.customer_id,
        customer_name=customer.name if customer else None, date=inv.date, due_date=inv.due_date,
        currency=inv.currency, exchange_rate=Decimal(inv.exchange_rate or 1), status=inv.status,
        net_total=inv.net_total, vat_total=inv.vat_total, grand_total=inv.grand_total,
        base_grand_total=q(Decimal(inv.grand_total) * Decimal(inv.exchange_rate or 1)),
        amount_paid=inv.amount_paid,
        balance_due=q(inv.grand_total - (inv.retention_amount - inv.retention_released) - inv.amount_paid),
        journal_entry_id=inv.journal_entry_id, notes=inv.notes, lines=lines,
        retention_applicable=inv.retention_applicable, retention_basis=inv.retention_basis,
        retention_amount=q(inv.retention_amount), retention_released=q(inv.retention_released),
        retention_outstanding=q(inv.retention_amount - inv.retention_released),
        retention_reference=inv.retention_reference, retention_release_date=inv.retention_release_date,
        contract_reference=inv.contract_reference,
        created_at=inv.created_at.isoformat() if inv.created_at else None,
    )


def _get_invoice(db: Session, invoice_id: str) -> SalesInvoice:
    inv = db.execute(
        select(SalesInvoice).where(SalesInvoice.id == invoice_id).options(selectinload(SalesInvoice.lines))
    ).scalar_one_or_none()
    if not inv:
        raise SalesError("Invoice not found.")
    return inv


def create_invoice(db: Session, data: InvoiceIn) -> InvoiceOut:
    customer = db.get(Customer, data.customer_id)
    if not customer:
        raise SalesError("Customer not found.")
    from . import system_settings
    default_revenue = db.get(Account, system_settings.default_sales_account_id(db)) or _account_by_code(db, C.CODE_SALES)
    rate = currency.resolve_rate(db, data.currency, data.exchange_rate, data.date)
    inv = SalesInvoice(
        number=_next_invoice_number(db), customer_id=data.customer_id, date=data.date,
        due_date=data.due_date, currency=data.currency, exchange_rate=rate, status="draft",
        notes=data.notes, project=data.project, department=data.department,
        cost_center=data.cost_center, salesperson=data.salesperson, sales_category=data.sales_category,
    )
    net_total = ZERO
    vat_total = ZERO
    for i, ln in enumerate(data.lines):
        rev_id = ln.revenue_account_id or default_revenue.id
        rev_acct = db.get(Account, rev_id)
        if not rev_acct:
            raise SalesError(f"Revenue account '{rev_id}' not found.")
        if rev_acct.is_group:
            raise SalesError(f"Revenue account '{rev_acct.code}' is a group account.")
        net = q(ln.quantity * ln.unit_price)
        vat = q(net * ln.vat_rate)
        inv.lines.append(SalesInvoiceLine(
            ordinal=i, description=ln.description, quantity=ln.quantity, unit_price=ln.unit_price,
            vat_rate=ln.vat_rate, vat_treatment=(ln.vat_treatment or ("SR" if ln.vat_rate > 0 else "ZR")),
            revenue_account_id=rev_id, net_amount=net, vat_amount=vat, line_total=q(net + vat),
        ))
        net_total += net
        vat_total += vat
    inv.net_total = q(net_total)
    inv.vat_total = q(vat_total)
    inv.grand_total = q(net_total + vat_total)
    if data.retention_applicable:
        rs = calc.document_summary(
            subtotal=inv.net_total, vat_amount=inv.vat_total, retention_basis=data.retention_basis,
            retention_percent=data.retention_percent, retention_amount=data.retention_amount)
        ret = rs["retention"]
        if ret <= 0:
            raise SalesError("Retention is enabled but computes to zero — set a percent or amount.")
        if ret > inv.grand_total:
            raise SalesError("Retention cannot exceed the invoice amount.")
        if data.retention_account_id:
            _leaf_or_error(db, data.retention_account_id)
        inv.retention_applicable = True
        inv.retention_basis = data.retention_basis
        inv.retention_percent = data.retention_percent
        inv.retention_amount = ret
        inv.retention_reference = data.retention_reference
        inv.retention_release_date = data.retention_release_date
        inv.retention_account_id = data.retention_account_id
        inv.contract_reference = data.contract_reference
    db.add(inv)
    db.flush()
    if data.auto_post:
        _post_invoice_je(db, inv)
        inv.status = "posted"
    db.commit()
    db.refresh(inv)
    return _invoice_out(db, inv)


def _post_invoice_je(db: Session, inv: SalesInvoice) -> None:
    # The ledger is kept in base currency: convert the invoice's own-currency amounts at its
    # rate. AR is set to the sum of the credit lines so the entry always balances despite
    # per-line rounding.
    ar = _account_by_code(db, C.CODE_AR)
    vat_acct = _account_by_code(db, C.CODE_VAT_OUTPUT)
    rate = Decimal(inv.exchange_rate or 1)
    revenue_by_account: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for ln in inv.lines:
        revenue_by_account[ln.revenue_account_id] += ln.net_amount
    credit_lines = []
    total_credit = ZERO
    for acct_id, net in revenue_by_account.items():
        base = q(net * rate)
        credit_lines.append(JournalLineIn(account_id=acct_id, credit=base))
        total_credit += base
    if inv.vat_total > 0:
        vat_base = q(inv.vat_total * rate)
        credit_lines.append(JournalLineIn(account_id=vat_acct.id, credit=vat_base))
        total_credit += vat_base
    # Split the debit: hold back retention in a dedicated account, the rest to AR.
    retention_base = q(inv.retention_amount * rate) if inv.retention_applicable else ZERO
    debit_lines = []
    if retention_base > 0:
        debit_lines.append(JournalLineIn(account_id=_retention_account(db, inv).id, debit=retention_base))
    debit_lines.append(JournalLineIn(account_id=ar.id, debit=q(total_credit - retention_base)))
    lines = [*debit_lines, *credit_lines]
    fx = "" if rate == 1 else f" @ {rate} {inv.currency}/AED"
    entry = ledger.create_journal_entry(
        db,
        JournalEntryIn(
            date=inv.date, memo=f"Sales invoice {inv.number}{fx}", reference=inv.number,
            source="sales", currency="AED", lines=lines, auto_post=True,
        ),
    )
    inv.journal_entry_id = entry.id


def post_invoice(db: Session, invoice_id: str) -> InvoiceOut:
    inv = _get_invoice(db, invoice_id)
    if inv.status != "draft":
        raise SalesError(f"Only draft invoices can be posted (status is '{inv.status}').")
    _post_invoice_je(db, inv)
    inv.status = "posted"
    db.commit()
    db.refresh(inv)
    return _invoice_out(db, inv)


def void_invoice(db: Session, invoice_id: str) -> InvoiceOut:
    inv = _get_invoice(db, invoice_id)
    if inv.amount_paid > 0:
        raise SalesError("Cannot void an invoice with payments — void the payments first.")
    if inv.journal_entry_id:
        ledger.void_entry(db, inv.journal_entry_id)
    inv.status = "void"
    db.commit()
    db.refresh(inv)
    return _invoice_out(db, inv)


def update_invoice(db: Session, invoice_id: str, data: InvoiceIn, actor: str | None = None,
                   reason: str | None = None) -> InvoiceOut:
    """Edit a sales invoice via reverse-and-repost (id + number preserved). Blocked when the
    invoice has downstream settlement (payments / advance recovery / credit-note allocation) or
    released retention — reverse those first — and when it falls in a locked period."""
    from . import audit, system_settings
    inv = _get_invoice(db, invoice_id)
    if inv.status == "void":
        raise SalesError("Cannot edit a voided invoice.")
    if Decimal(inv.amount_paid) > 0:
        raise SalesError("This invoice has payments/allocations applied — reverse them before editing.")
    if Decimal(inv.retention_released or 0) > 0:
        raise SalesError("Retention has been released on this invoice — reverse it before editing.")
    customer = db.get(Customer, data.customer_id)
    if not customer:
        raise SalesError("Customer not found.")
    system_settings.assert_period_open(db, inv.date, "edit")
    system_settings.assert_period_open(db, data.date, "edit")
    from . import system_settings as _ss
    default_revenue = db.get(Account, _ss.default_sales_account_id(db)) or _account_by_code(db, C.CODE_SALES)

    prev_status = inv.status
    before = {"date": str(inv.date), "customer_id": inv.customer_id, "net_total": str(inv.net_total),
              "vat_total": str(inv.vat_total), "grand_total": str(inv.grand_total),
              "retention_amount": str(inv.retention_amount), "lines": len(inv.lines)}
    if inv.journal_entry_id:
        ledger.void_entry(db, inv.journal_entry_id)
        inv.journal_entry_id = None

    inv.customer_id = data.customer_id
    inv.date = data.date
    inv.due_date = data.due_date
    inv.currency = data.currency
    inv.exchange_rate = currency.resolve_rate(db, data.currency, data.exchange_rate, data.date)
    inv.notes = data.notes
    inv.project = data.project
    inv.department = data.department
    inv.cost_center = data.cost_center
    inv.salesperson = data.salesperson
    inv.sales_category = data.sales_category
    inv.lines.clear()
    db.flush()
    net_total = vat_total = ZERO
    for i, ln in enumerate(data.lines):
        rev_id = ln.revenue_account_id or default_revenue.id
        rev_acct = db.get(Account, rev_id)
        if not rev_acct or rev_acct.is_group:
            raise SalesError(f"Revenue account '{rev_id}' is invalid.")
        net = q(ln.quantity * ln.unit_price)
        vat = q(net * ln.vat_rate)
        inv.lines.append(SalesInvoiceLine(
            ordinal=i, description=ln.description, quantity=ln.quantity, unit_price=ln.unit_price,
            vat_rate=ln.vat_rate, vat_treatment=(ln.vat_treatment or ("SR" if ln.vat_rate > 0 else "ZR")),
            revenue_account_id=rev_id, net_amount=net, vat_amount=vat, line_total=q(net + vat)))
        net_total += net
        vat_total += vat
    inv.net_total = q(net_total)
    inv.vat_total = q(vat_total)
    inv.grand_total = q(net_total + vat_total)
    # Reset then recompute retention.
    inv.retention_applicable = False
    inv.retention_amount = ZERO
    if data.retention_applicable:
        rs = calc.document_summary(subtotal=inv.net_total, vat_amount=inv.vat_total,
                                   retention_basis=data.retention_basis,
                                   retention_percent=data.retention_percent,
                                   retention_amount=data.retention_amount)
        ret = rs["retention"]
        if ret <= 0:
            raise SalesError("Retention is enabled but computes to zero — set a percent or amount.")
        if ret > inv.grand_total:
            raise SalesError("Retention cannot exceed the invoice amount.")
        inv.retention_applicable = True
        inv.retention_basis = data.retention_basis
        inv.retention_percent = data.retention_percent
        inv.retention_amount = ret
        inv.retention_reference = data.retention_reference
        inv.retention_release_date = data.retention_release_date
        inv.retention_account_id = data.retention_account_id
        inv.contract_reference = data.contract_reference
    if prev_status == "posted":
        _post_invoice_je(db, inv)
        inv.status = "posted"
    after = {"date": str(inv.date), "customer_id": inv.customer_id, "net_total": str(inv.net_total),
             "vat_total": str(inv.vat_total), "grand_total": str(inv.grand_total),
             "retention_amount": str(inv.retention_amount), "lines": len(inv.lines)}
    audit.record_txn_audit(db, entity_type="invoice", entity_id=inv.id, doc_number=inv.number,
                           actor=actor, action="edit", reason=reason, prev_status=prev_status,
                           new_status=inv.status, changes=audit.diff(before, after))
    db.commit()
    db.refresh(inv)
    return _invoice_out(db, inv)


def invoice_audit(db: Session, invoice_id: str) -> list[dict]:
    from . import audit
    return audit.list_txn_audit(db, "invoice", invoice_id)


_INVOICE_META = ("due_date", "project", "department", "cost_center", "salesperson", "notes")


def update_invoice_meta(db: Session, invoice_id: str, data, actor: str | None = None,
                        reason: str | None = None) -> InvoiceOut:
    """Edit an invoice's NON-financial detail fields (due date, dimensions, notes) without
    touching lines, amounts or the ledger — safe even when the invoice is paid/settled."""
    from . import audit
    inv = _get_invoice(db, invoice_id)
    if inv.status == "void":
        raise SalesError("Cannot edit a voided invoice.")
    payload = data.model_dump(exclude_unset=True) if hasattr(data, "model_dump") else dict(data)
    before = {f: str(getattr(inv, f, None)) for f in _INVOICE_META}
    for f in _INVOICE_META:
        if f in payload:
            setattr(inv, f, payload[f])
    changes = audit.diff(before, {f: str(getattr(inv, f, None)) for f in _INVOICE_META})
    audit.record_txn_audit(db, entity_type="invoice", entity_id=inv.id, doc_number=inv.number,
                           actor=actor, action="edit", reason=reason or "Edited details",
                           prev_status=inv.status, new_status=inv.status, changes=changes)
    db.commit()
    db.refresh(inv)
    return _invoice_out(db, inv)


def list_invoices(db: Session, customer_id: str | None = None, status: str | None = None) -> list[InvoiceSummary]:
    stmt = select(SalesInvoice).order_by(SalesInvoice.number.desc())
    if customer_id:
        stmt = stmt.where(SalesInvoice.customer_id == customer_id)
    if status:
        stmt = stmt.where(SalesInvoice.status == status)
    names = {c.id: c.name for c in db.execute(select(Customer)).scalars()}
    return [
        InvoiceSummary(
            id=inv.id, number=inv.number, customer_id=inv.customer_id,
            customer_name=names.get(inv.customer_id), date=inv.date, due_date=inv.due_date,
            status=inv.status, grand_total=inv.grand_total, amount_paid=inv.amount_paid,
            balance_due=q(inv.grand_total - inv.amount_paid),
        )
        for inv in db.execute(stmt).scalars()
    ]


def get_invoice(db: Session, invoice_id: str) -> InvoiceOut:
    return _invoice_out(db, _get_invoice(db, invoice_id))


# ── Payments ────────────────────────────────────────────────────────────────────────────
def record_payment(db: Session, data: PaymentIn) -> PaymentOut:
    inv = _get_invoice(db, data.invoice_id)
    if inv.status == "void":
        raise SalesError("Cannot record a payment against a voided invoice.")
    if inv.status == "draft":
        raise SalesError("Post the invoice before recording a payment.")
    # The customer only owes the collectible portion now: grand total less any retention still held.
    retention_outstanding = q(inv.retention_amount - inv.retention_released)
    collectible = q(inv.grand_total - retention_outstanding)
    balance = q(collectible - inv.amount_paid)
    if data.amount > balance:
        raise SalesError(f"Payment {data.amount} exceeds the outstanding balance {balance}.")
    deposit = (
        db.get(Account, data.deposit_account_id) if data.deposit_account_id
        else _account_by_code(db, C.CODE_BANK)
    )
    if not deposit:
        raise SalesError("Deposit account not found.")
    ar = _account_by_code(db, C.CODE_AR)

    # Base-currency conversion: relieve AR at the INVOICE rate, receive cash at the PAYMENT
    # rate; the difference is a realized FX gain/loss.
    rate_inv = Decimal(inv.exchange_rate or 1)
    rate_pay = currency.resolve_rate(db, inv.currency, data.exchange_rate, data.date)
    pay_foreign = q(data.amount)
    ar_base = q(pay_foreign * rate_inv)
    bank_base = q(pay_foreign * rate_pay)
    fx = q(bank_base - ar_base)
    lines = [JournalLineIn(account_id=deposit.id, debit=bank_base),
             JournalLineIn(account_id=ar.id, credit=ar_base)]
    if fx > 0:
        lines.append(JournalLineIn(account_id=_account_by_code(db, C.CODE_FX_GAIN).id, credit=fx))
    elif fx < 0:
        lines.append(JournalLineIn(account_id=_account_by_code(db, C.CODE_FX_LOSS).id, debit=q(-fx)))
    memo = f"Payment for {inv.number}" + ("" if rate_pay == 1 else f" @ {rate_pay} {inv.currency}/AED")
    entry = ledger.create_journal_entry(
        db,
        JournalEntryIn(
            date=data.date, memo=memo, reference=data.reference or inv.number,
            source="sales", currency="AED", lines=lines, auto_post=True,
        ),
    )
    pay = CustomerPayment(
        customer_id=inv.customer_id, invoice_id=inv.id, date=data.date, amount=q(data.amount),
        method=data.method, deposit_account_id=deposit.id, reference=data.reference,
        journal_entry_id=entry.id,
    )
    db.add(pay)
    inv.amount_paid = q(inv.amount_paid + data.amount)
    inv.status = "paid" if inv.amount_paid >= collectible else "partial"
    db.commit()
    db.refresh(pay)
    return PaymentOut(
        id=pay.id, customer_id=pay.customer_id, invoice_id=pay.invoice_id, date=pay.date,
        amount=pay.amount, method=pay.method, deposit_account_id=pay.deposit_account_id,
        reference=pay.reference, journal_entry_id=pay.journal_entry_id,
        created_at=pay.created_at.isoformat() if pay.created_at else None,
    )


def void_payment(db: Session, payment_id: str, actor: str | None = None,
                 reason: str | None = None) -> dict:
    """Reverse a customer receipt: void its journal entry, restore the invoice's outstanding
    balance, and audit. Blocked in a locked period."""
    from . import audit, system_settings
    from ..models import CustomerPayment
    pay = db.get(CustomerPayment, payment_id)
    if not pay:
        raise SalesError("Payment not found.")
    system_settings.assert_period_open(db, pay.date, "reverse")
    inv = db.get(SalesInvoice, pay.invoice_id)
    if pay.journal_entry_id:
        ledger.void_entry(db, pay.journal_entry_id)
    if inv:
        inv.amount_paid = q(max(Decimal(inv.amount_paid) - Decimal(pay.amount), ZERO))
        collectible = q(Decimal(inv.grand_total) - (Decimal(inv.retention_amount) - Decimal(inv.retention_released)))
        inv.status = "paid" if inv.amount_paid >= collectible else ("partial" if inv.amount_paid > 0 else "posted")
    audit.record_txn_audit(db, entity_type="customer_payment", entity_id=pay.id,
                           doc_number=(inv.number if inv else None), actor=actor, action="reverse",
                           reason=reason or "Payment voided", prev_status="posted", new_status="void",
                           changes=[{"field": "amount", "old": str(pay.amount), "new": None}])
    db.delete(pay)
    db.commit()
    return {"ok": True, "invoice_id": pay.invoice_id, "amount": str(pay.amount)}


def payment_audit_for_invoice(db: Session, invoice_id: str) -> list[dict]:
    from . import audit
    inv = db.get(SalesInvoice, invoice_id)
    return audit.list_txn_audit(db, "customer_payment", invoice_id) if inv else []


def hold_retention(db: Session, invoice_id: str, data, actor: str | None = None) -> InvoiceOut:
    """Hold retention on an already-posted invoice: move an amount from AR into Retention
    Receivable (Dr Retention Receivable / Cr AR), reducing the currently collectible balance.
    Use when retention wasn't set at invoicing time."""
    from . import audit
    inv = _get_invoice(db, invoice_id)
    if inv.status not in ("posted", "partial"):
        raise SalesError("Retention can only be held on a posted invoice.")
    amt = q(data.amount)
    if amt <= 0:
        raise SalesError("Retention amount must be positive.")
    collectible = q(inv.grand_total - (inv.retention_amount - inv.retention_released))
    balance = q(collectible - inv.amount_paid)
    if amt > balance:
        raise SalesError(f"Retention {amt} exceeds the collectible balance {balance}.")
    rate = Decimal(inv.exchange_rate or 1)
    base = q(amt * rate)
    ret_acct = _retention_account(db, inv)
    ar = _account_by_code(db, C.CODE_AR)
    entry = ledger.create_journal_entry(db, JournalEntryIn(
        date=data.date, memo=f"Retention held — {inv.number}", reference=inv.number, source="sales",
        currency="AED", lines=[JournalLineIn(account_id=ret_acct.id, debit=base),
                               JournalLineIn(account_id=ar.id, credit=base)], auto_post=True))
    prev = q(inv.retention_amount)
    inv.retention_applicable = True
    inv.retention_amount = q(prev + amt)
    if not inv.retention_basis:
        inv.retention_basis = "amount"
    collectible = q(inv.grand_total - (inv.retention_amount - inv.retention_released))
    inv.status = "paid" if inv.amount_paid >= collectible else ("partial" if inv.amount_paid > 0 else "posted")
    audit.record_txn_audit(db, entity_type="invoice", entity_id=inv.id, doc_number=inv.number,
                           actor=actor, action="edit", reason="Retention held",
                           prev_status="posted", new_status=inv.status,
                           changes=[{"field": "retention_amount", "old": str(prev), "new": str(inv.retention_amount)}])
    db.commit()
    db.refresh(inv)
    return _invoice_out(db, inv)


def release_retention(db: Session, invoice_id: str, data) -> InvoiceOut:
    """Release held retention: move it from Retention Receivable into AR (to_bank=False) or
    collect it straight to bank (to_bank=True)."""
    inv = _get_invoice(db, invoice_id)
    if not inv.retention_applicable or inv.retention_amount <= 0:
        raise SalesError("This invoice has no retention to release.")
    outstanding = q(inv.retention_amount - inv.retention_released)
    amt = q(data.amount)
    if amt <= 0:
        raise SalesError("Release amount must be positive.")
    if amt > outstanding:
        raise SalesError(f"Release {amt} exceeds outstanding retention {outstanding}.")
    rate = Decimal(inv.exchange_rate or 1)
    base = q(amt * rate)
    ret_acct = _retention_account(db, inv)
    if data.to_bank:
        target = db.get(Account, data.__dict__.get("deposit_account_id")) if getattr(data, "deposit_account_id", None) else _account_by_code(db, C.CODE_BANK)
        memo = f"Retention released & collected — {inv.number}"
    else:
        target = _account_by_code(db, C.CODE_AR)
        memo = f"Retention released to receivable — {inv.number}"
    entry = ledger.create_journal_entry(db, JournalEntryIn(
        date=data.date, memo=memo, reference=data.reference or inv.number, source="sales",
        currency="AED", lines=[JournalLineIn(account_id=target.id, debit=base),
                               JournalLineIn(account_id=ret_acct.id, credit=base)], auto_post=True))
    inv.retention_released = q(inv.retention_released + amt)
    if data.to_bank:
        inv.amount_paid = q(inv.amount_paid + amt)   # collected, no longer receivable
        collectible = q(inv.grand_total - (inv.retention_amount - inv.retention_released))
        inv.status = "paid" if inv.amount_paid >= collectible else "partial"
    db.commit()
    db.refresh(inv)
    return _invoice_out(db, inv)


# AR aging now lives in services/reports.py (shared bucketing with AP aging + risk scoring).
