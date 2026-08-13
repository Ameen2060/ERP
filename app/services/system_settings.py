"""System settings (singleton): document number formats, default posting accounts, rounding
precision and default VAT rate. Resolved dynamically so nothing is hard-coded in the UI."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import constants as C
from ..models import Account, SystemSettings

_ID = "system"
_ACCT_KEYS = ["default_sales_code", "default_ar_code", "default_ap_code", "default_input_vat_code",
              "default_output_vat_code", "default_bank_code", "default_cash_code"]


class SettingsError(ValueError):
    """Domain error → HTTP 400."""


def get_settings_row(db: Session) -> SystemSettings:
    s = db.get(SystemSettings, _ID)
    if not s:
        s = SystemSettings(id=_ID)
        db.add(s)
        db.commit()
        db.refresh(s)
    return s


def profile(db: Session) -> dict:
    s = get_settings_row(db)
    return {
        "invoice_number_format": s.invoice_number_format,
        "credit_note_number_format": s.credit_note_number_format,
        "bill_number_format": s.bill_number_format,
        "payment_number_format": s.payment_number_format,
        "default_sales_code": s.default_sales_code, "default_ar_code": s.default_ar_code,
        "default_ap_code": s.default_ap_code, "default_input_vat_code": s.default_input_vat_code,
        "default_output_vat_code": s.default_output_vat_code, "default_bank_code": s.default_bank_code,
        "default_cash_code": s.default_cash_code,
        "decimal_places": s.decimal_places, "rounding_mode": s.rounding_mode,
        "default_vat_rate": str(s.default_vat_rate),
        "advance_vat_status": s.advance_vat_status,
        "advance_vat_label": ADVANCE_VAT_LABEL.get(s.advance_vat_status, s.advance_vat_status),
        "advance_vat_filing_ready": s.advance_vat_status == "enabled",
        "advance_vat_approved_by": s.advance_vat_approved_by,
        "advance_vat_approved_at": s.advance_vat_approved_at.isoformat() if s.advance_vat_approved_at else None,
        "books_locked_before": s.books_locked_before.isoformat() if s.books_locked_before else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


# ── Period lock ─────────────────────────────────────────────────────────────────────────
def period_locked(db: Session, txn_date) -> bool:
    """True when a transaction dated on/before the lock date falls in a closed period."""
    lock = get_settings_row(db).books_locked_before
    return bool(lock and txn_date and txn_date <= lock)


def assert_period_open(db: Session, txn_date, action: str = "edit") -> None:
    if period_locked(db, txn_date):
        lock = get_settings_row(db).books_locked_before
        raise SettingsError(
            f"This transaction falls in a locked accounting period (books closed on/before "
            f"{lock.isoformat()}). Direct {action} is not allowed — post a reversal/adjustment "
            f"in the current open period instead.")


def set_period_lock(db: Session, lock_date) -> dict:
    s = get_settings_row(db)
    s.books_locked_before = lock_date
    db.commit()
    return profile(db)


ADVANCE_VAT_LABEL = {
    "off": "VAT on Advances — OFF (disabled)",
    "sme_review": "VAT on Advances — Requires UAE VAT SME Validation Before Filing",
    "approved": "VAT on Advances — SME approved (not yet enabled)",
    "enabled": "VAT on Advances — Enabled (SME-approved, filing-ready)",
}
_ADV_FLOW = {"request_review": ("off", "sme_review"), "approve": ("sme_review", "approved"),
             "enable": ("approved", "enabled")}


def advance_vat_allowed(db: Session) -> bool:
    """Whether VAT-on-advance data entry is permitted at all (feature not fully off)."""
    return get_settings_row(db).advance_vat_status != "off"


def advance_vat_filing_ready(db: Session) -> bool:
    """Whether the SME has approved+enabled the rule for filing-ready VAT returns."""
    return get_settings_row(db).advance_vat_status == "enabled"


def transition_advance_vat(db: Session, action: str, actor: str = "local") -> dict:
    from datetime import datetime, timezone
    s = get_settings_row(db)
    if action == "disable":
        s.advance_vat_status = "off"
        s.advance_vat_approved_by = s.advance_vat_approved_at = None
        db.commit()
        return profile(db)
    if action not in _ADV_FLOW:
        raise SettingsError("Unknown action.")
    frm, to = _ADV_FLOW[action]
    if s.advance_vat_status != frm:
        raise SettingsError(f"Cannot {action}: status is '{s.advance_vat_status}' (must be '{frm}').")
    s.advance_vat_status = to
    if to == "approved":
        s.advance_vat_approved_by = actor
        s.advance_vat_approved_at = datetime.now(timezone.utc)
    db.commit()
    return profile(db)


def _valid_code(db: Session, code: str | None) -> None:
    if code and not db.execute(select(Account).where(Account.code == code)).scalars().first():
        raise SettingsError(f"Account code '{code}' does not exist in the Chart of Accounts.")


def update(db: Session, data) -> dict:
    s = get_settings_row(db)
    for k in _ACCT_KEYS:
        _valid_code(db, getattr(data, k, None))
    if not (0 <= int(data.decimal_places) <= 4):
        raise SettingsError("Decimal places must be between 0 and 4.")
    if not (Decimal(0) <= Decimal(str(data.default_vat_rate)) <= Decimal(1)):
        raise SettingsError("Default VAT rate must be between 0 and 1 (e.g. 0.05).")
    s.invoice_number_format = data.invoice_number_format or s.invoice_number_format
    s.credit_note_number_format = data.credit_note_number_format or s.credit_note_number_format
    s.bill_number_format = data.bill_number_format or s.bill_number_format
    s.payment_number_format = data.payment_number_format or s.payment_number_format
    for k in _ACCT_KEYS:
        setattr(s, k, getattr(data, k, None) or None)
    s.decimal_places = int(data.decimal_places)
    s.rounding_mode = data.rounding_mode or "half_up"
    s.default_vat_rate = Decimal(str(data.default_vat_rate))
    db.commit()
    return profile(db)


# ── Resolvers used by the posting engine (configurable defaults, fall back to constants) ──────
def _account_id_for(db: Session, code: str | None, fallback_code: str) -> str:
    if code:
        a = db.execute(select(Account).where(Account.code == code)).scalars().first()
        if a and not a.is_group:
            return a.id
    a = db.execute(select(Account).where(Account.code == fallback_code)).scalars().first()
    return a.id if a else None


def default_sales_account_id(db: Session) -> str:
    return _account_id_for(db, get_settings_row(db).default_sales_code, C.CODE_SALES)


def default_vat_rate(db: Session) -> Decimal:
    return Decimal(get_settings_row(db).default_vat_rate)
