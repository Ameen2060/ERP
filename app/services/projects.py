"""Project Master — single source of truth for project metadata, with financial roll-ups
computed live from transactions that carry the project's `code` (free-text field already on
invoices/bills/expenses/advances). No duplication of transactional data."""

from __future__ import annotations

from datetime import date as _Date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Customer,
    CustomerAdvance,
    Expense,
    Project,
    ProjectEvent,
    SalesInvoice,
    VendorAdvance,
    VendorBill,
)

_POSTED = ("posted", "partial", "paid")
_STATUSES = ("planned", "active", "on_hold", "completed", "cancelled", "archived")


class ProjectError(ValueError):
    """Domain error → HTTP 400."""


def _f(x) -> float:
    return float(x or 0)


def _out(db: Session, p: Project) -> dict:
    cust = db.get(Customer, p.customer_id) if p.customer_id else None
    return {
        "id": p.id, "code": p.code, "name": p.name, "description": p.description,
        "customer_id": p.customer_id, "customer_name": cust.name if cust else None,
        "owner": p.owner, "project_type": p.project_type, "location": p.location, "manager": p.manager,
        "start_date": str(p.start_date) if p.start_date else None,
        "expected_completion": str(p.expected_completion) if p.expected_completion else None,
        "actual_completion": str(p.actual_completion) if p.actual_completion else None,
        "status": p.status, "contract_value": str(p.contract_value), "budget": str(p.budget),
        "currency": p.currency, "vat_treatment": p.vat_treatment,
        "retention_percent": str(p.retention_percent), "retention_amount": str(p.retention_amount),
        "advance_percent": str(p.advance_percent), "advance_amount": str(p.advance_amount),
        "progress_percent": str(p.progress_percent),
        "notes": p.notes, "created_by": p.created_by, "updated_by": p.updated_by,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def _log(db: Session, p: Project, action: str, actor: str, note: str | None = None) -> None:
    db.add(ProjectEvent(project_id=p.id, action=action, actor=actor or "local", note=note))


def _get(db: Session, project_id: str) -> Project:
    p = db.get(Project, project_id)
    if not p:
        # allow lookup by code too
        p = db.execute(select(Project).where(Project.code == project_id)).scalars().first()
    if not p:
        raise ProjectError("Project not found.")
    return p


def create(db: Session, data, actor: str = "local") -> dict:
    code = (data.code or "").strip()
    name = (data.name or "").strip()
    if not code:
        raise ProjectError("Project code is required.")
    if not name:
        raise ProjectError("Project name is required.")
    if data.status and data.status not in _STATUSES:
        raise ProjectError(f"Status must be one of: {', '.join(_STATUSES)}.")
    if db.execute(select(Project).where(Project.code == code)).scalars().first():
        raise ProjectError(f"Project code '{code}' already exists.")
    if data.customer_id and not db.get(Customer, data.customer_id):
        raise ProjectError("Customer not found.")
    p = Project(code=code, name=name, description=data.description, customer_id=data.customer_id,
                owner=data.owner, project_type=data.project_type, location=data.location,
                manager=data.manager, start_date=data.start_date, expected_completion=data.expected_completion,
                actual_completion=data.actual_completion, status=data.status or "active",
                contract_value=data.contract_value or 0, budget=data.budget or 0,
                currency=data.currency or "AED", vat_treatment=data.vat_treatment,
                retention_percent=data.retention_percent or 0, retention_amount=data.retention_amount or 0,
                advance_percent=data.advance_percent or 0, advance_amount=data.advance_amount or 0,
                progress_percent=data.progress_percent or 0,
                notes=data.notes, created_by=actor, updated_by=actor)
    db.add(p)
    db.flush()
    _log(db, p, "created", actor, note=f"Project {code} created.")
    db.commit()
    db.refresh(p)
    return _out(db, p)


def update(db: Session, project_id: str, data, actor: str = "local") -> dict:
    p = _get(db, project_id)
    code = (data.code or "").strip()
    if not code:
        raise ProjectError("Project code is required.")
    if code != p.code and db.execute(select(Project).where(Project.code == code)).scalars().first():
        raise ProjectError(f"Project code '{code}' already exists.")
    if data.status and data.status not in _STATUSES:
        raise ProjectError(f"Status must be one of: {', '.join(_STATUSES)}.")
    old_status = p.status
    p.code = code
    p.name = (data.name or "").strip() or p.name
    p.description = data.description
    p.customer_id = data.customer_id
    p.owner = data.owner
    p.project_type = data.project_type
    p.location = data.location
    p.manager = data.manager
    p.start_date = data.start_date
    p.expected_completion = data.expected_completion
    p.actual_completion = data.actual_completion
    p.status = data.status or p.status
    p.contract_value = data.contract_value or 0
    p.budget = data.budget or 0
    p.currency = data.currency or "AED"
    p.vat_treatment = data.vat_treatment
    p.retention_percent = data.retention_percent or 0
    p.retention_amount = data.retention_amount or 0
    p.advance_percent = data.advance_percent or 0
    p.advance_amount = data.advance_amount or 0
    p.progress_percent = data.progress_percent or 0
    p.notes = data.notes
    p.updated_by = actor
    _log(db, p, "updated", actor)
    if data.status and data.status != old_status:
        _log(db, p, "status_change", actor, note=f"{old_status} → {data.status}")
    db.commit()
    db.refresh(p)
    return _out(db, p)


def archive(db: Session, project_id: str, actor: str = "local") -> dict:
    p = _get(db, project_id)
    p.status = "archived"
    p.updated_by = actor
    _log(db, p, "archived", actor)
    db.commit()
    db.refresh(p)
    return _out(db, p)


def list_projects(db: Session, status: str | None = None, include_archived: bool = True) -> list[dict]:
    stmt = select(Project).order_by(Project.code)
    if status:
        stmt = stmt.where(Project.status == status)
    elif not include_archived:
        stmt = stmt.where(Project.status != "archived")
    return [_out(db, p) for p in db.execute(stmt).scalars()]


def get(db: Session, project_id: str) -> dict:
    return _out(db, _get(db, project_id))


def events(db: Session, project_id: str) -> list[dict]:
    p = _get(db, project_id)
    evs = db.execute(select(ProjectEvent).where(ProjectEvent.project_id == p.id)
                     .order_by(ProjectEvent.at.desc())).scalars()
    return [{"at": e.at.isoformat() if e.at else None, "actor": e.actor, "action": e.action, "note": e.note}
            for e in evs]


# ── Financial roll-up (computed from transactions carrying this project code) ────────────────
def financials(db: Session, code: str) -> dict:
    invs = list(db.execute(select(SalesInvoice).where(
        SalesInvoice.project == code, SalesInvoice.status.in_(_POSTED))).scalars())
    bills = list(db.execute(select(VendorBill).where(
        VendorBill.project == code, VendorBill.status.in_(_POSTED))).scalars())
    exps = list(db.execute(select(Expense).where(
        Expense.project == code, Expense.status == "posted")).scalars())
    cadv = list(db.execute(select(CustomerAdvance).where(CustomerAdvance.project == code)).scalars())
    vadv = list(db.execute(select(VendorAdvance).where(VendorAdvance.project == code)).scalars())

    def ret_out(x):
        return _f(x.retention_amount) - _f(x.retention_released)

    def collectible(x):
        return _f(x.grand_total) - ret_out(x)

    total_sales = sum(_f(i.net_total) for i in invs)
    output_vat = sum(_f(i.vat_total) for i in invs)
    collections = sum(_f(i.amount_paid) for i in invs)
    receivables = sum(collectible(i) - _f(i.amount_paid) for i in invs)
    retention_receivable = sum(ret_out(i) for i in invs)

    purchases = sum(_f(b.net_total) for b in bills)
    expenses_total = sum(_f(e.net_amount) for e in exps)
    input_vat = sum(_f(b.vat_total) for b in bills) + sum(_f(e.vat_amount) for e in exps)
    vendor_payments = sum(_f(b.amount_paid) for b in bills)
    payables = sum(collectible(b) - _f(b.amount_paid) for b in bills)
    retention_payable = sum(ret_out(b) for b in bills)

    revenue = round(total_sales, 2)
    cost = round(purchases + expenses_total, 2)
    gross_profit = round(revenue - cost, 2)
    margin = round(gross_profit / revenue * 100, 2) if revenue else 0.0
    budget = _f(db.execute(select(Project.budget).where(Project.code == code)).scalars().first())
    return {
        "code": code, "counts": {"invoices": len(invs), "bills": len(bills), "expenses": len(exps)},
        "total_sales": revenue, "output_vat": round(output_vat, 2), "collections": round(collections, 2),
        "receivables": round(receivables, 2), "retention_receivable": round(retention_receivable, 2),
        "total_purchases": round(purchases, 2), "total_expenses": round(expenses_total, 2),
        "input_vat": round(input_vat, 2), "vendor_payments": round(vendor_payments, 2),
        "payables": round(payables, 2), "retention_payable": round(retention_payable, 2),
        "customer_advances": round(sum(_f(a.amount) for a in cadv), 2),
        "vendor_advances": round(sum(_f(a.amount) for a in vadv), 2),
        "revenue": revenue, "cost": cost, "gross_profit": gross_profit, "gross_margin": margin,
        "budget": round(budget, 2), "budget_variance": round(budget - cost, 2),
        "net_vat": round(output_vat - input_vat, 2),
    }


def transactions(db: Session, code: str) -> dict:
    """Flat transaction listing for a project (for drill-down)."""
    rows = []
    for i in db.execute(select(SalesInvoice).where(SalesInvoice.project == code)).scalars():
        rows.append({"type": "Sales Invoice", "id": i.id, "number": i.number, "date": str(i.date),
                     "amount": str(i.grand_total), "status": i.status, "link": "invoice"})
    for b in db.execute(select(VendorBill).where(VendorBill.project == code)).scalars():
        rows.append({"type": "Vendor Bill", "id": b.id, "number": b.number, "date": str(b.date),
                     "amount": str(b.grand_total), "status": b.status, "link": "bill"})
    for e in db.execute(select(Expense).where(Expense.project == code)).scalars():
        rows.append({"type": "Expense", "id": e.id, "number": e.number, "date": str(e.date),
                     "amount": str(e.total_amount), "status": e.status, "link": "expense"})
    rows.sort(key=lambda r: r["date"])
    return {"code": code, "rows": rows}


def dashboard(db: Session) -> dict:
    projects = list(db.execute(select(Project)).scalars())
    by_status: dict[str, int] = {}
    for p in projects:
        by_status[p.status] = by_status.get(p.status, 0) + 1
    contract_total = sum(_f(p.contract_value) for p in projects)
    agg = {"revenue": 0.0, "cost": 0.0, "gross_profit": 0.0, "receivables": 0.0, "payables": 0.0}
    for p in projects:
        if p.status in ("cancelled", "archived"):
            continue
        f = financials(db, p.code)
        agg["revenue"] += f["revenue"]; agg["cost"] += f["cost"]; agg["gross_profit"] += f["gross_profit"]
        agg["receivables"] += f["receivables"]; agg["payables"] += f["payables"]
    return {
        "total_projects": len(projects), "by_status": by_status,
        "active": by_status.get("active", 0), "completed": by_status.get("completed", 0),
        "on_hold": by_status.get("on_hold", 0), "contract_value": round(contract_total, 2),
        **{k: round(v, 2) for k, v in agg.items()},
    }


def portfolio_report(db: Session, status: str | None = None) -> dict:
    """Per-project P&L: contract value, revenue, cost, gross profit/margin, receivables,
    payables, budget variance — for the projects report + export."""
    stmt = select(Project).order_by(Project.code)
    if status:
        stmt = stmt.where(Project.status == status)
    projects = list(db.execute(stmt).scalars())
    rows = []
    tot = {"contract_value": 0.0, "revenue": 0.0, "cost": 0.0, "gross_profit": 0.0,
           "receivables": 0.0, "payables": 0.0}
    for p in projects:
        f = financials(db, p.code)
        cv = _f(p.contract_value)
        row = {"code": p.code, "name": p.name, "status": p.status, "contract_value": round(cv, 2),
               "revenue": f["revenue"], "cost": f["cost"], "gross_profit": f["gross_profit"],
               "gross_margin": f["gross_margin"], "receivables": f["receivables"],
               "payables": f["payables"], "budget": f["budget"], "budget_variance": f["budget_variance"],
               "progress_percent": _f(p.progress_percent)}
        rows.append(row)
        tot["contract_value"] += cv; tot["revenue"] += f["revenue"]; tot["cost"] += f["cost"]
        tot["gross_profit"] += f["gross_profit"]; tot["receivables"] += f["receivables"]
        tot["payables"] += f["payables"]
    return {"rows": rows, "totals": {k: round(v, 2) for k, v in tot.items()},
            "overall_margin": round(tot["gross_profit"] / tot["revenue"] * 100, 2) if tot["revenue"] else 0.0}
