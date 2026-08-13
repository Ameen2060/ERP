"""SQLAlchemy ORM models for the accounting system."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="admin")  # admin | accountant | viewer
    is_active: Mapped[bool] = mapped_column(default=True)
    # Set when a password change is forced (admin reset / recovery) — advisory flag for the UI.
    must_change_password: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PasswordResetToken(Base):
    """A single-use, expiring credential for the forgot-password flow. Only a hash of the
    token is stored (never the token itself), so a DB leak can't be used to reset accounts."""

    __tablename__ = "password_reset_tokens"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), index=True)  # sha256 hex of the raw token
    kind: Mapped[str] = mapped_column(String(16), default="self")     # self | admin
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    requested_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)


class TransactionAudit(Base):
    """Append-only edit history for financial transactions (invoice, bill, expense, journal, …).
    Records the field-level before→after diff, who changed it, why (reason), and the status
    transition. Reusable across every transaction module — keyed by (entity_type, entity_id)."""

    __tablename__ = "transaction_audits"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    entity_type: Mapped[str] = mapped_column(String(32), index=True)
    entity_id: Mapped[str] = mapped_column(String(32), index=True)
    doc_number: Mapped[str | None] = mapped_column(String(48), nullable=True)
    actor: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(16), default="edit")  # create|edit|void|reverse
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    prev_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    new_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    changes: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON [{field,old,new}]


class RolePermission(Base):
    """Editable role→action permission matrix (view/create/edit/delete/approve/reverse/
    view_audit). Seeded from defaults on first startup; admins can toggle it in the UI."""

    __tablename__ = "role_permissions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    role: Mapped[str] = mapped_column(String(24), index=True)
    action: Mapped[str] = mapped_column(String(24), index=True)
    allowed: Mapped[bool] = mapped_column(default=False)


class ExportLog(Base):
    """Audit of every export/print: who, when, what report/transaction, format, filters, file."""

    __tablename__ = "export_logs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    actor: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(24), default="report")  # report | document
    ref: Mapped[str | None] = mapped_column(String(64), nullable=True)  # report name or doc kind/id
    fmt: Mapped[str] = mapped_column(String(8), default="pdf")
    filters: Mapped[str | None] = mapped_column(Text, nullable=True)
    filename: Mapped[str | None] = mapped_column(String(160), nullable=True)


class SecurityEvent(Base):
    """Append-only audit trail for authentication/credential activity (login, password change,
    reset request, reset complete, admin reset). Never stores passwords or raw tokens."""

    __tablename__ = "security_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    event: Mapped[str] = mapped_column(String(32), index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    actor: Mapped[str | None] = mapped_column(String(64), nullable=True)  # who performed it (e.g. admin)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)


