"""Accounting domain constants: account types, normal balances, UAE default Chart of
Accounts, and the well-known account codes the Sales module posts to."""

from __future__ import annotations

from enum import Enum


class AccountType(str, Enum):
    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    INCOME = "income"
    EXPENSE = "expense"


class NormalBalance(str, Enum):
    DEBIT = "debit"
    CREDIT = "credit"


# Assets/expenses increase on the debit side; liabilities/equity/income on the credit side.
NORMAL_BALANCE: dict[AccountType, NormalBalance] = {
    AccountType.ASSET: NormalBalance.DEBIT,
    AccountType.EXPENSE: NormalBalance.DEBIT,
    AccountType.LIABILITY: NormalBalance.CREDIT,
    AccountType.EQUITY: NormalBalance.CREDIT,
    AccountType.INCOME: NormalBalance.CREDIT,
}

BALANCE_SHEET_TYPES = {AccountType.ASSET, AccountType.LIABILITY, AccountType.EQUITY}
INCOME_STATEMENT_TYPES = {AccountType.INCOME, AccountType.EXPENSE}


def default_normal_balance(account_type: AccountType) -> NormalBalance:
    return NORMAL_BALANCE[account_type]


# (code, name, type, is_group, parent_code, normal_balance_override, is_cost_of_sales)
DEFAULT_COA: list[tuple[str, str, AccountType, bool, str | None, NormalBalance | None, bool]] = [
    ("1000", "Assets", AccountType.ASSET, True, None, None, False),
    ("1010", "Cash", AccountType.ASSET, False, "1000", None, False),
    ("1020", "Bank", AccountType.ASSET, False, "1000", None, False),
    ("1100", "Accounts Receivable", AccountType.ASSET, False, "1000", None, False),
    ("1200", "Inventory", AccountType.ASSET, False, "1000", None, False),
    ("1300", "VAT Recoverable (Input VAT)", AccountType.ASSET, False, "1000", None, False),
    ("1150", "Retention Receivable", AccountType.ASSET, False, "1000", None, False),
    ("1170", "Vendor Advances (Prepayments)", AccountType.ASSET, False, "1000", None, False),
    ("1500", "Fixed Assets", AccountType.ASSET, False, "1000", None, False),
    ("1510", "Accumulated Depreciation", AccountType.ASSET, False, "1000", NormalBalance.CREDIT, False),
    ("2000", "Liabilities", AccountType.LIABILITY, True, None, None, False),
    ("2010", "Accounts Payable", AccountType.LIABILITY, False, "2000", None, False),
    ("2100", "VAT Payable (Output VAT)", AccountType.LIABILITY, False, "2000", None, False),
    ("2110", "Salaries Payable", AccountType.LIABILITY, False, "2000", None, False),
    ("2120", "Payroll Deductions Payable", AccountType.LIABILITY, False, "2000", None, False),
    ("2130", "End of Service Provision", AccountType.LIABILITY, False, "2000", None, False),
    ("2150", "Retention Payable", AccountType.LIABILITY, False, "2000", None, False),
    ("2160", "Customer Advances (Contract Liability)", AccountType.LIABILITY, False, "2000", None, False),
    ("2200", "Loans Payable", AccountType.LIABILITY, False, "2000", None, False),
    ("2300", "Corporate Tax Payable", AccountType.LIABILITY, False, "2000", None, False),
    ("3000", "Equity", AccountType.EQUITY, True, None, None, False),
    ("3010", "Capital", AccountType.EQUITY, False, "3000", None, False),
    ("3020", "Retained Earnings", AccountType.EQUITY, False, "3000", None, False),
    ("4000", "Income", AccountType.INCOME, True, None, None, False),
    ("4010", "Sales Revenue", AccountType.INCOME, False, "4000", None, False),
    ("4020", "Service Revenue", AccountType.INCOME, False, "4000", None, False),
    ("4080", "Gain on Asset Disposal", AccountType.INCOME, False, "4000", None, False),
    ("4085", "Foreign Exchange Gain", AccountType.INCOME, False, "4000", None, False),
    ("4090", "Other Income", AccountType.INCOME, False, "4000", None, False),
    ("5000", "Expenses", AccountType.EXPENSE, True, None, None, False),
    ("5010", "Salaries", AccountType.EXPENSE, False, "5000", None, False),
    ("5020", "Rent", AccountType.EXPENSE, False, "5000", None, False),
    ("5030", "Utilities", AccountType.EXPENSE, False, "5000", None, False),
    ("5040", "Marketing", AccountType.EXPENSE, False, "5000", None, False),
    ("5015", "End of Service Benefits", AccountType.EXPENSE, False, "5000", None, False),
    ("5050", "Depreciation Expense", AccountType.EXPENSE, False, "5000", None, False),
    ("5060", "Cost of Goods Sold", AccountType.EXPENSE, False, "5000", None, True),
    ("5070", "Loss on Asset Disposal", AccountType.EXPENSE, False, "5000", None, False),
    ("5075", "Foreign Exchange Loss", AccountType.EXPENSE, False, "5000", None, False),
    ("5080", "Inventory Adjustment", AccountType.EXPENSE, False, "5000", None, False),
    ("5090", "Miscellaneous Expenses", AccountType.EXPENSE, False, "5000", None, False),
]

