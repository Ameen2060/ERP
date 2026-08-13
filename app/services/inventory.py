"""Inventory service: products, warehouses, stock movements with FIFO / weighted-average
cost valuation, and perpetual GL posting.

Posting rules (source='inventory'):
  Receipt (stock in)     Dr Inventory (1200) / Cr credit account (default AP 2010)
  Opening (stock in)     Dr Inventory (1200) / Cr Capital (3010)
  Issue (stock out)      Dr Cost of Goods Sold (5060) / Cr Inventory (1200)   [cost from FIFO/WAC]
  Adjustment +           Dr Inventory (1200) / Cr Inventory Adjustment (5080)
  Adjustment −           Dr Inventory Adjustment (5080) / Cr Inventory (1200)

The GL Inventory account balance therefore always equals the total stock valuation.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import constants as C
from ..models import Account, JournalEntry, JournalLine, Product, StockMovement, Warehouse
from ..schemas import (
    InventoryValuation,
    JournalEntryIn,
    JournalLineIn,
    LowStockRow,
    MovementIn,
    MovementOut,
    ProductIn,
    ProductOut,
    ValuationRow,
    WarehouseIn,
    WarehouseOut,
    q,
)
from . import ledger

ZERO = Decimal(0)
Q4 = Decimal("0.0001")


class InventoryError(ValueError):
    """Domain error → HTTP 400."""


def _account_by_code(db: Session, code: str) -> Account:
    a = db.execute(select(Account).where(Account.code == code)).scalar_one_or_none()
    if not a:
        raise InventoryError(f"Required account '{code}' is missing — seed the Chart of Accounts first.")
    return a


# ── Warehouses ──────────────────────────────────────────────────────────────────────────
def _next_wh_code(db: Session) -> str:
    n = (db.execute(select(func.count(Warehouse.id))).scalar() or 0) + 1
    return f"WH-{n:02d}"


def create_warehouse(db: Session, data: WarehouseIn) -> WarehouseOut:
    code = data.code or _next_wh_code(db)
    if db.execute(select(Warehouse).where(Warehouse.code == code)).scalar_one_or_none():
        raise InventoryError(f"Warehouse code '{code}' already exists.")
    wh = Warehouse(code=code, name=data.name, location=data.location)
    db.add(wh)
    db.commit()
    db.refresh(wh)
    return WarehouseOut(id=wh.id, code=wh.code, name=wh.name, location=wh.location, is_active=wh.is_active)


def list_warehouses(db: Session) -> list[WarehouseOut]:
    return [
        WarehouseOut(id=w.id, code=w.code, name=w.name, location=w.location, is_active=w.is_active)
        for w in db.execute(select(Warehouse).order_by(Warehouse.code)).scalars()
    ]


# ── Products ────────────────────────────────────────────────────────────────────────────
def _next_sku(db: Session) -> str:
    n = (db.execute(select(func.count(Product.id))).scalar() or 0) + 1
    return f"SKU-{n:04d}"


def _product_totals(db: Session, product_id: str, warehouse_id: str | None = None) -> tuple[Decimal, Decimal]:
    """(on_hand qty, stock value) summed across movements — value is Σ signed total_cost."""
    qty_stmt = select(func.coalesce(func.sum(StockMovement.quantity), 0)).where(StockMovement.product_id == product_id)
    val_stmt = select(func.coalesce(func.sum(StockMovement.total_cost), 0)).where(StockMovement.product_id == product_id)
    if warehouse_id:
        qty_stmt = qty_stmt.where(StockMovement.warehouse_id == warehouse_id)
        val_stmt = val_stmt.where(StockMovement.warehouse_id == warehouse_id)
    on_hand = Decimal(db.execute(qty_stmt).scalar() or 0)
    value = q(Decimal(db.execute(val_stmt).scalar() or 0))
    return on_hand, value


def _product_out(db: Session, p: Product) -> ProductOut:
    on_hand, value = _product_totals(db, p.id)
    avg = q(value / on_hand) if on_hand > 0 else ZERO
    return ProductOut(
        id=p.id, sku=p.sku, name=p.name, type=p.type, category=p.category, unit=p.unit,
        cost_method=p.cost_method, sales_price=Decimal(p.sales_price), purchase_cost=Decimal(p.purchase_cost),
        reorder_level=Decimal(p.reorder_level), track_inventory=p.track_inventory, is_active=p.is_active,
        on_hand=on_hand.quantize(Q4), stock_value=value, avg_cost=avg,
    )


def create_product(db: Session, data: ProductIn) -> ProductOut:
    sku = data.sku or _next_sku(db)
    if db.execute(select(Product).where(Product.sku == sku)).scalar_one_or_none():
        raise InventoryError(f"SKU '{sku}' already exists.")
    if data.cost_method not in ("weighted_average", "fifo"):
        raise InventoryError("cost_method must be 'weighted_average' or 'fifo'.")
    p = Product(
        sku=sku, name=data.name, type=data.type, category=data.category, unit=data.unit,
        cost_method=data.cost_method, sales_price=q(data.sales_price), purchase_cost=q(data.purchase_cost),
        reorder_level=Decimal(data.reorder_level), track_inventory=data.track_inventory and data.type == "product",
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return _product_out(db, p)


def list_products(db: Session, active_only: bool = True) -> list[ProductOut]:
    stmt = select(Product).order_by(Product.sku)
    if active_only:
        stmt = stmt.where(Product.is_active.is_(True))
    return [_product_out(db, p) for p in db.execute(stmt).scalars()]


def get_product(db: Session, product_id: str) -> ProductOut:
    p = db.get(Product, product_id)
    if not p:
        raise InventoryError("Product not found.")
    return _product_out(db, p)


_PRODUCT_FIELDS = ("sku", "name", "category", "unit", "sales_price", "purchase_cost",
                   "reorder_level", "track_inventory", "is_active")


def update_product(db: Session, product_id: str, data: ProductIn, actor: str | None = None) -> ProductOut:
    """Edit a product's descriptive/pricing fields. The costing method is LOCKED once movements
    exist (changing FIFO↔WAC would retroactively revalue history). Field-level audit written."""
    from . import audit
    p = db.get(Product, product_id)
    if not p:
        raise InventoryError("Product not found.")
    new_sku = (data.sku or p.sku).strip()
    if new_sku != p.sku and db.execute(select(Product).where(
            Product.sku == new_sku, Product.id != product_id)).scalar_one_or_none():
        raise InventoryError(f"SKU '{new_sku}' already exists.")
    if data.cost_method and data.cost_method != p.cost_method:
        has_moves = db.execute(select(StockMovement).where(StockMovement.product_id == product_id)).first()
        if has_moves:
            raise InventoryError("Costing method cannot change after stock movements exist.")
        if data.cost_method not in ("weighted_average", "fifo"):
            raise InventoryError("cost_method must be 'weighted_average' or 'fifo'.")
        p.cost_method = data.cost_method
    before = {f: getattr(p, f, None) for f in _PRODUCT_FIELDS}
    p.sku = new_sku
    p.name = data.name
    p.category = data.category
    p.unit = data.unit
    p.sales_price = q(data.sales_price)
    p.purchase_cost = q(data.purchase_cost)
    p.reorder_level = Decimal(data.reorder_level)
    p.track_inventory = data.track_inventory and p.type == "product"
    p.is_active = data.is_active
    changes = audit.diff(before, {f: getattr(p, f, None) for f in _PRODUCT_FIELDS})
    audit.record_profile_change(db, entity_type="product", entity_id=p.id, entity_label=p.name,
                                actor=actor, changes=changes)
    db.commit()
    db.refresh(p)
    return _product_out(db, p)


def product_audit(db: Session, product_id: str) -> list[dict]:
    from . import audit
    return audit.list_profile_audit(db, "product", product_id)


# ── Valuation engine ────────────────────────────────────────────────────────────────────
def _ordered_movements(db: Session, product_id: str, warehouse_id: str) -> list[StockMovement]:
    return list(db.execute(
        select(StockMovement).where(
            StockMovement.product_id == product_id, StockMovement.warehouse_id == warehouse_id
        ).order_by(StockMovement.date, StockMovement.created_at)
    ).scalars())


def _current_state(db: Session, product_id: str, warehouse_id: str, method: str):
    """Replay movements for one product+warehouse. Returns (qty, value, layers) where layers is
    a FIFO queue of [remaining_qty, unit_cost] (used only for FIFO issue costing)."""
    qty = ZERO
    value = ZERO
    layers: list[list[Decimal]] = []
    for m in _ordered_movements(db, product_id, warehouse_id):
        if m.quantity > 0:
            qty += m.quantity
            value += m.total_cost
            layers.append([Decimal(m.quantity), Decimal(m.unit_cost)])
        elif m.quantity < 0:
            out_q = -Decimal(m.quantity)
            if method == "fifo":
                _consume_fifo(layers, out_q)
            qty += m.quantity
            value += m.total_cost  # negative
    return qty, q(value), layers


def _consume_fifo(layers: list[list[Decimal]], out_q: Decimal) -> Decimal:
    """Consume out_q from the oldest layers, mutating them. Returns total cost consumed."""
    remaining = out_q
    cost = ZERO
    while remaining > 0 and layers:
        layer = layers[0]
        take = min(layer[0], remaining)
        cost += take * layer[1]
        layer[0] -= take
        remaining -= take
        if layer[0] <= 0:
            layers.pop(0)
    return cost


def _issue_cost(db: Session, product: Product, warehouse_id: str, out_q: Decimal) -> Decimal:
    qty, value, layers = _current_state(db, product.id, warehouse_id, product.cost_method)
    if out_q > qty:
        raise InventoryError(
            f"Cannot issue {out_q} of {product.sku} — only {qty.quantize(Q4)} on hand in this warehouse."
        )
    if product.cost_method == "fifo":
        return q(_consume_fifo(layers, out_q))
    avg = (value / qty) if qty > 0 else ZERO
    return q(avg * out_q)


# ── Movements ───────────────────────────────────────────────────────────────────────────
def record_movement(db: Session, data: MovementIn) -> MovementOut:
    product = db.get(Product, data.product_id)
    if not product:
        raise InventoryError("Product not found.")
    if not product.track_inventory:
        raise InventoryError(f"{product.sku} is a service / non-inventory item and has no stock.")
    wh = db.get(Warehouse, data.warehouse_id)
    if not wh:
        raise InventoryError("Warehouse not found.")
    mtype = data.movement_type
    if mtype not in ("receipt", "issue", "adjustment", "opening"):
        raise InventoryError("movement_type must be receipt, issue, adjustment or opening.")

    inv = _account_by_code(db, C.CODE_INVENTORY)
    inflow = mtype in ("receipt", "opening") or (mtype == "adjustment" and data.increase)

    if inflow:
        unit_cost = data.unit_cost if data.unit_cost is not None else Decimal(product.purchase_cost)
        signed_qty = Decimal(data.quantity)
        total = q(signed_qty * unit_cost)
        if mtype == "receipt":
            credit = _account_by_code(db, data.credit_account_code or C.CODE_AP)
        elif mtype == "opening":
            credit = _account_by_code(db, data.credit_account_code or "3010")
        else:  # positive adjustment
            credit = _account_by_code(db, C.CODE_INV_ADJ)
        je_lines = [JournalLineIn(account_id=inv.id, debit=total),
                    JournalLineIn(account_id=credit.id, credit=total)]
        unit_cost_stored = q(unit_cost)
        total_stored = total
        qty_stored = signed_qty
    else:
        out_q = Decimal(data.quantity)
        cost = _issue_cost(db, product, wh.id, out_q)
        signed_qty = -out_q
        total_stored = q(-cost)
        unit_cost_stored = q(cost / out_q) if out_q > 0 else ZERO
        if mtype == "issue":
            debit = _account_by_code(db, C.CODE_COGS)
        else:  # negative adjustment
            debit = _account_by_code(db, C.CODE_INV_ADJ)
        je_lines = [JournalLineIn(account_id=debit.id, debit=cost),
                    JournalLineIn(account_id=inv.id, credit=cost)]
        qty_stored = signed_qty

    entry = ledger.create_journal_entry(
        db,
        JournalEntryIn(
            date=data.date, memo=f"Inventory {mtype}: {product.sku} {product.name}",
            reference=data.reference, source="inventory", currency="AED",
            lines=je_lines, auto_post=True,
        ),
    )
    mv = StockMovement(
        product_id=product.id, warehouse_id=wh.id, date=data.date, movement_type=mtype,
        quantity=qty_stored, unit_cost=unit_cost_stored, total_cost=total_stored,
        reference=data.reference, source="inventory", batch_number=data.batch_number,
        serial_number=data.serial_number, journal_entry_id=entry.id,
    )
    db.add(mv)
    db.commit()
    db.refresh(mv)
    return _movement_out(db, mv)


def _movement_out(db: Session, mv: StockMovement) -> MovementOut:
    p = db.get(Product, mv.product_id)
    w = db.get(Warehouse, mv.warehouse_id)
    return MovementOut(
        id=mv.id, product_id=mv.product_id, product_name=p.name if p else None,
        warehouse_id=mv.warehouse_id, warehouse_name=w.name if w else None, date=mv.date,
        movement_type=mv.movement_type, quantity=Decimal(mv.quantity).quantize(Q4),
        unit_cost=q(mv.unit_cost), total_cost=q(mv.total_cost), reference=mv.reference,
        source=mv.source, batch_number=mv.batch_number, serial_number=mv.serial_number,
        journal_entry_id=mv.journal_entry_id, created_at=mv.created_at.isoformat() if mv.created_at else None,
    )


def list_movements(db: Session, product_id: str | None = None, warehouse_id: str | None = None) -> list[MovementOut]:
    stmt = select(StockMovement).order_by(StockMovement.date.desc(), StockMovement.created_at.desc()).limit(500)
    if product_id:
        stmt = stmt.where(StockMovement.product_id == product_id)
    if warehouse_id:
        stmt = stmt.where(StockMovement.warehouse_id == warehouse_id)
    return [_movement_out(db, m) for m in db.execute(stmt).scalars()]


# ── Reports ─────────────────────────────────────────────────────────────────────────────
def valuation(db: Session) -> InventoryValuation:
    from datetime import datetime, timezone
    rows: list[ValuationRow] = []
    total = ZERO
    for p in db.execute(select(Product).where(Product.track_inventory.is_(True)).order_by(Product.sku)).scalars():
        on_hand, value = _product_totals(db, p.id)
        if on_hand == 0 and value == 0:
            continue
        avg = q(value / on_hand) if on_hand > 0 else ZERO
        total += value
        rows.append(ValuationRow(
            product_id=p.id, sku=p.sku, name=p.name, category=p.category, cost_method=p.cost_method,
            on_hand=on_hand.quantize(Q4), avg_cost=avg, stock_value=value,
        ))
    # GL Inventory balance (Dr − Cr on 1200) for a control-account cross-check.
    inv = db.execute(select(Account).where(Account.code == C.CODE_INVENTORY)).scalar_one_or_none()
    gl_bal = ZERO
    if inv:
        gl_bal = Decimal(db.execute(
            select(func.coalesce(func.sum(JournalLine.debit - JournalLine.credit), 0))
            .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
            .where(JournalEntry.status == C.ENTRY_POSTED, JournalLine.account_id == inv.id)
        ).scalar() or 0)
    return InventoryValuation(
        generated_at=datetime.now(timezone.utc).isoformat(), rows=rows, total_value=q(total),
        gl_inventory_balance=q(gl_bal), in_sync=q(total) == q(gl_bal),
    )


def low_stock(db: Session) -> list[LowStockRow]:
    rows: list[LowStockRow] = []
    for p in db.execute(select(Product).where(Product.track_inventory.is_(True), Product.is_active.is_(True))).scalars():
        if Decimal(p.reorder_level) <= 0:
            continue
        on_hand, _ = _product_totals(db, p.id)
        if on_hand <= Decimal(p.reorder_level):
            rows.append(LowStockRow(
                product_id=p.id, sku=p.sku, name=p.name, on_hand=on_hand.quantize(Q4),
                reorder_level=Decimal(p.reorder_level).quantize(Q4),
                shortfall=(Decimal(p.reorder_level) - on_hand).quantize(Q4),
            ))
    return rows
