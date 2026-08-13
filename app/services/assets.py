"""Fixed Assets service: register, depreciation (straight-line & declining-balance),
disposal — all integrated with the general ledger.

Posting rules (source='assets'):

  Acquisition (optional)  Dr Fixed Assets / Cr Bank or Accounts Payable
  Depreciation (monthly)  Dr Depreciation Expense / Cr Accumulated Depreciation
  Disposal                Cr Fixed Assets (cost), Dr Accumulated Depreciation,
                          Dr Bank (proceeds), and the balancing Gain/Loss on Disposal
"""

from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .. import constants as C
from ..models import AssetTransaction, FixedAsset, JournalEntry, Vendor
from ..schemas import (
    AssetIn,
    AssetOut,
    AssetTransactionOut,
    DepreciationScheduleRow,
    DisposalIn,
    q,
)
from . import ledger, validation
from ..schemas import JournalEntryIn, JournalLineIn

ZERO = Decimal(0)


class AssetError(ValueError):
    """Domain error → HTTP 400."""


def _account_by_code(db: Session, code: str):
    from ..models import Account
    acct = db.execute(select(Account).where(Account.code == code)).scalar_one_or_none()
    if not acct:
        raise AssetError(f"Required account '{code}' is missing — seed the Chart of Accounts first.")
    return acct


