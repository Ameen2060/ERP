"""Reusable drill-down engine.

A single registry maps every dashboard KPI to a builder that queries the live
ledger/subledgers and returns the underlying records — each linking back to its source
document (journal entry, invoice, bill) — together with a reconciliation check that the
rows sum to the dashboard figure. New KPIs get drill-down by adding one registry entry;
there is no per-card hardcoded logic.

Scope note: this is a pure double-entry ledger. Records drill to their source journal
entry / invoice / bill (Levels 1–3). There are no uploaded documents or OCR here, so
Level-4 "source region in the original document" belongs to the VAT Platform, not this app.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import constants as C
from ..constants import AccountType, NormalBalance
from ..models import Account, Customer, JournalEntry, JournalLine, SalesInvoice
from ..schemas import DrillColumn, DrillDown, DrillKey, DrillLink, DrillRow, q
from . import reports

ZERO = Decimal(0)


def _m(v: Decimal) -> str:
    return f"{q(Decimal(v)):.2f}"


# ── Generic builders ─────────────────────────────────────────────────────────────────────
def _account_rows(db: Session, code: str, start: date | None, end: date | None):
    """Journal-line rows for one GL account, contribution signed in the account's normal
    direction (so they sum to the account balance the dashboard shows)."""
    acct = db.execute(select(Account).where(Account.code == code)).scalar_one_or_none()
    if not acct:
        return ZERO, []
    sign = Decimal(1) if NormalBalance(acct.normal_balance) == NormalBalance.DEBIT else Decimal(-1)
    stmt = (
        select(JournalEntry.id, JournalEntry.entry_no, JournalEntry.date, JournalEntry.memo,
               JournalEntry.source, JournalLine.debit, JournalLine.credit)
        .join(JournalLine, JournalLine.entry_id == JournalEntry.id)
        .where(JournalEntry.status == C.ENTRY_POSTED, JournalLine.account_id == acct.id)
        .order_by(JournalEntry.date, JournalEntry.entry_no)
    )
    if start:
        stmt = stmt.where(JournalEntry.date >= start)
    if end:
        stmt = stmt.where(JournalEntry.date <= end)
    rows = []
    total = ZERO
    for eid, eno, edate, memo, source, debit, credit in db.execute(stmt).all():
        amount = q((Decimal(debit) - Decimal(credit)) * sign)
        if amount == 0:
            continue
        total += amount
        rows.append(DrillRow(
            cells={"entry": f"#{eno}", "date": edate.isoformat(), "memo": memo or "—",
                   "source": source, "amount": _m(amount)},
            amount=amount, link=DrillLink(type="journal", id=eid),
        ))
    return q(total), rows


def _type_rows(db: Session, types: set[str], start: date | None, end: date | None):
    """Journal-line rows across accounts of the given types, contribution in normal direction."""
    accts = {a.id: a for a in db.execute(select(Account).where(Account.is_group.is_(False))).scalars()
             if a.type in types}
    if not accts:
        return ZERO, []
    stmt = (
        select(JournalEntry.id, JournalEntry.entry_no, JournalEntry.date, JournalEntry.memo,
               JournalLine.account_id, JournalLine.debit, JournalLine.credit)
        .join(JournalLine, JournalLine.entry_id == JournalEntry.id)
        .where(JournalEntry.status == C.ENTRY_POSTED, JournalLine.account_id.in_(list(accts)))
        .order_by(JournalEntry.date, JournalEntry.entry_no)
    )
    if start:
        stmt = stmt.where(JournalEntry.date >= start)
    if end:
        stmt = stmt.where(JournalEntry.date <= end)
    rows = []
    total = ZERO
    for eid, eno, edate, memo, acct_id, debit, credit in db.execute(stmt).all():
        acct = accts[acct_id]
        sign = Decimal(1) if NormalBalance(acct.normal_balance) == NormalBalance.DEBIT else Decimal(-1)
        amount = q((Decimal(debit) - Decimal(credit)) * sign)
        if amount == 0:
            continue
        total += amount
        rows.append(DrillRow(
            cells={"entry": f"#{eno}", "date": edate.isoformat(), "account": f"{acct.code} {acct.name}",
                   "memo": memo or "—", "amount": _m(amount)},
            amount=amount, link=DrillLink(type="journal", id=eid),
        ))
    return q(total), rows


def _invoice_rows(db: Session, overdue: bool, today: date):
    names = {c.id: c.name for c in db.execute(select(Customer)).scalars()}
    stmt = select(SalesInvoice).where(SalesInvoice.status.in_(("posted", "partial"))).order_by(SalesInvoice.date)
    rows = []
    for inv in db.execute(stmt).scalars():
        bal = q(inv.grand_total - inv.amount_paid)
        if bal <= 0:
            continue
        if overdue and not (inv.due_date and inv.due_date < today):
            continue
        rows.append(DrillRow(
            cells={"number": inv.number, "customer": names.get(inv.customer_id, "—"),
                   "date": inv.date.isoformat(), "due": inv.due_date.isoformat() if inv.due_date else "—",
                   "balance": _m(bal), "status": inv.status},
            amount=bal, link=DrillLink(type="invoice", id=inv.id),
        ))
    return rows


_GL_COLS = [DrillColumn(key="entry", label="Entry"), DrillColumn(key="date", label="Date"),
            DrillColumn(key="memo", label="Memo"), DrillColumn(key="source", label="Source"),
            DrillColumn(key="amount", label="Amount", numeric=True)]
_TYPE_COLS = [DrillColumn(key="entry", label="Entry"), DrillColumn(key="date", label="Date"),
              DrillColumn(key="account", label="Account"), DrillColumn(key="memo", label="Memo"),
              DrillColumn(key="amount", label="Amount", numeric=True)]
_INV_COLS = [DrillColumn(key="number", label="Invoice"), DrillColumn(key="customer", label="Customer"),
             DrillColumn(key="date", label="Date"), DrillColumn(key="due", label="Due"),
             DrillColumn(key="balance", label="Balance", numeric=True), DrillColumn(key="status", label="Status")]


# key -> (title, kind, builder(db, start, end, today) -> (kpi_value, columns, rows))
def _year_start(today: date) -> date:
    return date(today.year, 1, 1)


REGISTRY: dict[str, dict] = {
    "cash": {"title": "Cash", "kind": "amount", "code": "1010"},
    "bank": {"title": "Bank", "kind": "amount", "code": "1020"},
    "accounts_receivable": {"title": "Accounts Receivable", "kind": "amount", "code": "1100"},
    "accounts_payable": {"title": "Accounts Payable", "kind": "amount", "code": "2010"},
    "vat_payable": {"title": "VAT Payable", "kind": "amount", "code": "2100"},
    "vat_recoverable": {"title": "VAT Recoverable", "kind": "amount", "code": "1300"},
    "revenue_ytd": {"title": "Revenue (YTD)", "kind": "amount", "types": {"income"}, "ytd": True},
    "expenses_ytd": {"title": "Expenses (YTD)", "kind": "amount", "types": {"expense"}, "ytd": True},
    "net_profit_ytd": {"title": "Net Profit (YTD)", "kind": "amount", "net": True, "ytd": True},
    "gross_profit_ytd": {"title": "Gross Profit (YTD)", "kind": "amount", "gross": True, "ytd": True},
    "outstanding_invoices": {"title": "Outstanding Invoices", "kind": "count", "invoices": "open"},
    "overdue_invoices": {"title": "Overdue Invoices", "kind": "count", "invoices": "overdue"},
}


def list_keys() -> list[DrillKey]:
    return [DrillKey(key=k, title=v["title"], kind=v["kind"]) for k, v in REGISTRY.items()]


def build(db: Session, key: str, start: date | None = None, end: date | None = None) -> DrillDown:
    cfg = REGISTRY.get(key)
    if not cfg:
        raise ValueError(f"Unknown drill-down key '{key}'.")
    today = date.today()
    dash = reports.dashboard(db, today=today)
    kpi = getattr(dash, key, None)
    period_start = period_end = None

    if "code" in cfg:
        # Account KPIs on the dashboard are as-of-today, so bound the drill-down the same way
        # (otherwise future-dated entries would show here but not in the KPI).
        total, rows = _account_rows(db, cfg["code"], start, end or today)
        columns = _GL_COLS
    elif cfg.get("invoices"):
        rows = _invoice_rows(db, overdue=cfg["invoices"] == "overdue", today=today)
        columns = _INV_COLS
        total = q(sum((r.amount for r in rows), ZERO))
    elif cfg.get("net"):
        period_start, period_end = _year_start(today), today
        inc_t, inc_rows = _type_rows(db, {"income"}, period_start, period_end)
        exp_t, exp_rows = _type_rows(db, {"expense"}, period_start, period_end)
        # Expenses reduce profit: negate their contribution.
        for r in exp_rows:
            r.amount = q(-r.amount)
            r.cells["amount"] = _m(r.amount)
        rows = inc_rows + exp_rows
        total = q(inc_t - exp_t)
        columns = _TYPE_COLS
    elif cfg.get("gross"):
        period_start, period_end = _year_start(today), today
        inc_t, inc_rows = _type_rows(db, {"income"}, period_start, period_end)
        # Cost of sales only.
        cos_accts = [a.id for a in db.execute(select(Account).where(Account.is_group.is_(False))).scalars()
                     if a.type == AccountType.EXPENSE.value and a.is_cost_of_sales]
        cos_t = ZERO
        cos_rows = []
        if cos_accts:
            stmt = (select(JournalEntry.id, JournalEntry.entry_no, JournalEntry.date, JournalEntry.memo,
                           JournalLine.account_id, JournalLine.debit, JournalLine.credit)
                    .join(JournalLine, JournalLine.entry_id == JournalEntry.id)
                    .where(JournalEntry.status == C.ENTRY_POSTED, JournalLine.account_id.in_(cos_accts),
                           JournalEntry.date >= period_start, JournalEntry.date <= period_end)
                    .order_by(JournalEntry.date))
            accts = {a.id: a for a in db.execute(select(Account)).scalars()}
            for eid, eno, edate, memo, acct_id, debit, credit in db.execute(stmt).all():
                amt = q(-(Decimal(debit) - Decimal(credit)))  # COGS reduces gross profit
                if amt == 0:
                    continue
                cos_t += -amt
                cos_rows.append(DrillRow(cells={"entry": f"#{eno}", "date": edate.isoformat(),
                    "account": f"{accts[acct_id].code} {accts[acct_id].name}", "memo": memo or "—", "amount": _m(amt)},
                    amount=amt, link=DrillLink(type="journal", id=eid)))
        rows = inc_rows + cos_rows
        total = q(inc_t - cos_t)
        columns = _TYPE_COLS
    else:  # ytd type rows (revenue/expenses)
        period_start, period_end = _year_start(today), today
        total, rows = _type_rows(db, cfg["types"], period_start, period_end)
        columns = _TYPE_COLS

    if cfg["kind"] == "count":
        kpi_value = Decimal(kpi if kpi is not None else len(rows))
        computed = Decimal(len(rows))
        reconciles = int(kpi_value) == len(rows)
    else:
        kpi_value = q(Decimal(kpi if kpi is not None else total))
        computed = q(total)
        reconciles = abs(kpi_value - computed) < Decimal("0.01")

    return DrillDown(
        key=key, title=cfg["title"], kind=cfg["kind"], kpi_value=kpi_value, computed_total=computed,
        reconciles=reconciles, count=len(rows), period_start=period_start, period_end=period_end,
        columns=columns, rows=rows,
        note=None if reconciles else "Rows do not reconcile to the dashboard figure — investigate.",
    )
