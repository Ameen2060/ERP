"""Centralized document calculation engine.

Every money document (invoice, bill, expense, credit note, ...) flows through the same
breakdown so the arithmetic is defined in exactly one place:

    subtotal → discount → taxable → VAT → gross → retention → advance recovery → net amount due

All inputs/outputs are Decimals quantised to 2 dp. Percentages are whole numbers (10 = 10%).
"""

from __future__ import annotations

from decimal import Decimal

ZERO = Decimal("0.00")
_CENTS = Decimal("0.01")


def q(x) -> Decimal:
    return Decimal(str(x)).quantize(_CENTS)


def _dec(x) -> Decimal:
    return Decimal(str(x or 0))


def document_summary(
    *,
    subtotal,
    discount=0,
    vat_rate=None,
    vat_amount=None,
    retention_basis: str = "none",   # none | net | gross | amount
    retention_percent=0,
    retention_amount=0,
    advance_recovery=0,
) -> dict:
    """Return the full breakdown for one document. `vat_amount` overrides `vat_rate` when given.
    Retention basis 'net' → on taxable, 'gross' → on taxable+VAT, 'amount' → use retention_amount."""
    subtotal = _dec(subtotal)
    discount = _dec(discount)
    taxable = q(subtotal - discount)

    if vat_amount is not None:
        vat = q(vat_amount)
    else:
        vat = q(taxable * _dec(vat_rate))
    gross = q(taxable + vat)

    basis = (retention_basis or "none").lower()
    if basis == "amount":
        retention = q(retention_amount)
    elif basis == "net":
        retention = q(taxable * _dec(retention_percent) / Decimal(100))
    elif basis == "gross":
        retention = q(gross * _dec(retention_percent) / Decimal(100))
    else:
        retention = ZERO

    advance = q(advance_recovery)
    net_due = q(gross - retention - advance)
    return {
        "subtotal": q(subtotal), "discount": q(discount), "taxable": taxable,
        "vat": vat, "gross": gross, "retention": retention,
        "advance_recovery": advance, "net_due": net_due,
    }
