"""Banking service: bank accounts (backed by GL asset accounts), transfers, statement
import, statement↔ledger matching (auto + manual), and bank reconciliation.

Reconciliation identity (opening balances aligned):
    book_balance = statement_balance + deposits_in_transit − outstanding_payments − Σ(unbooked statement lines)
so with everything matched both ways the difference is zero.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import constants as C
from ..constants import AccountType
from ..models import (
    Account,
    BankAccount,
    BankReconciliation,
    BankStatementLine,
    JournalEntry,
    JournalLine,
)
from ..schemas import (
    BankAccountIn,
    BankAccountOut,
    JournalEntryIn,
    JournalLineIn,
    MatchCandidate,
    ReconcileItem,
    ReconcileSummary,
    ReconcileSummaryIn,
    ReconciliationOut,
    StatementImportIn,
    StatementLineOut,
    TransferIn,
    q,
)
from . import ledger

ZERO = Decimal(0)


class BankingError(ValueError):
    """Domain error → HTTP 400."""


def _account(db: Session, account_id: str) -> Account:
    a = db.get(Account, account_id)
    if not a:
        raise BankingError("Account not found.")
    return a


def _gl_balance(db: Session, gl_account_id: str, as_of: date | None = None) -> Decimal:
    stmt = (
        select(func.coalesce(func.sum(JournalLine.debit - JournalLine.credit), 0))
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .where(JournalEntry.status == C.ENTRY_POSTED, JournalLine.account_id == gl_account_id)
    )
    if as_of is not None:
        stmt = stmt.where(JournalEntry.date <= as_of)
    return q(Decimal(db.execute(stmt).scalar() or 0))


# ── Bank accounts ───────────────────────────────────────────────────────────────────────
def _next_bank_code(db: Session) -> str:
    used = {a.code for a in db.execute(select(Account)).scalars()}
    for n in range(1021, 1100):
        if str(n) not in used:
            return str(n)
    raise BankingError("No free asset account code available for a new bank account.")


_BANK_FIELDS = ("name", "bank_name", "account_name", "account_number", "iban", "swift",
                "branch", "currency", "is_active")


def _bank_out(db: Session, ba: BankAccount) -> BankAccountOut:
    gl = db.get(Account, ba.gl_account_id)
    return BankAccountOut(
        id=ba.id, name=ba.name, gl_account_id=ba.gl_account_id,
        gl_account_code=gl.code if gl else None, gl_account_name=gl.name if gl else None,
        bank_name=ba.bank_name, account_name=ba.account_name, account_number=ba.account_number,
        iban=ba.iban, swift=ba.swift, branch=ba.branch, currency=ba.currency,
        is_active=ba.is_active, balance=_gl_balance(db, ba.gl_account_id),
        created_at=ba.created_at.isoformat() if ba.created_at else None,
    )


def update_bank_account(db: Session, bank_account_id: str, data, actor: str | None = None) -> BankAccountOut:
    """Edit descriptive bank-account details. The GL account link is IMMUTABLE, so historical
    bank movements and completed reconciliations are preserved exactly. Field-level audit."""
    ba = db.get(BankAccount, bank_account_id)
    if not ba:
        raise BankingError("Bank account not found.")
    name = (data.name or "").strip()
    if not name:
        raise BankingError("Bank account name is required.")
    dup = db.execute(select(BankAccount).where(
        func.lower(BankAccount.name) == name.lower(), BankAccount.id != bank_account_id)).scalars().first()
    if dup:
        raise BankingError("Another bank account already uses that name.")
    before = {f: getattr(ba, f, None) for f in _BANK_FIELDS}
    for f in _BANK_FIELDS:
        if hasattr(data, f):
            setattr(ba, f, getattr(data, f))
    from . import audit
    changes = audit.diff(before, {f: getattr(ba, f, None) for f in _BANK_FIELDS})
    audit.record_profile_change(db, entity_type="bank_account", entity_id=ba.id,
                                entity_label=ba.name, actor=actor, changes=changes)
    db.commit()
    db.refresh(ba)
    return _bank_out(db, ba)


def bank_account_audit(db: Session, bank_account_id: str) -> list[dict]:
    from . import audit
    return audit.list_profile_audit(db, "bank_account", bank_account_id)


def create_bank_account(db: Session, data: BankAccountIn) -> BankAccountOut:
    if data.gl_account_code:
        gl = db.execute(select(Account).where(Account.code == data.gl_account_code)).scalar_one_or_none()
        if gl is None:
            # Auto-create the GL account under Assets with the requested code.
            parent = db.execute(select(Account).where(Account.code == "1000")).scalar_one_or_none()
            gl = Account(code=data.gl_account_code, name=data.name, type=AccountType.ASSET.value,
                         parent_id=parent.id if parent else None, is_group=False, normal_balance="debit")
            db.add(gl)
            db.flush()
        elif gl.is_group:
            raise BankingError(f"Account '{gl.code}' is a group account.")
    else:
        parent = db.execute(select(Account).where(Account.code == "1000")).scalar_one_or_none()
        gl = Account(code=_next_bank_code(db), name=data.name, type=AccountType.ASSET.value,
                     parent_id=parent.id if parent else None, is_group=False, normal_balance="debit")
        db.add(gl)
        db.flush()
    if db.execute(select(BankAccount).where(BankAccount.gl_account_id == gl.id)).scalar_one_or_none():
        raise BankingError(f"A bank account is already linked to GL account '{gl.code}'.")
    ba = BankAccount(
        name=data.name, gl_account_id=gl.id, bank_name=data.bank_name,
        account_name=getattr(data, "account_name", None), account_number=data.account_number,
        iban=data.iban, swift=getattr(data, "swift", None), branch=getattr(data, "branch", None),
        currency=data.currency,
    )
    db.add(ba)
    db.flush()
    from . import audit
    audit.record_profile_change(db, entity_type="bank_account", entity_id=ba.id,
                                entity_label=ba.name, actor=None, changes=[], action="create")
    db.commit()
    db.refresh(ba)
    return _bank_out(db, ba)


def list_bank_accounts(db: Session) -> list[BankAccountOut]:
    return [_bank_out(db, ba) for ba in db.execute(select(BankAccount).order_by(BankAccount.name)).scalars()]


def get_bank_account(db: Session, bank_account_id: str) -> BankAccountOut:
    ba = db.get(BankAccount, bank_account_id)
    if not ba:
        raise BankingError("Bank account not found.")
    return _bank_out(db, ba)


def _get_ba(db: Session, bank_account_id: str) -> BankAccount:
    ba = db.get(BankAccount, bank_account_id)
    if not ba:
        raise BankingError("Bank account not found.")
    return ba


# ── Transfers ───────────────────────────────────────────────────────────────────────────
def transfer(db: Session, data: TransferIn) -> dict:
    if data.from_bank_account_id == data.to_bank_account_id:
        raise BankingError("Source and destination must be different accounts.")
    src = _get_ba(db, data.from_bank_account_id)
    dst = _get_ba(db, data.to_bank_account_id)
    entry = ledger.create_journal_entry(
        db,
        JournalEntryIn(
            date=data.date, memo=data.memo or f"Transfer {src.name} → {dst.name}",
            reference=data.reference, source="bank", currency=src.currency,
            lines=[
                JournalLineIn(account_id=dst.gl_account_id, debit=q(data.amount)),
                JournalLineIn(account_id=src.gl_account_id, credit=q(data.amount)),
            ],
            auto_post=True,
        ),
    )
    return {"journal_entry_id": entry.id, "entry_no": entry.entry_no, "amount": str(q(data.amount))}


# ── Statement import & lines ─────────────────────────────────────────────────────────────
def import_statement(db: Session, data: StatementImportIn) -> dict:
    ba = _get_ba(db, data.bank_account_id)
    added = 0
    for ln in data.lines:
        db.add(BankStatementLine(
            bank_account_id=ba.id, date=ln.date, description=ln.description,
            reference=ln.reference, amount=q(ln.amount), status="unmatched",
        ))
        added += 1
    db.commit()
    return {"added": added}


def _entry_no(db: Session, entry_id: str | None) -> int | None:
    if not entry_id:
        return None
    e = db.get(JournalEntry, entry_id)
    return e.entry_no if e else None


def _line_out(db: Session, ln: BankStatementLine) -> StatementLineOut:
    return StatementLineOut(
        id=ln.id, bank_account_id=ln.bank_account_id, date=ln.date, description=ln.description,
        reference=ln.reference, amount=q(ln.amount), status=ln.status, matched_entry_id=ln.matched_entry_id,
        matched_entry_no=_entry_no(db, ln.matched_entry_id),
        created_at=ln.created_at.isoformat() if ln.created_at else None,
    )


def list_statement_lines(db: Session, bank_account_id: str, status: str | None = None) -> list[StatementLineOut]:
    stmt = select(BankStatementLine).where(BankStatementLine.bank_account_id == bank_account_id).order_by(BankStatementLine.date)
    if status:
        stmt = stmt.where(BankStatementLine.status == status)
    return [_line_out(db, ln) for ln in db.execute(stmt).scalars()]


# ── Matching ────────────────────────────────────────────────────────────────────────────
def _movements(db: Session, ba: BankAccount, as_of: date | None = None):
    """Posted, non-void GL movements on the bank account's GL account: list of dicts with a
    signed `amount` (+ money in / − money out)."""
    stmt = (
        select(JournalEntry.id, JournalEntry.entry_no, JournalEntry.date, JournalEntry.memo,
               JournalEntry.reference, JournalEntry.source, JournalLine.debit, JournalLine.credit)
        .join(JournalLine, JournalLine.entry_id == JournalEntry.id)
        .where(JournalEntry.status == C.ENTRY_POSTED, JournalLine.account_id == ba.gl_account_id)
    )
    if as_of is not None:
        stmt = stmt.where(JournalEntry.date <= as_of)
    out = []
    for eid, eno, edate, memo, ref, source, debit, credit in db.execute(stmt).all():
        out.append({"entry_id": eid, "entry_no": eno, "date": edate, "memo": memo,
                    "reference": ref, "source": source, "amount": q(Decimal(debit) - Decimal(credit))})
    return out


def _matched_entry_ids(db: Session, bank_account_id: str, exclude_line: str | None = None) -> set[str]:
    stmt = select(BankStatementLine.matched_entry_id).where(
        BankStatementLine.bank_account_id == bank_account_id, BankStatementLine.matched_entry_id.isnot(None)
    )
    if exclude_line:
        stmt = stmt.where(BankStatementLine.id != exclude_line)
    return {row[0] for row in db.execute(stmt).all()}


def match_candidates(db: Session, statement_line_id: str) -> list[MatchCandidate]:
    ln = db.get(BankStatementLine, statement_line_id)
    if not ln:
        raise BankingError("Statement line not found.")
    ba = _get_ba(db, ln.bank_account_id)
    taken = _matched_entry_ids(db, ba.id, exclude_line=statement_line_id)
    cands = []
    for m in _movements(db, ba):
        if m["entry_id"] in taken:
            continue
        if q(m["amount"]) == q(ln.amount):
            cands.append(m)
    # Closest date first.
    cands.sort(key=lambda m: abs((m["date"] - ln.date).days))
    return [
        MatchCandidate(entry_id=m["entry_id"], entry_no=m["entry_no"], date=m["date"], memo=m["memo"],
                       reference=m["reference"], amount=m["amount"], source=m["source"])
        for m in cands
    ]


def manual_match(db: Session, statement_line_id: str, entry_id: str) -> StatementLineOut:
    ln = db.get(BankStatementLine, statement_line_id)
    if not ln:
        raise BankingError("Statement line not found.")
    if entry_id in _matched_entry_ids(db, ln.bank_account_id, exclude_line=statement_line_id):
        raise BankingError("That journal entry is already matched to another statement line.")
    ln.matched_entry_id = entry_id
    ln.status = "matched"
    db.commit()
    db.refresh(ln)
    return _line_out(db, ln)


def unmatch(db: Session, statement_line_id: str) -> StatementLineOut:
    ln = db.get(BankStatementLine, statement_line_id)
    if not ln:
        raise BankingError("Statement line not found.")
    if ln.status == "reconciled":
        raise BankingError("Cannot unmatch a reconciled line.")
    ln.matched_entry_id = None
    ln.status = "unmatched"
    db.commit()
    db.refresh(ln)
    return _line_out(db, ln)


def auto_match(db: Session, bank_account_id: str, window_days: int = 5) -> dict:
    ba = _get_ba(db, bank_account_id)
    lines = list(db.execute(
        select(BankStatementLine).where(
            BankStatementLine.bank_account_id == ba.id, BankStatementLine.status == "unmatched"
        ).order_by(BankStatementLine.date)
    ).scalars())
    movements = _movements(db, ba)
    taken = _matched_entry_ids(db, ba.id)
    matched = 0
    for ln in lines:
        cands = [
            m for m in movements
            if m["entry_id"] not in taken
            and q(m["amount"]) == q(ln.amount)
            and abs((m["date"] - ln.date).days) <= window_days
        ]
        if len(cands) == 1:
            ln.matched_entry_id = cands[0]["entry_id"]
            ln.status = "matched"
            taken.add(cands[0]["entry_id"])
            matched += 1
    db.commit()
    return {"matched": matched, "remaining_unmatched": len(lines) - matched}


# ── Reconciliation ──────────────────────────────────────────────────────────────────────
def reconcile_summary(db: Session, data: ReconcileSummaryIn) -> ReconcileSummary:
    ba = _get_ba(db, data.bank_account_id)
    book = _gl_balance(db, ba.gl_account_id, as_of=data.statement_date)
    movements = [m for m in _movements(db, ba, as_of=data.statement_date)]
    matched_ids = _matched_entry_ids(db, ba.id)

    deposits: list[ReconcileItem] = []
    payments: list[ReconcileItem] = []
    dit = ZERO
    outp = ZERO
    cleared = ZERO
    for m in movements:
        if m["entry_id"] in matched_ids:
            cleared = q(cleared + m["amount"])
            continue
        if m["amount"] > 0:
            dit = q(dit + m["amount"])
            deposits.append(ReconcileItem(entry_id=m["entry_id"], entry_no=m["entry_no"], date=m["date"],
                                          memo=m["memo"], amount=m["amount"], kind="deposit_in_transit"))
        elif m["amount"] < 0:
            outp = q(outp + (-m["amount"]))
            payments.append(ReconcileItem(entry_id=m["entry_id"], entry_no=m["entry_no"], date=m["date"],
                                          memo=m["memo"], amount=m["amount"], kind="outstanding_payment"))

    unmatched_lines = [
        _line_out(db, ln) for ln in db.execute(
            select(BankStatementLine).where(
                BankStatementLine.bank_account_id == ba.id,
                BankStatementLine.status == "unmatched",
                BankStatementLine.date <= data.statement_date,
            ).order_by(BankStatementLine.date)
        ).scalars()
    ]
    expected = q(data.statement_balance + dit - outp)
    difference = q(book - expected)
    reconciled = abs(difference) < Decimal("0.01") and len(unmatched_lines) == 0
    return ReconcileSummary(
        bank_account_id=ba.id, statement_date=data.statement_date, statement_balance=q(data.statement_balance),
        book_balance=book, cleared_total=cleared, deposits_in_transit=dit, outstanding_payments=outp,
        unmatched_statement_count=len(unmatched_lines), difference=difference, reconciled=reconciled,
        deposits=deposits, payments=payments, unmatched_statement=unmatched_lines,
    )


def complete_reconciliation(db: Session, data: ReconcileSummaryIn) -> ReconciliationOut:
    ba = _get_ba(db, data.bank_account_id)
    summary = reconcile_summary(db, data)
    rec = BankReconciliation(
        bank_account_id=ba.id, statement_date=data.statement_date,
        statement_balance=q(data.statement_balance), book_balance=summary.book_balance,
        difference=summary.difference, cleared_count=0, status="completed",
    )
    db.add(rec)
    db.flush()
    # Lock every matched (not-yet-reconciled) line up to the statement date into this rec.
    lines = db.execute(
        select(BankStatementLine).where(
            BankStatementLine.bank_account_id == ba.id, BankStatementLine.status == "matched",
            BankStatementLine.date <= data.statement_date,
        )
    ).scalars()
    count = 0
    for ln in lines:
        ln.status = "reconciled"
        ln.reconciliation_id = rec.id
        count += 1
    rec.cleared_count = count
    db.commit()
    db.refresh(rec)
    return _rec_out(db, rec)


def _rec_out(db: Session, rec: BankReconciliation) -> ReconciliationOut:
    ba = db.get(BankAccount, rec.bank_account_id)
    return ReconciliationOut(
        id=rec.id, bank_account_id=rec.bank_account_id, bank_account_name=ba.name if ba else None,
        statement_date=rec.statement_date, statement_balance=q(rec.statement_balance),
        book_balance=q(rec.book_balance), difference=q(rec.difference), cleared_count=rec.cleared_count,
        status=rec.status, created_at=rec.created_at.isoformat() if rec.created_at else None,
    )


def list_reconciliations(db: Session, bank_account_id: str | None = None) -> list[ReconciliationOut]:
    stmt = select(BankReconciliation).order_by(BankReconciliation.created_at.desc())
    if bank_account_id:
        stmt = stmt.where(BankReconciliation.bank_account_id == bank_account_id)
    return [_rec_out(db, r) for r in db.execute(stmt).scalars()]