class VatTreatment(Base):
    """UAE VAT treatment master — the single source of VAT rules. Each transaction line stores
    the treatment *code* used, so history is immutable when the master is later edited."""

    __tablename__ = "vat_treatments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(16), default="standard")  # standard|zero|exempt|out_of_scope|reverse_charge|other
    rate: Mapped[Decimal] = mapped_column(Numeric(9, 4), default=Decimal(0))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_vat_account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    input_vat_account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    return_box: Mapped[str | None] = mapped_column(String(32), nullable=True)
    taxable: Mapped[bool] = mapped_column(default=True)
    recoverable: Mapped[bool] = mapped_column(default=True)
    active: Mapped[bool] = mapped_column(default=True, index=True)
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    applicable_txn_types: Mapped[str] = mapped_column(String(120), default="sales,purchase,expense")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class SystemSettings(Base):
    """Singleton system configuration: document number formats, default posting accounts,
    rounding/precision, and the default VAT rate. Resolved dynamically — never hard-coded."""

    __tablename__ = "system_settings"

    id: Mapped[str] = mapped_column(String(8), primary_key=True, default="system")
    invoice_number_format: Mapped[str] = mapped_column(String(32), default="INV-{seq:04d}")
    credit_note_number_format: Mapped[str] = mapped_column(String(32), default="CN-{seq:04d}")
    bill_number_format: Mapped[str] = mapped_column(String(32), default="BILL-{seq:04d}")
    payment_number_format: Mapped[str] = mapped_column(String(32), default="Pmt-{seq:04d}")
    # default posting accounts (by code; blank → fall back to the built-in constant)
    default_sales_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    default_ar_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    default_ap_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    default_input_vat_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    default_output_vat_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    default_bank_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    default_cash_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    decimal_places: Mapped[int] = mapped_column(Integer, default=2)
    rounding_mode: Mapped[str] = mapped_column(String(12), default="half_up")
    default_vat_rate: Mapped[Decimal] = mapped_column(Numeric(9, 4), default=Decimal("0.05"))
    # VAT-on-advances SME-approval gate: off → sme_review → approved → enabled
    advance_vat_status: Mapped[str] = mapped_column(String(12), default="off")
    advance_vat_approved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    advance_vat_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Period lock: transactions dated on/before this date are closed and cannot be edited/voided
    # directly (a reversal-adjustment in an open period is required instead).
    books_locked_before: Mapped[date | None] = mapped_column(Date, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class Organization(Base):
    """Singleton company profile + VAT configuration. Flows into reports, documents and PDFs.
    One row only (id fixed to 'org')."""

    __tablename__ = "organization"

    id: Mapped[str] = mapped_column(String(8), primary_key=True, default="org")
    name: Mapped[str] = mapped_column(String(160), default="")
    legal_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    trn: Mapped[str | None] = mapped_column(String(20), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    email: Mapped[str | None] = mapped_column(String(120), nullable=True)
    website: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # E-invoicing identity (registered under the UAE e-invoicing framework).
    trade_license: Mapped[str | None] = mapped_column(String(64), nullable=True)
    country: Mapped[str] = mapped_column(String(2), default="AE")
    einvoice_scheme: Mapped[str | None] = mapped_column(String(24), nullable=True)  # party-id scheme (e.g. TRN/Peppol scheme)
    einvoice_id: Mapped[str | None] = mapped_column(String(64), nullable=True)      # endpoint/participant id on the network
    bank_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    bank_iban: Mapped[str | None] = mapped_column(String(64), nullable=True)
    financial_year_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    financial_year_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    base_currency: Mapped[str] = mapped_column(String(3), default="AED")
    vat_registered: Mapped[bool] = mapped_column(default=True)
    vat_return_frequency: Mapped[str] = mapped_column(String(12), default="quarterly")  # monthly|quarterly|na
    logo_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    logo_mime: Mapped[str | None] = mapped_column(String(80), nullable=True)
    logo_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class Budget(Base):
    """A company or project budget (header). Approved budgets are the source of truth for
    'budget'; actuals always come from the posted ledger — never duplicated here."""

    __tablename__ = "budgets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(120))
    fiscal_year: Mapped[int] = mapped_column(Integer, index=True)
    version: Mapped[str] = mapped_column(String(16), default="v1")
    scope: Mapped[str] = mapped_column(String(10), default="company", index=True)  # company | project
    project_code: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    period_type: Mapped[str] = mapped_column(String(8), default="annual")  # annual | monthly
    currency: Mapped[str] = mapped_column(String(3), default="AED")
    status: Mapped[str] = mapped_column(String(10), default="draft", index=True)  # draft|submitted|approved|locked|closed
    owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    lines: Mapped[list["BudgetLine"]] = relationship(
        back_populates="budget", cascade="all, delete-orphan")


class BudgetLine(Base):
    """A budgeted amount for an account (optionally cost-centre) in a period (month 0 = full year)."""

    __tablename__ = "budget_lines"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    budget_id: Mapped[str] = mapped_column(ForeignKey("budgets.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    cost_center: Mapped[str | None] = mapped_column(String(64), nullable=True)
    month: Mapped[int] = mapped_column(Integer, default=0)   # 0 = full year, 1-12 = month
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))

    budget: Mapped[Budget] = relationship(back_populates="lines")


class BudgetEvent(Base):
    """Audit trail for budget workflow transitions and revisions."""

    __tablename__ = "budget_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    budget_id: Mapped[str] = mapped_column(ForeignKey("budgets.id", ondelete="CASCADE"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    actor: Mapped[str] = mapped_column(String(64), default="local")
    action: Mapped[str] = mapped_column(String(24))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class Project(Base):
    """Project master — the single source of truth for project metadata. Transactions link by
    the free-text `project` code they already carry, so historical data is preserved."""

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    customer_id: Mapped[str | None] = mapped_column(ForeignKey("customers.id"), nullable=True, index=True)
    owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    project_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    location: Mapped[str | None] = mapped_column(String(160), nullable=True)
    manager: Mapped[str | None] = mapped_column(String(120), nullable=True)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expected_completion: Mapped[date | None] = mapped_column(Date, nullable=True)
    actual_completion: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="active", index=True)  # planned|active|on_hold|completed|cancelled|archived
    contract_value: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    budget: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    currency: Mapped[str] = mapped_column(String(3), default="AED")
    vat_treatment: Mapped[str | None] = mapped_column(String(32), nullable=True)
    retention_percent: Mapped[Decimal] = mapped_column(Numeric(9, 4), default=Decimal(0))
    retention_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    advance_percent: Mapped[Decimal] = mapped_column(Numeric(9, 4), default=Decimal(0))
    advance_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    progress_percent: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=Decimal(0))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class ProjectEvent(Base):
    """Audit trail for project create/edit/status/archive actions."""

    __tablename__ = "project_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    actor: Mapped[str] = mapped_column(String(64), default="local")
    action: Mapped[str] = mapped_column(String(24))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class DocumentTemplate(Base):
    """A saved layout for invoices / receipts: which sections show, their order, logo
    placement, page size, accent colour, bank details and footer notes. Purely presentational
    — never affects accounting calculations or transaction data."""

    __tablename__ = "document_templates"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(80))
    doc_type: Mapped[str] = mapped_column(String(12), default="invoice", index=True)  # invoice | receipt
    is_default: Mapped[bool] = mapped_column(default=False, index=True)
    page_size: Mapped[str] = mapped_column(String(8), default="A4")   # A4 | A5 | Letter
    logo_position: Mapped[str] = mapped_column(String(8), default="left")  # left|center|right
    accent_color: Mapped[str] = mapped_column(String(9), default="#2563eb")
    font_size: Mapped[int] = mapped_column(Integer, default=9)
    sections_json: Mapped[str] = mapped_column(Text, default="{}")
    order_json: Mapped[str] = mapped_column(Text, default="[]")
    bank_details: Mapped[str | None] = mapped_column(Text, nullable=True)
    footer_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class Account(Base):
    """A node in the Chart of Accounts. Self-referential so accounts form a tree of
    groups → sub-accounts. Group accounts are non-postable headers."""

    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    type: Mapped[str] = mapped_column(String(16), index=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"), nullable=True, index=True)
    is_group: Mapped[bool] = mapped_column(default=False)
    normal_balance: Mapped[str] = mapped_column(String(8), default="debit")
    is_active: Mapped[bool] = mapped_column(default=True)
    is_cost_of_sales: Mapped[bool] = mapped_column(default=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    parent: Mapped["Account | None"] = relationship(remote_side=[id])
    lines: Mapped[list["JournalLine"]] = relationship(back_populates="account")


class JournalEntry(Base):
    """A balanced double-entry journal entry."""

    __tablename__ = "journal_entries"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    entry_no: Mapped[int] = mapped_column(Integer, index=True, default=0)
    date: Mapped[date] = mapped_column(Date, index=True)
    memo: Mapped[str | None] = mapped_column(Text, nullable=True)
    reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="manual", index=True)
    currency: Mapped[str] = mapped_column(String(3), default="AED")
    status: Mapped[str] = mapped_column(String(8), default="posted", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    lines: Mapped[list["JournalLine"]] = relationship(
        back_populates="entry", cascade="all, delete-orphan", order_by="JournalLine.ordinal"
    )


class JournalLine(Base):
    __tablename__ = "journal_lines"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    entry_id: Mapped[str] = mapped_column(ForeignKey("journal_entries.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    debit: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    credit: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    entry: Mapped[JournalEntry] = relationship(back_populates="lines")
    account: Mapped[Account] = relationship(back_populates="lines")


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255), index=True)
    trn: Mapped[str | None] = mapped_column(String(32), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    contact_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)          # legacy / general
    billing_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    shipping_address: Mapped[str | None] = mapped_column(Text, nullable=True)  # aka service address
    payment_terms: Mapped[str | None] = mapped_column(String(32), nullable=True)  # e.g. "Net 30"
    credit_limit: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    currency: Mapped[str] = mapped_column(String(3), default="AED")
    # E-invoicing party master data.
    country: Mapped[str] = mapped_column(String(2), default="AE")
    tax_status: Mapped[str] = mapped_column(String(16), default="unknown")  # registered|not_registered|unknown
    party_type: Mapped[str] = mapped_column(String(4), default="b2b")       # b2b | b2c
    einvoice_scheme: Mapped[str | None] = mapped_column(String(24), nullable=True)
    einvoice_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ProfileAudit(Base):
    """Append-only change log for master-data profiles (customer / vendor / bank_account).
    Records who changed a profile, the field-level before→after diff, and when. Master edits
    never touch posted transactions — this log is the record of the master change itself."""

    __tablename__ = "profile_audits"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    entity_type: Mapped[str] = mapped_column(String(24), index=True)  # customer|vendor|bank_account
    entity_id: Mapped[str] = mapped_column(String(32), index=True)
    entity_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actor: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(16), default="update")  # create|update
    changes: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON [{field,old,new}]


class RecurringPlan(Base):
    """A subscription / recurring-billing plan for a customer. On each due date it generates a
    normal sales invoice (same GL/VAT engine) and advances the schedule. Lines are stored as
    JSON so the plan owns its own template independent of any single invoice."""

    __tablename__ = "recurring_plans"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(160))
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    frequency: Mapped[str] = mapped_column(String(12), default="monthly")  # weekly|monthly|quarterly|annual
    currency: Mapped[str] = mapped_column(String(3), default="AED")
    start_date: Mapped[date] = mapped_column(Date)
    next_run_date: Mapped[date] = mapped_column(Date, index=True)
    max_occurrences: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = open-ended
    occurrences_done: Mapped[int] = mapped_column(Integer, default=0)
    auto_post: Mapped[bool] = mapped_column(default=True)
    active: Mapped[bool] = mapped_column(default=True, index=True)
    lines_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # [{description,quantity,unit_price,vat_rate,revenue_account_id}]
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class RecurringRun(Base):
    """Audit of each invoice generated from a plan."""

    __tablename__ = "recurring_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    plan_id: Mapped[str] = mapped_column(ForeignKey("recurring_plans.id"), index=True)
    invoice_id: Mapped[str | None] = mapped_column(ForeignKey("sales_invoices.id"), nullable=True)
    invoice_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    run_date: Mapped[date] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class SalesInvoice(Base):
    __tablename__ = "sales_invoices"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="AED")
    status: Mapped[str] = mapped_column(String(8), default="draft", index=True)

    net_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    vat_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    grand_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    amount_paid: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))

    # Exchange rate to base (AED per 1 unit of `currency`) at invoice date; 1 for AED.
    exchange_rate: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal(1))
    journal_entry_id: Mapped[str | None] = mapped_column(ForeignKey("journal_entries.id"), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Optional reporting dimensions (used by grouping/filtering in reports).
    project: Mapped[str | None] = mapped_column(String(64), nullable=True)
    department: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cost_center: Mapped[str | None] = mapped_column(String(64), nullable=True)
    salesperson: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sales_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Retention (holdback) — configurable basis; retained amount tracked separately from AR.
    retention_applicable: Mapped[bool] = mapped_column(default=False)
    retention_basis: Mapped[str] = mapped_column(String(8), default="net")   # net|gross|amount
    retention_percent: Mapped[Decimal] = mapped_column(Numeric(9, 4), default=Decimal(0))
    retention_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    retention_released: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    retention_release_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    retention_reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retention_account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    contract_reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    customer: Mapped[Customer] = relationship()
    lines: Mapped[list["SalesInvoiceLine"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan", order_by="SalesInvoiceLine.ordinal"
    )


class SalesInvoiceLine(Base):
    __tablename__ = "sales_invoice_lines"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("sales_invoices.id", ondelete="CASCADE"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    description: Mapped[str] = mapped_column(Text, default="")
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal(1))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    vat_rate: Mapped[Decimal] = mapped_column(Numeric(6, 4), default=Decimal("0.05"))
    vat_treatment: Mapped[str] = mapped_column(String(16), default="SR")   # VAT treatment code used
    revenue_account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    net_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    vat_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    line_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))

    invoice: Mapped[SalesInvoice] = relationship(back_populates="lines")


