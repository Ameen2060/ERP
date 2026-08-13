"""Payroll service: employees, payroll runs, payslips, UAE end-of-service accrual, and GL
posting.

Posting rules (source='payroll'):
  Post run   Dr Salaries Expense (gross)         Cr Salaries Payable (net)
             Dr EOSB Expense (accrual, optional)  Cr Payroll Deductions Payable (deductions)
                                                  Cr End of Service Provision (accrual)
  Pay run    Dr Salaries Payable (net)  /  Cr Bank (net)
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .. import constants as C
from ..models import Employee, Payslip, PayrollRun
from ..schemas import (
    EmployeeIn,
    EmployeeOut,
    JournalEntryIn,
    JournalLineIn,
    PayrollRunIn,
    PayrollRunOut,
    PayrollRunSummary,
    PayslipOut,
    q,
)
from . import ledger
from ..models import Account

ZERO = Decimal(0)
EOSB_FACTOR = Decimal(C.EOSB_MONTHLY_FACTOR)


class PayrollError(ValueError):
    """Domain error → HTTP 400."""


def _account_by_code(db: Session, code: str) -> Account:
    a = db.execute(select(Account).where(Account.code == code)).scalar_one_or_none()
    if not a:
        raise PayrollError(f"Required account '{code}' is missing — seed the Chart of Accounts first.")
    return a


# ── Employees ───────────────────────────────────────────────────────────────────────────
def _gross(e: Employee) -> Decimal:
    return q(Decimal(e.basic_salary) + Decimal(e.housing_allowance) + Decimal(e.transport_allowance) + Decimal(e.other_allowance))


def _emp_out(e: Employee) -> EmployeeOut:
    return EmployeeOut(
        id=e.id, code=e.code, name=e.name, department=e.department, designation=e.designation,
        join_date=e.join_date, basic_salary=q(e.basic_salary), housing_allowance=q(e.housing_allowance),
        transport_allowance=q(e.transport_allowance), other_allowance=q(e.other_allowance),
        gross_salary=_gross(e), iban=e.iban, currency=e.currency, is_active=e.is_active,
    )


def _next_emp_code(db: Session) -> str:
    n = (db.execute(select(func.count(Employee.id))).scalar() or 0) + 1
    return f"EMP-{n:04d}"


def create_employee(db: Session, data: EmployeeIn) -> EmployeeOut:
    code = data.code or _next_emp_code(db)
    if db.execute(select(Employee).where(Employee.code == code)).scalar_one_or_none():
        raise PayrollError(f"Employee code '{code}' already exists.")
    e = Employee(
        code=code, name=data.name, department=data.department, designation=data.designation,
        join_date=data.join_date, basic_salary=q(data.basic_salary), housing_allowance=q(data.housing_allowance),
        transport_allowance=q(data.transport_allowance), other_allowance=q(data.other_allowance),
        iban=data.iban, currency=data.currency,
    )
    db.add(e)
    db.commit()
    db.refresh(e)
    return _emp_out(e)


def list_employees(db: Session, active_only: bool = True) -> list[EmployeeOut]:
    stmt = select(Employee).order_by(Employee.code)
    if active_only:
        stmt = stmt.where(Employee.is_active.is_(True))
    return [_emp_out(e) for e in db.execute(stmt).scalars()]


def get_employee(db: Session, employee_id: str) -> EmployeeOut:
    e = db.get(Employee, employee_id)
    if not e:
        raise PayrollError("Employee not found.")
    return _emp_out(e)


_EMP_FIELDS = ("code", "name", "department", "designation", "join_date", "basic_salary",
               "housing_allowance", "transport_allowance", "other_allowance", "iban",
               "currency", "is_active")


def update_employee(db: Session, employee_id: str, data: EmployeeIn, actor: str | None = None) -> EmployeeOut:
    """Edit an employee's profile / salary structure. Posted payslips keep their own amounts,
    so historical payroll runs are unaffected. Field-level audit written."""
    from . import audit
    e = db.get(Employee, employee_id)
    if not e:
        raise PayrollError("Employee not found.")
    new_code = (data.code or e.code).strip()
    if new_code != e.code and db.execute(select(Employee).where(
            Employee.code == new_code, Employee.id != employee_id)).scalar_one_or_none():
        raise PayrollError(f"Employee code '{new_code}' already exists.")
    before = {f: getattr(e, f, None) for f in _EMP_FIELDS}
    e.code = new_code
    e.name = data.name
    e.department = data.department
    e.designation = data.designation
    e.join_date = data.join_date
    e.basic_salary = q(data.basic_salary)
    e.housing_allowance = q(data.housing_allowance)
    e.transport_allowance = q(data.transport_allowance)
    e.other_allowance = q(data.other_allowance)
    e.iban = data.iban
    e.currency = data.currency
    e.is_active = data.is_active
    changes = audit.diff(before, {f: getattr(e, f, None) for f in _EMP_FIELDS})
    audit.record_profile_change(db, entity_type="employee", entity_id=e.id, entity_label=e.name,
                                actor=actor, changes=changes)
    db.commit()
    db.refresh(e)
    return _emp_out(e)


def employee_audit(db: Session, employee_id: str) -> list[dict]:
    from . import audit
    return audit.list_profile_audit(db, "employee", employee_id)


# ── Payroll runs ────────────────────────────────────────────────────────────────────────
def _payslip_out(db: Session, ps: Payslip) -> PayslipOut:
    e = db.get(Employee, ps.employee_id)
    return PayslipOut(
        id=ps.id, employee_id=ps.employee_id, employee_code=e.code if e else None,
        employee_name=e.name if e else None, basic=q(ps.basic), allowances=q(ps.allowances),
        overtime=q(ps.overtime), gross=q(ps.gross), deductions=q(ps.deductions), net=q(ps.net),
        eosb_accrual=q(ps.eosb_accrual),
    )


def _run_out(db: Session, run: PayrollRun) -> PayrollRunOut:
    return PayrollRunOut(
        id=run.id, period_label=run.period_label, period_start=run.period_start, period_end=run.period_end,
        pay_date=run.pay_date, accrue_eosb=run.accrue_eosb, status=run.status, gross_total=q(run.gross_total),
        deductions_total=q(run.deductions_total), net_total=q(run.net_total), eosb_total=q(run.eosb_total),
        journal_entry_id=run.journal_entry_id, payment_entry_id=run.payment_entry_id,
        payslips=[_payslip_out(db, ps) for ps in run.payslips],
        created_at=run.created_at.isoformat() if run.created_at else None,
    )


def _get_run(db: Session, run_id: str) -> PayrollRun:
    run = db.execute(
        select(PayrollRun).where(PayrollRun.id == run_id).options(selectinload(PayrollRun.payslips))
    ).scalar_one_or_none()
    if not run:
        raise PayrollError("Payroll run not found.")
    return run


def create_run(db: Session, data: PayrollRunIn) -> PayrollRunOut:
    employees = list(db.execute(select(Employee).where(Employee.is_active.is_(True)).order_by(Employee.code)).scalars())
    if not employees:
        raise PayrollError("No active employees to run payroll for.")
    adj = {a.employee_id: a for a in data.adjustments}
    run = PayrollRun(
        period_label=data.period_label, period_start=data.period_start, period_end=data.period_end,
        pay_date=data.pay_date, accrue_eosb=data.accrue_eosb, status="draft",
    )
    gross_t = ded_t = net_t = eosb_t = ZERO
    for i, e in enumerate(employees):
        a = adj.get(e.id)
        overtime = q(a.overtime) if a else ZERO
        deductions = q(a.deductions) if a else ZERO
        allowances = q(Decimal(e.housing_allowance) + Decimal(e.transport_allowance) + Decimal(e.other_allowance))
        basic = q(e.basic_salary)
        gross = q(basic + allowances + overtime)
        net = q(gross - deductions)
        eosb = q(Decimal(e.basic_salary) * EOSB_FACTOR) if data.accrue_eosb else ZERO
        run.payslips.append(Payslip(
            employee_id=e.id, ordinal=i, basic=basic, allowances=allowances, overtime=overtime,
            gross=gross, deductions=deductions, net=net, eosb_accrual=eosb,
        ))
        gross_t += gross; ded_t += deductions; net_t += net; eosb_t += eosb
    run.gross_total = q(gross_t)
    run.deductions_total = q(ded_t)
    run.net_total = q(net_t)
    run.eosb_total = q(eosb_t)
    db.add(run)
    db.flush()
    if data.auto_post:
        _post_run_je(db, run)
        run.status = "posted"
    db.commit()
    db.refresh(run)
    return _run_out(db, run)


def _post_run_je(db: Session, run: PayrollRun) -> None:
    lines = [JournalLineIn(account_id=_account_by_code(db, C.CODE_SALARY_EXPENSE).id, debit=q(run.gross_total)),
             JournalLineIn(account_id=_account_by_code(db, C.CODE_SALARIES_PAYABLE).id, credit=q(run.net_total))]
    if run.deductions_total > 0:
        lines.append(JournalLineIn(account_id=_account_by_code(db, C.CODE_DEDUCTIONS_PAYABLE).id, credit=q(run.deductions_total)))
    if run.eosb_total > 0:
        lines.append(JournalLineIn(account_id=_account_by_code(db, C.CODE_EOSB_EXPENSE).id, debit=q(run.eosb_total)))
        lines.append(JournalLineIn(account_id=_account_by_code(db, C.CODE_EOSB_PROVISION).id, credit=q(run.eosb_total)))
    entry = ledger.create_journal_entry(
        db,
        JournalEntryIn(
            date=run.pay_date, memo=f"Payroll {run.period_label}", reference=run.period_label,
            source="payroll", currency="AED", lines=lines, auto_post=True,
        ),
    )
    run.journal_entry_id = entry.id


def post_run(db: Session, run_id: str) -> PayrollRunOut:
    run = _get_run(db, run_id)
    if run.status != "draft":
        raise PayrollError(f"Only draft runs can be posted (status is '{run.status}').")
    _post_run_je(db, run)
    run.status = "posted"
    db.commit()
    db.refresh(run)
    return _run_out(db, run)


def pay_run(db: Session, run_id: str, payment_account_code: str | None = None) -> PayrollRunOut:
    run = _get_run(db, run_id)
    if run.status != "posted":
        raise PayrollError("Only a posted run can be paid.")
    payable = _account_by_code(db, C.CODE_SALARIES_PAYABLE)
    bank = _account_by_code(db, payment_account_code or C.CODE_BANK)
    entry = ledger.create_journal_entry(
        db,
        JournalEntryIn(
            date=run.pay_date, memo=f"Payroll payment {run.period_label}", reference=run.period_label,
            source="payroll", currency="AED",
            lines=[JournalLineIn(account_id=payable.id, debit=q(run.net_total)),
                   JournalLineIn(account_id=bank.id, credit=q(run.net_total))],
            auto_post=True,
        ),
    )
    run.payment_entry_id = entry.id
    run.status = "paid"
    db.commit()
    db.refresh(run)
    return _run_out(db, run)


def void_run(db: Session, run_id: str) -> PayrollRunOut:
    run = _get_run(db, run_id)
    if run.status == "paid":
        raise PayrollError("Cannot void a paid run.")
    if run.journal_entry_id:
        ledger.void_entry(db, run.journal_entry_id)
    run.status = "void"
    db.commit()
    db.refresh(run)
    return _run_out(db, run)


def list_runs(db: Session) -> list[PayrollRunSummary]:
    runs = db.execute(select(PayrollRun).options(selectinload(PayrollRun.payslips)).order_by(PayrollRun.created_at.desc())).scalars()
    return [
        PayrollRunSummary(
            id=r.id, period_label=r.period_label, pay_date=r.pay_date, status=r.status,
            gross_total=q(r.gross_total), net_total=q(r.net_total), eosb_total=q(r.eosb_total),
            employee_count=len(r.payslips),
        )
        for r in runs
    ]


def get_run(db: Session, run_id: str) -> PayrollRunOut:
    return _run_out(db, _get_run(db, run_id))
