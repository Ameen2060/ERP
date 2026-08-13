"""Direct Expenses (Purchases → Expenses).

A direct expense records spend without a purchase order or inventory receipt. When
`paid_directly` it posts Dr Expense / Dr Input VAT / Cr Bank·Cash; otherwise it books a
payable (Cr Accounts Payable) so it can be settled later through the normal AP flow.
All amounts run through the shared calculation engine (`calc`)."""

from __future__ import annotations

from datetime import date as _Date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import constants as C
from ..models import Account, Expense, Vendor
from ..schemas import JournalEntryIn, JournalLineIn
from . import calc, ledger


class ExpenseError(ValueError):
    """Domain error → HTTP 400."""


def _account_by_code(db: Session, code: str) -> Account:
    a = db.execute(select(Account).where(Account.code == code)).scalar_one_or_none()
    if not a:
        raise ExpenseError(f"Required account '{code}' is missing — seed the Chart of Accounts first.")
    return a


def _leaf(db: Session, account_id: str, label: str) -> Account:
    a = db.get(Account, account_id)
    if not a:
        raise ExpenseError(f"{label} account not found.")
    if a.is_group:
        raise ExpenseError(f"{label} account '{a.code}' is a group account and cannot be posted to.")
    return a


def _next_number(db: Session) -> str:
    n = db.execute(select(func.count(Expense.id))).scalar() or 0
    return f"EXP-{n + 1:04d}"


def _out(db: Session, e: Expense) -> dict:
    exp = db.get(Account, e.expense_account_id)
    pay = db.get(Account, e.payment_account_id) if e.payment_account_id else None
    vendor = db.get(Vendor, e.vendor_id) if e.vendor_id else None
    return {
        "id": e.id, "number": e.number, "date": str(e.date), "reference": e.reference,
        "vendor_id": e.vendor_id, "vendor_name": vendor.name if vendor else None,
        "vendor_trn": vendor.trn if vendor else None,
        "vendor_address": vendor.address if vendor else None,
        "payee_name": e.payee_name, "category": e.category, "description": e.description,
        "project": e.project, "cost_center": e.cost_center, "currency": e.currency,
        "expense_account_id": e.expense_account_id,
        "expense_account_code": exp.code if exp else None,
        "expense_account_name": exp.name if exp else None,
        "payment_account_id": e.payment_account_id,
        "payment_account_code": pay.code if pay else None,
        "payment_method": e.payment_method, "paid_directly": e.paid_directly,
        "net_amount": str(e.net_amount), "vat_rate": str(e.vat_rate),
        "vat_amount": str(e.vat_amount), "total_amount": str(e.total_amount),
        "status": e.status, "journal_entry_id": e.journal_entry_id, "notes": e.notes,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }


def create_expense(db: Session, data) -> dict:
    exp_acct = _leaf(db, data.expense_account_id, "Expense")
    if data.vendor_id and not db.get(Vendor, data.vendor_id):
        raise ExpenseError("Vendor not found.")
    if data.paid_directly and not data.payment_account_id:
        raise ExpenseError("A direct payment requires a bank/cash payment account.")
    pay_acct = _leaf(db, data.payment_account_id, "Payment") if data.payment_account_id else None

    s = calc.document_summary(subtotal=data.net_amount, vat_rate=data.vat_rate)
    e = Expense(
        number=_next_number(db), date=data.date, reference=data.reference,
        vendor_id=data.vendor_id, payee_name=data.payee_name, category=data.category,
        description=data.description, project=data.project, cost_center=data.cost_center,
        currency=data.currency, expense_account_id=exp_acct.id,
        payment_account_id=pay_acct.id if pay_acct else None,
        payment_method=data.payment_method, paid_directly=data.paid_directly,
        net_amount=s["taxable"], vat_rate=data.vat_rate, vat_amount=s["vat"],
        total_amount=s["gross"], status="draft", notes=data.notes,
    )
    db.add(e)
    db.flush()
    if getattr(data, "auto_post", True):
        _post_je(db, e)
        e.status = "posted"
    db.commit()
    db.refresh(e)
    return _out(db, e)


def _post_je(db: Session, e: Expense) -> None:
    vat_in = _account_by_code(db, C.CODE_VAT_INPUT)
    lines = [JournalLineIn(account_id=e.expense_account_id, debit=e.net_amount)]
    if e.vat_amount and e.vat_amount > 0:
        lines.append(JournalLineIn(account_id=vat_in.id, debit=e.vat_amount))
    if e.paid_directly:
        credit_acct = db.get(Account, e.payment_account_id)
        memo = f"Expense {e.number} (paid {e.payment_method})"
    else:
        credit_acct = _account_by_code(db, C.CODE_AP)
        memo = f"Expense {e.number} (payable)"
    lines.append(JournalLineIn(account_id=credit_acct.id, credit=e.total_amount))
    entry = ledger.create_journal_entry(db, JournalEntryIn(
        date=e.date, memo=memo, reference=e.reference or e.number, source="expense",
        currency=e.currency, lines=lines, auto_post=True))
    e.journal_entry_id = entry.id


def post_expense(db: Session, expense_id: str) -> dict:
    e = _get(db, expense_id)
    if e.status != "draft":
        raise ExpenseError(f"Only draft expenses can be posted (status is '{e.status}').")
    _post_je(db, e)
    e.status = "posted"
    db.commit()
    db.refresh(e)
    return _out(db, e)


def void_expense(db: Session, expense_id: str) -> dict:
    e = _get(db, expense_id)
    if e.status == "void":
        raise ExpenseError("Expense is already void.")
    if e.journal_entry_id:
        ledger.void_entry(db, e.journal_entry_id)
    e.status = "void"
    db.commit()
    db.refresh(e)
    return _out(db, e)