class CustomerPayment(Base):
    __tablename__ = "customer_payments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    invoice_id: Mapped[str | None] = mapped_column(ForeignKey("sales_invoices.id"), nullable=True, index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    method: Mapped[str] = mapped_column(String(16), default="bank")
    deposit_account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    journal_entry_id: Mapped[str | None] = mapped_column(ForeignKey("journal_entries.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# ── Purchases / Accounts Payable ─────────────────────────────────────────────────────────
class Vendor(Base):
    __tablename__ = "vendors"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255), index=True)
    trn: Mapped[str | None] = mapped_column(String(32), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    contact_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)          # legacy / general
    billing_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="AED")
    payment_terms: Mapped[str | None] = mapped_column(String(32), nullable=True)  # e.g. "Net 30"
    # E-invoicing party master data.
    country: Mapped[str] = mapped_column(String(2), default="AE")
    tax_status: Mapped[str] = mapped_column(String(16), default="unknown")  # registered|not_registered|unknown
    party_type: Mapped[str] = mapped_column(String(4), default="b2b")       # b2b | b2c
    einvoice_scheme: Mapped[str | None] = mapped_column(String(24), nullable=True)
    einvoice_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class VendorBill(Base):
    """A supplier bill. When posted it owns a balanced journal entry
    (Dr expense/asset + Input VAT / Cr Accounts Payable)."""

    __tablename__ = "vendor_bills"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    number: Mapped[str] = mapped_column(String(48), index=True)  # internal bill number
    vendor_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)  # vendor's invoice no.
    vendor_id: Mapped[str] = mapped_column(ForeignKey("vendors.id"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    payment_terms: Mapped[str | None] = mapped_column(String(32), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="AED")
    status: Mapped[str] = mapped_column(String(8), default="draft", index=True)  # draft|posted|partial|paid|void

    net_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    vat_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    grand_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    amount_paid: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))

    journal_entry_id: Mapped[str | None] = mapped_column(ForeignKey("journal_entries.id"), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    project: Mapped[str | None] = mapped_column(String(64), nullable=True)
    department: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cost_center: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expense_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Retention (holdback) — retained amount tracked separately from AP.
    retention_applicable: Mapped[bool] = mapped_column(default=False)
    retention_basis: Mapped[str] = mapped_column(String(8), default="net")
    retention_percent: Mapped[Decimal] = mapped_column(Numeric(9, 4), default=Decimal(0))
    retention_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    retention_released: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    retention_release_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    retention_reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retention_account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    contract_reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    vendor: Mapped[Vendor] = relationship()
    lines: Mapped[list["VendorBillLine"]] = relationship(
        back_populates="bill", cascade="all, delete-orphan", order_by="VendorBillLine.ordinal"
    )


class VendorBillLine(Base):
    __tablename__ = "vendor_bill_lines"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    bill_id: Mapped[str] = mapped_column(ForeignKey("vendor_bills.id", ondelete="CASCADE"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    description: Mapped[str] = mapped_column(Text, default="")
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal(1))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    vat_rate: Mapped[Decimal] = mapped_column(Numeric(6, 4), default=Decimal("0.05"))
    vat_treatment: Mapped[str] = mapped_column(String(16), default="SR")
    # The account this line debits (expense, inventory or fixed-asset account).
    expense_account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    net_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    vat_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    line_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))

    bill: Mapped[VendorBill] = relationship(back_populates="lines")


