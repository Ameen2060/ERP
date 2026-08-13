"""Global search across the main entities (customers, vendors, sales invoices, vendor bills,
expenses, journal entries, advances, credit notes, projects). Returns typed, drillable hits.

Each hit carries `open` = the frontend opener spec so the UI can route the click without
knowing each entity's modal function."""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..models import (
    Customer,
    CustomerAdvance,
    CustomerCreditNote,
    Expense,
    JournalEntry,
    Project,
    SalesInvoice,
    Vendor,
    VendorAdvance,
    VendorBill,
    VendorCreditNote,
)


def _like(col, q):
    return col.ilike(f"%{q}%")


def search(db: Session, q: str, limit: int = 8) -> dict:
    """Return {groups: [{type, label, hits:[{id,title,sub,open}]}]}. `open` = {fn, args}."""
    q = (q or "").strip()
    if len(q) < 2:
        return {"query": q, "groups": []}
    groups: list[dict] = []

    def add(type_key, label, rows, title, sub, open_fn, arg_fn):
        hits = [{"id": r.id, "title": title(r), "sub": sub(r),
                 "open": {"fn": open_fn, "args": arg_fn(r)}} for r in rows]
        if hits:
            groups.append({"type": type_key, "label": label, "hits": hits})

    cust = db.execute(select(Customer).where(or_(_like(Customer.name, q), _like(Customer.trn, q))).limit(limit)).scalars().all()
    add("customer", "Customers", cust, lambda r: r.name, lambda r: f"TRN {r.trn}" if r.trn else "Customer", "openCustomer", lambda r: [r.id])

    ven = db.execute(select(Vendor).where(or_(_like(Vendor.name, q), _like(Vendor.trn, q))).limit(limit)).scalars().all()
    add("vendor", "Vendors", ven, lambda r: r.name, lambda r: f"TRN {r.trn}" if r.trn else "Vendor", "openVendor", lambda r: [r.id])

    inv = db.execute(select(SalesInvoice).where(_like(SalesInvoice.number, q)).order_by(SalesInvoice.number.desc()).limit(limit)).scalars().all()
    add("invoice", "Sales Invoices", inv, lambda r: r.number, lambda r: f"{r.date} · {r.grand_total} · {r.status}", "openInv", lambda r: [r.id])

    bill = db.execute(select(VendorBill).where(or_(_like(VendorBill.number, q), _like(VendorBill.vendor_ref, q))).order_by(VendorBill.number.desc()).limit(limit)).scalars().all()
    add("bill", "Vendor Bills", bill, lambda r: r.number, lambda r: f"{r.date} · {r.grand_total} · {r.status}", "openBill", lambda r: [r.id])

    exp = db.execute(select(Expense).where(or_(_like(Expense.number, q), _like(Expense.reference, q), _like(Expense.category, q))).order_by(Expense.number.desc()).limit(limit)).scalars().all()
    add("expense", "Expenses", exp, lambda r: r.number, lambda r: f"{r.date} · {r.category or ''} · {r.total_amount}", "openExpense", lambda r: [r.id])

    je = db.execute(select(JournalEntry).where(or_(_like(JournalEntry.entry_no, q), _like(JournalEntry.memo, q), _like(JournalEntry.reference, q))).order_by(JournalEntry.entry_no.desc()).limit(limit)).scalars().all()
    add("journal", "Journal Entries", je, lambda r: f"#{r.entry_no}", lambda r: f"{r.date} · {r.memo or ''}", "openJe", lambda r: [r.id])

    cadv = db.execute(select(CustomerAdvance).where(or_(_like(CustomerAdvance.number, q), _like(CustomerAdvance.reference, q))).limit(limit)).scalars().all()
    add("customer_advance", "Customer Advances", cadv, lambda r: r.number, lambda r: f"{r.date} · {r.amount}", "openAdvance", lambda r: ["customer", r.id])

    vadv = db.execute(select(VendorAdvance).where(or_(_like(VendorAdvance.number, q), _like(VendorAdvance.reference, q))).limit(limit)).scalars().all()
    add("vendor_advance", "Vendor Advances", vadv, lambda r: r.number, lambda r: f"{r.date} · {r.amount}", "openAdvance", lambda r: ["vendor", r.id])

    ccn = db.execute(select(CustomerCreditNote).where(_like(CustomerCreditNote.number, q)).limit(limit)).scalars().all()
    add("customer_cn", "Customer Credit Notes", ccn, lambda r: r.number, lambda r: f"{r.date} · {r.grand_total}", "openCreditNote", lambda r: ["customer", r.id])

    vcn = db.execute(select(VendorCreditNote).where(_like(VendorCreditNote.number, q)).limit(limit)).scalars().all()
    add("vendor_cn", "Vendor Credit Notes", vcn, lambda r: r.number, lambda r: f"{r.date} · {r.grand_total}", "openCreditNote", lambda r: ["vendor", r.id])

    prj = db.execute(select(Project).where(or_(_like(Project.code, q), _like(Project.name, q))).limit(limit)).scalars().all()
    add("project", "Projects", prj, lambda r: r.code, lambda r: f"{r.name} · {r.status}", "openProject", lambda r: [r.id])

    return {"query": q, "groups": groups, "count": sum(len(g["hits"]) for g in groups)}
