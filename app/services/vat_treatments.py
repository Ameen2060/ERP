"""UAE VAT Treatment / Rate Master — the single centralized VAT engine.

Seeds the five standard UAE treatments (Standard 5%, Zero-rated, Exempt, Out-of-scope,
Reverse-charge) and lets authorized users add more. Each transaction line stores the treatment
CODE it used, so historical VAT is immutable when the master is edited later.
"""

from __future__ import annotations

from datetime import date as _Date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Account, SalesInvoice, SalesInvoiceLine, VatTreatment, VendorBill, VendorBillLine
from ..schemas import q

# (code, name, kind, rate, taxable, recoverable, return_box, description)
SEED = [
    ("SR", "Standard Rated", "standard", "0.05", True, True, "1 / 9",
     "Standard-rated taxable supplies at 5%."),
    ("ZR", "Zero Rated", "zero", "0", True, True, "4",
     "Zero-rated taxable supplies (0%) — reported as zero-rated, not exempt."),
    ("EX", "Exempt", "exempt", "0", False, False, "Exempt",
     "Exempt supplies — reported separately from zero-rated; input VAT not recoverable."),
    ("OS", "Out of Scope", "out_of_scope", "0", False, False, "N/A",
     "Outside the scope of UAE VAT — excluded from taxable VAT calculations."),
    ("RC", "Reverse Charge", "reverse_charge", "0.05", True, True, "3 / 10",
     "Reverse-charge supplies — output and input VAT both accounted by the recipient."),
]


class VatTreatmentError(ValueError):
    """Domain error → HTTP 400."""


def ensure_seed(db: Session) -> None:
    existing = {t.code for t in db.execute(select(VatTreatment)).scalars()}
    for code, name, kind, rate, taxable, recoverable, box, desc in SEED:
        if code in existing:
            continue
        db.add(VatTreatment(code=code, name=name, kind=kind, rate=Decimal(rate), taxable=taxable,
                            recoverable=recoverable, return_box=box, description=desc, active=True))
    db.commit()


def _out(db: Session, t: VatTreatment) -> dict:
    def acc(aid):
        a = db.get(Account, aid) if aid else None
        return a.code if a else None
    return {
        "id": t.id, "code": t.code, "name": t.name, "kind": t.kind, "rate": str(t.rate),
        "description": t.description, "output_vat_code": acc(t.output_vat_account_id),
        "input_vat_code": acc(t.input_vat_account_id), "return_box": t.return_box,
        "taxable": t.taxable, "recoverable": t.recoverable, "active": t.active,
        "effective_from": str(t.effective_from) if t.effective_from else None,
        "effective_to": str(t.effective_to) if t.effective_to else None,
        "applicable_txn_types": t.applicable_txn_types,
    }


def list_treatments(db: Session, active_only: bool = False, txn_type: str | None = None) -> list[dict]:
    stmt = select(VatTreatment).order_by(VatTreatment.code)
    if active_only:
        stmt = stmt.where(VatTreatment.active.is_(True))
    rows = [t for t in db.execute(stmt).scalars()]
    if txn_type:
        rows = [t for t in rows if txn_type in (t.applicable_txn_types or "")]
    return [_out(db, t) for t in rows]


def get_by_code(db: Session, code: str) -> VatTreatment:
    t = db.execute(select(VatTreatment).where(VatTreatment.code == code)).scalars().first()
    if not t:
        raise VatTreatmentError(f"VAT treatment '{code}' not found.")
    return t


def _acc_id(db: Session, code: str | None) -> str | None:
    if not code:
        return None
    a = db.execute(select(Account).where(Account.code == code)).scalars().first()
    if not a:
        raise VatTreatmentError(f"Account code '{code}' not found.")
    return a.id


def create(db: Session, data) -> dict:
    code = (data.code or "").strip().upper()
    if not code:
        raise VatTreatmentError("Treatment code is required.")
    if db.execute(select(VatTreatment).where(VatTreatment.code == code)).scalars().first():
        raise VatTreatmentError(f"Treatment code '{code}' already exists.")
    if data.kind not in ("standard", "zero", "exempt", "out_of_scope", "reverse_charge", "other"):
        raise VatTreatmentError("Invalid VAT treatment kind.")
    if not (Decimal(0) <= Decimal(str(data.rate)) <= Decimal(1)):
        raise VatTreatmentError("Rate must be between 0 and 1.")
    t = VatTreatment(code=code, name=data.name or code, kind=data.kind, rate=Decimal(str(data.rate)),
                     description=data.description, output_vat_account_id=_acc_id(db, data.output_vat_code),
                     input_vat_account_id=_acc_id(db, data.input_vat_code), return_box=data.return_box,
                     taxable=data.taxable, recoverable=data.recoverable, active=data.active,
                     effective_from=data.effective_from, effective_to=data.effective_to,
                     applicable_txn_types=data.applicable_txn_types or "sales,purchase,expense")
    db.add(t)
    db.commit()
    db.refresh(t)
    return _out(db, t)