class BillPayment(Base):
    __tablename__ = "bill_payments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    vendor_id: Mapped[str] = mapped_column(ForeignKey("vendors.id"), index=True)
    bill_id: Mapped[str | None] = mapped_column(ForeignKey("vendor_bills.id"), nullable=True, index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    method: Mapped[str] = mapped_column(String(16), default="bank")
    payment_account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))  # bank/cash paid from
    reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    journal_entry_id: Mapped[str | None] = mapped_column(ForeignKey("journal_entries.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# ── Fixed Assets ─────────────────────────────────────────────────────────────────────────
class FixedAsset(Base):
    __tablename__ = "fixed_assets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    asset_code: Mapped[str] = mapped_column(String(48), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    category: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    purchase_date: Mapped[date] = mapped_column(Date, index=True)
    in_service_date: Mapped[date | None] = mapped_column(Date, nullable=True)  # capitalization date
    supplier: Mapped[str | None] = mapped_column(String(255), nullable=True)   # free-text fallback
    vendor_id: Mapped[str | None] = mapped_column(ForeignKey("vendors.id"), nullable=True, index=True)
    invoice_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bill_id: Mapped[str | None] = mapped_column(ForeignKey("vendor_bills.id"), nullable=True)

    purchase_cost: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    vat_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    currency: Mapped[str] = mapped_column(String(3), default="AED")
    residual_value: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))

    # Depreciation config
    method: Mapped[str] = mapped_column(String(24), default="straight_line")  # straight_line|declining_balance
    useful_life_months: Mapped[int] = mapped_column(Integer, default=60)
    declining_rate: Mapped[Decimal] = mapped_column(Numeric(6, 4), default=Decimal("0"))  # annual %, for DB method
    depreciation_start: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Running depreciation state
    accumulated_depreciation: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    last_depreciation_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Dimensions / metadata
    location: Mapped[str | None] = mapped_column(String(128), nullable=True)
    department: Mapped[str | None] = mapped_column(String(64), nullable=True)
    project: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cost_center: Mapped[str | None] = mapped_column(String(64), nullable=True)
    responsible_person: Mapped[str | None] = mapped_column(String(128), nullable=True)
    serial_number: Mapped[str | None] = mapped_column(String(128), nullable=True)
    warranty_info: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # active | disposed | written_off | retired
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    disposal_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    disposal_proceeds: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    disposal_gain_loss: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    transactions: Mapped[list["AssetTransaction"]] = relationship(
        back_populates="asset", cascade="all, delete-orphan", order_by="AssetTransaction.date"
    )


