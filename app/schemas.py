"""Pydantic models for the API. Money is Decimal, serialised as strings in JSON."""

from __future__ import annotations

from datetime import date
from datetime import date as _Date  # alias for models whose field is literally named `date`
from decimal import Decimal

from pydantic import BaseModel, Field, model_validator

from .constants import AccountType, NormalBalance

CENTS = Decimal("0.01")


def q(amount: Decimal) -> Decimal:
    return Decimal(amount).quantize(CENTS)


# ── Accounts ────────────────────────────────────────────────────────────────────────────
class AccountIn(BaseModel):
    code: str = Field(..., min_length=1, max_length=32)
    name: str = Field(..., min_length=1, max_length=255)
    type: AccountType
    parent_code: str | None = None
    is_group: bool = False
    normal_balance: NormalBalance | None = None
    is_cost_of_sales: bool = False
    description: str | None = None


class AccountOut(BaseModel):
    id: str
    code: str
    name: str
    type: AccountType
    parent_id: str | None
    parent_code: str | None = None
    is_group: bool
    normal_balance: NormalBalance
    is_active: bool
    is_cost_of_sales: bool = False
    description: str | None = None


class AccountNode(AccountOut):
    children: list[AccountNode] = Field(default_factory=list)


# ── Journal entries ─────────────────────────────────────────────────────────────────────
class JournalLineIn(BaseModel):
    account_id: str
    debit: Decimal = Decimal(0)
    credit: Decimal = Decimal(0)
    description: str | None = None

    @model_validator(mode="after")
    def _validate_sides(self) -> JournalLineIn:
        if self.debit < 0 or self.credit < 0:
            raise ValueError("Debit and credit amounts must not be negative.")
        if self.debit > 0 and self.credit > 0:
            raise ValueError("A journal line cannot have both a debit and a credit.")
        if self.debit == 0 and self.credit == 0:
            raise ValueError("A journal line must have a non-zero debit or credit.")
        return self


class JournalEntryIn(BaseModel):
    date: date
    memo: str | None = None
    reference: str | None = None
    source: str = "manual"
    currency: str = "AED"
    lines: list[JournalLineIn] = Field(..., min_length=2)
    auto_post: bool = True

    @model_validator(mode="after")
    def _validate_balanced(self) -> JournalEntryIn:
        debits = sum((ln.debit for ln in self.lines), Decimal(0))
        credits = sum((ln.credit for ln in self.lines), Decimal(0))
        if q(debits) != q(credits):
            raise ValueError(f"Not balanced: debits {q(debits)} != credits {q(credits)}.")
        if q(debits) == 0:
            raise ValueError("Journal entry total must be greater than zero.")
        return self


class JournalLineOut(BaseModel):
    id: str
    account_id: str
    account_code: str | None = None
    account_name: str | None = None
    debit: Decimal
    credit: Decimal
    description: str | None = None


class JournalEntryOut(BaseModel):
    id: str
    entry_no: int
    date: date
    memo: str | None
    reference: str | None
    source: str
    currency: str
    status: str
    total: Decimal
    lines: list[JournalLineOut]
    created_at: str | None = None


class JournalEntrySummary(BaseModel):
    id: str
    entry_no: int
    date: date
    memo: str | None
    reference: str | None
    source: str
    status: str
    total: Decimal


# ── Ledger reports ──────────────────────────────────────────────────────────────────────
class TrialBalanceRow(BaseModel):
    account_id: str
    code: str
    name: str
    type: AccountType
    debit: Decimal
    credit: Decimal


class TrialBalance(BaseModel):
    as_of: date | None = None
    rows: list[TrialBalanceRow]
    total_debit: Decimal
    total_credit: Decimal
    balanced: bool


class LedgerRow(BaseModel):
    entry_id: str
    entry_no: int
    date: date
    memo: str | None
    reference: str | None
    source: str
    debit: Decimal
    credit: Decimal
    balance: Decimal


class GeneralLedger(BaseModel):
    account_id: str
    code: str
    name: str
    type: AccountType
    normal_balance: NormalBalance
    opening_balance: Decimal
    rows: list[LedgerRow]
    closing_balance: Decimal


# ── Financial statements ────────────────────────────────────────────────────────────────
class StatementLine(BaseModel):
    account_id: str
    code: str
    name: str
    amount: Decimal


class StatementSection(BaseModel):
    title: str
    lines: list[StatementLine]
    total: Decimal


class IncomeStatement(BaseModel):
    period_start: date | None = None
    period_end: date | None = None
    revenue: StatementSection
    cost_of_sales: StatementSection
    gross_profit: Decimal
    operating_expenses: StatementSection
    total_income: Decimal
    total_expenses: Decimal
    net_profit: Decimal


class BalanceSheet(BaseModel):
    as_of: date | None = None
    assets: StatementSection
    liabilities: StatementSection
    equity: StatementSection
    current_earnings: Decimal
    total_assets: Decimal
    total_liabilities: Decimal
    total_equity: Decimal
    balanced: bool


class CashFlowStatement(BaseModel):
    period_start: date | None = None
    period_end: date | None = None
    opening_cash: Decimal
    lines: list[StatementLine]
    net_change: Decimal
    closing_cash: Decimal


class DashboardKPIs(BaseModel):
    cash: Decimal
    bank: Decimal
    accounts_receivable: Decimal
    accounts_payable: Decimal
    revenue_ytd: Decimal
    expenses_ytd: Decimal
    gross_profit_ytd: Decimal
    net_profit_ytd: Decimal
    vat_payable: Decimal
    vat_recoverable: Decimal
    outstanding_invoices: int
    overdue_invoices: int
    ar_total: Decimal


# ── Sales ───────────────────────────────────────────────────────────────────────────────
class CustomerIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    trn: str | None = None
    email: str | None = None
    phone: str | None = None
    contact_name: str | None = None
    address: str | None = None
    billing_address: str | None = None
    shipping_address: str | None = None
    payment_terms: str | None = None
    credit_limit: Decimal = Decimal(0)
    currency: str = "AED"
    # E-invoicing party master data
    country: str = "AE"
    tax_status: str = "unknown"      # registered | not_registered | unknown
    party_type: str = "b2b"          # b2b | b2c
    einvoice_scheme: str | None = None
    einvoice_id: str | None = None
    notes: str | None = None


