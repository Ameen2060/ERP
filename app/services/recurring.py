"""Recurring / subscription invoicing.

A RecurringPlan holds a customer + an invoice-line template + a schedule. Generating a plan
creates a normal sales invoice through the standard sales engine (so GL, VAT, AR, statements
and reports all behave identically), records a RecurringRun, and advances the schedule.
"""

from __future__ import annotations

import calendar
import json
from datetime import date as _Date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Customer, RecurringPlan, RecurringRun, SalesInvoice
from ..schemas import InvoiceIn, InvoiceLineIn
from . import sales

_FREQ = ("weekly", "monthly", "quarterly", "annual")


class RecurringError(ValueError):
    """Domain error → HTTP 400."""


def _add_months(d: _Date, months: int) -> _Date:
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return _Date(y, m, day)


def _advance(d: _Date, freq: str) -> _Date:
    if freq == "weekly":
        return d.fromordinal(d.toordinal() + 7)
    if freq == "monthly":
        return _add_months(d, 1)
    if freq == "quarterly":
        return _add_months(d, 3)
    return _add_months(d, 12)   # annual


def _out(db: Session, p: RecurringPlan) -> dict:
    cust = db.get(Customer, p.customer_id)
    lines = json.loads(p.lines_json) if p.lines_json else []
    amount = sum(Decimal(str(l.get("quantity", 1))) * Decimal(str(l.get("unit_price", 0))) for l in lines)
    runs = db.execute(select(func.count(RecurringRun.id)).where(RecurringRun.plan_id == p.id)).scalar() or 0
    return {
        "id": p.id, "name": p.name, "customer_id": p.customer_id,
        "customer_name": cust.name if cust else None, "frequency": p.frequency, "currency": p.currency,
        "start_date": str(p.start_date), "next_run_date": str(p.next_run_date),
        "max_occurrences": p.max_occurrences, "occurrences_done": p.occurrences_done,
        "auto_post": p.auto_post, "active": p.active, "lines": lines,
        "line_net": str(amount), "generated_count": runs, "notes": p.notes,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


def _validate(db: Session, data) -> list[dict]:
    if not db.get(Customer, data.customer_id):
        raise RecurringError("Customer not found.")
    if data.frequency not in _FREQ:
        raise RecurringError(f"Frequency must be one of: {', '.join(_FREQ)}.")
    lines = [l.model_dump() if hasattr(l, "model_dump") else dict(l) for l in (data.lines or [])]
    if not lines:
        raise RecurringError("At least one line is required.")
    return lines


def create_plan(db: Session, data) -> dict:
    lines = _validate(db, data)
    p = RecurringPlan(
        name=data.name, customer_id=data.customer_id, frequency=data.frequency,
        currency=data.currency or "AED", start_date=data.start_date,
        next_run_date=data.start_date, max_occurrences=data.max_occurrences,
        auto_post=data.auto_post if data.auto_post is not None else True,
        lines_json=json.dumps(lines, default=str), notes=data.notes)
    db.add(p)
    db.commit()
    db.refresh(p)
    return _out(db, p)


def update_plan(db: Session, plan_id: str, data) -> dict:
    p = _get(db, plan_id)
    lines = _validate(db, data)
    p.name = data.name
    p.customer_id = data.customer_id
    p.frequency = data.frequency
    p.currency = data.currency or "AED"
    p.max_occurrences = data.max_occurrences
    p.auto_post = data.auto_post if data.auto_post is not None else True
    p.lines_json = json.dumps(lines, default=str)
    p.notes = data.notes
    # Only move the schedule if the start date changed and nothing has generated yet.
    if p.occurrences_done == 0:
        p.start_date = data.start_date
        p.next_run_date = data.start_date
    db.commit()
    db.refresh(p)
    return _out(db, p)


def _get(db: Session, plan_id: str) -> RecurringPlan:
    p = db.get(RecurringPlan, plan_id)
    if not p:
        raise RecurringError("Recurring plan not found.")
    return p


def get_plan(db: Session, plan_id: str) -> dict:
    return _out(db, _get(db, plan_id))


def list_plans(db: Session, active_only: bool = False) -> list[dict]:
    stmt = select(RecurringPlan).order_by(RecurringPlan.next_run_date)
    if active_only:
        stmt = stmt.where(RecurringPlan.active.is_(True))
    return [_out(db, p) for p in db.execute(stmt).scalars()]


def set_active(db: Session, plan_id: str, active: bool) -> dict:
    p = _get(db, plan_id)
    p.active = bool(active)
    db.commit()
    return _out(db, p)


def runs_for(db: Session, plan_id: str) -> list[dict]:
    rows = db.execute(select(RecurringRun).where(RecurringRun.plan_id == plan_id)
                      .order_by(RecurringRun.run_date.desc())).scalars()
    return [{"id": r.id, "run_date": str(r.run_date), "invoice_id": r.invoice_id,
             "invoice_number": r.invoice_number, "amount": str(r.amount)} for r in rows]


def _generate(db: Session, p: RecurringPlan, on_date: _Date) -> dict:
    lines = json.loads(p.lines_json) if p.lines_json else []
    inv = sales.create_invoice(db, InvoiceIn(
        customer_id=p.customer_id, date=on_date, currency=p.currency, auto_post=p.auto_post,
        notes=f"Recurring: {p.name}",
        lines=[InvoiceLineIn(description=l.get("description", ""), quantity=Decimal(str(l.get("quantity", 1))),
                             unit_price=Decimal(str(l.get("unit_price", 0))),
                             vat_rate=Decimal(str(l.get("vat_rate", "0.05"))),
                             revenue_account_id=l.get("revenue_account_id")) for l in lines]))
    db.add(RecurringRun(plan_id=p.id, invoice_id=inv.id, invoice_number=inv.number,
                        run_date=on_date, amount=inv.grand_total))
    p.occurrences_done += 1
    p.next_run_date = _advance(on_date, p.frequency)
    if p.max_occurrences and p.occurrences_done >= p.max_occurrences:
        p.active = False
    db.commit()
    return {"plan_id": p.id, "invoice_id": inv.id, "invoice_number": inv.number,
            "amount": str(inv.grand_total), "next_run_date": str(p.next_run_date), "active": p.active}


def generate_now(db: Session, plan_id: str, on_date: _Date | None = None) -> dict:
    p = _get(db, plan_id)
    if not p.active:
        raise RecurringError("Plan is inactive.")
    return _generate(db, p, on_date or p.next_run_date)


def run_due(db: Session, as_of: _Date, catch_up: bool = False) -> dict:
    """Generate invoices for every active plan due on/before `as_of`. When catch_up, keep
    generating a plan until its next_run_date passes as_of (fills missed periods); otherwise
    one invoice per plan per call."""
    generated = []
    plans = list(db.execute(select(RecurringPlan).where(
        RecurringPlan.active.is_(True), RecurringPlan.next_run_date <= as_of)).scalars())
    for p in plans:
        while p.active and p.next_run_date <= as_of:
            run_on = p.next_run_date
            generated.append(_generate(db, p, run_on))
            if not catch_up:
                break
    return {"as_of": str(as_of), "generated": generated, "count": len(generated)}