class AssetTransaction(Base):
    """Immutable history of every event on an asset (acquisition, depreciation, transfer,
    revaluation, impairment, disposal, write-off)."""

    __tablename__ = "asset_transactions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    asset_id: Mapped[str] = mapped_column(ForeignKey("fixed_assets.id", ondelete="CASCADE"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    type: Mapped[str] = mapped_column(String(24))  # acquisition|depreciation|disposal|transfer|revaluation|impairment|write_off
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    journal_entry_id: Mapped[str | None] = mapped_column(ForeignKey("journal_entries.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    asset: Mapped[FixedAsset] = relationship(back_populates="transactions")


# ── Multi-currency ───────────────────────────────────────────────────────────────────────
class Currency(Base):
    __tablename__ = "currencies"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    code: Mapped[str] = mapped_column(String(3), unique=True, index=True)  # ISO, e.g. USD
    name: Mapped[str] = mapped_column(String(64))
    symbol: Mapped[str | None] = mapped_column(String(8), nullable=True)
    is_base: Mapped[bool] = mapped_column(default=False)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ExchangeRate(Base):
    """Rate = base-currency units per 1 unit of `currency_code` (e.g. USD→AED 3.6725)."""

    __tablename__ = "exchange_rates"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    currency_code: Mapped[str] = mapped_column(String(3), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    rate: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal(1))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# ── Banking ──────────────────────────────────────────────────────────────────────────────
class BankAccount(Base):
    """A bank or cash account, backed by a general-ledger asset account. All movements on the
    GL account are what reconciliation compares against the imported statement."""

    __tablename__ = "bank_accounts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255), index=True)
    gl_account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), unique=True)
    bank_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    account_name: Mapped[str | None] = mapped_column(String(255), nullable=True)  # name on the account
    account_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    iban: Mapped[str | None] = mapped_column(String(64), nullable=True)
    swift: Mapped[str | None] = mapped_column(String(32), nullable=True)  # SWIFT/BIC
    branch: Mapped[str | None] = mapped_column(String(128), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="AED")
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    gl_account: Mapped["Account"] = relationship()


class BankStatementLine(Base):
    """A single line imported from a bank statement. `amount` is signed: positive = money in
    (debit to the bank GL account), negative = money out (credit)."""

    __tablename__ = "bank_statement_lines"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    bank_account_id: Mapped[str] = mapped_column(ForeignKey("bank_accounts.id", ondelete="CASCADE"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))  # signed
    # unmatched | matched | reconciled
    status: Mapped[str] = mapped_column(String(12), default="unmatched", index=True)
    matched_entry_id: Mapped[str | None] = mapped_column(ForeignKey("journal_entries.id"), nullable=True)
    reconciliation_id: Mapped[str | None] = mapped_column(
        ForeignKey("bank_reconciliations.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class BankReconciliation(Base):
    __tablename__ = "bank_reconciliations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    bank_account_id: Mapped[str] = mapped_column(ForeignKey("bank_accounts.id"), index=True)
    statement_date: Mapped[date] = mapped_column(Date, index=True)
    statement_balance: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    book_balance: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    difference: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    cleared_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(12), default="completed")  # completed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# ── Inventory ────────────────────────────────────────────────────────────────────────────
class Warehouse(Base):
    __tablename__ = "warehouses"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Product(Base):
    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    sku: Mapped[str] = mapped_column(String(48), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    # product = stock-tracked item; service = non-inventory.
    type: Mapped[str] = mapped_column(String(12), default="product", index=True)
    category: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    unit: Mapped[str] = mapped_column(String(16), default="unit")
    # fifo | weighted_average
    cost_method: Mapped[str] = mapped_column(String(20), default="weighted_average")
    sales_price: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    purchase_cost: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    reorder_level: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal(0))
    track_inventory: Mapped[bool] = mapped_column(default=True)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    department: Mapped[str | None] = mapped_column(String(64), nullable=True)
    designation: Mapped[str | None] = mapped_column(String(128), nullable=True)
    join_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    basic_salary: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    housing_allowance: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    transport_allowance: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    other_allowance: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    iban: Mapped[str | None] = mapped_column(String(64), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="AED")
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PayrollRun(Base):
    __tablename__ = "payroll_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    period_label: Mapped[str] = mapped_column(String(32), index=True)  # e.g. 2025-01
    period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    pay_date: Mapped[date] = mapped_column(Date)
    accrue_eosb: Mapped[bool] = mapped_column(default=True)
    status: Mapped[str] = mapped_column(String(8), default="draft", index=True)  # draft|posted|paid|void

    gross_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    deductions_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    net_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    eosb_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))

    journal_entry_id: Mapped[str | None] = mapped_column(ForeignKey("journal_entries.id"), nullable=True)
    payment_entry_id: Mapped[str | None] = mapped_column(ForeignKey("journal_entries.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    payslips: Mapped[list["Payslip"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="Payslip.ordinal"
    )


class Payslip(Base):
    __tablename__ = "payslips"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("payroll_runs.id", ondelete="CASCADE"), index=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employees.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    basic: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    allowances: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    overtime: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    gross: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    deductions: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    net: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    eosb_accrual: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))

    run: Mapped[PayrollRun] = relationship(back_populates="payslips")