class CustomerOut(BaseModel):
    id: str
    name: str
    trn: str | None
    email: str | None
    phone: str | None
    contact_name: str | None = None
    address: str | None
    billing_address: str | None = None
    shipping_address: str | None = None
    payment_terms: str | None = None
    credit_limit: Decimal = Decimal(0)
    currency: str
    country: str = "AE"
    tax_status: str = "unknown"
    party_type: str = "b2b"
    einvoice_scheme: str | None = None
    einvoice_id: str | None = None
    notes: str | None
    is_active: bool
    warnings: list[str] = Field(default_factory=list)
    created_at: str | None = None


class InvoiceLineIn(BaseModel):
    description: str = ""
    quantity: Decimal = Decimal(1)
    unit_price: Decimal = Decimal(0)
    vat_rate: Decimal = Decimal("0.05")
    vat_treatment: str | None = None      # VAT treatment code (SR/ZR/EX/OS/RC/…); derived if omitted
    revenue_account_id: str | None = None


class InvoiceMetaIn(BaseModel):
    """Non-financial invoice fields, editable even after payment."""
    due_date: date | None = None
    project: str | None = None
    department: str | None = None
    cost_center: str | None = None
    salesperson: str | None = None
    notes: str | None = None


class BillMetaIn(BaseModel):
    """Non-financial bill fields, editable even after payment."""
    due_date: date | None = None
    vendor_ref: str | None = None
    payment_terms: str | None = None
    department: str | None = None
    project: str | None = None
    cost_center: str | None = None
    expense_category: str | None = None
    notes: str | None = None


class CnMetaIn(BaseModel):
    """Non-financial credit-note fields, editable even after the note is applied."""
    reason: str | None = None
    description: str | None = None
    project: str | None = None
    contract_reference: str | None = None
    vendor_ref: str | None = None
    notes: str | None = None


class RecurringPlanIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=160)
    customer_id: str
    frequency: str = "monthly"            # weekly | monthly | quarterly | annual
    currency: str = "AED"
    start_date: date
    max_occurrences: int | None = None
    auto_post: bool | None = True
    lines: list[InvoiceLineIn] = Field(default_factory=list)
    notes: str | None = None


class InvoiceIn(BaseModel):
    customer_id: str
    date: date
    due_date: date | None = None
    currency: str = "AED"
    # AED per 1 unit of `currency`; when omitted for a non-AED invoice the latest rate is used.
    exchange_rate: Decimal | None = None
    lines: list[InvoiceLineIn] = Field(..., min_length=1)
    notes: str | None = None
    project: str | None = None
    department: str | None = None
    cost_center: str | None = None
    salesperson: str | None = None
    sales_category: str | None = None
    # Retention (holdback)
    retention_applicable: bool = False
    retention_basis: str = "net"          # net | gross | amount
    retention_percent: Decimal = Decimal(0)
    retention_amount: Decimal = Decimal(0)
    retention_reference: str | None = None
    retention_release_date: _Date | None = None
    retention_account_id: str | None = None
    contract_reference: str | None = None
    auto_post: bool = True


class RetentionReleaseIn(BaseModel):
    amount: Decimal
    date: _Date
    to_bank: bool = False                 # False → move to AR/AP; True → settle via bank
    reference: str | None = None


class InvoiceLineOut(BaseModel):
    id: str
    description: str
    quantity: Decimal
    unit_price: Decimal
    vat_rate: Decimal
    vat_treatment: str | None = None
    revenue_account_id: str
    revenue_account_code: str | None = None
    net_amount: Decimal
    vat_amount: Decimal
    line_total: Decimal


class InvoiceOut(BaseModel):
    id: str
    number: str
    customer_id: str
    customer_name: str | None = None
    date: date
    due_date: date | None
    currency: str
    exchange_rate: Decimal = Decimal(1)
    status: str
    net_total: Decimal
    vat_total: Decimal
    grand_total: Decimal
    base_grand_total: Decimal = Decimal(0)  # grand total converted to base (AED)
    amount_paid: Decimal
    balance_due: Decimal
    journal_entry_id: str | None
    notes: str | None
    lines: list[InvoiceLineOut]
    # Retention
    retention_applicable: bool = False
    retention_basis: str = "net"
    retention_amount: Decimal = Decimal(0)
    retention_released: Decimal = Decimal(0)
    retention_outstanding: Decimal = Decimal(0)
    retention_reference: str | None = None
    retention_release_date: _Date | None = None
    contract_reference: str | None = None
    created_at: str | None = None


class InvoiceSummary(BaseModel):
    id: str
    number: str
    customer_id: str
    customer_name: str | None = None
    date: date
    due_date: date | None
    status: str
    grand_total: Decimal
    amount_paid: Decimal
    balance_due: Decimal


class PaymentIn(BaseModel):
    invoice_id: str
    date: date
    amount: Decimal = Field(..., gt=0)   # in the invoice's currency
    method: str = "bank"
    deposit_account_id: str | None = None
    reference: str | None = None
    # AED per 1 unit of the invoice currency at the payment date (defaults to latest rate).
    exchange_rate: Decimal | None = None


class CurrencyIn(BaseModel):
    code: str = Field(..., min_length=3, max_length=3)
    name: str
    symbol: str | None = None


class CurrencyOut(BaseModel):
    id: str
    code: str
    name: str
    symbol: str | None
    is_base: bool
    is_active: bool
    latest_rate: Decimal | None = None


class ExchangeRateIn(BaseModel):
    currency_code: str = Field(..., min_length=3, max_length=3)
    date: date
    rate: Decimal = Field(..., gt=0)


class ExchangeRateOut(BaseModel):
    id: str
    currency_code: str
    date: date
    rate: Decimal


class PaymentOut(BaseModel):
    id: str
    customer_id: str
    invoice_id: str | None
    date: date
    amount: Decimal
    method: str
    deposit_account_id: str
    reference: str | None
    journal_entry_id: str | None
    created_at: str | None = None


