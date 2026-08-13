"""Financial statements and dashboard KPIs, derived live from the general ledger."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import constants as C
from ..constants import AccountType, NormalBalance
from ..models import (
    Account,
    BillPayment,
    Customer,
    CustomerPayment,
    FixedAsset,
    JournalEntry,
    JournalLine,
    SalesInvoice,
    SalesInvoiceLine,
    VatTreatment,
    Vendor,
    VendorBill,
    VendorBillLine,
)
from ..schemas import (
    AgingLine,
    ApAgingReport,
    ArAgingReport,
    CTComputation,
    CTLine,
    PartyStatement,
    ProductReport,
    ProductRow,
    PartyStatementLine,
    Vat201Box,
    Vat201Return,
    AssetDashboard,
    AssetRegister,
    AssetRegisterRow,
    BalanceSheet,
    CashFlowStatement,
    CustomerAging,
    DashboardKPIs,
    DocReport,
    DocReportGroup,
    DocReportRow,
    IncomeStatement,
    NameValue,
    StatementLine,
    StatementSection,
    VendorAging,
    q,
)

ZERO = Decimal(0)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _net_by_account(db: Session, start: date | None = None, end: date | None = None) -> dict[str, Decimal]:
    stmt = (
        select(JournalLine.account_id, JournalLine.debit, JournalLine.credit)
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .where(JournalEntry.status == C.ENTRY_POSTED)
    )
    if start is not None:
        stmt = stmt.where(JournalEntry.date >= start)
    if end is not None:
        stmt = stmt.where(JournalEntry.date <= end)
    out: dict[str, Decimal] = {}
    for account_id, debit, credit in db.execute(stmt).all():
        out[account_id] = out.get(account_id, ZERO) + (debit - credit)
    return out


def _section(title: str, accounts: list[Account], nets: dict[str, Decimal], *, credit_positive: bool) -> StatementSection:
    lines: list[StatementLine] = []
    total = ZERO
    for a in accounts:
        net = nets.get(a.id, ZERO)
        amount = -net if credit_positive else net
        if amount == 0:
            continue
        total += amount
        lines.append(StatementLine(account_id=a.id, code=a.code, name=a.name, amount=q(amount)))
    lines.sort(key=lambda ln: ln.code)
    return StatementSection(title=title, lines=lines, total=q(total))


def _leaf_accounts(db: Session) -> list[Account]:
    return list(db.execute(select(Account).where(Account.is_group.is_(False)).order_by(Account.code)).scalars())


def income_statement(db: Session, start: date | None = None, end: date | None = None) -> IncomeStatement:
    nets = _net_by_account(db, start=start, end=end)
    accts = _leaf_accounts(db)
    revenue_accts = [a for a in accts if a.type == AccountType.INCOME.value]
    cos_accts = [a for a in accts if a.type == AccountType.EXPENSE.value and a.is_cost_of_sales]
    opex_accts = [a for a in accts if a.type == AccountType.EXPENSE.value and not a.is_cost_of_sales]
    revenue = _section("Revenue", revenue_accts, nets, credit_positive=True)
    cost_of_sales = _section("Cost of Sales", cos_accts, nets, credit_positive=False)
    operating = _section("Operating Expenses", opex_accts, nets, credit_positive=False)
    gross_profit = q(revenue.total - cost_of_sales.total)
    total_expenses = q(cost_of_sales.total + operating.total)
    net_profit = q(revenue.total - total_expenses)
    return IncomeStatement(
        period_start=start, period_end=end, revenue=revenue, cost_of_sales=cost_of_sales,
        gross_profit=gross_profit, operating_expenses=operating, total_income=revenue.total,
        total_expenses=total_expenses, net_profit=net_profit,
    )


def balance_sheet(db: Session, as_of: date | None = None) -> BalanceSheet:
    nets = _net_by_account(db, end=as_of)
    accts = _leaf_accounts(db)
    assets = _section("Assets", [a for a in accts if a.type == AccountType.ASSET.value], nets, credit_positive=False)
    liabilities = _section("Liabilities", [a for a in accts if a.type == AccountType.LIABILITY.value], nets, credit_positive=True)
    equity = _section("Equity", [a for a in accts if a.type == AccountType.EQUITY.value], nets, credit_positive=True)
    income_accts = [a for a in accts if a.type == AccountType.INCOME.value]
    expense_accts = [a for a in accts if a.type == AccountType.EXPENSE.value]
    total_income = sum((-nets.get(a.id, ZERO) for a in income_accts), ZERO)
    total_expense = sum((nets.get(a.id, ZERO) for a in expense_accts), ZERO)
    current_earnings = q(total_income - total_expense)
    total_equity = q(equity.total + current_earnings)
    balanced = q(assets.total) == q(liabilities.total + total_equity)
    return BalanceSheet(
        as_of=as_of, assets=assets, liabilities=liabilities, equity=equity,
        current_earnings=current_earnings, total_assets=q(assets.total),
        total_liabilities=q(liabilities.total), total_equity=total_equity, balanced=balanced,
    )


def cash_flow(db: Session, start: date | None = None, end: date | None = None) -> CashFlowStatement:
    cash_ids = {
        a.id for a in db.execute(select(Account).where(Account.code.in_([C.CODE_CASH, C.CODE_BANK]))).scalars()
    }
    if not cash_ids:
        return CashFlowStatement(period_start=start, period_end=end, opening_cash=ZERO, lines=[], net_change=ZERO, closing_cash=ZERO)
    opening = ZERO
    if start is not None:
        pre = (
            select(JournalLine.debit, JournalLine.credit)
            .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
            .where(JournalEntry.status == C.ENTRY_POSTED, JournalLine.account_id.in_(cash_ids), JournalEntry.date < start)
        )
        for debit, credit in db.execute(pre).all():
            opening += debit - credit
    stmt = (
        select(JournalEntry.source, JournalLine.debit, JournalLine.credit)
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .where(JournalEntry.status == C.ENTRY_POSTED, JournalLine.account_id.in_(cash_ids))
    )
    if start is not None:
        stmt = stmt.where(JournalEntry.date >= start)
    if end is not None:
        stmt = stmt.where(JournalEntry.date <= end)
    by_source: dict[str, Decimal] = {}
    net_change = ZERO
    for source, debit, credit in db.execute(stmt).all():
        move = debit - credit
        by_source[source] = by_source.get(source, ZERO) + move
        net_change += move
    lines = [
        StatementLine(account_id=src, code=src, name=src.replace("_", " ").title(), amount=q(amt))
        for src, amt in sorted(by_source.items()) if amt != 0
    ]
    return CashFlowStatement(
        period_start=start, period_end=end, opening_cash=q(opening), lines=lines,
        net_change=q(net_change), closing_cash=q(opening + net_change),
    )


def _account_balance(db: Session, code: str, nets: dict[str, Decimal]) -> Decimal:
    """Balance of a single account in its normal direction, given a net (debit-credit) map."""
    acct = db.execute(select(Account).where(Account.code == code)).scalar_one_or_none()
    if not acct:
        return ZERO
    net = nets.get(acct.id, ZERO)
    sign = Decimal(1) if NormalBalance(acct.normal_balance) == NormalBalance.DEBIT else Decimal(-1)
    return q(net * sign)


def dashboard(db: Session, today: date | None = None) -> DashboardKPIs:
    today = today or date.today()
    year_start = date(today.year, 1, 1)
    nets_all = _net_by_account(db, end=today)
    pl = income_statement(db, start=year_start, end=today)

    invoices = list(db.execute(select(SalesInvoice).where(SalesInvoice.status.in_(("posted", "partial")))).scalars())
    ar_total = sum((q(inv.grand_total - inv.amount_paid) for inv in invoices), ZERO)
    overdue = sum(1 for inv in invoices if inv.due_date and inv.due_date < today and (inv.grand_total - inv.amount_paid) > 0)

    return DashboardKPIs(
        cash=_account_balance(db, C.CODE_CASH, nets_all),
        bank=_account_balance(db, C.CODE_BANK, nets_all),
        accounts_receivable=_account_balance(db, C.CODE_AR, nets_all),
        accounts_payable=_account_balance(db, C.CODE_AP, nets_all),
        revenue_ytd=pl.total_income,
        expenses_ytd=pl.total_expenses,
        gross_profit_ytd=pl.gross_profit,
        net_profit_ytd=pl.net_profit,
        vat_payable=_account_balance(db, C.CODE_VAT_OUTPUT, nets_all),
        vat_recoverable=_account_balance(db, C.CODE_VAT_INPUT, nets_all),
        outstanding_invoices=len(invoices),
        overdue_invoices=overdue,
        ar_total=q(ar_total),
    )


# ── Aging (shared bucketing for AR + AP) ─────────────────────────────────────────────────
_BUCKET_NAMES = ["current", "d1_30", "d31_60", "d61_90", "d91_120", "d120_plus"]


def _bucket_index(days_overdue: int) -> int:
    if days_overdue <= 0:
        return 0
    if days_overdue <= 30:
        return 1
    if days_overdue <= 60:
        return 2
    if days_overdue <= 90:
        return 3
    if days_overdue <= 120:
        return 4
    return 5


def _empty_buckets() -> list[Decimal]:
    return [ZERO, ZERO, ZERO, ZERO, ZERO, ZERO]


def _classify_risk(buckets: list[Decimal], total: Decimal) -> str:
    """Risk from the actual outstanding distribution: the further overdue and the larger the
    overdue share, the higher the risk."""
    if total <= 0:
        return "low"
    overdue = total - buckets[0]
    if buckets[5] > 0 or buckets[4] > 0:
        return "high"
    if buckets[3] > 0:
        return "high" if overdue / total > Decimal("0.5") else "medium"
    if buckets[1] > 0 or buckets[2] > 0:
        return "medium"
    return "low"


def _aging_rows(docs: list, as_of: date):
    """docs: list of tuples (party_id, party_name, doc_id, number, party_ref, doc_date,
    due_date, original, paid). Returns (per_party dict, grand_buckets)."""
    per_party: dict[str, dict] = {}
    grand = _empty_buckets()
    for (pid, pname, did, number, pref, ddate, due, original, paid) in docs:
        outstanding = q(Decimal(original) - Decimal(paid))
        if outstanding <= 0:
            continue
        ref_date = due or ddate
        days = (as_of - ref_date).days
        idx = _bucket_index(days)
        p = per_party.setdefault(pid, {"name": pname, "buckets": _empty_buckets(), "lines": []})
        p["buckets"][idx] = q(p["buckets"][idx] + outstanding)
        grand[idx] = q(grand[idx] + outstanding)
        p["lines"].append(AgingLine(
            doc_id=did, number=number, party_ref=pref, date=ddate, due_date=due,
            original=q(Decimal(original)), paid=q(Decimal(paid)), outstanding=outstanding,
            days_overdue=max(days, 0), bucket=_BUCKET_NAMES[idx],
        ))
    return per_party, grand


def ar_aging(db: Session, as_of: date | None = None) -> ArAgingReport:
    as_of = as_of or date.today()
    invoices = list(db.execute(select(SalesInvoice).where(SalesInvoice.status.in_(("posted", "partial")))).scalars())
    names = {c.id: c.name for c in db.execute(select(Customer)).scalars()}
    docs = [(inv.customer_id, names.get(inv.customer_id, "?"), inv.id, inv.number, None,
             inv.date, inv.due_date, inv.grand_total, inv.amount_paid) for inv in invoices]
    per_party, grand = _aging_rows(docs, as_of)
    rows: list[CustomerAging] = []
    for pid, p in per_party.items():
        b = p["buckets"]
        total = q(sum(b, ZERO))
        rows.append(CustomerAging(
            customer_id=pid, customer_name=p["name"], current=b[0], d1_30=b[1], d31_60=b[2],
            d61_90=b[3], d91_120=b[4], d120_plus=b[5], total=total, overdue=q(total - b[0]),
            risk=_classify_risk(b, total), lines=sorted(p["lines"], key=lambda x: x.date),
        ))
    rows.sort(key=lambda r: r.customer_name)
    total = q(sum(grand, ZERO))
    return ArAgingReport(
        as_of=as_of, generated_at=_now_iso(), rows=rows, current=grand[0], d1_30=grand[1],
        d31_60=grand[2], d61_90=grand[3], d91_120=grand[4], d120_plus=grand[5], total=total,
        total_current=grand[0], total_overdue=q(total - grand[0]),
    )


def ap_aging(db: Session, as_of: date | None = None) -> ApAgingReport:
    as_of = as_of or date.today()
    bills = list(db.execute(select(VendorBill).where(VendorBill.status.in_(("posted", "partial")))).scalars())
    names = {v.id: v.name for v in db.execute(select(Vendor)).scalars()}
    docs = [(b.vendor_id, names.get(b.vendor_id, "?"), b.id, b.number, b.vendor_ref,
             b.date, b.due_date, b.grand_total, b.amount_paid) for b in bills]
    per_party, grand = _aging_rows(docs, as_of)
    rows: list[VendorAging] = []
    for pid, p in per_party.items():
        b = p["buckets"]
        total = q(sum(b, ZERO))
        rows.append(VendorAging(
            vendor_id=pid, vendor_name=p["name"], current=b[0], d1_30=b[1], d31_60=b[2],
            d61_90=b[3], d91_120=b[4], d120_plus=b[5], total=total, overdue=q(total - b[0]),
            lines=sorted(p["lines"], key=lambda x: x.date),
        ))
    rows.sort(key=lambda r: r.vendor_name)
    total = q(sum(grand, ZERO))
    return ApAgingReport(
        as_of=as_of, generated_at=_now_iso(), rows=rows, current=grand[0], d1_30=grand[1],
        d31_60=grand[2], d61_90=grand[3], d91_120=grand[4], d120_plus=grand[5], total=total,
        total_current=grand[0], total_overdue=q(total - grand[0]),
    )


# ── Document reports (invoice-by-vendor / sales-by-customer) ─────────────────────────────
def _quarter(d: date) -> str:
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


def _group_key(row: DocReportRow, group_by: str) -> tuple[str, str]:
    if group_by == "month":
        return (f"{row.date:%Y-%m}", f"{row.date:%Y-%m}")
    if group_by == "quarter":
        return (_quarter(row.date), _quarter(row.date))
    if group_by == "year":
        return (str(row.date.year), str(row.date.year))
    if group_by == "project":
        return (row.project or "—", row.project or "(none)")
    if group_by == "category":
        return (row.category or "—", row.category or "(none)")
    return (row.party_id, row.party_name)  # party (vendor/customer)


def _doc_report(title: str, rows: list[DocReportRow], group_by: str, as_of: date, start, end) -> DocReport:
    groups: dict[str, dict] = {}
    for r in rows:
        key, label = _group_key(r, group_by)
        g = groups.setdefault(key, {"label": label, "net": ZERO, "vat": ZERO, "gross": ZERO, "paid": ZERO, "out": ZERO, "count": 0})
        g["net"] += r.net; g["vat"] += r.vat; g["gross"] += r.gross; g["paid"] += r.paid; g["out"] += r.outstanding; g["count"] += 1
    group_list = [
        DocReportGroup(key=k, label=g["label"], net=q(g["net"]), vat=q(g["vat"]), gross=q(g["gross"]),
                       paid=q(g["paid"]), outstanding=q(g["out"]), count=g["count"])
        for k, g in groups.items()
    ]
    group_list.sort(key=lambda x: x.label)
    overdue = q(sum((r.outstanding for r in rows if r.due_date and r.due_date < as_of), ZERO))
    return DocReport(
        title=title, as_of=as_of.isoformat(), generated_at=_now_iso(), period_start=start, period_end=end,
        group_by=group_by, groups=group_list, rows=rows,
        total_documents=len(rows),
        total_net=q(sum((r.net for r in rows), ZERO)),
        total_vat=q(sum((r.vat for r in rows), ZERO)),
        total_gross=q(sum((r.gross for r in rows), ZERO)),
        total_paid=q(sum((r.paid for r in rows), ZERO)),
        total_outstanding=q(sum((r.outstanding for r in rows), ZERO)),
        total_overdue=overdue,
    )


def invoice_by_vendor(db: Session, group_by: str = "vendor", start: date | None = None, end: date | None = None) -> DocReport:
    stmt = select(VendorBill).where(VendorBill.status != "void")
    if start:
        stmt = stmt.where(VendorBill.date >= start)
    if end:
        stmt = stmt.where(VendorBill.date <= end)
    vendors = {v.id: v for v in db.execute(select(Vendor)).scalars()}
    rows = []
    for b in db.execute(stmt.order_by(VendorBill.date)).scalars():
        v = vendors.get(b.vendor_id)
        rows.append(DocReportRow(
            party_id=b.vendor_id, party_name=v.name if v else "?", party_trn=v.trn if v else None,
            doc_id=b.id, number=b.number, ref=b.vendor_ref, date=b.date, due_date=b.due_date,
            description=b.notes, net=b.net_total, vat=b.vat_total, gross=b.grand_total,
            paid=b.amount_paid, outstanding=q(b.grand_total - b.amount_paid), status=b.status,
            currency=b.currency, project=b.project, category=b.expense_category,
        ))
    return _doc_report("Invoice by Vendor", rows, group_by, end or date.today(), start, end)


def sales_by_customer(db: Session, group_by: str = "customer", start: date | None = None, end: date | None = None) -> DocReport:
    stmt = select(SalesInvoice).where(SalesInvoice.status != "void")
    if start:
        stmt = stmt.where(SalesInvoice.date >= start)
    if end:
        stmt = stmt.where(SalesInvoice.date <= end)
    customers = {c.id: c for c in db.execute(select(Customer)).scalars()}
    rows = []
    for inv in db.execute(stmt.order_by(SalesInvoice.date)).scalars():
        c = customers.get(inv.customer_id)
        rows.append(DocReportRow(
            party_id=inv.customer_id, party_name=c.name if c else "?", party_trn=c.trn if c else None,
            doc_id=inv.id, number=inv.number, ref=None, date=inv.date, due_date=inv.due_date,
            description=inv.notes, net=inv.net_total, vat=inv.vat_total, gross=inv.grand_total,
            paid=inv.amount_paid, outstanding=q(inv.grand_total - inv.amount_paid), status=inv.status,
            currency=inv.currency, project=inv.project, category=inv.sales_category,
        ))
    return _doc_report("Sales by Customer", rows, group_by, end or date.today(), start, end)


# ── Fixed assets ─────────────────────────────────────────────────────────────────────────
def _register_row(a: FixedAsset) -> AssetRegisterRow:
    cost = Decimal(a.purchase_cost)
    accum = Decimal(a.accumulated_depreciation)
    return AssetRegisterRow(
        id=a.id, asset_code=a.asset_code, name=a.name, category=a.category, purchase_date=a.purchase_date,
        total_cost=q(cost + Decimal(a.vat_amount)), accumulated_depreciation=q(accum),
        net_book_value=q(cost - accum), status=a.status, location=a.location, department=a.department,
    )


def fixed_asset_register(db: Session, category: str | None = None, location: str | None = None,
                         department: str | None = None, project: str | None = None,
                         status: str | None = None) -> AssetRegister:
    stmt = select(FixedAsset).order_by(FixedAsset.asset_code)
    if category:
        stmt = stmt.where(FixedAsset.category == category)
    if location:
        stmt = stmt.where(FixedAsset.location == location)
    if department:
        stmt = stmt.where(FixedAsset.department == department)
    if project:
        stmt = stmt.where(FixedAsset.project == project)
    if status:
        stmt = stmt.where(FixedAsset.status == status)
    rows = [_register_row(a) for a in db.execute(stmt).scalars()]
    return AssetRegister(
        generated_at=_now_iso(), rows=rows,
        total_cost=q(sum((r.total_cost for r in rows), ZERO)),
        total_accumulated_depreciation=q(sum((r.accumulated_depreciation for r in rows), ZERO)),
        total_net_book_value=q(sum((r.net_book_value for r in rows), ZERO)),
        count=len(rows),
    )


_POSTED_SALES = ("posted", "partial", "paid")


def _statement(kind, party_id, party_name, party_trn, currency, charges, credits, start, end):
    """Assemble a running-balance statement from charge events (debit) and credit events.
    `charges`/`credits` are lists of (date, type, reference, amount, doc_id, link)."""
    from datetime import datetime, timezone
    before = ZERO
    events = []
    for evs, is_debit in ((charges, True), (credits, False)):
        for (d, typ, ref, amount, doc_id, link) in evs:
            amount = q(amount)
            if start and d < start:
                before += amount if is_debit else -amount
                continue
            if end and d > end:
                continue
            events.append((d, typ, ref, amount if is_debit else ZERO, ZERO if is_debit else amount, doc_id, link))
    events.sort(key=lambda e: (e[0], 0 if e[3] > 0 else 1))
    running = q(before)
    lines: list[PartyStatementLine] = []
    td = tc = ZERO
    for (d, typ, ref, dr, cr, doc_id, link) in events:
        running = q(running + dr - cr)
        td += dr
        tc += cr
        lines.append(PartyStatementLine(date=d, type=typ, reference=ref, doc_id=doc_id, link=link,
                                        debit=dr, credit=cr, balance=running))
    return PartyStatement(
        party_id=party_id, party_name=party_name, party_trn=party_trn, kind=kind, currency=currency,
        period_start=start, period_end=end, generated_at=datetime.now(timezone.utc).isoformat(),
        opening_balance=q(before), lines=lines, total_debit=q(td), total_credit=q(tc), closing_balance=running,
    )


def product_movement(db: Session, direction: str, start: date | None = None, end: date | None = None) -> ProductReport:
    """Sales (issues) or purchases (receipts) by product, from stock movements — the only
    product-linked transactions in the ledger."""
    from datetime import datetime, timezone

    from ..models import Product, StockMovement
    title = "Sales by Product (stock issues)" if direction == "issue" else "Purchases by Product (stock receipts)"
    stmt = select(StockMovement).where(StockMovement.movement_type == direction)
    if start:
        stmt = stmt.where(StockMovement.date >= start)
    if end:
        stmt = stmt.where(StockMovement.date <= end)
    products = {p.id: p for p in db.execute(select(Product)).scalars()}
    agg: dict[str, list] = {}
    for mv in db.execute(stmt).scalars():
        a = agg.setdefault(mv.product_id, [ZERO, ZERO, 0])
        a[0] += abs(Decimal(mv.quantity))
        a[1] += abs(Decimal(mv.total_cost))
        a[2] += 1
    rows = []
    for pid, (qty, val, cnt) in agg.items():
        p = products.get(pid)
        rows.append(ProductRow(product_id=pid, sku=p.sku if p else "?", name=p.name if p else "?",
                               category=p.category if p else None, quantity=q(qty).quantize(Decimal("0.0001")),
                               value=q(val), movements=cnt))
    rows.sort(key=lambda r: r.value, reverse=True)
    return ProductReport(
        title=title, direction=direction, period_start=start, period_end=end,
        generated_at=datetime.now(timezone.utc).isoformat(), rows=rows,
        total_quantity=q(sum((r.quantity for r in rows), ZERO)).quantize(Decimal("0.0001")),
        total_value=q(sum((r.value for r in rows), ZERO)),
    )


def ct_computation(db: Session, start: date | None = None, end: date | None = None) -> CTComputation:
    """UAE Corporate Tax computation from the ledger: accounting profit → adjustments →
    taxable income → configurable threshold/rate → CT payable. PROVISIONAL — the accounting
    treatment is NOT assumed to equal the tax treatment; classification/losses are not applied
    unless configured, and every figure needs SME validation before filing."""
    from datetime import datetime, timezone

    pl = income_statement(db, start=start, end=end)
    accounting_profit = q(pl.net_profit)

    # Non-deductible add-backs from configured accounts (empty by default → 0).
    nets = _net_by_account(db, start=start, end=end)
    addbacks = ZERO
    if C.CT_NONDEDUCTIBLE_CODES:
        accts = {a.code: a for a in db.execute(select(Account)).scalars()}
        for code in C.CT_NONDEDUCTIBLE_CODES:
            a = accts.get(code)
            if a:
                addbacks += nets.get(a.id, ZERO)  # expense = debit-positive
    addbacks = q(addbacks)
    exempt = ZERO
    taxable_before = q(accounting_profit + addbacks - exempt)
    losses = ZERO  # no loss schedule configured — never auto-applied
    taxable_income = q(taxable_before - losses)

    threshold = Decimal(C.CT_THRESHOLD_AED)
    rate = Decimal(C.CT_STANDARD_RATE)
    ct_payable = q(max(ZERO, taxable_income - threshold) * rate) if taxable_income > 0 else ZERO
    eff = q(ct_payable / taxable_income) if taxable_income > 0 else ZERO

    lines = [
        CTLine(label="Accounting profit / (loss)", amount=accounting_profit, kind="subtotal"),
        CTLine(label="Add: Non-deductible expenses", amount=addbacks,
               note="No accounts classified as non-deductible — requires tax review" if addbacks == 0 else None),
        CTLine(label="Less: Exempt income", amount=q(-exempt)),
        CTLine(label="Taxable income before reliefs", amount=taxable_before, kind="subtotal"),
        CTLine(label="Less: Tax losses utilised", amount=q(-losses),
               note="No loss schedule configured — losses are not auto-applied"),
        CTLine(label="Taxable income", amount=taxable_income, kind="subtotal"),
        CTLine(label=f"First {threshold:,.0f} @ 0%", amount=ZERO, kind="rate"),
        CTLine(label=f"Above {threshold:,.0f} @ {rate*100:.0f}%", amount=ct_payable, kind="rate"),
        CTLine(label="Corporate Tax payable", amount=ct_payable, kind="total"),
    ]
    notes = [
        "PROVISIONAL — " + C.CT_LEGAL_REF + ". Requires professional (SME) validation before filing.",
        "Accounting treatment is shown separately from tax treatment; an accounting expense is NOT "
        "assumed deductible for CT. Unclassified items should be treated as 'Requires Tax Review'.",
        "Tax adjustments, exempt income and loss relief are only applied when configured for the entity "
        "— none are configured here, so accounting profit flows through to taxable income.",
    ]
    return CTComputation(
        period_start=start, period_end=end, generated_at=datetime.now(timezone.utc).isoformat(),
        accounting_profit=accounting_profit, non_deductible_addbacks=addbacks, exempt_income=exempt,
        taxable_income_before_reliefs=taxable_before, tax_losses_utilised=losses,
        taxable_income=taxable_income, threshold=q(threshold), standard_rate=rate, ct_payable=ct_payable,
        effective_rate=eff, status="provisional", legal_ref=C.CT_LEGAL_REF, lines=lines, notes=notes,
        pl_net_profit=q(pl.net_profit), reconciled=(accounting_profit == q(pl.net_profit)),
    )


def retention_report(db: Session, side: str = "customer") -> dict:
    """Retention receivable (customer) or payable (vendor): per-document held / released /
    outstanding, with release-date scheduling. side = 'customer' | 'vendor'."""
    today = datetime.now(timezone.utc).date()
    if side == "vendor":
        docs = db.execute(select(VendorBill).where(VendorBill.retention_applicable.is_(True),
                                                   VendorBill.status != "void")).scalars()
        vendors = {v.id: v.name for v in db.execute(select(Vendor)).scalars()}
        party_of, num_of, ref = (lambda d: vendors.get(d.vendor_id, "—")), (lambda d: d.number), "Retention Payable"
    else:
        docs = db.execute(select(SalesInvoice).where(SalesInvoice.retention_applicable.is_(True),
                                                     SalesInvoice.status != "void")).scalars()
        customers = {c.id: c.name for c in db.execute(select(Customer)).scalars()}
        party_of, num_of, ref = (lambda d: customers.get(d.customer_id, "—")), (lambda d: d.number), "Retention Receivable"

    rows = []
    t_amt = t_rel = t_out = 0.0
    for d in docs:
        amt = float(d.retention_amount); rel = float(d.retention_released); out = round(amt - rel, 2)
        rd = d.retention_release_date
        if out <= 0:
            status = "released"
        elif rd is None:
            status = "unscheduled"
        elif rd <= today:
            status = "due"
        else:
            status = "scheduled"
        rows.append({
            "id": d.id, "number": num_of(d), "party": party_of(d), "date": str(d.date),
            "project": d.project or "", "contract": d.contract_reference or "",
            "retention_amount": round(amt, 2), "released": round(rel, 2), "outstanding": out,
            "release_date": str(rd) if rd else "", "status": status,
            "days_to_release": (rd - today).days if rd else None,
            "journal_entry_id": d.journal_entry_id,
        })
        t_amt += amt; t_rel += rel; t_out += out
    rows.sort(key=lambda r: (r["status"] != "due", r["release_date"] or "9999"))
    return {"side": side, "account": ref, "rows": rows,
            "total_retained": round(t_amt, 2), "total_released": round(t_rel, 2),
            "total_outstanding": round(t_out, 2),
            "due_now": round(sum(r["outstanding"] for r in rows if r["status"] == "due"), 2)}


def customer_statement(db: Session, customer_id: str, start: date | None = None, end: date | None = None) -> PartyStatement:
    cust = db.get(Customer, customer_id)
    if not cust:
        from ..services.sales import SalesError
        raise SalesError("Customer not found.")
    invs = db.execute(select(SalesInvoice).where(
        SalesInvoice.customer_id == customer_id, SalesInvoice.status.in_(_POSTED_SALES))).scalars()
    charges = [(i.date, "Invoice", i.number, i.grand_total, i.id, "invoice") for i in invs]
    pays = db.execute(select(CustomerPayment).where(CustomerPayment.customer_id == customer_id)).scalars()
    credits = [(p.date, "Receipt", p.reference, p.amount, p.journal_entry_id, "journal") for p in pays]
    return _statement("customer", cust.id, cust.name, cust.trn, cust.currency, charges, credits, start, end)


def vendor_statement(db: Session, vendor_id: str, start: date | None = None, end: date | None = None) -> PartyStatement:
    ven = db.get(Vendor, vendor_id)
    if not ven:
        from ..services.purchases import PurchaseError
        raise PurchaseError("Vendor not found.")
    bills = db.execute(select(VendorBill).where(
        VendorBill.vendor_id == vendor_id, VendorBill.status.in_(_POSTED_SALES))).scalars()
    charges = [(b.date, "Bill", b.number, b.grand_total, b.id, "bill") for b in bills]
    pays = db.execute(select(BillPayment).where(BillPayment.vendor_id == vendor_id)).scalars()
    credits = [(p.date, "Payment", p.reference, p.amount, p.journal_entry_id, "journal") for p in pays]
    return _statement("vendor", ven.id, ven.name, ven.trn, ven.currency, charges, credits, start, end)


def vat_return(db: Session, start: date | None = None, end: date | None = None) -> Vat201Return:
    """UAE FTA VAT201 return computed from posted sales invoices (output tax) and vendor
    bills (recoverable input tax), reconciled to the GL VAT accounts."""
    posted = ("posted", "partial", "paid")

    # VAT201 box mapping is driven by the VAT Treatment master (configurable), NOT hard-coded.
    # Each line stores the treatment code it was raised under; we classify by that code's *kind*.
    kinds = {t.code: t.kind for t in db.execute(select(VatTreatment)).scalars()}

    def _kind(code: str | None, rate) -> str:
        """Resolve a line's VAT treatment kind. Falls back to rate (standard if >0 else zero)
        for legacy lines with no stored treatment code / an unknown code."""
        if code and code in kinds:
            return kinds[code]
        return "standard" if Decimal(rate or 0) > 0 else "zero"

    # Output side — sales invoice lines classified by treatment kind.
    std_sales_net = std_sales_vat = zero_sales_net = exempt_sales_net = rc_sales_net = os_sales_net = ZERO
    inv_stmt = (
        select(SalesInvoiceLine, SalesInvoice.date)
        .join(SalesInvoice, SalesInvoiceLine.invoice_id == SalesInvoice.id)
        .where(SalesInvoice.status.in_(posted))
    )
    if start:
        inv_stmt = inv_stmt.where(SalesInvoice.date >= start)
    if end:
        inv_stmt = inv_stmt.where(SalesInvoice.date <= end)
    for line, _d in db.execute(inv_stmt).all():
        k = _kind(line.vat_treatment, line.vat_rate)
        if k == "standard":
            std_sales_net += line.net_amount; std_sales_vat += line.vat_amount
        elif k == "zero":
            zero_sales_net += line.net_amount
        elif k == "exempt":
            exempt_sales_net += line.net_amount
        elif k == "reverse_charge":
            rc_sales_net += line.net_amount     # domestic RC supply — recipient self-assesses
        else:  # out_of_scope / other
            os_sales_net += line.net_amount

    # Input side — vendor bill lines classified by treatment kind (RC self-assessed both sides).
    std_exp_net = std_exp_vat = zero_exp_net = exempt_exp_net = rc_exp_net = rc_exp_vat = ZERO
    bill_stmt = (
        select(VendorBillLine, VendorBill.date)
        .join(VendorBill, VendorBillLine.bill_id == VendorBill.id)
        .where(VendorBill.status.in_(posted))
    )
    if start:
        bill_stmt = bill_stmt.where(VendorBill.date >= start)
    if end:
        bill_stmt = bill_stmt.where(VendorBill.date <= end)
    for line, _d in db.execute(bill_stmt).all():
        k = _kind(line.vat_treatment, line.vat_rate)
        if k == "reverse_charge":
            rc_exp_net += line.net_amount; rc_exp_vat += line.vat_amount
        elif k == "standard":
            std_exp_net += line.net_amount; std_exp_vat += line.vat_amount
        elif k == "zero":
            zero_exp_net += line.net_amount
        elif k == "exempt":
            exempt_exp_net += line.net_amount
        # out_of_scope / other: excluded from the return

    rc_vat = q(rc_exp_vat)                      # self-assessed reverse-charge VAT (net-zero)
    out_vat = q(std_sales_vat + rc_vat)
    in_vat = q(std_exp_vat + rc_vat)            # RC input is recoverable
    out_amount = q(std_sales_net + zero_sales_net + exempt_sales_net + rc_sales_net)
    in_amount = q(std_exp_net + rc_exp_net)
    net = q(out_vat - in_vat)

    boxes = [
        Vat201Box(box="1", label="Standard rated supplies", amount=q(std_sales_net), vat=q(std_sales_vat)),
        Vat201Box(box="2", label="Tax refunds to tourists", amount=ZERO, vat=ZERO),
        Vat201Box(box="3", label="Supplies subject to reverse charge",
                  amount=q(rc_sales_net + rc_exp_net), vat=rc_vat),
        Vat201Box(box="4", label="Zero rated supplies", amount=q(zero_sales_net), vat=ZERO),
        Vat201Box(box="5", label="Exempt supplies", amount=q(exempt_sales_net), vat=ZERO),
        Vat201Box(box="6", label="Goods imported into the UAE", amount=ZERO, vat=ZERO),
        Vat201Box(box="7", label="Adjustments to goods imported", amount=ZERO, vat=ZERO),
        Vat201Box(box="8", label="Totals (Sales / outputs)", amount=out_amount, vat=out_vat),
        Vat201Box(box="9", label="Standard rated expenses", amount=q(std_exp_net), vat=q(std_exp_vat)),
        Vat201Box(box="10", label="Supplies subject to reverse charge (recoverable)",
                  amount=q(rc_exp_net), vat=rc_vat),
        Vat201Box(box="11", label="Totals (Expenses / inputs)", amount=in_amount, vat=in_vat),
        Vat201Box(box="12", label="Total output tax due", amount=ZERO, vat=out_vat),
        Vat201Box(box="13", label="Total input tax recoverable", amount=ZERO, vat=in_vat),
        Vat201Box(box="14", label="Net VAT due (payable / refundable)", amount=ZERO, vat=net),
    ]

    # Reconcile to the GL VAT accounts over the period.
    def _acct_movement(code: str, credit_positive: bool) -> Decimal:
        acct = db.execute(select(Account).where(Account.code == code)).scalar_one_or_none()
        if not acct:
            return ZERO
        s = (select(func.coalesce(func.sum(JournalLine.debit - JournalLine.credit), 0))
             .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
             .where(JournalEntry.status == C.ENTRY_POSTED, JournalLine.account_id == acct.id))
        if start:
            s = s.where(JournalEntry.date >= start)
        if end:
            s = s.where(JournalEntry.date <= end)
        net_dc = Decimal(db.execute(s).scalar() or 0)
        return q(-net_dc if credit_positive else net_dc)

    gl_out = _acct_movement(C.CODE_VAT_OUTPUT, credit_positive=True)   # VAT Payable is credit-normal
    gl_in = _acct_movement(C.CODE_VAT_INPUT, credit_positive=False)    # VAT Recoverable is debit-normal
    reconciled = abs(gl_out - out_vat) < Decimal("0.01") and abs(gl_in - in_vat) < Decimal("0.01")
    notes = [
        "Boxes are mapped from each line's VAT Treatment (configurable master): standard→Box 1/9, "
        "zero-rated→Box 4, exempt→Box 5, reverse-charge→Box 3/10, out-of-scope→excluded.",
        "Output VAT is derived from posted sales invoices; input VAT from posted vendor bills.",
        "Emirate-level split (boxes 1a–1g) and imports are shown as zero — this ledger does not "
        "capture those dimensions; add them at source if required by the FTA filing.",
        "Reverse-charge VAT (treatment RC) is self-assessed as both output (Box 3) and recoverable "
        "input (Box 10), net-zero, and included in the output/input totals.",
    ]
    if not reconciled:
        notes.append(f"⚠ Subledger VAT ({out_vat}/{in_vat}) does not tie to the GL VAT accounts "
                     f"({gl_out}/{gl_in}) — investigate manual journals to 1300/2100.")

    # VAT-on-advances SME gate: if any VAT-bearing advance falls in the period and the rule is
    # not SME-approved+enabled, the return is PROVISIONAL and not filing-ready.
    from ..models import CustomerAdvance, VendorAdvance
    from . import system_settings
    provisional = False
    provisional_reason = None
    adv_q = select(func.count()).where(CustomerAdvance.vat_applicable.is_(True))
    vadv_q = select(func.count()).where(VendorAdvance.vat_applicable.is_(True))
    if start:
        adv_q = adv_q.where(CustomerAdvance.date >= start); vadv_q = vadv_q.where(VendorAdvance.date >= start)
    if end:
        adv_q = adv_q.where(CustomerAdvance.date <= end); vadv_q = vadv_q.where(VendorAdvance.date <= end)
    has_adv_vat = (db.execute(adv_q).scalar() or 0) + (db.execute(vadv_q).scalar() or 0) > 0
    if has_adv_vat and not system_settings.advance_vat_filing_ready(db):
        provisional = True
        provisional_reason = ("Provisional — SME validation required. This return includes VAT on "
                              "advance payments, a treatment that must be validated and approved by a "
                              "UAE VAT SME before filing.")
        notes.append(provisional_reason)

    from datetime import datetime, timezone
    return Vat201Return(
        period_start=start, period_end=end, generated_at=datetime.now(timezone.utc).isoformat(),
        boxes=boxes, total_output_vat=out_vat, total_input_vat=in_vat, net_vat_due=net,
        is_refund=net < 0, gl_output_vat=gl_out, gl_input_vat=gl_in, reconciled=reconciled,
        provisional=provisional, provisional_reason=provisional_reason, notes=notes,
    )


def fixed_asset_dashboard(db: Session, today: date | None = None) -> AssetDashboard:
    today = today or date.today()
    year_start = date(today.year, 1, 1)
    assets = list(db.execute(select(FixedAsset)).scalars())
    active = [a for a in assets if a.status == "active"]
    total_cost = q(sum((Decimal(a.purchase_cost) + Decimal(a.vat_amount) for a in assets), ZERO))
    accum = q(sum((Decimal(a.accumulated_depreciation) for a in assets), ZERO))
    nbv = q(sum((Decimal(a.purchase_cost) - Decimal(a.accumulated_depreciation) for a in active), ZERO))

    # Current-year depreciation from posted 'assets'-source depreciation entries.
    cy_dep = ZERO
    dep_stmt = (
        select(JournalLine.debit)
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .join(Account, JournalLine.account_id == Account.id)
        .where(JournalEntry.status == C.ENTRY_POSTED, JournalEntry.source == "assets",
               Account.code == C.CODE_DEP_EXPENSE, JournalEntry.date >= year_start, JournalEntry.date <= today)
    )
    for (debit,) in db.execute(dep_stmt).all():
        cy_dep += debit

    def _grp(attr):
        agg: dict[str, list] = defaultdict(lambda: [ZERO, 0])
        for a in assets:
            key = getattr(a, attr) or "—"
            agg[key][0] = q(agg[key][0] + (Decimal(a.purchase_cost) - Decimal(a.accumulated_depreciation)))
            agg[key][1] += 1
        return [NameValue(name=k, value=v[0], count=v[1]) for k, v in sorted(agg.items())]

    def _rows(items):
        return [_register_row(a) for a in items]

    approaching = [a for a in active if a.last_depreciation_date and
                   sum(1 for t in a.transactions if t.type == "depreciation") >= a.useful_life_months - 3]
    recent = sorted(assets, key=lambda a: a.purchase_date, reverse=True)[:5]
    disposed = [a for a in assets if a.status in ("disposed", "written_off", "retired")]

    return AssetDashboard(
        total_cost=total_cost, accumulated_depreciation=accum, net_book_value=nbv,
        current_year_depreciation=q(cy_dep), asset_count=len(assets), active_count=len(active),
        disposed_count=len(disposed), by_category=_grp("category"), by_location=_grp("location"),
        by_department=_grp("department"), approaching_end_of_life=_rows(approaching),
        recently_acquired=_rows(recent), disposed=_rows(disposed),
    )