class StockMovement(Base):
    """A signed stock movement. quantity/total_cost are positive for receipts (stock in) and
    negative for issues (stock out); adjustments may be either sign."""

    __tablename__ = "stock_movements"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), index=True)
    warehouse_id: Mapped[str] = mapped_column(ForeignKey("warehouses.id"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    # receipt | issue | adjustment | opening
    movement_type: Mapped[str] = mapped_column(String(16), index=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal(0))   # signed
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal(0))
    total_cost: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))  # signed
    reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="inventory")
    batch_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    serial_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    journal_entry_id: Mapped[str | None] = mapped_column(ForeignKey("journal_entries.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# ── Direct Expenses (Purchases → Expenses) ──────────────────────────────────────────────────
class Expense(Base):
    """A direct expense. When paid_directly the posting is Dr Expense / Dr Input VAT /
    Cr Bank·Cash (no payable). Otherwise it books a payable (Cr Accounts Payable)."""

    __tablename__ = "expenses"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vendor_id: Mapped[str | None] = mapped_column(ForeignKey("vendors.id"), nullable=True, index=True)
    payee_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    project: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cost_center: Mapped[str | None] = mapped_column(String(64), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="AED")
    expense_account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    payment_account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    payment_method: Mapped[str] = mapped_column(String(16), default="bank")   # bank|cash|card|cheque|other
    paid_directly: Mapped[bool] = mapped_column(default=True)
    net_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    vat_rate: Mapped[Decimal] = mapped_column(Numeric(9, 4), default=Decimal(0))
    vat_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    total_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    status: Mapped[str] = mapped_column(String(8), default="draft", index=True)  # draft|posted|void
    journal_entry_id: Mapped[str | None] = mapped_column(ForeignKey("journal_entries.id"), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


# ── Advances (customer received / vendor paid) + applications ────────────────────────────────
class CustomerAdvance(Base):
    """Money received from a customer before invoicing. Posts Dr Bank·Cash / Cr Customer
    Advances (contract liability). Recovered against invoices via AdvanceApplication."""

    __tablename__ = "customer_advances"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="AED")
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))  # gross received
    vat_applicable: Mapped[bool] = mapped_column(default=False)
    vat_rate: Mapped[Decimal] = mapped_column(Numeric(9, 4), default=Decimal(0))
    vat_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    net_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    tax_point_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    deposit_account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    advance_account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    project: Mapped[str | None] = mapped_column(String(64), nullable=True)
    contract_reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    journal_entry_id: Mapped[str | None] = mapped_column(ForeignKey("journal_entries.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(8), default="posted", index=True)  # posted|void
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class VendorAdvance(Base):
    """Money paid to a vendor before billing. Posts Dr Vendor Advances (prepayment asset) /
    Cr Bank·Cash. Applied against vendor bills via AdvanceApplication."""

    __tablename__ = "vendor_advances"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    vendor_id: Mapped[str] = mapped_column(ForeignKey("vendors.id"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="AED")
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))  # gross paid
    vat_applicable: Mapped[bool] = mapped_column(default=False)
    vat_rate: Mapped[Decimal] = mapped_column(Numeric(9, 4), default=Decimal(0))
    vat_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    net_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    tax_point_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    payment_account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    advance_account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    project: Mapped[str | None] = mapped_column(String(64), nullable=True)
    contract_reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    journal_entry_id: Mapped[str | None] = mapped_column(ForeignKey("journal_entries.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(8), default="posted", index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AdvanceApplication(Base):
    """One recovery/application of an advance against an invoice (customer) or bill (vendor).
    The available balance of an advance is amount − Σ(its applications)."""

    __tablename__ = "advance_applications"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    advance_type: Mapped[str] = mapped_column(String(10), index=True)   # customer | vendor
    advance_id: Mapped[str] = mapped_column(String(32), index=True)
    target_type: Mapped[str] = mapped_column(String(16))               # sales_invoice | vendor_bill
    target_id: Mapped[str] = mapped_column(String(32), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    journal_entry_id: Mapped[str | None] = mapped_column(ForeignKey("journal_entries.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# ── Credit Notes (customer / vendor) ─────────────────────────────────────────────────────────
class CustomerCreditNote(Base):
    """A customer credit note. Posted: Dr Sales Returns/Revenue + Dr Output VAT / Cr AR —
    reducing receivable and revenue. Applied against invoices via CreditNoteApplication."""

    __tablename__ = "customer_credit_notes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    invoice_id: Mapped[str | None] = mapped_column(ForeignKey("sales_invoices.id"), nullable=True, index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    reason: Mapped[str | None] = mapped_column(String(160), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="AED")
    project: Mapped[str | None] = mapped_column(String(64), nullable=True)
    contract_reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    net_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    vat_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    grand_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    status: Mapped[str] = mapped_column(String(8), default="draft", index=True)  # draft|posted|void
    journal_entry_id: Mapped[str | None] = mapped_column(ForeignKey("journal_entries.id"), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    retention_applicable: Mapped[bool] = mapped_column(default=False)
    retention_basis: Mapped[str] = mapped_column(String(8), default="net")
    retention_percent: Mapped[Decimal] = mapped_column(Numeric(9, 4), default=Decimal(0))
    retention_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    retention_account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    lines: Mapped[list["CustomerCreditNoteLine"]] = relationship(
        back_populates="credit_note", cascade="all, delete-orphan", order_by="CustomerCreditNoteLine.ordinal")


class CustomerCreditNoteLine(Base):
    __tablename__ = "customer_credit_note_lines"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    credit_note_id: Mapped[str] = mapped_column(ForeignKey("customer_credit_notes.id", ondelete="CASCADE"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal(1))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    vat_rate: Mapped[Decimal] = mapped_column(Numeric(9, 4), default=Decimal(0))
    revenue_account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    net_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    vat_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    line_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))

    credit_note: Mapped[CustomerCreditNote] = relationship(back_populates="lines")


class VendorCreditNote(Base):
    """A vendor credit note. Posted: Dr AP / Cr Expense·Inventory·Asset + Cr Input VAT —
    reducing payable and cost. Applied against bills via CreditNoteApplication."""

    __tablename__ = "vendor_credit_notes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    vendor_id: Mapped[str] = mapped_column(ForeignKey("vendors.id"), index=True)
    bill_id: Mapped[str | None] = mapped_column(ForeignKey("vendor_bills.id"), nullable=True, index=True)
    vendor_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    reason: Mapped[str | None] = mapped_column(String(160), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="AED")
    project: Mapped[str | None] = mapped_column(String(64), nullable=True)
    contract_reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    net_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    vat_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    grand_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    status: Mapped[str] = mapped_column(String(8), default="draft", index=True)
    journal_entry_id: Mapped[str | None] = mapped_column(ForeignKey("journal_entries.id"), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    retention_applicable: Mapped[bool] = mapped_column(default=False)
    retention_basis: Mapped[str] = mapped_column(String(8), default="net")
    retention_percent: Mapped[Decimal] = mapped_column(Numeric(9, 4), default=Decimal(0))
    retention_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    retention_account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    lines: Mapped[list["VendorCreditNoteLine"]] = relationship(
        back_populates="credit_note", cascade="all, delete-orphan", order_by="VendorCreditNoteLine.ordinal")


class VendorCreditNoteLine(Base):
    __tablename__ = "vendor_credit_note_lines"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    credit_note_id: Mapped[str] = mapped_column(ForeignKey("vendor_credit_notes.id", ondelete="CASCADE"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal(1))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    vat_rate: Mapped[Decimal] = mapped_column(Numeric(9, 4), default=Decimal(0))
    expense_account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    net_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    vat_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    line_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))

    credit_note: Mapped[VendorCreditNote] = relationship(back_populates="lines")


class CreditNoteApplication(Base):
    """Allocation of a posted credit note against an invoice (customer) or bill (vendor).
    Unapplied balance = credit note grand total − Σ applications."""

    __tablename__ = "credit_note_applications"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    cn_type: Mapped[str] = mapped_column(String(10), index=True)     # customer | vendor
    cn_id: Mapped[str] = mapped_column(String(32), index=True)
    target_type: Mapped[str] = mapped_column(String(16))            # sales_invoice | vendor_bill
    target_id: Mapped[str] = mapped_column(String(32), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# ── Corporate Tax: Provisional → SME-Validation review workflow ─────────────────────────────
class CTReview(Base):
    """A Corporate Tax computation captured for a period and taken through a mandatory
    SME-validation workflow before it may be treated as filing-ready. The computation is
    snapshotted at creation so the figures under review can't silently drift."""

    __tablename__ = "ct_reviews"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    # draft → provisional → sme_reviewed → validated ; plus rejected (rework)
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    snapshot_json: Mapped[str] = mapped_column(Text, default="{}")   # frozen CTComputation
    taxable_income: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    ct_payable: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    prepared_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)   # SME who signed off items
    validated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)  # SME who validated
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sme_name: Mapped[str | None] = mapped_column(String(128), nullable=True)     # named tax specialist
    sme_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    items: Mapped[list[CTReviewItem]] = relationship(
        back_populates="review", cascade="all, delete-orphan", order_by="CTReviewItem.ordinal")
    events: Mapped[list[CTReviewEvent]] = relationship(
        back_populates="review", cascade="all, delete-orphan", order_by="CTReviewEvent.at")


class CTReviewItem(Base):
    """One line of the computation the SME must individually sign off before validation."""

    __tablename__ = "ct_review_items"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    review_id: Mapped[str] = mapped_column(ForeignKey("ct_reviews.id", ondelete="CASCADE"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    line_key: Mapped[str] = mapped_column(String(64))
    line_label: Mapped[str] = mapped_column(String(160))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    requires_signoff: Mapped[bool] = mapped_column(default=True)
    signed_off: Mapped[bool] = mapped_column(default=False)
    signed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    review: Mapped[CTReview] = relationship(back_populates="items")


class CTReviewEvent(Base):
    """Immutable audit-trail entry for every workflow transition / sign-off action."""

    __tablename__ = "ct_review_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    review_id: Mapped[str] = mapped_column(ForeignKey("ct_reviews.id", ondelete="CASCADE"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    actor: Mapped[str] = mapped_column(String(64), default="local")
    action: Mapped[str] = mapped_column(String(32))          # created|submitted|signed_off|unsigned|reviewed|validated|rejected|reopened
    from_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    review: Mapped[CTReview] = relationship(back_populates="events")


# ── UAE E-Invoicing (provisional, configuration-driven compliance layer) ─────────────────────
class EInvoiceConfig(Base):
    """Singleton configuration for the UAE e-invoicing compliance layer (id fixed to 'einv').

    Every regulatory input — which transaction types are in scope, the mandatory field list,
    the schema identifier/version, and the accredited service provider — is stored here as
    editable configuration so it can be updated when the UAE MoF/FTA requirements change
    WITHOUT rebuilding the accounting engine or touching historical transactions.

    `provisional` defaults True: until each rule/schema is verified against the latest official
    UAE MoF/FTA documentation by a qualified SME, the module must not present its output as
    regulatory-compliant (see `EInvoice.regulatory_confirmed`)."""

    __tablename__ = "einvoice_config"

    id: Mapped[str] = mapped_column(String(8), primary_key=True, default="einv")
    enabled: Mapped[bool] = mapped_column(default=False)
    environment: Mapped[str] = mapped_column(String(12), default="sandbox")  # sandbox | production
    # Accredited Service Provider adapter key (modular — swap without changing the engine).
    provider: Mapped[str] = mapped_column(String(32), default="manual")
    provider_config_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # endpoint/credentials ref (never secrets in clear)
    # Structured-format identifier + schema version actually in force (loaded from official spec).
    schema_id: Mapped[str] = mapped_column(String(48), default="PINT-AE")
    schema_version: Mapped[str] = mapped_column(String(24), default="0.0-provisional")
    # Which accounting transaction types are e-invoiceable (CSV of source_type keys).
    applicable_types: Mapped[str] = mapped_column(String(200),
        default="sales_invoice,customer_cn")
    # Mandatory fields the internal validator enforces (JSON list of payload field keys).
    required_fields_json: Mapped[str] = mapped_column(Text, default="[]")
    # Regulatory provenance for the active ruleset — the source/version/date it was taken from.
    ruleset_version: Mapped[str] = mapped_column(String(32), default="v0-provisional")
    ruleset_source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ruleset_date: Mapped[str | None] = mapped_column(String(32), nullable=True)  # ISO date the source was published/checked
    provisional: Mapped[bool] = mapped_column(default=True)
    require_buyer_trn_b2b: Mapped[bool] = mapped_column(default=True)
    # SME sign-off of the ACTIVE ruleset (e.g. by a tax advisory firm such as Deloitte). Any edit
    # to the schema/rules re-arms `provisional` and clears this stamp — a rule change invalidates
    # a prior validation. This is separate from a document's `regulatory_confirmed` (ASP/FTA).
    sme_firm: Mapped[str | None] = mapped_column(String(160), nullable=True)
    sme_validator: Mapped[str | None] = mapped_column(String(160), nullable=True)
    sme_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sme_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class EInvoice(Base):
    """One e-invoice record, layered on top of an already-posted accounting transaction. It never
    creates accounting entries itself — the source transaction is the single source of the GL/VAT
    postings, so submission/resubmission can never duplicate the accounting. Exactly one live
    e-invoice exists per (source_type, source_id)."""

    __tablename__ = "einvoices"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    # sales_invoice | customer_cn | vendor_bill | vendor_cn | customer_advance | vendor_advance
    source_type: Mapped[str] = mapped_column(String(24), index=True)
    source_id: Mapped[str] = mapped_column(String(32), index=True)
    doc_number: Mapped[str | None] = mapped_column(String(48), index=True, nullable=True)
    direction: Mapped[str] = mapped_column(String(10), default="outbound")  # outbound | inbound
    doc_type_code: Mapped[str] = mapped_column(String(16), default="invoice")  # invoice|credit_note|debit_note|prepayment
    party_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    party_trn: Mapped[str | None] = mapped_column(String(32), nullable=True)
    party_class: Mapped[str | None] = mapped_column(String(4), nullable=True)  # b2b | b2c
    currency: Mapped[str] = mapped_column(String(3), default="AED")
    net_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    vat_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    grand_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    # Lifecycle: draft|validating|validation_failed|ready|submitted|accepted|rejected|
    #            cancelled|corrected|error|pending
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    # Internal checks passed — NOT a statement of legal/regulatory compliance.
    system_validation_passed: Mapped[bool] = mapped_column(default=False)
    # Only true once an external accredited service provider / FTA confirms acceptance.
    regulatory_confirmed: Mapped[bool] = mapped_column(default=False)
    provisional: Mapped[bool] = mapped_column(default=True)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)      # structured e-invoice (authoritative electronic record)
    validation_json: Mapped[str | None] = mapped_column(Text, nullable=True)   # [{field,code,message,hint,severity}]
    qr_json: Mapped[str | None] = mapped_column(Text, nullable=True)           # QR payload — only where the UAE spec requires it
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider_ref: Mapped[str | None] = mapped_column(String(96), nullable=True)  # ASP/network document id
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)     # last provider/network response
    # Regulatory provenance stamped at generation time (immutable per record).
    schema_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    schema_version: Mapped[str | None] = mapped_column(String(24), nullable=True)
    ruleset_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Correction / replacement relationships (Original → Adjustment → Corrected).
    original_einvoice_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    replaces_einvoice_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    events: Mapped[list["EInvoiceEvent"]] = relationship(
        back_populates="einvoice", cascade="all, delete-orphan", order_by="EInvoiceEvent.at")


class EInvoiceEvent(Base):
    """Immutable audit trail for every e-invoice action: creation, modification, validation,
    submission, acceptance, rejection, cancellation, resubmission, correction, and every raw
    API/service-provider response."""

    __tablename__ = "einvoice_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    einvoice_id: Mapped[str] = mapped_column(ForeignKey("einvoices.id", ondelete="CASCADE"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    actor: Mapped[str] = mapped_column(String(64), default="local")
    action: Mapped[str] = mapped_column(String(24))  # created|revalidated|submitted|accepted|rejected|cancelled|resubmitted|corrected|response|error
    from_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    einvoice: Mapped[EInvoice] = relationship(back_populates="events")


# ── Transaction document attachments ────────────────────────────────────────────────────────
class Attachment(Base):
    """A supporting document linked to a specific transaction. Polymorphic: `entity_type` +
    `entity_id` point at the owning record (journal_entry, sales_invoice, vendor_bill, ...).
    Soft-deleted (is_deleted) so removals stay in the audit trail rather than vanishing."""

    __tablename__ = "attachments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    entity_type: Mapped[str] = mapped_column(String(32), index=True)   # journal_entry|sales_invoice|vendor_bill|...
    entity_id: Mapped[str] = mapped_column(String(64), index=True)
    display_name: Mapped[str] = mapped_column(String(255))             # user-editable
    original_name: Mapped[str] = mapped_column(String(255))
    file_ext: Mapped[str] = mapped_column(String(12))                 # pdf|jpg|png|xlsx|xls|csv|docx|doc
    mime_type: Mapped[str] = mapped_column(String(128), default="application/octet-stream")
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    storage_path: Mapped[str] = mapped_column(String(512))
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    uploaded_by: Mapped[str] = mapped_column(String(64), default="local")
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    modified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    # workflow / intelligence
    review_status: Mapped[str] = mapped_column(String(16), default="pending")     # pending|reviewed
    extraction_status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|done|unsupported|failed
    match_status: Mapped[str] = mapped_column(String(16), default="unknown")       # unknown|matched|mismatch
    match_difference: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    extracted_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # soft delete
    is_deleted: Mapped[bool] = mapped_column(default=False, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    events: Mapped[list[AttachmentEvent]] = relationship(
        back_populates="attachment", cascade="all, delete-orphan", order_by="AttachmentEvent.at")


class AttachmentEvent(Base):
    """Immutable audit-trail entry for every attachment action (upload/view/download/rename/
    replace/review/delete)."""

    __tablename__ = "attachment_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    attachment_id: Mapped[str] = mapped_column(ForeignKey("attachments.id", ondelete="CASCADE"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    actor: Mapped[str] = mapped_column(String(64), default="local")
    action: Mapped[str] = mapped_column(String(24))    # uploaded|viewed|downloaded|renamed|replaced|reviewed|extracted|deleted
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    attachment: Mapped[Attachment] = relationship(back_populates="events")