# Well-known codes the modules post to.
CODE_AR = "1100"
CODE_AP = "2010"
CODE_VAT_OUTPUT = "2100"
CODE_VAT_INPUT = "1300"
CODE_SALES = "4010"
CODE_BANK = "1020"
CODE_CASH = "1010"
CODE_FIXED_ASSETS = "1500"
CODE_ACCUM_DEP = "1510"
CODE_DEP_EXPENSE = "5050"
CODE_GAIN_DISPOSAL = "4080"
CODE_LOSS_DISPOSAL = "5070"
CODE_INVENTORY = "1200"
CODE_COGS = "5060"
CODE_INV_ADJ = "5080"
CODE_FX_GAIN = "4085"
CODE_FX_LOSS = "5075"
BASE_CURRENCY = "AED"
CODE_SALARY_EXPENSE = "5010"
CODE_SALARIES_PAYABLE = "2110"
CODE_DEDUCTIONS_PAYABLE = "2120"
CODE_EOSB_PROVISION = "2130"
CODE_EOSB_EXPENSE = "5015"
# Retention & advances (configurable via settings overrides; these are the seeded defaults).
CODE_RETENTION_RECEIVABLE = "1150"
CODE_RETENTION_PAYABLE = "2150"
CODE_CUSTOMER_ADVANCES = "2160"
CODE_VENDOR_ADVANCES = "1170"

# UAE end-of-service gratuity: 21 days basic wage per year (first 5 years) → monthly accrual.
EOSB_MONTHLY_FACTOR = "0.058333"  # 21 / 360

# UAE Corporate Tax (Federal Decree-Law 47/2022) — CONFIGURABLE / version-controlled.
# 0% on taxable income up to the threshold, standard rate above. PROVISIONAL — verify with an
# SME and update here (do NOT hard-code legal conclusions elsewhere).
CT_THRESHOLD_AED = "375000"   # 0% band ceiling
CT_STANDARD_RATE = "0.09"     # 9% above the threshold
CT_LEGAL_REF = "Federal Decree-Law No. 47 of 2022 (PROVISIONAL — requires SME validation)"
# Expense account codes treated as non-deductible add-backs (empty by default — classification
# is entity-specific and must be configured, never assumed).
CT_NONDEDUCTIBLE_CODES: list[str] = []

# Journal-entry lifecycle.
ENTRY_DRAFT = "draft"
ENTRY_POSTED = "posted"
ENTRY_VOID = "void"
ENTRY_STATUSES = (ENTRY_DRAFT, ENTRY_POSTED, ENTRY_VOID)
