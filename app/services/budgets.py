"""Budget Management — company & project budgets. The budget module owns the *budget* numbers;
*actuals* are always pulled live from the posted ledger (company scope) or the project's
sub-ledger lines (project scope). Nothing is duplicated.

Workflow: draft → submitted → approved → locked → closed. Lines are editable only while
draft/submitted; changing an approved budget means creating a new version (revision)."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..models import (
    Account,
    Budget,
    BudgetEvent,
    BudgetLine,
    Expense,
    JournalEntry,
    SalesInvoice,
    VendorBill,
)

ZERO = Decimal("0.00")
_EDITABLE = ("draft", "submitted")
ALERT_THRESHOLDS = [(110, "critical"), (100, "over"), (90, "high"), (80, "warning")]


class BudgetError(ValueError):
    """Domain error → HTTP 400."""


def _f(x) -> float:
    return float(x or 0)


def _get(db: Session, budget_id: str) -> Budget:
    b = db.execute(select(Budget).where(Budget.id == budget_id)
                   .options(selectinload(Budget.lines))).scalar_one_or_none()
    if not b:
        raise BudgetError("Budget not found.")
    return b


def _log(db: Session, b: Budget, action: str, actor: str, note: str | None = None) -> None:
    db.add(BudgetEvent(budget_id=b.id, action=action, actor=actor or "local", note=note))


def _out(db: Session, b: Budget, with_lines: bool = True) -> dict:
    accts = {a.id: a for a in db.execute(select(Account)).scalars()} if with_lines else {}
    lines = [{"id": l.id, "account_id": l.account_id,
              "account_code": accts[l.account_id].code if l.account_id in accts else None,
              "account_name": accts[l.account_id].name if l.account_id in accts else None,
              "cost_center": l.cost_center, "month": l.month, "amount": str(l.amount)}
             for l in sorted(b.lines, key=lambda x: (x.month,))] if with_lines else []
    return {
        "id": b.id, "name": b.name, "fiscal_year": b.fiscal_year, "version": b.version,
        "scope": b.scope, "project_code": b.project_code, "period_type": b.period_type,
        "currency": b.currency, "status": b.status, "owner": b.owner, "notes": b.notes,
        "approved_at": b.approved_at.isoformat() if b.approved_at else None,
        "total_budget": str(sum((l.amount for l in b.lines), Decimal(0))),
        "line_count": len(b.lines), "lines": lines,
        "updated_at": b.updated_at.isoformat() if b.updated_at else None,
    }


# ── CRUD ────────────────────────────────────────────────────────────────────────────────────
def create(db: Session, data, actor: str = "local") -> dict:
    if not (data.name or "").strip():
        raise BudgetError("Budget name is required.")
    if data.scope not in ("company", "project"):
        raise BudgetError("scope must be 'company' or 'project'.")
    if data.scope == "project" and not (data.project_code or "").strip():
        raise BudgetError("A project budget requires a project_code.")
    # one budget per (scope, fiscal_year, version, project) — prevent duplicates
    dup = db.execute(select(Budget).where(
        Budget.scope == data.scope, Budget.fiscal_year == data.fiscal_year,
        Budget.version == (data.version or "v1"),
        Budget.project_code == (data.project_code or None))).scalars().first()
    if dup:
        raise BudgetError("A budget with this scope/year/version already exists — create a new version.")
    b = Budget(name=data.name.strip(), fiscal_year=int(data.fiscal_year), version=data.version or "v1",
               scope=data.scope, project_code=(data.project_code or None), period_type=data.period_type or "annual",
               currency=data.currency or "AED", owner=data.owner, notes=data.notes,
               created_by=actor, updated_by=actor)
    db.add(b)
    db.flush()
    _set_lines(db, b, data.lines or [])
    _log(db, b, "created", actor)
    db.commit()
    db.refresh(b)
    return _out(db, b)


def _set_lines(db: Session, b: Budget, lines) -> None:
    for l in list(b.lines):
        db.delete(l)
    b.lines.clear()
    db.flush()
    for ln in lines:
        acc = db.get(Account, ln.account_id)
        if not acc:
            raise BudgetError(f"Account '{ln.account_id}' not found.")
        if acc.is_group:
            raise BudgetError(f"Account '{acc.code}' is a group account.")
        m = int(ln.month or 0)
        if m < 0 or m > 12:
            raise BudgetError("month must be 0 (full year) or 1-12.")
        db.add(BudgetLine(budget_id=b.id, account_id=ln.account_id, cost_center=ln.cost_center,
                          month=m, amount=ln.amount or 0))


def update_lines(db: Session, budget_id: str, lines, actor: str = "local") -> dict:
    b = _get(db, budget_id)
    if b.status not in _EDITABLE:
        raise BudgetError(f"Budget is '{b.status}' and locked for edits — create a new version to change it.")
    _set_lines(db, b, lines)
    b.updated_by = actor
    _log(db, b, "lines_updated", actor)
    db.commit()
    db.refresh(b)
    return _out(db, b)


def list_budgets(db: Session, fiscal_year: int | None = None, scope: str | None = None) -> list[dict]:
    stmt = select(Budget).order_by(Budget.fiscal_year.desc(), Budget.name)
    if fiscal_year:
        stmt = stmt.where(Budget.fiscal_year == fiscal_year)
    if scope:
        stmt = stmt.where(Budget.scope == scope)
    return [_out(db, b, with_lines=False) for b in db.execute(stmt.options(selectinload(Budget.lines))).scalars()]


def get(db: Session, budget_id: str) -> dict:
    return _out(db, _get(db, budget_id))


# ── Workflow ─────────────────────────────────────────────────────────────────────────────────
_FLOW = {"submit": ("draft", "submitted"), "approve": ("submitted", "approved"),
         "lock": ("approved", "locked"), "close": ("locked", "closed")}


def transition(db: Session, budget_id: str, action: str, actor: str = "local") -> dict:
    b = _get(db, budget_id)
    if action not in _FLOW:
        raise BudgetError("Unknown action.")
    frm, to = _FLOW[action]
    if b.status != frm:
        raise BudgetError(f"Cannot {action} a '{b.status}' budget (must be '{frm}').")
    b.status = to
    b.updated_by = actor
    if to == "approved":
        from datetime import datetime, timezone
        b.approved_at = datetime.now(timezone.utc)
    _log(db, b, action, actor, note=f"{frm} → {to}")
    db.commit()
    db.refresh(b)
    return _out(db, b)


def create_revision(db: Session, budget_id: str, new_version: str, actor: str = "local", reason: str | None = None) -> dict:
    src = _get(db, budget_id)
    if db.execute(select(Budget).where(
            Budget.scope == src.scope, Budget.fiscal_year == src.fiscal_year,
            Budget.version == new_version, Budget.project_code == src.project_code)).scalars().first():
        raise BudgetError(f"Version '{new_version}' already exists.")
    b = Budget(name=src.name, fiscal_year=src.fiscal_year, version=new_version, scope=src.scope,
               project_code=src.project_code, period_type=src.period_type, currency=src.currency,
               owner=src.owner, notes=src.notes, status="draft", created_by=actor, updated_by=actor)
    db.add(b)
    db.flush()
    for l in src.lines:
        db.add(BudgetLine(budget_id=b.id, account_id=l.account_id, cost_center=l.cost_center,
                          month=l.month, amount=l.amount))
    _log(db, b, "revision_created", actor, note=f"from {src.version}: {reason or ''}")
    db.commit()
    db.refresh(b)
    return _out(db, b)


def events(db: Session, budget_id: str) -> list[dict]:
    b = _get(db, budget_id)
    evs = db.execute(select(BudgetEvent).where(BudgetEvent.budget_id == b.id)
                     .order_by(BudgetEvent.at.desc())).scalars()
    return [{"at": e.at.isoformat() if e.at else None, "actor": e.actor, "action": e.action, "note": e.note}
            for e in evs]


# ── Actuals (from the ledger / sub-ledger) ────────────────────────────────────────────────────
def _gl_actual(db: Session, account: Account, year: int, month: int) -> Decimal:
    d = c = Decimal(0)
    for e in db.execute(select(JournalEntry).where(JournalEntry.status == "posted")
                        .options(selectinload(JournalEntry.lines))).scalars():
        if e.date.year != year or (month and e.date.month != month):
            continue
        for l in e.lines:
            if l.account_id == account.id:
                d += l.debit
                c += l.credit
    return (c - d) if account.normal_balance == "credit" else (d - c)


def _project_actuals_by_account(db: Session, code: str) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    posted = ("posted", "partial", "paid")
    for i in db.execute(select(SalesInvoice).where(
            SalesInvoice.project == code, SalesInvoice.status.in_(posted))
            .options(selectinload(SalesInvoice.lines))).scalars():
        for l in i.lines:
            out[l.revenue_account_id] = out.get(l.revenue_account_id, ZERO) + l.net_amount
    for b in db.execute(select(VendorBill).where(
            VendorBill.project == code, VendorBill.status.in_(posted))
            .options(selectinload(VendorBill.lines))).scalars():
        for l in b.lines:
            out[l.expense_account_id] = out.get(l.expense_account_id, ZERO) + l.net_amount
    for e in db.execute(select(Expense).where(Expense.project == code, Expense.status == "posted")).scalars():
        out[e.expense_account_id] = out.get(e.expense_account_id, ZERO) + e.net_amount
    return out


def _alert(util: float) -> str | None:
    for thr, label in ALERT_THRESHOLDS:
        if util >= thr:
            return label
    return None


def budget_vs_actual(db: Session, budget_id: str) -> dict:
    b = _get(db, budget_id)
    accts = {a.id: a for a in db.execute(select(Account)).scalars()}
    proj_actuals = _project_actuals_by_account(db, b.project_code) if b.scope == "project" else {}

    rows = []
    tot_b = tot_a = 0.0
    for l in b.lines:
        acc = accts.get(l.account_id)
        if not acc:
            continue
        budget = _f(l.amount)
        if b.scope == "project":
            actual = _f(proj_actuals.get(l.account_id, ZERO))   # project = annual, sub-ledger based
        else:
            actual = _f(_gl_actual(db, acc, b.fiscal_year, l.month))
        variance = round(actual - budget, 2)
        remaining = round(budget - actual, 2)
        util = round(actual / budget * 100, 2) if budget else 0.0
        is_expense = acc.type == "expense"
        rows.append({
            "account_code": acc.code, "account_name": acc.name, "account_type": acc.type,
            "month": l.month, "budget": round(budget, 2), "actual": round(actual, 2),
            "variance": variance, "variance_pct": round(variance / budget * 100, 2) if budget else 0.0,
            "remaining": remaining, "utilization": util,
            # for expenses over-budget is bad; for revenue below-budget is bad
            "status": ("over_budget" if variance > 0 else "under_budget") if is_expense
                      else ("above_budget" if variance >= 0 else "below_budget"),
            "alert": _alert(util) if is_expense else None,
        })
        tot_b += budget
        tot_a += actual
    rows.sort(key=lambda r: (r["account_code"], r["month"]))
    return {"budget_id": b.id, "name": b.name, "fiscal_year": b.fiscal_year, "scope": b.scope,
            "project_code": b.project_code, "status": b.status, "rows": rows,
            "total_budget": round(tot_b, 2), "total_actual": round(tot_a, 2),
            "total_variance": round(tot_a - tot_b, 2),
            "total_remaining": round(tot_b - tot_a, 2),
            "utilization": round(tot_a / tot_b * 100, 2) if tot_b else 0.0,
            "alerts": [r for r in rows if r["alert"] in ("over", "critical")]}


def forecast(db: Session, budget_id: str, months_elapsed: int | None = None) -> dict:
    """Year-end projection from run-rate: for each account, project the full-year actual as
    actual-to-date × (12 / months elapsed), and compare to the annual budget. Purely derived
    from posted actuals — no data stored."""
    from datetime import date as _D
    b = _get(db, budget_id)
    if months_elapsed is None:
        today = _D.today()
        months_elapsed = today.month if today.year == b.fiscal_year else (12 if today.year > b.fiscal_year else 1)
    months_elapsed = max(1, min(12, int(months_elapsed)))
    factor = 12.0 / months_elapsed
    bva = budget_vs_actual(db, budget_id)
    # Aggregate the (possibly per-month) rows to one row per account.
    by_acct: dict[str, dict] = {}
    for r in bva["rows"]:
        a = by_acct.setdefault(r["account_code"], {"account_code": r["account_code"],
            "account_name": r["account_name"], "account_type": r["account_type"], "budget": 0.0, "actual": 0.0})
        a["budget"] += r["budget"]; a["actual"] += r["actual"]
    rows = []
    tot_b = tot_a = tot_p = 0.0
    for a in by_acct.values():
        projected = round(a["actual"] * factor, 2)
        proj_var = round(projected - a["budget"], 2)
        is_expense = a["account_type"] == "expense"
        rows.append({**{k: round(a[k], 2) if isinstance(a[k], float) else a[k] for k in a},
                     "projected": projected, "projected_variance": proj_var,
                     "projected_pct": round(projected / a["budget"] * 100, 2) if a["budget"] else 0.0,
                     "status": ("over_budget" if proj_var > 0 else "under_budget") if is_expense
                               else ("above_budget" if proj_var >= 0 else "below_budget")})
        tot_b += a["budget"]; tot_a += a["actual"]; tot_p += projected
    rows.sort(key=lambda r: r["account_code"])
    return {"budget_id": b.id, "name": b.name, "fiscal_year": b.fiscal_year, "scope": b.scope,
            "months_elapsed": months_elapsed, "rows": rows,
            "total_budget": round(tot_b, 2), "total_actual_ytd": round(tot_a, 2),
            "total_projected": round(tot_p, 2), "projected_variance": round(tot_p - tot_b, 2),
            "projected_utilization": round(tot_p / tot_b * 100, 2) if tot_b else 0.0}


def dashboard(db: Session, fiscal_year: int | None = None) -> dict:
    stmt = select(Budget).where(Budget.status.in_(("approved", "locked", "closed")))
    if fiscal_year:
        stmt = stmt.where(Budget.fiscal_year == fiscal_year)
    budgets = list(db.execute(stmt.options(selectinload(Budget.lines))).scalars())
    tb = ta = 0.0
    for b in budgets:
        bva = budget_vs_actual(db, b.id)
        tb += bva["total_budget"]
        ta += bva["total_actual"]
    return {"approved_budgets": len(budgets), "total_budget": round(tb, 2), "total_actual": round(ta, 2),
            "variance": round(ta - tb, 2), "utilization": round(ta / tb * 100, 2) if tb else 0.0}
