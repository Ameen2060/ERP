"""Document layout templates for invoices & receipts. Presentation only — the config drives
how doc_pdf renders (section visibility/order, logo placement, page size, accent, bank
details, footer notes). Never touches accounting data."""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import DocumentTemplate

# Body blocks that can be toggled and reordered (header is always first).
BLOCKS = ["customer", "invoice_details", "line_items", "totals", "bank_details", "notes", "signature"]
DEFAULT_SECTIONS = {
    "logo": True, "company": True, "customer": True, "invoice_details": True,
    "line_items": True, "totals": True, "vat": True, "payment": True,
    "bank_details": True, "notes": True, "signature": False,
}
DEFAULT_ORDER = ["customer", "invoice_details", "line_items", "totals", "bank_details", "notes", "signature"]
DEFAULT_CONFIG = {
    "page_size": "A4", "logo_position": "left", "accent_color": "#2563eb", "font_size": 9,
    "sections": dict(DEFAULT_SECTIONS), "order": list(DEFAULT_ORDER),
    "bank_details": "", "footer_notes": "",
}


class TemplateError(ValueError):
    """Domain error → HTTP 400."""


def _out(t: DocumentTemplate) -> dict:
    try:
        sections = {**DEFAULT_SECTIONS, **json.loads(t.sections_json or "{}")}
    except ValueError:
        sections = dict(DEFAULT_SECTIONS)
    try:
        order = json.loads(t.order_json or "[]") or list(DEFAULT_ORDER)
    except ValueError:
        order = list(DEFAULT_ORDER)
    # ensure every block appears exactly once
    order = [b for b in order if b in BLOCKS] + [b for b in BLOCKS if b not in order]
    return {
        "id": t.id, "name": t.name, "doc_type": t.doc_type, "is_default": t.is_default,
        "page_size": t.page_size, "logo_position": t.logo_position, "accent_color": t.accent_color,
        "font_size": t.font_size, "sections": sections, "order": order,
        "bank_details": t.bank_details or "", "footer_notes": t.footer_notes or "",
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


def config_of(t: DocumentTemplate | None) -> dict:
    """Merged render config for doc_pdf. None → the built-in default."""
    if t is None:
        return dict(DEFAULT_CONFIG)
    o = _out(t)
    return {"page_size": o["page_size"], "logo_position": o["logo_position"],
            "accent_color": o["accent_color"], "font_size": o["font_size"],
            "sections": o["sections"], "order": o["order"],
            "bank_details": o["bank_details"], "footer_notes": o["footer_notes"]}


def config_from_payload(data) -> dict:
    """Build a render config from a raw (possibly unsaved) payload — used for live preview."""
    return {
        "page_size": data.page_size or "A4", "logo_position": data.logo_position or "left",
        "accent_color": data.accent_color or "#2563eb", "font_size": int(data.font_size or 9),
        "sections": {**DEFAULT_SECTIONS, **(data.sections or {})},
        "order": data.order or list(DEFAULT_ORDER),
        "bank_details": data.bank_details or "", "footer_notes": data.footer_notes or "",
    }


def list_templates(db: Session, doc_type: str | None = None) -> list[dict]:
    stmt = select(DocumentTemplate).order_by(DocumentTemplate.doc_type, DocumentTemplate.name)
    if doc_type:
        stmt = stmt.where(DocumentTemplate.doc_type == doc_type)
    return [_out(t) for t in db.execute(stmt).scalars()]


def get(db: Session, template_id: str) -> DocumentTemplate:
    t = db.get(DocumentTemplate, template_id)
    if not t:
        raise TemplateError("Template not found.")
    return t


def get_default(db: Session, doc_type: str) -> DocumentTemplate | None:
    return db.execute(select(DocumentTemplate).where(
        DocumentTemplate.doc_type == doc_type, DocumentTemplate.is_default.is_(True))).scalars().first()


def resolve_config(db: Session, doc_type: str, template_id: str | None) -> dict:
    if template_id:
        return config_of(get(db, template_id))
    return config_of(get_default(db, doc_type))


def _apply(t: DocumentTemplate, data) -> None:
    if not (data.name or "").strip():
        raise TemplateError("Template name is required.")
    if data.doc_type not in ("invoice", "receipt"):
        raise TemplateError("doc_type must be 'invoice' or 'receipt'.")
    if data.page_size not in ("A4", "A5", "Letter"):
        raise TemplateError("page_size must be A4, A5 or Letter.")
    t.name = data.name.strip()
    t.doc_type = data.doc_type
    t.page_size = data.page_size
    t.logo_position = data.logo_position if data.logo_position in ("left", "center", "right") else "left"
    t.accent_color = data.accent_color or "#2563eb"
    t.font_size = int(data.font_size or 9)
    t.sections_json = json.dumps({**DEFAULT_SECTIONS, **(data.sections or {})})
    t.order_json = json.dumps(data.order or list(DEFAULT_ORDER))
    t.bank_details = data.bank_details or None
    t.footer_notes = data.footer_notes or None


def create(db: Session, data) -> dict:
    t = DocumentTemplate()
    _apply(t, data)
    db.add(t)
    db.flush()
    if data.is_default:
        _clear_defaults(db, t.doc_type, t.id)
        t.is_default = True
    db.commit()
    db.refresh(t)
    return _out(t)


def update(db: Session, template_id: str, data) -> dict:
    t = get(db, template_id)
    _apply(t, data)
    if data.is_default:
        _clear_defaults(db, t.doc_type, t.id)
        t.is_default = True
    else:
        t.is_default = False
    db.commit()
    db.refresh(t)
    return _out(t)


def set_default(db: Session, template_id: str) -> dict:
    t = get(db, template_id)
    _clear_defaults(db, t.doc_type, t.id)
    t.is_default = True
    db.commit()
    db.refresh(t)
    return _out(t)


def _clear_defaults(db: Session, doc_type: str, keep_id: str) -> None:
    for other in db.execute(select(DocumentTemplate).where(
            DocumentTemplate.doc_type == doc_type, DocumentTemplate.is_default.is_(True))).scalars():
        if other.id != keep_id:
            other.is_default = False


def delete(db: Session, template_id: str) -> None:
    db.delete(get(db, template_id))
    db.commit()
