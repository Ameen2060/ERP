"""Organization profile + VAT configuration (singleton). Retrieved dynamically everywhere —
never hard-code company info. Updating it changes all future reports, documents and PDFs."""

from __future__ import annotations

import os
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Organization

_ORG_ID = "org"
_LOGO_EXT = {"png", "jpg", "jpeg", "svg", "webp", "gif"}
_LOGO_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
              "svg": "image/svg+xml", "webp": "image/webp", "gif": "image/gif"}
_FREQ = {"monthly", "quarterly", "na"}


class OrganizationError(ValueError):
    """Domain error → HTTP 400."""


def get_org(db: Session) -> Organization:
    org = db.get(Organization, _ORG_ID)
    if not org:
        org = Organization(id=_ORG_ID, name="My Company", vat_registered=True,
                           vat_return_frequency="quarterly", base_currency="AED")
        db.add(org)
        db.commit()
        db.refresh(org)
    return org


def profile(db: Session) -> dict:
    o = get_org(db)
    return {
        "name": o.name, "legal_name": o.legal_name, "address": o.address, "trn": o.trn,
        "phone": o.phone, "email": o.email, "website": o.website,
        "financial_year_start": str(o.financial_year_start) if o.financial_year_start else None,
        "financial_year_end": str(o.financial_year_end) if o.financial_year_end else None,
        "base_currency": o.base_currency, "vat_registered": o.vat_registered,
        "vat_return_frequency": o.vat_return_frequency,
        "trade_license": o.trade_license, "country": o.country or "AE",
        "einvoice_scheme": o.einvoice_scheme, "einvoice_id": o.einvoice_id,
        "bank_name": o.bank_name, "bank_iban": o.bank_iban,
        "has_logo": bool(o.logo_path and os.path.exists(o.logo_path)),
        "logo_filename": o.logo_filename, "logo_mime": o.logo_mime,
        "updated_at": o.updated_at.isoformat() if o.updated_at else None,
    }


def _validate_trn(trn: str | None) -> str | None:
    if trn is None or str(trn).strip() == "":
        return None
    t = str(trn).strip()
    if not re.fullmatch(r"\d{15}", t):
        raise OrganizationError("TRN must be a 15-digit UAE Tax Registration Number.")
    return t


def update(db: Session, data) -> dict:
    o = get_org(db)
    name = (data.name or "").strip()
    if not name:
        raise OrganizationError("Company name is required.")
    trn = _validate_trn(data.trn)
    if data.financial_year_start and data.financial_year_end and data.financial_year_start >= data.financial_year_end:
        raise OrganizationError("Financial year start date must be before the end date.")
    freq = (data.vat_return_frequency or "").lower()
    if freq and freq not in _FREQ:
        raise OrganizationError("VAT return frequency must be monthly, quarterly or na.")
    if data.vat_registered:
        if freq not in ("monthly", "quarterly"):
            raise OrganizationError("Select a VAT return frequency (monthly or quarterly) for a VAT-registered company.")
        if not trn:
            raise OrganizationError("A VAT-registered company requires a valid 15-digit TRN.")
    else:
        freq = "na"

    o.name = name
    o.legal_name = (data.legal_name or None)
    o.address = (data.address or None)
    o.trn = trn
    o.phone = (data.phone or None)
    o.email = (data.email or None)
    o.website = (data.website or None)
    o.financial_year_start = data.financial_year_start
    o.financial_year_end = data.financial_year_end
    o.base_currency = (data.base_currency or "AED")
    o.vat_registered = bool(data.vat_registered)
    o.vat_return_frequency = freq
    # E-invoicing organization identity (optional; validated lightly).
    o.trade_license = (getattr(data, "trade_license", None) or None)
    o.country = (getattr(data, "country", None) or "AE")
    o.einvoice_scheme = (getattr(data, "einvoice_scheme", None) or None)
    o.einvoice_id = (getattr(data, "einvoice_id", None) or None)
    o.bank_name = (getattr(data, "bank_name", None) or None)
    o.bank_iban = (getattr(data, "bank_iban", None) or None)
    db.commit()
    db.refresh(o)
    return profile(db)


# ── Logo ──────────────────────────────────────────────────────────────────────────────────
def set_logo(db: Session, filename: str, content: bytes) -> dict:
    ext = (os.path.splitext(filename)[1].lstrip(".") or "").lower()
    if ext not in _LOGO_EXT:
        raise OrganizationError(f"Logo must be one of: {', '.join(sorted(_LOGO_EXT))}.")
    if not content:
        raise OrganizationError("The logo file is empty.")
    if len(content) > get_settings().max_upload_mb * 1024 * 1024:
        raise OrganizationError("Logo file is too large.")
    d = get_settings().org_dir
    os.makedirs(d, exist_ok=True)
    o = get_org(db)
    # remove a previous logo with a different extension
    if o.logo_path and os.path.exists(o.logo_path):
        try:
            os.remove(o.logo_path)
        except OSError:
            pass
    path = os.path.join(d, f"logo.{ext}")
    with open(path, "wb") as fh:
        fh.write(content)
    o.logo_filename = filename
    o.logo_mime = _LOGO_MIME.get(ext, "application/octet-stream")
    o.logo_path = path
    db.commit()
    return profile(db)


def remove_logo(db: Session) -> dict:
    o = get_org(db)
    if o.logo_path and os.path.exists(o.logo_path):
        try:
            os.remove(o.logo_path)
        except OSError:
            pass
    o.logo_filename = o.logo_mime = o.logo_path = None
    db.commit()
    return profile(db)


def logo_file(db: Session):
    o = get_org(db)
    if not o.logo_path or not os.path.exists(o.logo_path):
        return None
    return o.logo_path, o.logo_mime or "application/octet-stream"


# ── For reports / PDFs ──────────────────────────────────────────────────────────────────────
def company_header(db: Session) -> dict:
    """Compact company identity block used by reports and PDF headers."""
    o = get_org(db)
    return {"name": o.name or "", "trn": o.trn or "", "address": o.address or "",
            "vat_registered": o.vat_registered, "vat_return_frequency": o.vat_return_frequency}
