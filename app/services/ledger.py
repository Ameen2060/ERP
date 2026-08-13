"""Ledger service: Chart of Accounts, balanced journal posting, trial balance, general
ledger. Protects the invariant that every posted entry is balanced and only leaf accounts
are posted to — from which the trial balance and financial statements tie out."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .. import constants as C
from ..constants import AccountType, NormalBalance
from ..models import Account, JournalEntry, JournalLine
from ..schemas import (
    AccountIn,
    AccountNode,
    AccountOut,
    GeneralLedger,
    JournalEntryIn,
    JournalEntryOut,
    JournalEntrySummary,
    JournalLineOut,
    LedgerRow,
    TrialBalance,
    TrialBalanceRow,
    q,
)

ZERO = Decimal(0)


class LedgerError(ValueError):
    """Domain rule violation → HTTP 400."""


# ── Chart of Accounts ───────────────────────────────────────────────────────────────────
def seed_chart_of_accounts(db: Session) -> int:
    by_code: dict[str, Account] = {a.code: a for a in db.execute(select(Account)).scalars()}
    existing = set(by_code)
    added = 0
    for code, name, atype, is_group, parent_code, nb_override, is_cos in C.DEFAULT_COA:
        if code in existing:
            continue
        parent = by_code.get(parent_code) if parent_code else None
        acct = Account(
            code=code, name=name, type=atype.value,
            parent_id=parent.id if parent else None, is_group=is_group,
            normal_balance=(nb_override or C.default_normal_balance(atype)).value,
            is_active=True, is_cost_of_sales=is_cos,
        )
        db.add(acct)
        db.flush()
        by_code[code] = acct
        existing.add(code)
        added += 1
    db.commit()
    return added


def _account_out(a: Account, parent_code: str | None = None) -> AccountOut:
    return AccountOut(
        id=a.id, code=a.code, name=a.name, type=AccountType(a.type), parent_id=a.parent_id,
        parent_code=parent_code, is_group=a.is_group, normal_balance=NormalBalance(a.normal_balance),
        is_active=a.is_active, is_cost_of_sales=a.is_cost_of_sales, description=a.description,
    )


def list_accounts(db: Session, active_only: bool = False) -> list[AccountOut]:
    stmt = select(Account).order_by(Account.code)
    if active_only:
        stmt = stmt.where(Account.is_active.is_(True))
    accounts = list(db.execute(stmt).scalars())
    code_by_id = {a.id: a.code for a in accounts}
    return [_account_out(a, code_by_id.get(a.parent_id) if a.parent_id else None) for a in accounts]


def account_tree(db: Session) -> list[AccountNode]:
    accounts = list(db.execute(select(Account).order_by(Account.code)).scalars())
    code_by_id = {a.id: a.code for a in accounts}
    nodes: dict[str, AccountNode] = {
        a.id: AccountNode(
            **_account_out(a, code_by_id.get(a.parent_id) if a.parent_id else None).model_dump(),
            children=[],
        )
        for a in accounts
    }
    roots: list[AccountNode] = []
    for a in accounts:
        node = nodes[a.id]
        if a.parent_id and a.parent_id in nodes:
            nodes[a.parent_id].children.append(node)
        else:
            roots.append(node)
    return roots


def create_account(db: Session, data: AccountIn) -> AccountOut:
    if db.execute(select(Account).where(Account.code == data.code)).scalar_one_or_none():
        raise LedgerError(f"Account code '{data.code}' already exists.")
    parent = None
    if data.parent_code:
        parent = db.execute(select(Account).where(Account.code == data.parent_code)).scalar_one_or_none()
        if not parent:
            raise LedgerError(f"Parent account '{data.parent_code}' not found.")
        if not parent.is_group:
            raise LedgerError(f"Parent account '{data.parent_code}' is not a group account.")
    nb = data.normal_balance or C.default_normal_balance(data.type)
    acct = Account(
        code=data.code, name=data.name, type=data.type.value,
        parent_id=parent.id if parent else None, is_group=data.is_group, normal_balance=nb.value,
        is_active=True, is_cost_of_sales=data.is_cost_of_sales, description=data.description,
    )
    db.add(acct)
    db.commit()
    db.refresh(acct)
    return _account_out(acct, parent.code if parent else None)


def archive_account(db: Session, account_id: str) -> AccountOut:
    acct = db.get(Account, account_id)
    if not acct:
        raise LedgerError("Account not found.")
    acct.is_active = False
    db.commit()
    db.refresh(acct)
    return _account_out(acct)


# ── Journal entries ─────────────────────────────────────────────────────────────────────
def _next_entry_no(db: Session) -> int:
    current = db.execute(select(func.max(JournalEntry.entry_no))).scalar()
    return (current or 0) + 1


def create_journal_entry(db: Session, data: JournalEntryIn) -> JournalEntryOut:
    accounts: dict[str, Account] = {}
    for line in data.lines:
        acct = accounts.get(line.account_id) or db.get(Account, line.account_id)
        if not acct:
            raise LedgerError(f"Account '{line.account_id}' not found.")
        if acct.is_group:
            raise LedgerError(f"Account '{acct.code} {acct.name}' is a group account and cannot be posted to.")
        if not acct.is_active:
            raise LedgerError(f"Account '{acct.code} {acct.name}' is inactive.")
        accounts[line.account_id] = acct

    entry = JournalEntry(
        entry_no=_next_entry_no(db), date=data.date, memo=data.memo, reference=data.reference,
        source=data.source, currency=data.currency,
        status=C.ENTRY_POSTED if data.auto_post else C.ENTRY_DRAFT,
    )
    for i, line in enumerate(data.lines):
        entry.lines.append(
            JournalLine(
                account_id=line.account_id, ordinal=i, debit=q(line.debit), credit=q(line.credit),
                description=line.description,
            )
        )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return _entry_out(db, entry)


def _get_entry(db: Session, entry_id: str) -> JournalEntry:
    entry = db.execute(
        select(JournalEntry).where(JournalEntry.id == entry_id).options(selectinload(JournalEntry.lines))
    ).scalar_one_or_none()
    if not entry:
        raise LedgerError("Journal entry not found.")
    return entry


def _entry_out(db: Session, entry: JournalEntry) -> JournalEntryOut:
    acct_ids = {ln.account_id for ln in entry.lines}
    accts = {
        a.id: a for a in db.execute(select(Account).where(Account.id.in_(acct_ids))).scalars()
    } if acct_ids else {}
    lines = [
        JournalLineOut(
            id=ln.id, account_id=ln.account_id,
            account_code=accts[ln.account_id].code if ln.account_id in accts else None,
            account_name=accts[ln.account_id].name if ln.account_id in accts else None,
            debit=ln.debit, credit=ln.credit, description=ln.description,
        )
        for ln in entry.lines
    ]
    total = sum((ln.debit for ln in entry.lines), ZERO)
    return JournalEntryOut(
        id=entry.id, entry_no=entry.entry_no, date=entry.date, memo=entry.memo,
        reference=entry.reference, source=entry.source, currency=entry.currency, status=entry.status,
        total=q(total), lines=lines,
        created_at=entry.created_at.isoformat() if entry.created_at else None,
    )


def update_journal_entry(db: Session, entry_id: str, data: JournalEntryIn,
                         actor: str | None = None, reason: str | None = None) -> JournalEntryOut:
    """Edit a journal entry in place — the entry IS the GL record, so replacing its balanced
    lines updates the ledger directly (id + entry number preserved). Balance is guaranteed by
    the schema. Blocked in a locked period; writes a transaction-edit audit."""
    from . import audit, system_settings
    entry = _get_entry(db, entry_id)
    if entry.status == C.ENTRY_VOID:
        raise LedgerError("Cannot edit a voided entry — create a new one instead.")
    system_settings.assert_period_open(db, entry.date, "edit")
    system_settings.assert_period_open(db, data.date, "edit")
    for line in data.lines:
        acct = db.get(Account, line.account_id)
        if not acct:
            raise LedgerError(f"Account '{line.account_id}' not found.")
        if acct.is_group:
            raise LedgerError(f"Account '{acct.code} {acct.name}' is a group account and cannot be posted to.")
        if not acct.is_active:
            raise LedgerError(f"Account '{acct.code} {acct.name}' is inactive.")
    prev_status = entry.status
    before = {"date": str(entry.date), "memo": entry.memo, "reference": entry.reference,
              "currency": entry.currency, "total": str(q(sum((ln.debit for ln in entry.lines), ZERO))),
              "lines": len(entry.lines)}
    entry.date = data.date
    entry.memo = data.memo
    entry.reference = data.reference
    entry.currency = data.currency
    entry.lines.clear()
    db.flush()
    for i, line in enumerate(data.lines):
        entry.lines.append(JournalLine(account_id=line.account_id, ordinal=i, debit=q(line.debit),
                                       credit=q(line.credit), description=line.description))
    entry.status = C.ENTRY_POSTED if data.auto_post else C.ENTRY_DRAFT
    after = {"date": str(entry.date), "memo": entry.memo, "reference": entry.reference,
             "currency": entry.currency, "total": str(q(sum((ln.debit for ln in data.lines), ZERO))),
             "lines": len(data.lines)}
    audit.record_txn_audit(db, entity_type="journal", entity_id=entry.id, doc_number=entry.entry_no,
                           actor=actor, action="edit", reason=reason, prev_status=prev_status,
                           new_status=entry.status, changes=audit.diff(before, after))
    db.commit()
    db.refresh(entry)
    return _entry_out(db, entry)


def entry_audit(db: Session, entry_id: str) -> list[dict]:
    from . import audit
    return audit.list_txn_audit(db, "journal", entry_id)


def post_entry(db: Session, entry_id: str) -> JournalEntryOut:
    entry = _get_entry(db, entry_id)
    if entry.status == C.ENTRY_VOID:
        raise LedgerError("Cannot post a voided entry.")
    entry.status = C.ENTRY_POSTED
    db.commit()
    db.refresh(entry)
    return _entry_out(db, entry)


def void_entry(db: Session, entry_id: str) -> JournalEntryOut:
    entry = _get_entry(db, entry_id)
    entry.status = C.ENTRY_VOID
    db.commit()
    db.refresh(entry)
    return _entry_out(db, entry)


def get_entry(db: Session, entry_id: str) -> JournalEntryOut:
    return _entry_out(db, _get_entry(db, entry_id))


def list_entries(
    db: Session, source: str | None = None, status: str | None = None, limit: int = 200
) -> list[JournalEntrySummary]:
    stmt = (
        select(JournalEntry).options(selectinload(JournalEntry.lines))
        .order_by(JournalEntry.entry_no.desc()).limit(limit)
    )
    if source:
        stmt = stmt.where(JournalEntry.source == source)
    if status:
        stmt = stmt.where(JournalEntry.status == status)
    out: list[JournalEntrySummary] = []
    for e in db.execute(stmt).scalars():
        total = sum((ln.debit for ln in e.lines), ZERO)
        out.append(JournalEntrySummary(
            id=e.id, entry_no=e.entry_no, date=e.date, memo=e.memo, reference=e.reference,
            source=e.source, status=e.status, total=q(total),
        ))
    return out


# ── Ledger reports ──────────────────────────────────────────────────────────────────────
def _posted_line_query(as_of: date | None = None, start: date | None = None):
    stmt = (
        select(JournalLine, JournalEntry)
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .where(JournalEntry.status == C.ENTRY_POSTED)
    )
    if as_of is not None:
        stmt = stmt.where(JournalEntry.date <= as_of)
    if start is not None:
        stmt = stmt.where(JournalEntry.date >= start)
    return stmt


def trial_balance(db: Session, as_of: date | None = None) -> TrialBalance:
    sums: dict[str, list[Decimal]] = {}
    for line, _entry in db.execute(_posted_line_query(as_of=as_of)).all():
        d, c = sums.setdefault(line.account_id, [ZERO, ZERO])
        sums[line.account_id] = [d + line.debit, c + line.credit]
    accts = {a.id: a for a in db.execute(select(Account)).scalars()}
    rows: list[TrialBalanceRow] = []
    total_debit = ZERO
    total_credit = ZERO
    for account_id, (dsum, csum) in sums.items():
        acct = accts.get(account_id)
        if not acct:
            continue
        net = dsum - csum
        debit_col = net if net > 0 else ZERO
        credit_col = -net if net < 0 else ZERO
        if debit_col == 0 and credit_col == 0:
            continue
        total_debit += debit_col
        total_credit += credit_col
        rows.append(TrialBalanceRow(
            account_id=account_id, code=acct.code, name=acct.name, type=AccountType(acct.type),
            debit=q(debit_col), credit=q(credit_col),
        ))
    rows.sort(key=lambda r: r.code)
    return TrialBalance(
        as_of=as_of, rows=rows, total_debit=q(total_debit), total_credit=q(total_credit),
        balanced=q(total_debit) == q(total_credit),
    )


def general_ledger(
    db: Session, account_id: str, start: date | None = None, end: date | None = None
) -> GeneralLedger:
    acct = db.get(Account, account_id)
    if not acct:
        raise LedgerError("Account not found.")
    normal = NormalBalance(acct.normal_balance)
    sign = Decimal(1) if normal == NormalBalance.DEBIT else Decimal(-1)

    opening = ZERO
    if start is not None:
        for line, _e in db.execute(
            _posted_line_query(as_of=None).where(
                JournalLine.account_id == account_id, JournalEntry.date < start
            )
        ).all():
            opening += (line.debit - line.credit) * sign

    stmt = (
        select(JournalLine, JournalEntry)
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .where(JournalEntry.status == C.ENTRY_POSTED, JournalLine.account_id == account_id)
        .order_by(JournalEntry.date, JournalEntry.entry_no)
    )
    if start is not None:
        stmt = stmt.where(JournalEntry.date >= start)
    if end is not None:
        stmt = stmt.where(JournalEntry.date <= end)

    running = opening
    rows: list[LedgerRow] = []
    for line, entry in db.execute(stmt).all():
        running += (line.debit - line.credit) * sign
        rows.append(LedgerRow(
            entry_id=entry.id, entry_no=entry.entry_no, date=entry.date, memo=entry.memo,
            reference=entry.reference, source=entry.source, debit=q(line.debit), credit=q(line.credit),
            balance=q(running),
        ))
    return GeneralLedger(
        account_id=account_id, code=acct.code, name=acct.name, type=AccountType(acct.type),
        normal_balance=normal, opening_balance=q(opening), rows=rows, closing_balance=q(running),
    )
