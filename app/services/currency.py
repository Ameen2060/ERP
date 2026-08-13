"""Multi-currency: currencies, exchange rates, and conversion to the base currency (AED).

Rate convention: `rate` = base-currency units per 1 unit of the foreign currency
(e.g. USD→AED 3.6725). The general ledger is always kept in the base currency; documents
remember their own currency and the rate used, so realized FX differences can be booked.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import constants as C
from ..models import Currency, ExchangeRate
from ..schemas import CurrencyIn, CurrencyOut, ExchangeRateIn, ExchangeRateOut

ONE = Decimal(1)


class CurrencyError(ValueError):
    """Domain error → HTTP 400."""


def ensure_base(db: Session) -> None:
    """Seed the base currency (AED) once."""
    if not db.execute(select(Currency).where(Currency.code == C.BASE_CURRENCY)).scalar_one_or_none():
        db.add(Currency(code=C.BASE_CURRENCY, name="UAE Dirham", symbol="AED", is_base=True))
        db.commit()


def create_currency(db: Session, data: CurrencyIn) -> CurrencyOut:
    code = data.code.upper()
    if db.execute(select(Currency).where(Currency.code == code)).scalar_one_or_none():
        raise CurrencyError(f"Currency '{code}' already exists.")
    cur = Currency(code=code, name=data.name, symbol=data.symbol, is_base=(code == C.BASE_CURRENCY))
    db.add(cur)
    db.commit()
    db.refresh(cur)
    return _currency_out(db, cur)


def _latest_rate(db: Session, code: str, as_of: date | None = None) -> Decimal | None:
    if code == C.BASE_CURRENCY:
        return ONE
    stmt = select(ExchangeRate).where(ExchangeRate.currency_code == code)
    if as_of is not None:
        stmt = stmt.where(ExchangeRate.date <= as_of)
    stmt = stmt.order_by(ExchangeRate.date.desc())
    row = db.execute(stmt).scalars().first()
    return Decimal(row.rate) if row else None


def _currency_out(db: Session, cur: Currency) -> CurrencyOut:
    return CurrencyOut(
        id=cur.id, code=cur.code, name=cur.name, symbol=cur.symbol, is_base=cur.is_base,
        is_active=cur.is_active, latest_rate=_latest_rate(db, cur.code),
    )


def list_currencies(db: Session) -> list[CurrencyOut]:
    return [_currency_out(db, c) for c in db.execute(select(Currency).order_by(Currency.code)).scalars()]


def set_rate(db: Session, data: ExchangeRateIn) -> ExchangeRateOut:
    code = data.currency_code.upper()
    if code == C.BASE_CURRENCY:
        raise CurrencyError("The base currency (AED) always has rate 1 — no rate needed.")
    if not db.execute(select(Currency).where(Currency.code == code)).scalar_one_or_none():
        raise CurrencyError(f"Currency '{code}' does not exist — add it first.")
    # One rate per currency+date (update if present).
    existing = db.execute(
        select(ExchangeRate).where(ExchangeRate.currency_code == code, ExchangeRate.date == data.date)
    ).scalar_one_or_none()
    if existing:
        existing.rate = data.rate
        rate = existing
    else:
        rate = ExchangeRate(currency_code=code, date=data.date, rate=data.rate)
        db.add(rate)
    db.commit()
    db.refresh(rate)
    return ExchangeRateOut(id=rate.id, currency_code=rate.currency_code, date=rate.date, rate=Decimal(rate.rate))


def list_rates(db: Session, code: str | None = None) -> list[ExchangeRateOut]:
    stmt = select(ExchangeRate).order_by(ExchangeRate.currency_code, ExchangeRate.date.desc())
    if code:
        stmt = stmt.where(ExchangeRate.currency_code == code.upper())
    return [ExchangeRateOut(id=r.id, currency_code=r.currency_code, date=r.date, rate=Decimal(r.rate))
            for r in db.execute(stmt).scalars()]


def rate_for(db: Session, code: str, as_of: date | None = None) -> Decimal:
    """Rate to base for `code` on/just-before `as_of`. Raises if no rate is known."""
    code = (code or C.BASE_CURRENCY).upper()
    r = _latest_rate(db, code, as_of)
    if r is None:
        raise CurrencyError(f"No exchange rate available for {code} on or before {as_of or 'today'}.")
    return r


def resolve_rate(db: Session, code: str, supplied: Decimal | None, as_of: date | None) -> Decimal:
    """Use the supplied rate if given, else look it up. Base currency is always 1."""
    code = (code or C.BASE_CURRENCY).upper()
    if code == C.BASE_CURRENCY:
        return ONE
    if supplied is not None and supplied > 0:
        return Decimal(supplied)
    return rate_for(db, code, as_of)