def update(db: Session, treatment_id: str, data) -> dict:
    t = db.get(VatTreatment, treatment_id)
    if not t:
        raise VatTreatmentError("VAT treatment not found.")
    if not (Decimal(0) <= Decimal(str(data.rate)) <= Decimal(1)):
        raise VatTreatmentError("Rate must be between 0 and 1.")
    t.name = data.name or t.name
    t.kind = data.kind
    t.rate = Decimal(str(data.rate))
    t.description = data.description
    t.output_vat_account_id = _acc_id(db, data.output_vat_code)
    t.input_vat_account_id = _acc_id(db, data.input_vat_code)
    t.return_box = data.return_box
    t.taxable = data.taxable
    t.recoverable = data.recoverable
    t.active = data.active
    t.effective_from = data.effective_from
    t.effective_to = data.effective_to
    t.applicable_txn_types = data.applicable_txn_types or t.applicable_txn_types
    db.commit()
    db.refresh(t)
    return _out(db, t)


def compute(db: Session, code: str, amount, inclusive: bool = False) -> dict:
    """Net / VAT / gross for an amount under a treatment. amount is net (exclusive) unless
    `inclusive` (then it's VAT-inclusive gross)."""
    t = get_by_code(db, code)
    rate = Decimal(t.rate)
    amt = Decimal(str(amount))
    if inclusive and rate > 0:
        net = q(amt / (Decimal(1) + rate))
        vat = q(amt - net)
    else:
        net = q(amt)
        vat = q(net * rate)
    return {"code": t.code, "name": t.name, "kind": t.kind, "rate": str(rate),
            "taxable": t.taxable, "recoverable": t.recoverable,
            "net": str(net), "vat": str(vat), "gross": str(q(net + vat)),
            "reverse_charge": t.kind == "reverse_charge"}


# ── VAT-by-treatment report ──────────────────────────────────────────────────────────────────
def by_treatment_report(db: Session, start: _Date | None = None, end: _Date | None = None) -> dict:
    treatments = {t.code: t for t in db.execute(select(VatTreatment)).scalars()}
    posted = ("posted", "partial", "paid")
    rows: dict[str, dict] = {}

    def bucket(code):
        t = treatments.get(code)
        return rows.setdefault(code, {"code": code, "name": t.name if t else code,
                                      "kind": t.kind if t else "?", "side_output": 0.0,
                                      "side_input": 0.0, "net": 0.0, "vat": 0.0})

    sinv = select(SalesInvoiceLine, SalesInvoice.date).join(SalesInvoice, SalesInvoiceLine.invoice_id == SalesInvoice.id).where(SalesInvoice.status.in_(posted))
    if start:
        sinv = sinv.where(SalesInvoice.date >= start)
    if end:
        sinv = sinv.where(SalesInvoice.date <= end)
    for l, _d in db.execute(sinv).all():
        b = bucket(l.vat_treatment or "SR")
        b["net"] += float(l.net_amount); b["vat"] += float(l.vat_amount); b["side_output"] += float(l.vat_amount)

    binv = select(VendorBillLine, VendorBill.date).join(VendorBill, VendorBillLine.bill_id == VendorBill.id).where(VendorBill.status.in_(posted))
    if start:
        binv = binv.where(VendorBill.date >= start)
    if end:
        binv = binv.where(VendorBill.date <= end)
    for l, _d in db.execute(binv).all():
        b = bucket(l.vat_treatment or "SR")
        b["net"] += float(l.net_amount); b["vat"] += float(l.vat_amount); b["side_input"] += float(l.vat_amount)

    out = [{**r, "net": round(r["net"], 2), "vat": round(r["vat"], 2),
            "output_vat": round(r["side_output"], 2), "input_vat": round(r["side_input"], 2)}
           for r in rows.values()]
    out.sort(key=lambda r: r["code"])
    return {"rows": out, "total_net": round(sum(r["net"] for r in out), 2),
            "total_output_vat": round(sum(r["output_vat"] for r in out), 2),
            "total_input_vat": round(sum(r["input_vat"] for r in out), 2)}