class AgingLine(BaseModel):
    """A single overdue document (invoice or bill) within an aging report — the drill-down."""
    doc_id: str
    number: str
    party_ref: str | None = None  # vendor's invoice no. where relevant
    date: date
    due_date: date | None
    original: Decimal
    paid: Decimal
    outstanding: Decimal
    days_overdue: int
    bucket: str


class CustomerAging(BaseModel):
    customer_id: str
    customer_name: str
    current: Decimal
    d1_30: Decimal
    d31_60: Decimal
    d61_90: Decimal
    d91_120: Decimal
    d120_plus: Decimal
    total: Decimal
    overdue: Decimal
    risk: str  # low | medium | high
    lines: list[AgingLine] = Field(default_factory=list)


class ArAgingReport(BaseModel):
    as_of: date
    currency: str = "AED"
    generated_at: str | None = None
    rows: list[CustomerAging]
    current: Decimal
    d1_30: Decimal
    d31_60: Decimal
    d61_90: Decimal
    d91_120: Decimal
    d120_plus: Decimal
    total: Decimal
    total_current: Decimal
    total_overdue: Decimal


# ── Accounts Payable aging ──────────────────────────────────────────────────────────────
class VendorAging(BaseModel):
    vendor_id: str
    vendor_name: str
    current: Decimal
    d1_30: Decimal
    d31_60: Decimal
    d61_90: Decimal
    d91_120: Decimal
    d120_plus: Decimal
    total: Decimal
    overdue: Decimal
    lines: list[AgingLine] = Field(default_factory=list)


class ApAgingReport(BaseModel):
    as_of: date
    currency: str = "AED"
    generated_at: str | None = None
    rows: list[VendorAging]
    current: Decimal
    d1_30: Decimal
    d31_60: Decimal
    d61_90: Decimal
    d91_120: Decimal
    d120_plus: Decimal
    total: Decimal
    total_current: Decimal
    total_overdue: Decimal


# ── Vendors / bills ─────────────────────────────────────────────────────────────────────
class VendorIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    trn: str | None = None
    email: str | None = None
    phone: str | None = None
    contact_name: str | None = None
    address: str | None = None
    billing_address: str | None = None
    currency: str = "AED"
    payment_terms: str | None = None
    # E-invoicing party master data
    country: str = "AE"
    tax_status: str = "unknown"
    party_type: str = "b2b"
    einvoice_scheme: str | None = None
    einvoice_id: str | None = None
    notes: str | None = None


class VendorOut(BaseModel):
    id: str
    name: str
    trn: str | None
    email: str | None
    phone: str | None
    contact_name: str | None = None
    address: str | None
    billing_address: str | None = None
    currency: str
    payment_terms: str | None
    country: str = "AE"
    tax_status: str = "unknown"
    party_type: str = "b2b"
    einvoice_scheme: str | None = None
    einvoice_id: str | None = None
    notes: str | None
    is_active: bool
    warnings: list[str] = Field(default_factory=list)
    created_at: str | None = None


class BillLineIn(BaseModel):
    description: str = ""
    quantity: Decimal = Decimal(1)
    unit_price: Decimal = Decimal(0)
    vat_rate: Decimal = Decimal("0.05")
    vat_treatment: str | None = None
    expense_account_id: str  # expense, inventory or fixed-asset account this line debits


class BillIn(BaseModel):
    vendor_id: str
    date: date
    due_date: date | None = None
    vendor_ref: str | None = None
    payment_terms: str | None = None
    currency: str = "AED"
    lines: list[BillLineIn] = Field(..., min_length=1)
    notes: str | None = None
    project: str | None = None
    department: str | None = None
    cost_center: str | None = None
    expense_category: str | None = None
    retention_applicable: bool = False
    retention_basis: str = "net"
    retention_percent: Decimal = Decimal(0)
    retention_amount: Decimal = Decimal(0)
    retention_reference: str | None = None
    retention_release_date: _Date | None = None
    retention_account_id: str | None = None
    contract_reference: str | None = None
    auto_post: bool = True


class BillLineOut(BaseModel):
    id: str
    description: str
    quantity: Decimal
    unit_price: Decimal
    vat_rate: Decimal
    vat_treatment: str | None = None
    expense_account_id: str
    expense_account_code: str | None = None
    expense_account_name: str | None = None
    net_amount: Decimal
    vat_amount: Decimal
    line_total: Decimal


class BillOut(BaseModel):
    id: str
    number: str
    vendor_ref: str | None
    vendor_id: str
    vendor_name: str | None = None
    vendor_trn: str | None = None
    date: date
    due_date: date | None
    payment_terms: str | None
    currency: str
    status: str
    net_total: Decimal
    vat_total: Decimal
    grand_total: Decimal
    amount_paid: Decimal
    balance_due: Decimal
    journal_entry_id: str | None
    notes: str | None
    project: str | None
    department: str | None
    cost_center: str | None
    expense_category: str | None
    lines: list[BillLineOut]
    retention_applicable: bool = False
    retention_basis: str = "net"
    retention_amount: Decimal = Decimal(0)
    retention_released: Decimal = Decimal(0)
    retention_outstanding: Decimal = Decimal(0)
    retention_reference: str | None = None
    retention_release_date: _Date | None = None
    contract_reference: str | None = None
    created_at: str | None = None


class BillSummary(BaseModel):
    id: str
    number: str
    vendor_ref: str | None
    vendor_id: str
    vendor_name: str | None = None
    date: date
    due_date: date | None
    status: str
    grand_total: Decimal
    amount_paid: Decimal
    balance_due: Decimal


class BillPaymentIn(BaseModel):
    bill_id: str
    date: date
    amount: Decimal = Field(..., gt=0)
    method: str = "bank"
    payment_account_id: str | None = None
    reference: str | None = None


class BillPaymentOut(BaseModel):
    id: str
    vendor_id: str
    bill_id: str | None
    date: date
    amount: Decimal
    method: str
    payment_account_id: str
    reference: str | None
    journal_entry_id: str | None
    created_at: str | None = None