def _month_end(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


def _add_month(y: int, m: int) -> tuple[int, int]:
    return (y + 1, 1) if m == 12 else (y, m + 1)


# ── Depreciation maths ──────────────────────────────────────────────────────────────────
def _schedule(asset: FixedAsset) -> list[tuple[date, Decimal, Decimal, Decimal]]:
    """Full monthly schedule as (period_end, depreciation, accumulated, net_book_value).
    Depreciation base is the (net) purchase cost less residual value."""
    cost = Decimal(asset.purchase_cost)
    residual = Decimal(asset.residual_value)
    base = cost - residual
    if base <= 0 or asset.useful_life_months <= 0:
        return []
    start = asset.depreciation_start or asset.in_service_date or asset.purchase_date
    rows: list[tuple[date, Decimal, Decimal, Decimal]] = []
    accumulated = ZERO
    y, m = start.year, start.month
    if asset.method == "declining_balance" and asset.declining_rate and asset.declining_rate > 0:
        monthly_rate = Decimal(asset.declining_rate) / Decimal(12)
        for _ in range(asset.useful_life_months):
            nbv = cost - accumulated
            dep = q(nbv * monthly_rate)
            if accumulated + dep > base:
                dep = q(base - accumulated)
            if dep <= 0:
                break
            accumulated = q(accumulated + dep)
            rows.append((_month_end(y, m), dep, accumulated, q(cost - accumulated)))
            y, m = _add_month(y, m)
    else:  # straight_line
        monthly = q(base / Decimal(asset.useful_life_months))
        for i in range(asset.useful_life_months):
            dep = monthly if i < asset.useful_life_months - 1 else q(base - accumulated)
            accumulated = q(accumulated + dep)
            rows.append((_month_end(y, m), dep, accumulated, q(cost - accumulated)))
            y, m = _add_month(y, m)
    return rows


def depreciation_schedule(db: Session, asset_id: str) -> list[DepreciationScheduleRow]:
    asset = db.get(FixedAsset, asset_id)
    if not asset:
        raise AssetError("Asset not found.")
    return [
        DepreciationScheduleRow(period=f"{d.year:04d}-{d.month:02d}", depreciation=dep, accumulated=acc, net_book_value=nbv)
        for (d, dep, acc, nbv) in _schedule(asset)
    ]


# ── Serialisation ───────────────────────────────────────────────────────────────────────
def _asset_out(asset: FixedAsset, db: Session | None = None) -> AssetOut:
    cost = Decimal(asset.purchase_cost)
    accum = Decimal(asset.accumulated_depreciation)
    nbv = q(cost - accum)
    # Remaining life = months in the schedule still ahead of what's been booked.
    booked = sum(1 for t in asset.transactions if t.type == "depreciation")
    remaining = max(asset.useful_life_months - booked, 0) if asset.status == "active" else 0
    vendor = db.get(Vendor, asset.vendor_id) if (db and asset.vendor_id) else None
    v_name = vendor.name if vendor else asset.supplier
    v_addr = (vendor.billing_address or vendor.address) if vendor else None
    warnings = validation.party_warnings(vendor.trn if vendor else None, v_addr, party="Vendor") if vendor else []
    return AssetOut(
        id=asset.id, asset_code=asset.asset_code, name=asset.name, category=asset.category,
        description=asset.description, purchase_date=asset.purchase_date, in_service_date=asset.in_service_date,
        supplier=asset.supplier, vendor_id=asset.vendor_id, vendor_name=v_name,
        vendor_trn=vendor.trn if vendor else None, vendor_address=v_addr,
        invoice_number=asset.invoice_number, bill_id=asset.bill_id,
        purchase_cost=cost, vat_amount=Decimal(asset.vat_amount), total_cost=q(cost + Decimal(asset.vat_amount)),
        warnings=warnings,
        currency=asset.currency, residual_value=Decimal(asset.residual_value), method=asset.method,
        useful_life_months=asset.useful_life_months, declining_rate=Decimal(asset.declining_rate),
        depreciation_start=asset.depreciation_start, accumulated_depreciation=accum, net_book_value=nbv,
        remaining_life_months=remaining, last_depreciation_date=asset.last_depreciation_date,
        location=asset.location, department=asset.department, project=asset.project,
        cost_center=asset.cost_center, responsible_person=asset.responsible_person,
        serial_number=asset.serial_number, warranty_info=asset.warranty_info, status=asset.status,
        disposal_date=asset.disposal_date, disposal_proceeds=Decimal(asset.disposal_proceeds),
        disposal_gain_loss=Decimal(asset.disposal_gain_loss) if asset.disposal_gain_loss is not None else None,
        transactions=[
            AssetTransactionOut(id=t.id, date=t.date, type=t.type, amount=Decimal(t.amount), detail=t.detail,
                                journal_entry_id=t.journal_entry_id,
                                created_at=t.created_at.isoformat() if t.created_at else None)
            for t in asset.transactions
        ],
    )


def _get(db: Session, asset_id: str) -> FixedAsset:
    asset = db.execute(
        select(FixedAsset).where(FixedAsset.id == asset_id).options(selectinload(FixedAsset.transactions))
    ).scalar_one_or_none()
    if not asset:
        raise AssetError("Asset not found.")
    return asset


def _next_asset_code(db: Session) -> str:
    count = db.execute(select(func.count(FixedAsset.id))).scalar() or 0
    return f"FA-{count + 1:04d}"


# ── Lifecycle ───────────────────────────────────────────────────────────────────────────
def create_asset(db: Session, data: AssetIn) -> AssetOut:
    if data.asset_code:
        if db.execute(select(FixedAsset).where(FixedAsset.asset_code == data.asset_code)).scalar_one_or_none():
            raise AssetError(f"Asset code '{data.asset_code}' already exists.")
    if data.vendor_id and not db.get(Vendor, data.vendor_id):
        raise AssetError("Vendor not found.")
    asset = FixedAsset(
        asset_code=data.asset_code or _next_asset_code(db), name=data.name, category=data.category,
        description=data.description, purchase_date=data.purchase_date,
        in_service_date=data.in_service_date or data.purchase_date, supplier=data.supplier,
        vendor_id=data.vendor_id,
        invoice_number=data.invoice_number, bill_id=data.bill_id, purchase_cost=q(data.purchase_cost),
        vat_amount=q(data.vat_amount), currency=data.currency, residual_value=q(data.residual_value),
        method=data.method, useful_life_months=data.useful_life_months,
        declining_rate=data.declining_rate,
        depreciation_start=data.depreciation_start or data.in_service_date or data.purchase_date,
        location=data.location, department=data.department, project=data.project,
        cost_center=data.cost_center, responsible_person=data.responsible_person,
        serial_number=data.serial_number, warranty_info=data.warranty_info, status="active",
    )
    db.add(asset)
    db.flush()

    je_id = None
    if data.auto_post_acquisition:
        fa = _account_by_code(db, C.CODE_FIXED_ASSETS)
        credit = _account_by_code(db, data.acquisition_credit_code)
        entry = ledger.create_journal_entry(
            db,
            JournalEntryIn(
                date=data.purchase_date, memo=f"Acquisition of {asset.asset_code} {asset.name}",
                reference=asset.asset_code, source="assets", currency=data.currency,
                lines=[
                    JournalLineIn(account_id=fa.id, debit=q(data.purchase_cost)),
                    JournalLineIn(account_id=credit.id, credit=q(data.purchase_cost)),
                ],
                auto_post=True,
            ),
        )
        je_id = entry.id
    db.add(AssetTransaction(
        asset_id=asset.id, date=data.purchase_date, type="acquisition", amount=q(data.purchase_cost),
        detail=f"Acquired{' (posted)' if je_id else ''}", journal_entry_id=je_id,
    ))
    db.commit()
    db.refresh(asset)
    return _asset_out(asset, db)


_ASSET_AUDIT_FIELDS = ("name", "category", "purchase_date", "in_service_date", "supplier",
                       "vendor_id", "invoice_number", "purchase_cost", "vat_amount",
                       "residual_value", "method", "useful_life_months", "project", "cost_center")


def update_asset(db: Session, asset_id: str, data: AssetIn, actor: str | None = None,
                 reason: str | None = None) -> AssetOut:
    """Edit a fixed asset's acquisition details via reverse-and-repost. Blocked once
    depreciation has been booked (it would corrupt the schedule — reverse via disposal instead)
    or the asset is disposed/written-off, and in a locked period. Audited."""
    from . import audit, system_settings
    from ..models import Account
    asset = _get(db, asset_id)
    if asset.status != "active":
        raise AssetError(f"Cannot edit a {asset.status} asset.")
    if Decimal(asset.accumulated_depreciation) > 0 or any(t.type == "depreciation" for t in asset.transactions):
        raise AssetError("Depreciation has been booked — edit is blocked. Reverse depreciation/dispose first.")
    if data.vendor_id and not db.get(Vendor, data.vendor_id):
        raise AssetError("Vendor not found.")
    system_settings.assert_period_open(db, asset.purchase_date, "edit")
    system_settings.assert_period_open(db, data.purchase_date, "edit")

    before = {f: str(getattr(asset, f, None)) for f in _ASSET_AUDIT_FIELDS}
    acq = next((t for t in asset.transactions if t.type == "acquisition"), None)
    # Preserve the original credit account if the acquisition was posted, else default.
    prev_credit_code = None
    if acq and acq.journal_entry_id:
        je = db.get(JournalEntry, acq.journal_entry_id)
        if je:
            cr = next((ln for ln in je.lines if Decimal(ln.credit) > 0), None)
            if cr:
                ca = db.get(Account, cr.account_id)
                prev_credit_code = ca.code if ca else None
        ledger.void_entry(db, acq.journal_entry_id)   # reverse original acquisition GL

    asset.name = data.name
    asset.category = data.category
    asset.description = data.description
    asset.purchase_date = data.purchase_date
    asset.in_service_date = data.in_service_date or data.purchase_date
    asset.supplier = data.supplier
    asset.vendor_id = data.vendor_id
    asset.invoice_number = data.invoice_number
    asset.bill_id = data.bill_id
    asset.purchase_cost = q(data.purchase_cost)
    asset.vat_amount = q(data.vat_amount)
    asset.currency = data.currency
    asset.residual_value = q(data.residual_value)
    asset.method = data.method
    asset.useful_life_months = data.useful_life_months
    asset.declining_rate = data.declining_rate
    asset.depreciation_start = data.depreciation_start or data.in_service_date or data.purchase_date
    asset.location = data.location
    asset.department = data.department
    asset.project = data.project
    asset.cost_center = data.cost_center
    asset.responsible_person = data.responsible_person
    asset.serial_number = data.serial_number
    asset.warranty_info = data.warranty_info

    new_je = None
    repost = data.auto_post_acquisition or (acq and acq.journal_entry_id)
    if repost:
        fa = _account_by_code(db, C.CODE_FIXED_ASSETS)
        credit = _account_by_code(db, data.acquisition_credit_code or prev_credit_code or "1020")
        entry = ledger.create_journal_entry(db, JournalEntryIn(
            date=asset.purchase_date, memo=f"Acquisition of {asset.asset_code} {asset.name} (edited)",
            reference=asset.asset_code, source="assets", currency=asset.currency,
            lines=[JournalLineIn(account_id=fa.id, debit=q(data.purchase_cost)),
                   JournalLineIn(account_id=credit.id, credit=q(data.purchase_cost))], auto_post=True))
        new_je = entry.id
    if acq:
        acq.date = asset.purchase_date
        acq.amount = q(data.purchase_cost)
        acq.journal_entry_id = new_je
        acq.detail = f"Acquired{' (posted)' if new_je else ''} — edited"
    after = {f: str(getattr(asset, f, None)) for f in _ASSET_AUDIT_FIELDS}
    audit.record_txn_audit(db, entity_type="fixed_asset", entity_id=asset.id, doc_number=asset.asset_code,
                           actor=actor, action="edit", reason=reason, prev_status="active",
                           new_status="active", changes=audit.diff(before, after))
    db.commit()
    db.refresh(asset)
    return _asset_out(asset, db)


def asset_audit(db: Session, asset_id: str) -> list[dict]:
    from . import audit
    return audit.list_txn_audit(db, "fixed_asset", asset_id)


def run_depreciation(db: Session, as_of: date, asset_id: str | None = None) -> dict:
    """Post monthly depreciation for every active asset up to `as_of` (idempotent — periods
    already booked are skipped). Returns a summary."""
    dep_exp = _account_by_code(db, C.CODE_DEP_EXPENSE)
    accum = _account_by_code(db, C.CODE_ACCUM_DEP)
    stmt = select(FixedAsset).where(FixedAsset.status == "active").options(selectinload(FixedAsset.transactions))
    if asset_id:
        stmt = stmt.where(FixedAsset.id == asset_id)
    posted_count = 0
    total_amount = ZERO
    assets_touched = 0
    for asset in db.execute(stmt).scalars():
        last = asset.last_depreciation_date
        touched = False
        for (period_end, dep, _acc, _nbv) in _schedule(asset):
            if period_end > as_of:
                break
            if last is not None and period_end <= last:
                continue
            entry = ledger.create_journal_entry(
                db,
                JournalEntryIn(
                    date=period_end, memo=f"Depreciation {asset.asset_code} {period_end:%Y-%m}",
                    reference=asset.asset_code, source="assets", currency=asset.currency,
                    lines=[
                        JournalLineIn(account_id=dep_exp.id, debit=dep),
                        JournalLineIn(account_id=accum.id, credit=dep),
                    ],
                    auto_post=True,
                ),
            )
            db.add(AssetTransaction(
                asset_id=asset.id, date=period_end, type="depreciation", amount=dep,
                detail=f"Depreciation for {period_end:%Y-%m}", journal_entry_id=entry.id,
            ))
            asset.accumulated_depreciation = q(Decimal(asset.accumulated_depreciation) + dep)
            asset.last_depreciation_date = period_end
            posted_count += 1
            total_amount += dep
            touched = True
        if touched:
            assets_touched += 1
    db.commit()
    return {"periods_posted": posted_count, "assets": assets_touched, "total_depreciation": str(q(total_amount))}


def dispose_asset(db: Session, asset_id: str, data: DisposalIn) -> AssetOut:
    asset = _get(db, asset_id)
    if asset.status != "active":
        raise AssetError(f"Asset is not active (status '{asset.status}').")
    cost = Decimal(asset.purchase_cost)
    accum = Decimal(asset.accumulated_depreciation)
    nbv = q(cost - accum)
    proceeds = q(data.proceeds)
    gain = q(proceeds - nbv)  # positive = gain, negative = loss

    fa = _account_by_code(db, C.CODE_FIXED_ASSETS)
    accum_acct = _account_by_code(db, C.CODE_ACCUM_DEP)
    lines = [JournalLineIn(account_id=fa.id, credit=cost)]
    if accum > 0:
        lines.append(JournalLineIn(account_id=accum_acct.id, debit=accum))
    if proceeds > 0:
        proceeds_acct = _account_by_code(db, data.proceeds_account_code)
        lines.append(JournalLineIn(account_id=proceeds_acct.id, debit=proceeds))
    if gain > 0:
        lines.append(JournalLineIn(account_id=_account_by_code(db, C.CODE_GAIN_DISPOSAL).id, credit=gain))
    elif gain < 0:
        lines.append(JournalLineIn(account_id=_account_by_code(db, C.CODE_LOSS_DISPOSAL).id, debit=q(-gain)))

    entry = ledger.create_journal_entry(
        db,
        JournalEntryIn(
            date=data.disposal_date, memo=f"Disposal of {asset.asset_code} {asset.name}",
            reference=asset.asset_code, source="assets", currency=asset.currency, lines=lines, auto_post=True,
        ),
    )
    status_map = {"disposal": "disposed", "write_off": "written_off", "retirement": "retired"}
    asset.status = status_map.get(data.method, "disposed")
    asset.disposal_date = data.disposal_date
    asset.disposal_proceeds = proceeds
    asset.disposal_gain_loss = gain
    db.add(AssetTransaction(
        asset_id=asset.id, date=data.disposal_date, type=data.method, amount=proceeds,
        detail=(data.detail or "") + f" NBV {nbv}, {'gain' if gain >= 0 else 'loss'} {abs(gain)}",
        journal_entry_id=entry.id,
    ))
    db.commit()
    db.refresh(asset)
    return _asset_out(asset, db)


def list_assets(db: Session, status: str | None = None, category: str | None = None) -> list[AssetOut]:
    stmt = select(FixedAsset).order_by(FixedAsset.asset_code).options(selectinload(FixedAsset.transactions))
    if status:
        stmt = stmt.where(FixedAsset.status == status)
    if category:
        stmt = stmt.where(FixedAsset.category == category)
    return [_asset_out(a, db) for a in db.execute(stmt).scalars()]


def get_asset(db: Session, asset_id: str) -> AssetOut:
    return _asset_out(_get(db, asset_id), db)
