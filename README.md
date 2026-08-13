# Accounting

A standalone, self-contained **double-entry accounting system** for UAE businesses. One
FastAPI process serves both the JSON API and a built-in web UI on a single port — no Node,
no build step, no external services.

Completely separate from the VAT Compliance Platform (which runs on ports 8000/3000). This
app runs on **port 8100**.

## Features

- **General Ledger** — Chart of Accounts (tree of groups → sub-accounts), balanced
  double-entry journal entries (drafts, posting, voiding with an audit trail), Trial
  Balance, and per-account General Ledger with running balances.
- **Financial Statements** — Profit & Loss (with Gross Profit via a cost-of-sales flag),
  Balance Sheet (always balances: current-period earnings folded into equity), and a
  direct-method Cash Flow — all generated live from the ledger.
- **Sales** — Customers, tax invoices (auto-posting Dr AR / Cr Revenue + VAT Payable at
  5% UAE VAT), customer payments (Dr Bank / Cr AR) with partial/paid status, and an
  Accounts Receivable aging report.
- **Executive dashboard** — cash, bank, AR, AP, revenue/expenses/gross/net profit YTD,
  VAT payable/recoverable, and outstanding/overdue invoices.

Every invoice and payment resolves to a **balanced journal entry**, so the sub-ledgers
always reconcile to their control accounts and the books stay a single source of truth.

## Requirements

- Python 3.11+ (developed on 3.12)

## Setup

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt   # Windows
# source .venv/bin/activate && pip install -r requirements.txt  # macOS/Linux
```

## Run

```bash
.venv\Scripts\python.exe run.py
```

Then open **http://127.0.0.1:8100** in your browser. The default UAE Chart of Accounts is
seeded automatically on first start. Interactive API docs are at `/docs`.

## Test

```bash
.venv\Scripts\python.exe -m pytest -q
```

## Project layout

```
app/
  main.py            FastAPI app: serves API + built-in UI, seeds CoA on startup
  config.py          Settings (DB URL, port, VAT rate)
  database.py        SQLite engine / session
  constants.py       Account types, default Chart of Accounts, well-known codes
  models.py          ORM: Account, JournalEntry/Line, Customer, SalesInvoice/Line, Payment
  schemas.py         Pydantic request/response models
  services/
    ledger.py        CoA, journal posting, trial balance, general ledger
    reports.py       P&L, balance sheet, cash flow, dashboard KPIs
    sales.py         Customers, invoices, payments, AR aging
  api/routes.py      All /api endpoints
  web/index.html     Single-page web UI (vanilla JS, no build step)
tests/               pytest suite (ledger, reports, sales)
data/                SQLite database (created on first run)
```

## Data

SQLite database at `data/accounting.sqlite3`, created and seeded on first run. Delete that
file to reset to an empty set of books.

## Configuration

Override via environment variables or a `.env` file:

| Variable          | Default                              |
|-------------------|--------------------------------------|
| `DATABASE_URL`    | `sqlite:///./data/accounting.sqlite3`|
| `PORT`            | `8100`                               |
| `SEED_ON_STARTUP` | `true`                               |
| `VAT_STANDARD_RATE` | `0.05`                             |

## Roadmap

Each future module is another producer of journal entries posting into this ledger:
Purchases (bills + supplier payments), Banking (reconciliation), Inventory, Fixed Assets
(depreciation), Payroll, multi-currency, and PDF/Excel exports of the statements.