# ── Invoice-by-vendor / sales-by-customer reports ───────────────────────────────────────
class DocReportRow(BaseModel):
    party_id: str
    party_name: str
    party_trn: str | None = None
    doc_id: str
    number: str
    ref: str | None = None
    date: date
    due_date: date | None = None
    description: str | None = None
    net: Decimal
    vat: Decimal
    gross: Decimal
    paid: Decimal
    outstanding: Decimal
    status: str
    currency: str
    project: str | None = None
    category: str | None = None


class DocReportGroup(BaseModel):
    key: str
    label: str
    net: Decimal
    vat: Decimal
    gross: Decimal
    paid: Decimal
    outstanding: Decimal
    count: int


class DocReport(BaseModel):
    title: str
    as_of: str | None = None
    generated_at: str | None = None
    currency: str = "AED"
    period_start: date | None = None
    period_end: date | None = None
    group_by: str
    groups: list[DocReportGroup]
    rows: list[DocReportRow]
    # KPIs
    total_documents: int
    total_net: Decimal
    total_vat: Decimal
    total_gross: Decimal
    total_paid: Decimal
    total_outstanding: Decimal
    total_overdue: Decimal


# ── Fixed assets ────────────────────────────────────────────────────────────────────────
class AssetIn(BaseModel):
    asset_code: str | None = None  # auto-generated when omitted
    name: str = Field(..., min_length=1, max_length=255)
    category: str | None = None
    description: str | None = None
    purchase_date: date
    in_service_date: date | None = None
    supplier: str | None = None
    vendor_id: str | None = None
    invoice_number: str | None = None
    bill_id: str | None = None
    purchase_cost: Decimal = Field(..., ge=0)
    vat_amount: Decimal = Decimal(0)
    currency: str = "AED"
    residual_value: Decimal = Decimal(0)
    method: str = "straight_line"  # straight_line | declining_balance
    useful_life_months: int = Field(60, gt=0)
    declining_rate: Decimal = Decimal(0)  # annual %, e.g. 0.20
    depreciation_start: date | None = None
    location: str | None = None
    department: str | None = None
    project: str | None = None
    cost_center: str | None = None
    responsible_person: str | None = None
    serial_number: str | None = None
    warranty_info: str | None = None
    # When true, post an acquisition entry Dr Fixed Assets / Cr credit account.
    auto_post_acquisition: bool = False
    acquisition_credit_code: str = "1020"  # bank by default (or "2010" AP)


class AssetTransactionOut(BaseModel):
    id: str
    date: date
    type: str
    amount: Decimal
    detail: str | None
    journal_entry_id: str | None
    created_at: str | None = None


class AssetOut(BaseModel):
    id: str
    asset_code: str
    name: str
    category: str | None
    description: str | None
    purchase_date: date
    in_service_date: date | None
    supplier: str | None
    vendor_id: str | None = None
    vendor_name: str | None = None
    vendor_trn: str | None = None
    vendor_address: str | None = None
    invoice_number: str | None
    bill_id: str | None
    purchase_cost: Decimal
    vat_amount: Decimal
    total_cost: Decimal
    warnings: list[str] = Field(default_factory=list)
    currency: str
    residual_value: Decimal
    method: str
    useful_life_months: int
    declining_rate: Decimal
    depreciation_start: date | None
    accumulated_depreciation: Decimal
    net_book_value: Decimal
    remaining_life_months: int
    last_depreciation_date: date | None
    location: str | None
    department: str | None
    project: str | None
    cost_center: str | None
    responsible_person: str | None
    serial_number: str | None
    warranty_info: str | None
    status: str
    disposal_date: date | None
    disposal_proceeds: Decimal
    disposal_gain_loss: Decimal | None
    transactions: list[AssetTransactionOut] = Field(default_factory=list)


class DepreciationScheduleRow(BaseModel):
    period: str  # YYYY-MM
    depreciation: Decimal
    accumulated: Decimal
    net_book_value: Decimal


class DisposalIn(BaseModel):
    disposal_date: date
    proceeds: Decimal = Decimal(0)
    proceeds_account_code: str = "1020"  # bank/cash the proceeds land in
    method: str = "disposal"  # disposal | write_off | retirement
    detail: str | None = None


class RunDepreciationIn(BaseModel):
    as_of: date


class AssetRegisterRow(BaseModel):
    id: str
    asset_code: str
    name: str
    category: str | None
    purchase_date: date
    total_cost: Decimal
    accumulated_depreciation: Decimal
    net_book_value: Decimal
    status: str
    location: str | None = None
    department: str | None = None


class AssetRegister(BaseModel):
    as_of: date | None = None
    generated_at: str | None = None
    currency: str = "AED"
    rows: list[AssetRegisterRow]
    total_cost: Decimal
    total_accumulated_depreciation: Decimal
    total_net_book_value: Decimal
    count: int


class NameValue(BaseModel):
    name: str
    value: Decimal
    count: int = 0


class AssetDashboard(BaseModel):
    total_cost: Decimal
    accumulated_depreciation: Decimal
    net_book_value: Decimal
    current_year_depreciation: Decimal
    asset_count: int
    active_count: int
    disposed_count: int
    by_category: list[NameValue]
    by_location: list[NameValue]
    by_department: list[NameValue]
    approaching_end_of_life: list[AssetRegisterRow]
    recently_acquired: list[AssetRegisterRow]
    disposed: list[AssetRegisterRow]


# ── Banking ─────────────────────────────────────────────────────────────────────────────
class BankAccountIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    # Link to an existing GL account by code, or leave blank to auto-create one under Assets.
    gl_account_code: str | None = None
    bank_name: str | None = None
    account_name: str | None = None
    account_number: str | None = None
    iban: str | None = None
    swift: str | None = None
    branch: str | None = None
    currency: str = "AED"


class BankAccountUpdateIn(BaseModel):
    """Editable bank-account fields. The GL account link is immutable so historical bank
    movements and reconciliations are preserved exactly."""
    name: str = Field(..., min_length=1, max_length=255)
    bank_name: str | None = None
    account_name: str | None = None
    account_number: str | None = None
    iban: str | None = None
    swift: str | None = None
    branch: str | None = None
    currency: str = "AED"
    is_active: bool = True


