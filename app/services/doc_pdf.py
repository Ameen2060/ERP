"""Branded per-document PDFs (tax invoice, credit note, vendor bill/credit note).

Pulls the company identity + logo from the Organization profile so every document carries the
latest header dynamically. Raster logos (PNG/JPG/WEBP/GIF) are embedded; SVG is skipped
gracefully (name shown instead) since reportlab can't rasterise SVG without extra deps.
"""

from __future__ import annotations

import io
import os
from decimal import Decimal

from sqlalchemy.orm import Session

from . import advances, credit_notes, expenses, ledger, organization, purchases, sales, templates
from ..models import BillPayment, Customer, CustomerPayment, SalesInvoice, Vendor, VendorBill


def _m(v) -> str:
    try:
        return f"{Decimal(str(v)):,.2f}"
    except Exception:  # noqa: BLE001
        return str(v)


def _addr(party) -> str | None:
    """Effective document address: prefer the structured billing address, fall back to the
    legacy single address field. Works for Customer and Vendor."""
    if party is None:
        return None
    return getattr(party, "billing_address", None) or getattr(party, "address", None)


def _logo_flowable(db: Session, max_w=140, max_h=64):
    lf = organization.logo_file(db)
    if not lf:
        return None
    ref, mime = lf
    if "svg" in (mime or "") or str(ref).lower().endswith(".svg"):
        return None
    try:
        import io
        from reportlab.lib.utils import ImageReader
        from reportlab.platypus import Image
        data = organization.logo_bytes(db)   # bytes from durable storage (path or Blob URL)
        if not data:
            return None
        ir = ImageReader(io.BytesIO(data))
        iw, ih = ir.getSize()
        scale = min(max_w / iw, max_h / ih)
        return Image(io.BytesIO(data), width=iw * scale, height=ih * scale)
    except Exception:  # noqa: BLE001
        return None


_PAGE = {"A4": "A4", "A5": "A5", "Letter": "LETTER"}