_EXPENSE_AUDIT_FIELDS = ("date", "reference", "vendor_id", "payee_name", "category", "description",
                         "project", "cost_center", "currency", "expense_account_id",
                         "payment_account_id", "payment_method", "paid_directly", "net_amount",
                         "vat_rate", "vat_amount", "total_amount")


def update_expense(db: Session, expense_id: str, data, actor: str | None = None,
                   reason: str | None = None) -> dict:
    """Edit an expense via reverse-and-repost: the original journal entry is voided and a fresh
    one is posted from the recalculated figures, preserving the expense id + number. Blocked in
    a locked period; writes a transaction-edit audit (before→after + reason + status)."""
    from . import audit, system_settings
    e = _get(db, expense_id)
    if e.status == "void":
        raise ExpenseError("Cannot edit a voided expense.")
    system_settings.assert_period_open(db, e.date, "edit")
    system_settings.assert_period_open(db, data.date, "edit")
    exp_acct = _leaf(db, data.expense_account_id, "Expense")
    if data.vendor_id and not db.get(Vendor, data.vendor_id):
        raise ExpenseError("Vendor not found.")
    if data.paid_directly and not data.payment_account_id:
        raise ExpenseError("A direct payment requires a bank/cash payment account.")
    pay_acct = _leaf(db, data.payment_account_id, "Payment") if data.payment_account_id else None

    prev_status = e.status
    before = {f: str(getattr(e, f, None)) for f in _EXPENSE_AUDIT_FIELDS}
    if e.journal_entry_id:
        ledger.void_entry(db, e.journal_entry_id)   # reverse the original GL effect
        e.journal_entry_id = None

    s = calc.document_summary(subtotal=data.net_amount, vat_rate=data.vat_rate)
    e.date = data.date
    e.reference = data.reference
    e.vendor_id = data.vendor_id
    e.payee_name = data.payee_name
    e.category = data.category
    e.description = data.description
    e.project = data.project
    e.cost_center = data.cost_center
    e.currency = data.currency
    e.expense_account_id = exp_acct.id
    e.payment_account_id = pay_acct.id if pay_acct else None
    e.payment_method = data.payment_method
    e.paid_directly = data.paid_directly
    e.net_amount = s["taxable"]
    e.vat_rate = data.vat_rate
    e.vat_amount = s["vat"]
    e.total_amount = s["gross"]
    e.notes = data.notes
    db.flush()
    if prev_status == "posted":
        _post_je(db, e)                              # re-post the corrected entry
        e.status = "posted"
    after = {f: str(getattr(e, f, None)) for f in _EXPENSE_AUDIT_FIELDS}
    audit.record_txn_audit(db, entity_type="expense", entity_id=e.id, doc_number=e.number,
                           actor=actor, action="edit", reason=reason, prev_status=prev_status,
                           new_status=e.status, changes=audit.diff(before, after))
    db.commit()
    db.refresh(e)
    return _out(db, e)


def expense_audit(db: Session, expense_id: str) -> list[dict]:
    from . import audit
    return audit.list_txn_audit(db, "expense", expense_id)


def _get(db: Session, expense_id: str) -> Expense:
    e = db.get(Expense, expense_id)
    if not e:
        raise ExpenseError("Expense not found.")
    return e


def get_expense(db: Session, expense_id: str) -> dict:
    return _out(db, _get(db, expense_id))


def list_expenses(db: Session, status: str | None = None, project: str | None = None,
                  vendor_id: str | None = None) -> list[dict]:
    stmt = select(Expense).order_by(Expense.date.desc(), Expense.number.desc())
    if status:
        stmt = stmt.where(Expense.status == status)
    if project:
        stmt = stmt.where(Expense.project == project)
    if vendor_id:
        stmt = stmt.where(Expense.vendor_id == vendor_id)
    return [_out(db, e) for e in db.execute(stmt).scalars()]


# ── Reports ───────────────────────────────────────────────────────────────────────────────
def expense_report(db: Session, group_by: str = "category", start: _Date | None = None,
                   end: _Date | None = None, direct_only: bool = False) -> dict:
    """Aggregate posted expenses by category | vendor | project, with a VAT total."""
    stmt = select(Expense).where(Expense.status == "posted")
    if start:
        stmt = stmt.where(Expense.date >= start)
    if end:
        stmt = stmt.where(Expense.date <= end)
    if direct_only:
        stmt = stmt.where(Expense.paid_directly.is_(True))
    rows = list(db.execute(stmt).scalars())
    vendors = {v.id: v.name for v in db.execute(select(Vendor)).scalars()}

    def key(e: Expense) -> str:
        if group_by == "vendor":
            return vendors.get(e.vendor_id, e.payee_name or "—")
        if group_by == "project":
            return e.project or "(no project)"
        return e.category or "(uncategorised)"

    groups: dict[str, dict] = {}
    tot_net = tot_vat = tot_gross = 0.0
    for e in rows:
        g = groups.setdefault(key(e), {"group": key(e), "net": 0.0, "vat": 0.0, "gross": 0.0, "count": 0})
        g["net"] += float(e.net_amount); g["vat"] += float(e.vat_amount); g["gross"] += float(e.total_amount)
        g["count"] += 1
        tot_net += float(e.net_amount); tot_vat += float(e.vat_amount); tot_gross += float(e.total_amount)
    return {
        "group_by": group_by, "direct_only": direct_only,
        "rows": sorted(({**g, "net": round(g["net"], 2), "vat": round(g["vat"], 2),
                         "gross": round(g["gross"], 2)} for g in groups.values()),
                       key=lambda r: r["gross"], reverse=True),
        "total_net": round(tot_net, 2), "total_vat": round(tot_vat, 2), "total_gross": round(tot_gross, 2),
        "count": len(rows),
    }