class BankAccountOut(BaseModel):
    id: str
    name: str
    gl_account_id: str
    gl_account_code: str | None = None
    gl_account_name: str | None = None
    bank_name: str | None
    account_name: str | None = None
    account_number: str | None
    iban: str | None
    swift: str | None = None
    branch: str | None = None
    currency: str
    is_active: bool
    balance: Decimal
    created_at: str | None = None


class TransferIn(BaseModel):
    from_bank_account_id: str
    to_bank_account_id: str
    date: date
    amount: Decimal = Field(..., gt=0)
    reference: str | None = None
    memo: str | None = None


class StatementLineIn(BaseModel):
    date: date
    description: str | None = None
    amount: Decimal  # signed: + money in, - money out
    reference: str | None = None


class StatementImportIn(BaseModel):
    bank_account_id: str
    lines: list[StatementLineIn] = Field(..., min_length=1)


class StatementLineOut(BaseModel):
    id: str
    bank_account_id: str
    date: date
    description: str | None
    reference: str | None
    amount: Decimal
    status: str
    matched_entry_id: str | None
    matched_entry_no: int | None = None
    created_at: str | None = None


class MatchCandidate(BaseModel):
    entry_id: str
    entry_no: int
    date: date
    memo: str | None
    reference: str | None
    amount: Decimal  # signed on the bank account (+ in / - out)
    source: str


class ManualMatchIn(BaseModel):
    statement_line_id: str
    entry_id: str


class AutoMatchIn(BaseModel):
    bank_account_id: str
    window_days: int = 5


class ReconcileItem(BaseModel):
    entry_id: str
    entry_no: int
    date: date
    memo: str | None
    amount: Decimal
    kind: str  # deposit_in_transit | outstanding_payment


class ReconcileSummaryIn(BaseModel):
    bank_account_id: str
    statement_date: date
    statement_balance: Decimal


class ReconcileSummary(BaseModel):
    bank_account_id: str
    statement_date: date
    statement_balance: Decimal
    book_balance: Decimal
    cleared_total: Decimal
    deposits_in_transit: Decimal
    outstanding_payments: Decimal
    unmatched_statement_count: int
    difference: Decimal
    reconciled: bool
    deposits: list[ReconcileItem]
    payments: list[ReconcileItem]
    unmatched_statement: list[StatementLineOut]


class ReconciliationOut(BaseModel):
    id: str
    bank_account_id: str
    bank_account_name: str | None = None
    statement_date: date
    statement_balance: Decimal
    book_balance: Decimal
    difference: Decimal
    cleared_count: int
    status: str
    created_at: str | None = None


# ── Inventory ───────────────────────────────────────────────────────────────────────────
class WarehouseIn(BaseModel):
    code: str | None = None
    name: str = Field(..., min_length=1, max_length=255)
    location: str | None = None


class WarehouseOut(BaseModel):
    id: str
    code: str
    name: str
    location: str | None
    is_active: bool


class ProductIn(BaseModel):
    sku: str | None = None
    name: str = Field(..., min_length=1, max_length=255)
    type: str = "product"  # product | service
    category: str | None = None
    unit: str = "unit"
    cost_method: str = "weighted_average"  # weighted_average | fifo
    sales_price: Decimal = Decimal(0)
    purchase_cost: Decimal = Decimal(0)
    reorder_level: Decimal = Decimal(0)
    track_inventory: bool = True
    is_active: bool = True


class ProductOut(BaseModel):
    id: str
    sku: str
    name: str
    type: str
    category: str | None
    unit: str
    cost_method: str
    sales_price: Decimal
    purchase_cost: Decimal
    reorder_level: Decimal
    track_inventory: bool
    is_active: bool
    on_hand: Decimal
    stock_value: Decimal
    avg_cost: Decimal


class MovementIn(BaseModel):
    product_id: str
    warehouse_id: str
    date: date
    movement_type: str  # receipt | issue | adjustment | opening
    quantity: Decimal = Field(..., gt=0)  # magnitude; direction from movement_type/sign below
    # For receipts/opening/positive adjustments: unit_cost required. For issues it is computed.
    unit_cost: Decimal | None = None
    # For adjustments: true = increase stock, false = decrease.
    increase: bool = True
    reference: str | None = None
    batch_number: str | None = None
    serial_number: str | None = None
    # Credit account for a receipt (default AP) / posting counter-account.
    credit_account_code: str | None = None


class MovementOut(BaseModel):
    id: str
    product_id: str
    product_name: str | None = None
    warehouse_id: str
    warehouse_name: str | None = None
    date: date
    movement_type: str
    quantity: Decimal
    unit_cost: Decimal
    total_cost: Decimal
    reference: str | None
    source: str
    batch_number: str | None
    serial_number: str | None
    journal_entry_id: str | None
    created_at: str | None = None


class ValuationRow(BaseModel):
    product_id: str
    sku: str
    name: str
    category: str | None
    cost_method: str
    on_hand: Decimal
    avg_cost: Decimal
    stock_value: Decimal


class InventoryValuation(BaseModel):
    as_of: date | None = None
    generated_at: str | None = None
    rows: list[ValuationRow]
    total_value: Decimal
    gl_inventory_balance: Decimal
    in_sync: bool


class LowStockRow(BaseModel):
    product_id: str
    sku: str
    name: str
    on_hand: Decimal
    reorder_level: Decimal
    shortfall: Decimal


# ── Drill-down engine ───────────────────────────────────────────────────────────────────
class DrillLink(BaseModel):
    type: str  # journal | invoice | bill | asset | product
    id: str


class DrillColumn(BaseModel):
    key: str
    label: str
    numeric: bool = False


class DrillRow(BaseModel):
    cells: dict[str, str]
    amount: Decimal            # signed contribution to the KPI total
    link: DrillLink | None = None


class DrillDown(BaseModel):
    key: str
    title: str
    kind: str                  # amount | count
    kpi_value: Decimal         # the dashboard figure this drill explains
    computed_total: Decimal    # total derived from the rows below
    reconciles: bool           # computed_total ties to kpi_value
    count: int
    period_start: date | None = None
    period_end: date | None = None
    columns: list[DrillColumn]
    rows: list[DrillRow]
    note: str | None = None