def _render(db: Session, *, cfg: dict, title: str, number: str, meta: list, party_label: str,
            party_name: str, party_trn: str | None, headers: list, rows: list,
            totals: list, note: str | None = None, party_address: str | None = None) -> bytes:
    from reportlab.lib import colors, pagesizes
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    sec = cfg.get("sections", {})
    order = cfg.get("order") or ["customer", "invoice_details", "line_items", "totals", "bank_details", "notes", "signature"]
    accent = colors.HexColor(cfg.get("accent_color") or "#2563eb")
    fs = int(cfg.get("font_size") or 9)
    logo_pos = cfg.get("logo_position") or "left"
    pagesize = getattr(pagesizes, _PAGE.get(cfg.get("page_size", "A4"), "A4"))

    org = organization.company_header(db)
    styles = getSampleStyleSheet()
    right = ParagraphStyle("r", parent=styles["Normal"], alignment=2, fontSize=fs)
    base = ParagraphStyle("b", parent=styles["Normal"], fontSize=fs)
    small = ParagraphStyle("s", parent=styles["Normal"], fontSize=8, textColor=colors.HexColor("#555555"))
    h1 = ParagraphStyle("h1", parent=styles["Title"], alignment=2, fontSize=18)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=pagesize, leftMargin=16 * mm, rightMargin=16 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm)
    elems = []

    # ── Header (logo + company on one side, title + number on the other) ──
    logo = _logo_flowable(db) if sec.get("logo", True) else None
    company_cell = []
    if logo:
        company_cell += [logo, Spacer(1, 4)]
    if sec.get("company", True):
        cl = [f"<b>{org['name']}</b>"]
        if org.get("address"):
            cl.append(org["address"])
        if org.get("trn"):
            cl.append(f"TRN: {org['trn']}")
        company_cell.append(Paragraph("<br/>".join(cl), base))
    if not company_cell:
        company_cell = [Paragraph("", base)]
    title_cell = [Paragraph(title, h1), Paragraph(f"<b>{number}</b>", right)]
    if logo_pos == "right":
        head = Table([[title_cell, company_cell]], colWidths=["45%", "55%"])
    elif logo_pos == "center":
        head = Table([[company_cell], [title_cell]], colWidths=["100%"])
    else:
        head = Table([[company_cell, title_cell]], colWidths=["55%", "45%"])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    elems += [head, Spacer(1, 10)]

    # ── Body blocks (visibility + order from the template) ──
    def block_customer():
        party = f"<b>{party_label}:</b> {party_name}"
        if party_address:
            party += f"<br/>{str(party_address).replace(chr(10), '<br/>')}"
        if party_trn:
            party += f"<br/><b>TRN:</b> {party_trn}"
        return Paragraph(party, base)

    def block_meta():
        return Paragraph("<br/>".join(f"<b>{k}:</b> {v}" for k, v in meta if v not in (None, "", "None")), base)

    # customer + invoice_details share a row when both visible
    top = []
    if sec.get("customer", True):
        top.append(("customer", block_customer()))
    if sec.get("invoice_details", True):
        top.append(("invoice_details", block_meta()))
    if len(top) == 2:
        info = Table([[top[0][1], top[1][1]]], colWidths=["55%", "45%"])
        info.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
        elems += [info, Spacer(1, 8)]
    elif len(top) == 1:
        elems += [top[0][1], Spacer(1, 8)]

    def block_lines():
        numeric = {i for i in range(len(headers)) if i > 0}
        data = [headers] + [[str(c) for c in r] for r in rows]
        t = Table(data, repeatRows=1)
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), accent), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), fs),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f6f9")]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]
        for i in numeric:
            style.append(("ALIGN", (i, 0), (i, -1), "RIGHT"))
        t.setStyle(TableStyle(style))
        return t

    def block_totals():
        tot = Table([[k, _m(v) if not isinstance(v, str) else v] for k, v in totals], colWidths=[90, 90])
        tot.setStyle(TableStyle([
            ("ALIGN", (1, 0), (1, -1), "RIGHT"), ("FONTSIZE", (0, 0), (-1, -1), fs),
            ("LINEABOVE", (0, -1), (-1, -1), 0.5, colors.HexColor("#333333")),
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold")]))
        wrap = Table([[Paragraph("", base), tot]], colWidths=["60%", "40%"])
        wrap.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
        return wrap

    def block_bank():
        if not cfg.get("bank_details"):
            return None
        return Paragraph("<b>Bank details</b><br/>" + str(cfg["bank_details"]).replace("\n", "<br/>"), base)

    def block_notes():
        parts = []
        if note:
            parts.append(note)
        if cfg.get("footer_notes"):
            parts.append(str(cfg["footer_notes"]))
        return Paragraph("<br/>".join(parts), small) if parts else None

    def block_signature():
        return Paragraph("<br/><br/>_______________________<br/>Authorized signatory", small)

    builders = {"line_items": block_lines, "totals": block_totals,
                "bank_details": block_bank, "notes": block_notes, "signature": block_signature}
    for key in order:
        if key in ("customer", "invoice_details"):
            continue  # already rendered in the top row
        if not sec.get(key, True):
            continue
        fn = builders.get(key)
        if not fn:
            continue
        fl = fn()
        if fl is not None:
            elems += [fl, Spacer(1, 10)]

    elems += [Spacer(1, 6), Paragraph(
        f"{org['name']} · System-generated document.", small)]
    doc.build(elems)
    buf.seek(0)
    return buf.read()


# ── Document builders ─────────────────────────────────────────────────────────────────────
def invoice_pdf(db: Session, invoice_id: str, template_id: str | None = None) -> tuple[bytes, str]:
    cfg = templates.resolve_config(db, "invoice", template_id)
    inv = sales.get_invoice(db, invoice_id)
    cust = db.get(Customer, inv.customer_id)
    rows = [[l.description or "", _m(l.quantity), _m(l.unit_price), _m(l.net_amount),
             _m(l.vat_amount), _m(l.line_total)] for l in inv.lines]
    totals = [("Net", inv.net_total), ("VAT", inv.vat_total), ("Total", inv.grand_total)]
    if inv.retention_applicable and Decimal(inv.retention_amount) > 0:
        totals.append(("Retention", inv.retention_amount))
    totals += [("Paid", inv.amount_paid), ("Balance due", inv.balance_due)]
    meta = [("Date", str(inv.date)), ("Due date", str(inv.due_date or "—")), ("Currency", inv.currency)]
    data = _render(db, cfg=cfg, title="TAX INVOICE", number=inv.number, meta=meta, party_label="Bill to",
                   party_name=cust.name if cust else "", party_trn=cust.trn if cust else None,
                   party_address=_addr(cust),
                   headers=["Description", "Qty", "Unit Price", "Net", "VAT", "Total"], rows=rows,
                   totals=totals, note=(inv.notes or None))
    return data, f"{inv.number}.pdf"


def customer_cn_pdf(db: Session, cn_id: str, template_id: str | None = None) -> tuple[bytes, str]:
    cfg = templates.resolve_config(db, "invoice", template_id)
    cn = credit_notes.get_customer_cn(db, cn_id)
    rows = [[l["description"] or "", _m(l["net_amount"]), _m(l["vat_amount"]), _m(l["line_total"])]
            for l in cn["lines"]]
    totals = [("Net", cn["net_total"]), ("VAT", cn["vat_total"]), ("Total credit", cn["grand_total"])]
    meta = [("Date", cn["date"]), ("Currency", cn["currency"]),
            ("Original invoice", cn.get("invoice_number") or "—"), ("Project", cn.get("project") or "—")]
    note = f"Reason: {cn.get('reason') or ''}"
    cust = db.get(Customer, cn.get("customer_id")) if cn.get("customer_id") else None
    data = _render(db, cfg=cfg, title="CREDIT NOTE", number=cn["number"], meta=meta, party_label="Customer",
                   party_name=cn.get("customer_name") or "", party_trn=cust.trn if cust else None,
                   party_address=_addr(cust),
                   headers=["Description", "Net", "VAT", "Total"], rows=rows, totals=totals, note=note)
    return data, f"{cn['number']}.pdf"


def bill_pdf(db: Session, bill_id: str, template_id: str | None = None) -> tuple[bytes, str]:
    cfg = templates.resolve_config(db, "invoice", template_id)
    bill = purchases.get_bill(db, bill_id)
    ven = db.get(Vendor, bill.vendor_id)
    rows = [[l.description or "", _m(l.net_amount), _m(l.vat_amount), _m(l.line_total)] for l in bill.lines]
    totals = [("Net", bill.net_total), ("VAT", bill.vat_total), ("Total", bill.grand_total),
              ("Paid", bill.amount_paid), ("Balance due", bill.balance_due)]
    meta = [("Date", str(bill.date)), ("Due date", str(bill.due_date or "—")),
            ("Vendor ref", bill.vendor_ref or "—"), ("Currency", bill.currency)]
    data = _render(db, cfg=cfg, title="VENDOR BILL", number=bill.number, meta=meta, party_label="Vendor",
                   party_name=ven.name if ven else "", party_trn=ven.trn if ven else None,
                   party_address=_addr(ven),
                   headers=["Description", "Net", "VAT", "Total"], rows=rows, totals=totals,
                   note=(bill.notes or None))
    return data, f"{bill.number}.pdf"


def vendor_cn_pdf(db: Session, cn_id: str, template_id: str | None = None) -> tuple[bytes, str]:
    cfg = templates.resolve_config(db, "invoice", template_id)
    cn = credit_notes.get_vendor_cn(db, cn_id)
    rows = [[l["description"] or "", _m(l["net_amount"]), _m(l["vat_amount"]), _m(l["line_total"])]
            for l in cn["lines"]]
    totals = [("Net", cn["net_total"]), ("VAT", cn["vat_total"]), ("Total credit", cn["grand_total"])]
    meta = [("Date", cn["date"]), ("Currency", cn["currency"]),
            ("Original bill", cn.get("bill_number") or "—"), ("Project", cn.get("project") or "—")]
    note = f"Reason: {cn.get('reason') or ''}"
    ven = db.get(Vendor, cn.get("vendor_id")) if cn.get("vendor_id") else None
    data = _render(db, cfg=cfg, title="VENDOR CREDIT NOTE", number=cn["number"], meta=meta, party_label="Vendor",
                   party_name=cn.get("vendor_name") or "", party_trn=ven.trn if ven else None,
                   party_address=_addr(ven),
                   headers=["Description", "Net", "VAT", "Total"], rows=rows, totals=totals, note=note)
    return data, f"{cn['number']}.pdf"


def receipt_pdf(db: Session, payment_id: str, kind: str = "customer",
                template_id: str | None = None) -> tuple[bytes, str]:
    cfg = templates.resolve_config(db, "receipt", template_id)
    if kind == "vendor":
        p = db.get(BillPayment, payment_id)
        if not p:
            raise ValueError("Vendor payment not found.")
        party = db.get(Vendor, p.vendor_id)
        doc = db.get(VendorBill, p.bill_id) if p.bill_id else None
        label = "Paid to"
        against = ("Bill", doc.number if doc else "—")
    else:
        p = db.get(CustomerPayment, payment_id)
        if not p:
            raise ValueError("Customer payment not found.")
        party = db.get(Customer, p.customer_id)
        doc = db.get(SalesInvoice, p.invoice_id) if p.invoice_id else None
        label = "Received from"
        against = ("Invoice", doc.number if doc else "—")
    number = p.reference or f"RCPT-{p.id[:8]}"
    meta = [("Date", str(p.date)), ("Method", p.method), (against[0], against[1]),
            ("Reference", p.reference or "—")]
    rows = [[f"Payment received against {against[0].lower()} {against[1]}", _m(p.amount)]]
    totals = [("Amount received", p.amount)]
    data = _render(db, cfg=cfg, title="PAYMENT RECEIPT", number=number, meta=meta, party_label=label,
                   party_name=party.name if party else "", party_trn=party.trn if party else None,
                   party_address=_addr(party),
                   headers=["Description", "Amount"], rows=rows, totals=totals,
                   note="This receipt confirms the payment shown above.")
    return data, f"{number}.pdf"


def journal_entry_pdf(db: Session, entry_id: str, template_id: str | None = None) -> tuple[bytes, str]:
    cfg = templates.resolve_config(db, "invoice", template_id)
    e = ledger.get_entry(db, entry_id)
    rows = [[f"{l.account_code}  {l.account_name}", _m(l.debit) if l.debit else "",
             _m(l.credit) if l.credit else ""] for l in e.lines]
    tot_d = sum((Decimal(str(l.debit)) for l in e.lines), Decimal(0))
    tot_c = sum((Decimal(str(l.credit)) for l in e.lines), Decimal(0))
    balanced = "BALANCED ✓" if abs(tot_d - tot_c) < Decimal("0.01") else "NOT BALANCED"
    totals = [("Total debits", tot_d), ("Total credits", tot_c), (balanced, "")]
    meta = [("Date", str(e.date)), ("Reference", e.reference or "—"), ("Source", e.source),
            ("Status", e.status)]
    data = _render(db, cfg=cfg, title="JOURNAL ENTRY", number=f"JE-{e.entry_no}", meta=meta,
                   party_label="Memo", party_name=(e.memo or "—"), party_trn=None,
                   headers=["Account", "Debit", "Credit"], rows=rows, totals=totals,
                   note="Double-entry journal — total debits must equal total credits.")
    return data, f"JE-{e.entry_no}.pdf"


def expense_pdf(db: Session, expense_id: str, template_id: str | None = None) -> tuple[bytes, str]:
    cfg = templates.resolve_config(db, "invoice", template_id)
    e = expenses.get_expense(db, expense_id)
    rows = [[e.get("description") or e.get("category") or "Expense",
             _m(e["net_amount"]), _m(e["vat_amount"]), _m(e["total_amount"])]]
    totals = [("Net", e["net_amount"]), ("VAT", e["vat_amount"]), ("Total", e["total_amount"])]
    meta = [("Date", e["date"]), ("Category", e.get("category") or "—"),
            ("Project", e.get("project") or "—"),
            ("Payment", "Paid — " + (e.get("payment_account_code") or "") if e.get("paid_directly") else "Payable"),
            ("Method", e.get("payment_method") or "—")]
    data = _render(db, cfg=cfg, title="EXPENSE VOUCHER", number=e["number"], meta=meta,
                   party_label="Payee", party_name=(e.get("vendor_name") or e.get("payee_name") or "—"),
                   party_trn=e.get("vendor_trn"), party_address=e.get("vendor_address"),
                   headers=["Description", "Net", "VAT", "Total"], rows=rows,
                   totals=totals, note=(e.get("notes") or None))
    return data, f"{e['number']}.pdf"


def advance_pdf(db: Session, side: str, advance_id: str, template_id: str | None = None) -> tuple[bytes, str]:
    cfg = templates.resolve_config(db, "receipt", template_id)
    a = advances.get_customer_advance(db, advance_id) if side == "customer" else advances.get_vendor_advance(db, advance_id)
    party = a.get("customer_name") or a.get("vendor_name") or "—"
    title = "CUSTOMER ADVANCE RECEIPT" if side == "customer" else "VENDOR ADVANCE PAYMENT"
    rows = [["Advance " + a["number"], _m(a["net_amount"]), _m(a["vat_amount"]), _m(a["amount"])]]
    totals = [("Net", a["net_amount"]), ("VAT", a["vat_amount"]), ("Amount", a["amount"]),
              ("Applied", a["applied"]), ("Available", a["available"])]
    meta = [("Date", a["date"]), ("Reference", a.get("reference") or "—"), ("Currency", a["currency"]),
            ("VAT on advance", ("Yes @ " + str(a["vat_rate"])) if a.get("vat_applicable") else "No"),
            ("Tax point", a.get("tax_point_date") or "—"), ("Project", a.get("project") or "—")]
    data = _render(db, cfg=cfg, title=title, number=a["number"], meta=meta,
                   party_label=("Customer" if side == "customer" else "Vendor"), party_name=party,
                   party_trn=None, headers=["Description", "Net", "VAT", "Amount"], rows=rows,
                   totals=totals, note=(a.get("notes") or None))
    return data, f"{a['number']}.pdf"


def einvoice_pdf(db: Session, ei_id: str, template_id: str | None = None) -> tuple[bytes, str]:
    """Human-readable representation of an e-invoice. The structured payload remains the
    authoritative electronic record; this PDF carries the e-invoice identifier + compliance
    status. QR is included only where the configured UAE spec requires it (not fabricated here)."""
    from . import einvoicing
    ei = einvoicing.get(db, ei_id)
    d = einvoicing.detail(db, ei)
    payload = d.get("payload") or {}
    inv = payload.get("invoice") or {}
    lines = payload.get("lines") or []
    cfg = templates.resolve_config(db, "invoice", template_id)
    rows = [[l.get("description") or "", _m(l.get("quantity")), _m(l.get("unit_price")),
             _m(l.get("net_amount")), _m(l.get("vat_amount")), _m(l.get("line_total"))] for l in lines]
    totals = [("Net", ei.net_total), ("VAT", ei.vat_total), ("Total", ei.grand_total)]
    title = {"invoice": "TAX INVOICE (E-INVOICE)", "credit_note": "CREDIT NOTE (E-INVOICE)",
             "debit_note": "DEBIT NOTE (E-INVOICE)", "prepayment": "ADVANCE (E-INVOICE)"}.get(
                 ei.doc_type_code, "E-INVOICE")
    comp = "System Validation: " + ("PASSED" if ei.system_validation_passed else "NOT PASSED")
    comp += " | Regulatory Compliance: " + ("CONFIRMED" if ei.regulatory_confirmed else "NOT CONFIRMED")
    note = comp + ("\n" + einvoicing.PROVISIONAL_NOTICE if ei.provisional else "")
    meta = [("Document no.", ei.doc_number or "—"), ("Issue date", inv.get("issue_date") or "—"),
            ("Supply date", inv.get("supply_date") or "—"), ("Currency", ei.currency),
            ("E-invoice status", d["status_label"]),
            ("E-invoice id", ei.provider_ref or ei.id[:12]),
            ("Schema", f"{ei.schema_id or '—'} {ei.schema_version or ''}".strip())]
    refs = payload.get("references") or {}
    if refs.get("original_invoice") or refs.get("original_bill"):
        meta.append(("Original", refs.get("original_invoice") or refs.get("original_bill")))
    data = _render(db, cfg=cfg, title=title, number=ei.doc_number or ei.id[:8], meta=meta,
                   party_label=("Buyer" if ei.direction == "outbound" else "Supplier"),
                   party_name=ei.party_name or "", party_trn=ei.party_trn,
                   headers=["Description", "Qty", "Unit Price", "Net", "VAT", "Total"], rows=rows,
                   totals=totals, note=note)
    return data, f"EINV-{ei.doc_number or ei.id[:8]}.pdf"


def preview(db: Session, doc_type: str, cfg: dict) -> bytes:
    """Render a sample document with an arbitrary (unsaved) config — powers the live preview."""
    title = "PAYMENT RECEIPT" if doc_type == "receipt" else "TAX INVOICE"
    rows = [["Consulting services", "2", "500.00", "1,000.00", "50.00", "1,050.00"],
            ["Support (monthly)", "1", "300.00", "300.00", "15.00", "315.00"]]
    totals = [("Net", "1,300.00"), ("VAT", "65.00"), ("Total", "1,365.00"),
              ("Paid", "0.00"), ("Balance due", "1,365.00")]
    meta = [("Date", "2026-01-31"), ("Due date", "2026-02-28"), ("Currency", "AED")]
    return _render(db, cfg=cfg, title=title, number="SAMPLE-0001", meta=meta, party_label="Bill to",
                   party_name="Sample Customer LLC", party_trn="100200300400500",
                   headers=["Description", "Qty", "Unit Price", "Net", "VAT", "Total"],
                   rows=rows, totals=totals, note="Thank you for your business.")