class DrillKey(BaseModel):
    key: str
    title: str
    kind: str


# ── Payroll ─────────────────────────────────────────────────────────────────────────────
class EmployeeIn(BaseModel):
    code: str | None = None
    name: str = Field(..., min_length=1, max_length=255)
    department: str | None = None
    designation: str | None = None
    join_date: date | None = None
    basic_salary: Decimal = Decimal(0)
    housing_allowance: Decimal = Decimal(0)
    transport_allowance: Decimal = Decimal(0)
    other_allowance: Decimal = Decimal(0)
    iban: str | None = None
    currency: str = "AED"
    is_active: bool = True


class EmployeeOut(BaseModel):
    id: str
    code: str
    name: str
    department: str | None
    designation: str | None
    join_date: date | None
    basic_salary: Decimal
    housing_allowance: Decimal
    transport_allowance: Decimal
    other_allowance: Decimal
    gross_salary: Decimal
    iban: str | None
    currency: str
    is_active: bool


class PayrollAdjustment(BaseModel):
    employee_id: str
    overtime: Decimal = Decimal(0)
    deductions: Decimal = Decimal(0)


class PayrollRunIn(BaseModel):
    period_label: str = Field(..., min_length=1, max_length=32)
    period_start: date | None = None
    period_end: date | None = None
    pay_date: date
    accrue_eosb: bool = True
    adjustments: list[PayrollAdjustment] = Field(default_factory=list)
    auto_post: bool = True


class PayslipOut(BaseModel):
    id: str
    employee_id: str
    employee_code: str | None = None
    employee_name: str | None = None
    basic: Decimal
    allowances: Decimal
    overtime: Decimal
    gross: Decimal
    deductions: Decimal
    net: Decimal
    eosb_accrual: Decimal


class PayrollRunOut(BaseModel):
    id: str
    period_label: str
    period_start: date | None
    period_end: date | None
    pay_date: date
    accrue_eosb: bool
    status: str
    gross_total: Decimal
    deductions_total: Decimal
    net_total: Decimal
    eosb_total: Decimal
    journal_entry_id: str | None
    payment_entry_id: str | None
    payslips: list[PayslipOut]
    created_at: str | None = None


class PayrollRunSummary(BaseModel):
    id: str
    period_label: str
    pay_date: date
    status: str
    gross_total: Decimal
    net_total: Decimal
    eosb_total: Decimal
    employee_count: int


# ── Product movement reports (sales / purchases by product) ─────────────────────────────
class ProductRow(BaseModel):
    product_id: str
    sku: str
    name: str
    category: str | None = None
    quantity: Decimal
    value: Decimal
    movements: int


class ProductReport(BaseModel):
    title: str
    direction: str            # issue (sales) | receipt (purchases)
    period_start: _Date | None = None
    period_end: _Date | None = None
    generated_at: str | None = None
    rows: list[ProductRow]
    total_quantity: Decimal
    total_value: Decimal


# ── Corporate Tax (UAE CT) ──────────────────────────────────────────────────────────────
class CTLine(BaseModel):
    label: str
    amount: Decimal
    kind: str = "step"        # step | subtotal | total | rate
    note: str | None = None


class CTComputation(BaseModel):
    period_start: _Date | None = None
    period_end: _Date | None = None
    generated_at: str | None = None
    currency: str = "AED"
    accounting_profit: Decimal
    non_deductible_addbacks: Decimal
    exempt_income: Decimal
    taxable_income_before_reliefs: Decimal
    tax_losses_utilised: Decimal
    taxable_income: Decimal
    threshold: Decimal
    standard_rate: Decimal
    ct_payable: Decimal
    effective_rate: Decimal
    status: str = "provisional"
    legal_ref: str
    lines: list[CTLine]
    notes: list[str] = Field(default_factory=list)
    # reconciliation: accounting profit ties to the P&L net profit
    pl_net_profit: Decimal
    reconciled: bool


# ── Corporate Tax: SME-validation review workflow ───────────────────────────────────────────
class CTReviewCreate(BaseModel):
    period_start: _Date | None = None
    period_end: _Date | None = None
    prepared_by: str | None = None


class CTSignOffIn(BaseModel):
    signed_off: bool = True
    note: str | None = None


class CTTransitionIn(BaseModel):
    """Payload for submit / mark-reviewed / validate / reject / reopen actions."""
    sme_name: str | None = None       # required by the service on 'validate'
    note: str | None = None


class CTReviewItemOut(BaseModel):
    id: str
    ordinal: int
    line_key: str
    line_label: str
    amount: Decimal
    requires_signoff: bool
    signed_off: bool
    signed_by: str | None = None
    signed_at: str | None = None
    note: str | None = None


class CTReviewEventOut(BaseModel):
    at: str
    actor: str
    action: str
    from_status: str | None = None
    to_status: str | None = None
    note: str | None = None


class CTReviewSummary(BaseModel):
    id: str
    period_start: _Date | None = None
    period_end: _Date | None = None
    status: str
    status_label: str
    taxable_income: Decimal
    ct_payable: Decimal
    prepared_by: str | None = None
    validated_by: str | None = None
    validated_at: str | None = None
    sme_name: str | None = None
    can_file: bool
    signed_count: int
    signoff_total: int
    created_at: str | None = None
    updated_at: str | None = None


class CTReviewDetail(CTReviewSummary):
    sme_note: str | None = None
    computation: CTComputation
    items: list[CTReviewItemOut] = Field(default_factory=list)
    events: list[CTReviewEventOut] = Field(default_factory=list)
    # workflow affordances for the UI
    allowed_actions: list[str] = Field(default_factory=list)
    all_signed_off: bool = False


# ── Transaction document attachments ─────────────────────────────────────────────────────
class AttachmentOut(BaseModel):
    id: str
    entity_type: str
    entity_id: str
    display_name: str
    original_name: str
    file_ext: str
    mime_type: str
    file_size: int
    sha256: str | None = None
    uploaded_by: str
    uploaded_at: str | None = None
    modified_at: str | None = None
    review_status: str
    extraction_status: str
    match_status: str
    match_difference: Decimal | None = None
    extracted: dict = Field(default_factory=dict)
    transaction_amount: Decimal | None = None
    can_preview: bool = False


class AttachmentEventOut(BaseModel):
    at: str | None = None
    actor: str
    action: str
    note: str | None = None


class AttachmentStatusOut(BaseModel):
    code: str
    label: str
    count: int
    mismatch: int
    pending: int


class ExpenseIn(BaseModel):
    date: _Date
    reference: str | None = None
    vendor_id: str | None = None
    payee_name: str | None = None
    category: str | None = None
    description: str | None = None
    project: str | None = None
    cost_center: str | None = None
    currency: str = "AED"
    expense_account_id: str
    payment_account_id: str | None = None
    payment_method: str = "bank"
    paid_directly: bool = True
    net_amount: Decimal
    vat_rate: Decimal = Decimal("0.05")
    notes: str | None = None
    auto_post: bool = True


class CustomerAdvanceIn(BaseModel):
    customer_id: str
    date: _Date
    amount: Decimal
    reference: str | None = None
    currency: str = "AED"
    vat_applicable: bool = False           # UAE tax point on advance receipt — SME validation required
    vat_rate: Decimal = Decimal(0)
    deposit_account_id: str | None = None
    advance_account_id: str | None = None
    project: str | None = None
    contract_reference: str | None = None
    notes: str | None = None


class VendorAdvanceIn(BaseModel):
    vendor_id: str
    date: _Date
    amount: Decimal
    reference: str | None = None
    currency: str = "AED"
    vat_applicable: bool = False           # SME validation required
    vat_rate: Decimal = Decimal(0)
    payment_account_id: str | None = None
    advance_account_id: str | None = None
    project: str | None = None
    contract_reference: str | None = None
    notes: str | None = None


class AdvanceApplyIn(BaseModel):
    advance_type: str            # customer | vendor
    advance_id: str
    target_id: str               # invoice id (customer) or bill id (vendor)
    amount: Decimal
    date: _Date


class CreditNoteLineIn(BaseModel):
    description: str | None = None
    quantity: Decimal = Decimal(1)
    unit_price: Decimal = Decimal(0)
    vat_rate: Decimal = Decimal("0.05")
    revenue_account_id: str | None = None    # customer CN (defaults to Sales)
    expense_account_id: str | None = None    # vendor CN


class CustomerCreditNoteIn(BaseModel):
    customer_id: str
    date: _Date
    reason: str
    invoice_id: str | None = None
    description: str | None = None
    currency: str = "AED"
    project: str | None = None
    contract_reference: str | None = None
    lines: list[CreditNoteLineIn] = Field(..., min_length=1)
    notes: str | None = None
    retention_applicable: bool = False
    retention_basis: str = "net"
    retention_percent: Decimal = Decimal(0)
    retention_amount: Decimal = Decimal(0)
    retention_account_id: str | None = None
    auto_post: bool = True


class VendorCreditNoteIn(BaseModel):
    vendor_id: str
    date: _Date
    reason: str
    bill_id: str | None = None
    vendor_ref: str | None = None
    description: str | None = None
    currency: str = "AED"
    project: str | None = None
    contract_reference: str | None = None
    lines: list[CreditNoteLineIn] = Field(..., min_length=1)
    notes: str | None = None
    retention_applicable: bool = False
    retention_basis: str = "net"
    retention_percent: Decimal = Decimal(0)
    retention_amount: Decimal = Decimal(0)
    retention_account_id: str | None = None
    auto_post: bool = True


class CreditNoteApplyIn(BaseModel):
    cn_type: str                 # customer | vendor
    cn_id: str
    target_id: str
    amount: Decimal
    date: _Date


class BudgetLineIn(BaseModel):
    account_id: str
    cost_center: str | None = None
    month: int = 0            # 0 = full year, 1-12
    amount: Decimal = Decimal(0)


class BudgetIn(BaseModel):
    name: str
    fiscal_year: int
    version: str = "v1"
    scope: str = "company"    # company | project
    project_code: str | None = None
    period_type: str = "annual"
    currency: str = "AED"
    owner: str | None = None
    notes: str | None = None
    lines: list[BudgetLineIn] = Field(default_factory=list)


class BudgetLinesIn(BaseModel):
    lines: list[BudgetLineIn] = Field(default_factory=list)


class BudgetRevisionIn(BaseModel):
    new_version: str
    reason: str | None = None


class ProjectIn(BaseModel):
    code: str
    name: str
    description: str | None = None
    customer_id: str | None = None
    owner: str | None = None
    project_type: str | None = None
    location: str | None = None
    manager: str | None = None
    start_date: _Date | None = None
    expected_completion: _Date | None = None
    actual_completion: _Date | None = None
    status: str = "active"
    contract_value: Decimal = Decimal(0)
    budget: Decimal = Decimal(0)
    currency: str = "AED"
    vat_treatment: str | None = None
    retention_percent: Decimal = Decimal(0)
    retention_amount: Decimal = Decimal(0)
    advance_percent: Decimal = Decimal(0)
    advance_amount: Decimal = Decimal(0)
    progress_percent: Decimal = Decimal(0)
    notes: str | None = None


class DocumentTemplateIn(BaseModel):
    name: str = "Template"
    doc_type: str = "invoice"           # invoice | receipt
    is_default: bool = False
    page_size: str = "A4"               # A4 | A5 | Letter
    logo_position: str = "left"         # left | center | right
    accent_color: str = "#2563eb"
    font_size: int = 9
    sections: dict = Field(default_factory=dict)
    order: list[str] = Field(default_factory=list)
    bank_details: str | None = None
    footer_notes: str | None = None


class VatTreatmentIn(BaseModel):
    code: str
    name: str
    kind: str = "standard"       # standard|zero|exempt|out_of_scope|reverse_charge|other
    rate: Decimal = Decimal(0)
    description: str | None = None
    output_vat_code: str | None = None
    input_vat_code: str | None = None
    return_box: str | None = None
    taxable: bool = True
    recoverable: bool = True
    active: bool = True
    effective_from: _Date | None = None
    effective_to: _Date | None = None
    applicable_txn_types: str = "sales,purchase,expense"


class VatComputeIn(BaseModel):
    code: str
    amount: Decimal
    inclusive: bool = False


class SystemSettingsIn(BaseModel):
    invoice_number_format: str = "INV-{seq:04d}"
    credit_note_number_format: str = "CN-{seq:04d}"
    bill_number_format: str = "BILL-{seq:04d}"
    payment_number_format: str = "Pmt-{seq:04d}"
    default_sales_code: str | None = None
    default_ar_code: str | None = None
    default_ap_code: str | None = None
    default_input_vat_code: str | None = None
    default_output_vat_code: str | None = None
    default_bank_code: str | None = None
    default_cash_code: str | None = None
    decimal_places: int = 2
    rounding_mode: str = "half_up"
    default_vat_rate: Decimal = Decimal("0.05")


class OrganizationIn(BaseModel):
    name: str
    legal_name: str | None = None
    address: str | None = None
    trn: str | None = None
    phone: str | None = None
    email: str | None = None
    website: str | None = None
    financial_year_start: _Date | None = None
    financial_year_end: _Date | None = None
    base_currency: str = "AED"
    vat_registered: bool = True
    vat_return_frequency: str = "quarterly"   # monthly | quarterly | na
    # E-invoicing organization identity
    trade_license: str | None = None
    country: str = "AE"
    einvoice_scheme: str | None = None
    einvoice_id: str | None = None
    bank_name: str | None = None
    bank_iban: str | None = None


class CalcPreviewIn(BaseModel):
    subtotal: Decimal = Decimal(0)
    discount: Decimal = Decimal(0)
    vat_rate: Decimal | None = None
    vat_amount: Decimal | None = None
    retention_basis: str = "none"       # none | net | gross | amount
    retention_percent: Decimal = Decimal(0)
    retention_amount: Decimal = Decimal(0)
    advance_recovery: Decimal = Decimal(0)


class AttachmentRenameIn(BaseModel):
    display_name: str


class AttachmentReviewIn(BaseModel):
    note: str | None = None


# ── Party statements (customer / vendor) ────────────────────────────────────────────────
class PartyStatementLine(BaseModel):
    date: _Date
    type: str                 # Invoice | Receipt | Bill | Payment
    reference: str | None = None
    doc_id: str | None = None
    link: str | None = None   # "invoice" | "bill" | "journal"
    debit: Decimal            # increases the balance owed
    credit: Decimal           # decreases it
    balance: Decimal


class PartyStatement(BaseModel):
    party_id: str
    party_name: str
    party_trn: str | None = None
    kind: str                 # customer | vendor
    currency: str = "AED"
    period_start: _Date | None = None
    period_end: _Date | None = None
    generated_at: str | None = None
    opening_balance: Decimal
    lines: list[PartyStatementLine]
    total_debit: Decimal
    total_credit: Decimal
    closing_balance: Decimal


# ── VAT Return (UAE FTA VAT201) ─────────────────────────────────────────────────────────
class Vat201Box(BaseModel):
    box: str
    label: str
    amount: Decimal
    vat: Decimal


class Vat201Return(BaseModel):
    period_start: _Date | None = None
    period_end: _Date | None = None
    generated_at: str | None = None
    currency: str = "AED"
    boxes: list[Vat201Box]
    total_output_vat: Decimal    # box 12
    total_input_vat: Decimal     # box 13
    net_vat_due: Decimal         # box 14 (positive = payable, negative = refundable)
    is_refund: bool
    gl_output_vat: Decimal       # VAT Payable (2100) movement in the period
    gl_input_vat: Decimal        # VAT Recoverable (1300) movement in the period
    reconciled: bool
    provisional: bool = False    # True when a treatment needs SME validation (e.g. VAT on advances)
    provisional_reason: str | None = None
    notes: list[str] = Field(default_factory=list)


# ── TB / GL / Financial-report import ───────────────────────────────────────────────────
class ImportRow(BaseModel):
    row_no: int
    code: str | None = None
    name: str | None = None
    debit: Decimal = Decimal(0)
    credit: Decimal = Decimal(0)
    date: _Date | None = None
    reference: str | None = None
    memo: str | None = None
    # analysis annotations
    matched_account_id: str | None = None
    matched_code: str | None = None
    matched_name: str | None = None
    account_type: str | None = None
    status: str = "ok"       # ok | new | unmatched | skipped
    note: str | None = None


class ImportGroup(BaseModel):
    reference: str
    date: _Date | None = None
    total_debit: Decimal
    total_credit: Decimal
    balanced: bool
    row_nos: list[int]


class TypeTotal(BaseModel):
    type: str
    debit: Decimal
    credit: Decimal
    net: Decimal


class ImportAnalysis(BaseModel):
    kind: str                 # tb | gl | report
    filename: str
    detected_columns: list[str]
    mapping: dict[str, str]   # semantic field -> source column header
    rows: list[ImportRow]
    total_debit: Decimal
    total_credit: Decimal
    balanced: bool
    matched_count: int
    new_count: int
    unmatched_count: int
    groups: list[ImportGroup] = Field(default_factory=list)      # gl only
    by_type: list[TypeTotal] = Field(default_factory=list)       # report only
    warnings: list[str] = Field(default_factory=list)


class ImportRowIn(BaseModel):
    row_no: int = 0
    code: str | None = None
    name: str | None = None
    debit: Decimal = Decimal(0)
    credit: Decimal = Decimal(0)
    date: _Date | None = None
    reference: str | None = None
    memo: str | None = None


class ImportCommitIn(BaseModel):
    kind: str                 # tb | gl | report
    rows: list[ImportRowIn] = Field(..., min_length=1)
    opening_date: date | None = None       # tb/report
    create_missing: bool = False
    skip_unbalanced: bool = True           # gl
    memo: str | None = None


class ImportResult(BaseModel):
    kind: str
    journal_entry_ids: list[str]
    entries_created: int
    accounts_created: list[str]
    rows_imported: int
    skipped: list[str]
    message: str


AccountNode.model_rebuild()
