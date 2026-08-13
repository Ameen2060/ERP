"""All JSON API endpoints under /api."""

from __future__ import annotations

import io
import os
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import _token_from_request, decode_token
from ..database import get_db
from ..models import BillPayment, CustomerPayment, User
from ..schemas import (
    AccountIn,
    AccountNode,
    AccountOut,
    ApAgingReport,
    ArAgingReport,
    AssetDashboard,
    AssetIn,
    AssetOut,
    AssetRegister,
    AttachmentEventOut,
    AttachmentOut,
    AttachmentRenameIn,
    AttachmentReviewIn,
    AttachmentStatusOut,
    AutoMatchIn,
    BalanceSheet,
    BudgetIn,
    BudgetLinesIn,
    BudgetRevisionIn,
    CalcPreviewIn,
    DocumentTemplateIn,
    ExpenseIn,
    OrganizationIn,
    ProjectIn,
    RetentionReleaseIn,
    SystemSettingsIn,
    VatComputeIn,
    VatTreatmentIn,
    CustomerAdvanceIn,
    VendorAdvanceIn,
    AdvanceApplyIn,
    CustomerCreditNoteIn,
    VendorCreditNoteIn,
    CreditNoteApplyIn,
    BankAccountIn,
    BankAccountUpdateIn,
    BankAccountOut,
    BillIn,
    BillOut,
    BillPaymentIn,
    BillPaymentOut,
    BillSummary,
    CashFlowStatement,
    CTComputation,
    CTReviewCreate,
    CTReviewDetail,
    CTReviewSummary,
    CTSignOffIn,
    CTTransitionIn,
    CurrencyIn,
    CurrencyOut,
    CustomerIn,
    CustomerOut,
    ExchangeRateIn,
    ExchangeRateOut,
    DashboardKPIs,
    DepreciationScheduleRow,
    DisposalIn,
    DocReport,
    DrillDown,
    DrillKey,
    EmployeeIn,
    EmployeeOut,
    ImportAnalysis,
    ImportCommitIn,
    ImportResult,
    GeneralLedger,
    IncomeStatement,
    InventoryValuation,
    InvoiceIn,
    InvoiceOut,
    InvoiceSummary,
    JournalEntryIn,
    JournalEntryOut,
    JournalEntrySummary,
    LowStockRow,
    ManualMatchIn,
    MatchCandidate,
    MovementIn,
    MovementOut,
    PartyStatement,
    BillMetaIn,
    CnMetaIn,
    InvoiceMetaIn,
    PaymentIn,
    RecurringPlanIn,
    PaymentOut,
    ProductReport,
    PayrollRunIn,
    PayrollRunOut,
    PayrollRunSummary,
    ProductIn,
    ProductOut,
    ReconcileSummary,
    ReconcileSummaryIn,
    ReconciliationOut,
    RunDepreciationIn,
    StatementImportIn,
    StatementLineOut,
    TransferIn,
    TrialBalance,
    Vat201Return,
    VendorIn,
    VendorOut,
    WarehouseIn,
    WarehouseOut,
)
from ..services import (
    advances, assets, attachments, banking, budgets, calc, credit_notes, ct_review, currency,
    doc_pdf, drilldown, einvoicing, expenses, importing, inventory, ledger, organization, payroll,
    projects, purchases, recurring, reports, sales, system_settings, templates, vat_treatments,
)
from ..services.recurring import RecurringError
from ..services.advances import AdvanceError
from ..services.assets import AssetError
from ..services.budgets import BudgetError
from ..services.credit_notes import CreditNoteError
from ..services.organization import OrganizationError
from ..services.projects import ProjectError
from ..services.system_settings import SettingsError
from ..services.templates import TemplateError
from ..services.vat_treatments import VatTreatmentError
from ..services.attachments import AttachmentError
from ..services.banking import BankingError
from ..services.expenses import ExpenseError
from ..services.ct_review import CTReviewError
from ..services.einvoicing import EInvoiceError
from ..services.currency import CurrencyError
from ..services.importing import ImportError_
from ..services.inventory import InventoryError
from ..services.ledger import LedgerError
from ..services.payroll import PayrollError
from ..services.purchases import PurchaseError
from ..services.sales import SalesError
from ..services.validation import ValidationError

router = APIRouter(prefix="/api")


def _guard(fn):
    try:
        return fn()
    except (LedgerError, SalesError, PurchaseError, AssetError, BankingError, InventoryError,
            PayrollError, ImportError_, CurrencyError, CTReviewError, AttachmentError,
            ExpenseError, AdvanceError, CreditNoteError, OrganizationError, TemplateError,
            ProjectError, BudgetError, SettingsError, VatTreatmentError, ValidationError,
            RecurringError, EInvoiceError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def current_actor(request: Request) -> str:
    """Best-effort username of the caller (from the session token); 'local' when unauthenticated."""
    tok = _token_from_request(request)
    return (decode_token(tok) if tok else None) or "local"


def _actor_role(request: Request, db: Session) -> tuple[str, bool]:
    """Return (username, is_editor). Editors (admin/accountant) may modify attachments;
    viewers are read-only. When auth is disabled the local user is treated as an editor."""
    tok = _token_from_request(request)
    username = decode_token(tok) if tok else None
    if not username:
        return ("local", True)
    u = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
    return (username, bool(u) and u.role in ("admin", "accountant"))


def _require_editor(request: Request, db: Session) -> str:
    actor, is_editor = _actor_role(request, db)
    if not is_editor:
        raise HTTPException(status_code=403, detail="You do not have permission to modify attachments.")
    return actor


def _role_of(request: Request, db: Session) -> str | None:
    """The caller's role for permission checks; None when auth is disabled (→ full access)."""
    tok = _token_from_request(request)
    username = decode_token(tok) if tok else None
    if not username:
        return None
    u = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
    return u.role if u else None


def _require_perm(request: Request, db: Session, action: str) -> str:
    """Gate an endpoint on a granular action (edit/reverse/view_audit/…). Returns the actor."""
    from ..services import permissions
    role = _role_of(request, db)
    if not permissions.can(db, role, action):
        raise HTTPException(status_code=403, detail=f"Your role does not have '{action}' permission.")
    return current_actor(request)


# ── Dashboard ───────────────────────────────────────────────────────────────────────────
@router.get("/dashboard", response_model=DashboardKPIs, tags=["dashboard"])
def dashboard(db: Session = Depends(get_db)) -> DashboardKPIs:
    return reports.dashboard(db)


@router.get("/drilldown", response_model=list[DrillKey], tags=["dashboard"])
def drilldown_keys() -> list[DrillKey]:
    return drilldown.list_keys()


@router.get("/drilldown/{key}", response_model=DrillDown, tags=["dashboard"])
def drilldown_detail(
    key: str,
    start: date | None = Query(None),
    end: date | None = Query(None),
    db: Session = Depends(get_db),
) -> DrillDown:
    try:
        return drilldown.build(db, key, start=start, end=end)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


# ── Chart of Accounts ───────────────────────────────────────────────────────────────────
@router.post("/accounts/seed", tags=["accounts"])
def seed_accounts(db: Session = Depends(get_db)) -> dict:
    added = ledger.seed_chart_of_accounts(db)
    return {"added": added, "total": len(ledger.list_accounts(db))}


@router.get("/accounts", response_model=list[AccountOut], tags=["accounts"])
def list_accounts(active_only: bool = Query(False), db: Session = Depends(get_db)) -> list[AccountOut]:
    return ledger.list_accounts(db, active_only=active_only)


@router.get("/accounts/tree", response_model=list[AccountNode], tags=["accounts"])
def accounts_tree(db: Session = Depends(get_db)) -> list[AccountNode]:
    return ledger.account_tree(db)


@router.post("/accounts", response_model=AccountOut, tags=["accounts"])
def create_account(payload: AccountIn, db: Session = Depends(get_db)) -> AccountOut:
    return _guard(lambda: ledger.create_account(db, payload))


@router.delete("/accounts/{account_id}", response_model=AccountOut, tags=["accounts"])
def archive_account(account_id: str, db: Session = Depends(get_db)) -> AccountOut:
    return _guard(lambda: ledger.archive_account(db, account_id))


# ── Journal entries ─────────────────────────────────────────────────────────────────────
@router.post("/journal-entries", response_model=JournalEntryOut, tags=["journal"])
def create_entry(payload: JournalEntryIn, db: Session = Depends(get_db)) -> JournalEntryOut:
    return _guard(lambda: ledger.create_journal_entry(db, payload))


@router.get("/journal-entries", response_model=list[JournalEntrySummary], tags=["journal"])
def list_entries(
    source: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(200, le=1000),
    db: Session = Depends(get_db),
) -> list[JournalEntrySummary]:
    return ledger.list_entries(db, source=source, status=status, limit=limit)


@router.get("/journal-entries/{entry_id}", response_model=JournalEntryOut, tags=["journal"])
def get_entry(entry_id: str, db: Session = Depends(get_db)) -> JournalEntryOut:
    return _guard(lambda: ledger.get_entry(db, entry_id))


@router.post("/journal-entries/{entry_id}/post", response_model=JournalEntryOut, tags=["journal"])
def post_entry(entry_id: str, db: Session = Depends(get_db)) -> JournalEntryOut:
    return _guard(lambda: ledger.post_entry(db, entry_id))


@router.post("/journal-entries/{entry_id}/void", response_model=JournalEntryOut, tags=["journal"])
def void_entry(entry_id: str, request: Request, db: Session = Depends(get_db)) -> JournalEntryOut:
    _require_perm(request, db, "reverse")
    return _guard(lambda: ledger.void_entry(db, entry_id))


@router.put("/journal-entries/{entry_id}", response_model=JournalEntryOut, tags=["journal"])
def update_entry(entry_id: str, payload: JournalEntryIn, request: Request,
                 reason: str = Query(""), db: Session = Depends(get_db)) -> JournalEntryOut:
    actor = _require_perm(request, db, "edit")
    if not reason.strip():
        raise HTTPException(status_code=400, detail="A reason for the change is required.")
    return _guard(lambda: ledger.update_journal_entry(db, entry_id, payload, actor=actor, reason=reason))


@router.get("/journal-entries/{entry_id}/audit", tags=["journal"])
def entry_audit(entry_id: str, request: Request, db: Session = Depends(get_db)) -> list[dict]:
    _require_perm(request, db, "view_audit")
    return _guard(lambda: ledger.entry_audit(db, entry_id))


# ── Ledger reports ──────────────────────────────────────────────────────────────────────
@router.get("/trial-balance", response_model=TrialBalance, tags=["reports"])
def trial_balance(as_of: date | None = Query(None), db: Session = Depends(get_db)) -> TrialBalance:
    return ledger.trial_balance(db, as_of=as_of)


@router.get("/general-ledger/{account_id}", response_model=GeneralLedger, tags=["reports"])
def general_ledger(
    account_id: str,
    start: date | None = Query(None),
    end: date | None = Query(None),
    db: Session = Depends(get_db),
) -> GeneralLedger:
    return _guard(lambda: ledger.general_ledger(db, account_id, start=start, end=end))


# ── Financial statements ────────────────────────────────────────────────────────────────
@router.get("/reports/income-statement", response_model=IncomeStatement, tags=["reports"])
def income_statement(
    start: date | None = Query(None), end: date | None = Query(None), db: Session = Depends(get_db)
) -> IncomeStatement:
    return reports.income_statement(db, start=start, end=end)


@router.get("/reports/balance-sheet", response_model=BalanceSheet, tags=["reports"])
def balance_sheet(as_of: date | None = Query(None), db: Session = Depends(get_db)) -> BalanceSheet:
    return reports.balance_sheet(db, as_of=as_of)


@router.get("/reports/cash-flow", response_model=CashFlowStatement, tags=["reports"])
def cash_flow(
    start: date | None = Query(None), end: date | None = Query(None), db: Session = Depends(get_db)
) -> CashFlowStatement:
    return reports.cash_flow(db, start=start, end=end)


@router.get("/reports/vat-return", response_model=Vat201Return, tags=["reports"])
def vat_return(
    start: date | None = Query(None), end: date | None = Query(None), db: Session = Depends(get_db)
) -> Vat201Return:
    return reports.vat_return(db, start=start, end=end)


@router.get("/reports/ct-computation", response_model=CTComputation, tags=["reports", "corporate-tax"])
def ct_computation(
    start: date | None = Query(None), end: date | None = Query(None), db: Session = Depends(get_db)
) -> CTComputation:
    return reports.ct_computation(db, start=start, end=end)


# ── Corporate Tax — Provisional → SME-Validation review workflow ──────────────────────────
@router.post("/ct/reviews", response_model=CTReviewDetail, tags=["corporate-tax"])
def ct_create_review(
    payload: CTReviewCreate, db: Session = Depends(get_db), actor: str = Depends(current_actor)
) -> CTReviewDetail:
    r = _guard(lambda: ct_review.create_review(
        db, payload.period_start, payload.period_end, prepared_by=payload.prepared_by, actor=actor))
    return ct_review.detail(r)


@router.get("/ct/reviews", response_model=list[CTReviewSummary], tags=["corporate-tax"])
def ct_list_reviews(db: Session = Depends(get_db)) -> list[CTReviewSummary]:
    return [ct_review.summary(r) for r in ct_review.list_reviews(db)]


@router.get("/ct/reviews/kpis", tags=["corporate-tax"])
def ct_review_kpis(db: Session = Depends(get_db)) -> dict:
    """KPI dashboard: counts by status + totals + how many are filing-ready / need attention."""
    reviews = ct_review.list_reviews(db)
    by_status: dict[str, int] = {}
    ct_total = 0.0
    for r in reviews:
        by_status[r.status] = by_status.get(r.status, 0) + 1
        if r.status == "validated":
            ct_total += float(r.ct_payable)
    return {
        "total": len(reviews),
        "by_status": by_status,
        "draft": by_status.get("draft", 0),
        "provisional": by_status.get("provisional", 0),
        "sme_reviewed": by_status.get("sme_reviewed", 0),
        "validated": by_status.get("validated", 0),
        "rejected": by_status.get("rejected", 0),
        "awaiting_action": by_status.get("provisional", 0) + by_status.get("sme_reviewed", 0),
        "validated_ct_payable": round(ct_total, 2),
    }


@router.get("/ct/reviews/{review_id}", response_model=CTReviewDetail, tags=["corporate-tax"])
def ct_get_review(review_id: str, db: Session = Depends(get_db)) -> CTReviewDetail:
    return _guard(lambda: ct_review.detail(ct_review.get_review(db, review_id)))


@router.post("/ct/reviews/{review_id}/submit", response_model=CTReviewDetail, tags=["corporate-tax"])
def ct_submit(review_id: str, payload: CTTransitionIn, db: Session = Depends(get_db),
              actor: str = Depends(current_actor)) -> CTReviewDetail:
    return _guard(lambda: ct_review.detail(ct_review.submit(db, review_id, actor, payload.note)))


@router.post("/ct/reviews/{review_id}/items/{item_id}/signoff",
             response_model=CTReviewDetail, tags=["corporate-tax"])
def ct_signoff(review_id: str, item_id: str, payload: CTSignOffIn, db: Session = Depends(get_db),
               actor: str = Depends(current_actor)) -> CTReviewDetail:
    return _guard(lambda: ct_review.detail(
        ct_review.sign_off_item(db, review_id, item_id, payload.signed_off, payload.note, actor)))


@router.post("/ct/reviews/{review_id}/mark-reviewed", response_model=CTReviewDetail, tags=["corporate-tax"])
def ct_mark_reviewed(review_id: str, payload: CTTransitionIn, db: Session = Depends(get_db),
                     actor: str = Depends(current_actor)) -> CTReviewDetail:
    return _guard(lambda: ct_review.detail(ct_review.mark_reviewed(db, review_id, actor, payload.note)))


@router.post("/ct/reviews/{review_id}/validate", response_model=CTReviewDetail, tags=["corporate-tax"])
def ct_validate(review_id: str, payload: CTTransitionIn, db: Session = Depends(get_db),
                actor: str = Depends(current_actor)) -> CTReviewDetail:
    return _guard(lambda: ct_review.detail(
        ct_review.validate(db, review_id, payload.sme_name, actor, payload.note)))


@router.post("/ct/reviews/{review_id}/reject", response_model=CTReviewDetail, tags=["corporate-tax"])
def ct_reject(review_id: str, payload: CTTransitionIn, db: Session = Depends(get_db),
              actor: str = Depends(current_actor)) -> CTReviewDetail:
    return _guard(lambda: ct_review.detail(ct_review.reject(db, review_id, actor, payload.note)))


@router.post("/ct/reviews/{review_id}/reopen", response_model=CTReviewDetail, tags=["corporate-tax"])
def ct_reopen(review_id: str, payload: CTTransitionIn, db: Session = Depends(get_db),
              actor: str = Depends(current_actor)) -> CTReviewDetail:
    return _guard(lambda: ct_review.detail(ct_review.reopen(db, review_id, actor, payload.note)))


@router.get("/ct/reviews/{review_id}/export", tags=["corporate-tax"])
def ct_export_review(review_id: str, format: str = Query("pdf", pattern="^(xlsx|pdf)$"),
                     final: bool = Query(False), db: Session = Depends(get_db)):
    """Export a CT review. A 'final' export is BLOCKED unless the review is validated; any other
    export is stamped PROVISIONAL — REQUIRES SME VALIDATION."""
    from ..export import ct_review_export

    review = _guard(lambda: ct_review.get_review(db, review_id))
    if final and not ct_review.can_file(review):
        raise HTTPException(
            status_code=400,
            detail="Filing-ready ('final') export is blocked until the computation is SME-validated.")
    return ct_review_export(review, format, final=final)


@router.get("/reports/product-sales", response_model=ProductReport, tags=["reports", "inventory"])
def product_sales(
    start: date | None = Query(None), end: date | None = Query(None), db: Session = Depends(get_db)
) -> ProductReport:
    return reports.product_movement(db, "issue", start=start, end=end)


@router.get("/reports/product-purchases", response_model=ProductReport, tags=["reports", "inventory"])
def product_purchases(
    start: date | None = Query(None), end: date | None = Query(None), db: Session = Depends(get_db)
) -> ProductReport:
    return reports.product_movement(db, "receipt", start=start, end=end)


@router.get("/reports/customer-statement", response_model=PartyStatement, tags=["reports"])
def customer_statement(
    customer_id: str = Query(...), start: date | None = Query(None), end: date | None = Query(None),
    db: Session = Depends(get_db),
) -> PartyStatement:
    return _guard(lambda: reports.customer_statement(db, customer_id, start=start, end=end))


@router.get("/reports/vendor-statement", response_model=PartyStatement, tags=["reports"])
def vendor_statement(
    vendor_id: str = Query(...), start: date | None = Query(None), end: date | None = Query(None),
    db: Session = Depends(get_db),
) -> PartyStatement:
    return _guard(lambda: reports.vendor_statement(db, vendor_id, start=start, end=end))


@router.get("/reports/faf", tags=["reports"])
def vat_audit_file(
    start: date | None = Query(None), end: date | None = Query(None), db: Session = Depends(get_db)
):
    """FTA VAT Audit File (FAF) — multi-sheet Excel workbook of the return + transaction listings."""
    from fastapi.responses import StreamingResponse

    from ..faf import build_faf
    data = build_faf(db, start, end)
    stamp = str(end or date.today())
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="FAF-VAT-Audit-{stamp}.xlsx"'},
    )


# ── Budget Management ──────────────────────────────────────────────────────────────────────
@router.get("/budgets", tags=["budgets"])
def list_budgets(fiscal_year: int | None = Query(None), scope: str | None = Query(None),
                 db: Session = Depends(get_db)) -> list[dict]:
    return budgets.list_budgets(db, fiscal_year=fiscal_year, scope=scope)


@router.post("/budgets", tags=["budgets"])
def create_budget(payload: BudgetIn, request: Request, db: Session = Depends(get_db)) -> dict:
    actor = _require_editor(request, db)
    return _guard(lambda: budgets.create(db, payload, actor))


@router.get("/budgets/dashboard", tags=["budgets"])
def budgets_dashboard(fiscal_year: int | None = Query(None), db: Session = Depends(get_db)) -> dict:
    return budgets.dashboard(db, fiscal_year=fiscal_year)


@router.get("/budgets/{budget_id}", tags=["budgets"])
def get_budget(budget_id: str, db: Session = Depends(get_db)) -> dict:
    return _guard(lambda: budgets.get(db, budget_id))


@router.put("/budgets/{budget_id}/lines", tags=["budgets"])
def update_budget_lines(budget_id: str, payload: BudgetLinesIn, request: Request,
                        db: Session = Depends(get_db)) -> dict:
    actor = _require_editor(request, db)
    return _guard(lambda: budgets.update_lines(db, budget_id, payload.lines, actor))


@router.post("/budgets/{budget_id}/transition", tags=["budgets"])
def transition_budget(budget_id: str, action: str = Query(...), request: Request = None,
                      db: Session = Depends(get_db)) -> dict:
    actor = _require_editor(request, db)
    return _guard(lambda: budgets.transition(db, budget_id, action, actor))


@router.post("/budgets/{budget_id}/revision", tags=["budgets"])
def revise_budget(budget_id: str, payload: BudgetRevisionIn, request: Request,
                  db: Session = Depends(get_db)) -> dict:
    actor = _require_editor(request, db)
    return _guard(lambda: budgets.create_revision(db, budget_id, payload.new_version, actor, payload.reason))


@router.get("/budgets/{budget_id}/variance", tags=["budgets", "reports"])
def budget_variance(budget_id: str, db: Session = Depends(get_db)) -> dict:
    return _guard(lambda: budgets.budget_vs_actual(db, budget_id))


@router.get("/budgets/{budget_id}/forecast", tags=["budgets", "reports"])
def budget_forecast(budget_id: str, months_elapsed: int | None = Query(None),
                    db: Session = Depends(get_db)) -> dict:
    return _guard(lambda: budgets.forecast(db, budget_id, months_elapsed))


@router.get("/budgets/{budget_id}/events", tags=["budgets"])
def budget_events(budget_id: str, db: Session = Depends(get_db)) -> list[dict]:
    return _guard(lambda: budgets.events(db, budget_id))


# ── Project Master ───────────────────────────────────────────────────────────────────────────
@router.get("/projects", tags=["projects"])
def list_projects(status: str | None = Query(None), include_archived: bool = Query(True),
                  db: Session = Depends(get_db)) -> list[dict]:
    return projects.list_projects(db, status=status, include_archived=include_archived)


@router.post("/projects", tags=["projects"])
def create_project(payload: ProjectIn, request: Request, db: Session = Depends(get_db)) -> dict:
    actor = _require_editor(request, db)
    return _guard(lambda: projects.create(db, payload, actor))


@router.get("/projects/dashboard", tags=["projects"])
def projects_dashboard(db: Session = Depends(get_db)) -> dict:
    return projects.dashboard(db)


@router.get("/reports/projects", tags=["projects", "reports"])
def projects_portfolio(status: str | None = Query(None), db: Session = Depends(get_db)) -> dict:
    return projects.portfolio_report(db, status=status)


@router.get("/search", tags=["search"])
def global_search(q: str = Query(""), limit: int = Query(8), db: Session = Depends(get_db)) -> dict:
    from ..services import search as _search
    return _search.search(db, q, limit=limit)


@router.get("/projects/{project_id}", tags=["projects"])
def get_project(project_id: str, db: Session = Depends(get_db)) -> dict:
    return _guard(lambda: projects.get(db, project_id))


@router.put("/projects/{project_id}", tags=["projects"])
def update_project(project_id: str, payload: ProjectIn, request: Request,
                   db: Session = Depends(get_db)) -> dict:
    actor = _require_editor(request, db)
    return _guard(lambda: projects.update(db, project_id, payload, actor))


@router.post("/projects/{project_id}/archive", tags=["projects"])
def archive_project(project_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
    actor = _require_editor(request, db)
    return _guard(lambda: projects.archive(db, project_id, actor))


@router.get("/projects/{project_id}/financials", tags=["projects", "reports"])
def project_financials(project_id: str, db: Session = Depends(get_db)) -> dict:
    p = _guard(lambda: projects.get(db, project_id))
    return projects.financials(db, p["code"])


@router.get("/projects/{project_id}/transactions", tags=["projects", "reports"])
def project_transactions(project_id: str, db: Session = Depends(get_db)) -> dict:
    p = _guard(lambda: projects.get(db, project_id))
    return projects.transactions(db, p["code"])


@router.get("/projects/{project_id}/events", tags=["projects"])
def project_events(project_id: str, db: Session = Depends(get_db)) -> list[dict]:
    return _guard(lambda: projects.events(db, project_id))


# ── VAT Treatment / Rate Master ────────────────────────────────────────────────────────────
@router.get("/vat-treatments", tags=["vat"])
def list_vat_treatments(active_only: bool = Query(False), txn_type: str | None = Query(None),
                        db: Session = Depends(get_db)) -> list[dict]:
    return vat_treatments.list_treatments(db, active_only=active_only, txn_type=txn_type)


@router.post("/vat-treatments", tags=["vat"])
def create_vat_treatment(payload: VatTreatmentIn, request: Request, db: Session = Depends(get_db)) -> dict:
    _require_editor(request, db)
    return _guard(lambda: vat_treatments.create(db, payload))


@router.put("/vat-treatments/{treatment_id}", tags=["vat"])
def update_vat_treatment(treatment_id: str, payload: VatTreatmentIn, request: Request,
                         db: Session = Depends(get_db)) -> dict:
    _require_editor(request, db)
    return _guard(lambda: vat_treatments.update(db, treatment_id, payload))


@router.post("/vat-treatments/compute", tags=["vat"])
def compute_vat_treatment(payload: VatComputeIn, db: Session = Depends(get_db)) -> dict:
    return _guard(lambda: vat_treatments.compute(db, payload.code, payload.amount, payload.inclusive))


@router.get("/reports/vat-by-treatment", tags=["vat", "reports"])
def vat_by_treatment(start: date | None = Query(None), end: date | None = Query(None),
                     db: Session = Depends(get_db)) -> dict:
    return vat_treatments.by_treatment_report(db, start=start, end=end)


# ── System settings ──────────────────────────────────────────────────────────────────────────
@router.get("/system-settings", tags=["settings"])
def get_system_settings(db: Session = Depends(get_db)) -> dict:
    return system_settings.profile(db)


@router.put("/system-settings", tags=["settings"])
def update_system_settings(payload: SystemSettingsIn, request: Request, db: Session = Depends(get_db)) -> dict:
    _require_editor(request, db)
    return _guard(lambda: system_settings.update(db, payload))


@router.post("/system-settings/advance-vat/transition", tags=["settings"])
def advance_vat_transition(action: str = Query(...), request: Request = None,
                           db: Session = Depends(get_db)) -> dict:
    actor = _require_editor(request, db)
    return _guard(lambda: system_settings.transition_advance_vat(db, action, actor))


@router.post("/system-settings/period-lock", tags=["settings"])
def set_period_lock(lock_date: str = Query(""), request: Request = None,
                    db: Session = Depends(get_db)) -> dict:
    """Close the books on/before a date (blank = unlock). Admin-only. Transactions in a locked
    period can no longer be edited/voided directly."""
    _require_perm(request, db, "approve")
    from datetime import date as _d
    d = _d.fromisoformat(lock_date) if lock_date.strip() else None
    return _guard(lambda: system_settings.set_period_lock(db, d))


@router.get("/permissions", tags=["settings"])
def my_permissions(request: Request, db: Session = Depends(get_db)) -> dict:
    """The current caller's action permissions, for the UI to show/hide edit/void controls."""
    from ..services import permissions
    role = _role_of(request, db)
    return {"role": role or "local", "can": permissions.permissions_for(db, role)}


@router.get("/permissions/matrix", tags=["settings"])
def permissions_matrix(request: Request, db: Session = Depends(get_db)) -> dict:
    from ..services import permissions
    _require_perm(request, db, "approve")   # admin/accountant-level; admins edit it
    return permissions.matrix(db)


@router.put("/permissions/matrix", tags=["settings"])
def update_permissions_matrix(payload: dict, request: Request, db: Session = Depends(get_db)) -> dict:
    from ..services import permissions
    role = _role_of(request, db)
    if role is not None and role != "admin":
        raise HTTPException(status_code=403, detail="Only administrators can edit permissions.")
    return permissions.set_matrix(db, payload.get("matrix") or payload)


# ── Organization profile + VAT configuration ────────────────────────────────────────────────
@router.get("/organization", tags=["organization"])
def get_organization(db: Session = Depends(get_db)) -> dict:
    return organization.profile(db)


@router.put("/organization", tags=["organization"])
def update_organization(payload: OrganizationIn, request: Request, db: Session = Depends(get_db)) -> dict:
    _require_editor(request, db)
    return _guard(lambda: organization.update(db, payload))


@router.post("/organization/logo", tags=["organization"])
async def upload_organization_logo(request: Request, file: UploadFile = File(...),
                                   db: Session = Depends(get_db)) -> dict:
    _require_editor(request, db)
    data = await file.read()
    return _guard(lambda: organization.set_logo(db, file.filename or "logo.png", data))


@router.delete("/organization/logo", tags=["organization"])
def delete_organization_logo(request: Request, db: Session = Depends(get_db)) -> dict:
    _require_editor(request, db)
    return organization.remove_logo(db)


@router.get("/organization/logo", tags=["organization"])
def get_organization_logo(db: Session = Depends(get_db)):
    from fastapi import Response
    lf = organization.logo_file(db)
    if not lf:
        raise HTTPException(status_code=404, detail="No logo set.")
    _ref, mime = lf
    data = organization.logo_bytes(db)   # durable storage (filesystem path or Blob URL)
    if data is None:
        raise HTTPException(status_code=404, detail="No logo set.")
    return Response(content=data, media_type=mime)


# ── Credit Notes (customer / vendor) + application ──────────────────────────────────────────
@router.post("/sales/credit-notes", tags=["sales", "credit-notes"])
def create_customer_cn(payload: CustomerCreditNoteIn, db: Session = Depends(get_db)) -> dict:
    return _guard(lambda: credit_notes.create_customer_cn(db, payload))


@router.get("/sales/credit-notes", tags=["sales", "credit-notes"])
def list_customer_cn(customer_id: str | None = Query(None), db: Session = Depends(get_db)) -> list[dict]:
    return credit_notes.list_customer_cn(db, customer_id=customer_id)


@router.get("/sales/credit-notes/{cn_id}", tags=["sales", "credit-notes"])
def get_customer_cn(cn_id: str, db: Session = Depends(get_db)) -> dict:
    return _guard(lambda: credit_notes.get_customer_cn(db, cn_id))


@router.put("/sales/credit-notes/{cn_id}", tags=["sales", "credit-notes"])
def update_customer_cn(cn_id: str, payload: CustomerCreditNoteIn, request: Request,
                       reason: str = Query(""), db: Session = Depends(get_db)) -> dict:
    actor = _require_perm(request, db, "edit")
    if not reason.strip():
        raise HTTPException(status_code=400, detail="A reason for the change is required.")
    return _guard(lambda: credit_notes.update_customer_cn(db, cn_id, payload, actor=actor, reason=reason))


@router.get("/sales/credit-notes/{cn_id}/audit", tags=["sales", "credit-notes"])
def customer_cn_audit(cn_id: str, request: Request, db: Session = Depends(get_db)) -> list[dict]:
    _require_perm(request, db, "view_audit")
    return _guard(lambda: credit_notes.customer_cn_audit(db, cn_id))


@router.put("/sales/credit-notes/{cn_id}/details", tags=["sales", "credit-notes"])
def update_customer_cn_details(cn_id: str, payload: CnMetaIn, request: Request,
                               reason: str = Query(""), db: Session = Depends(get_db)) -> dict:
    actor = _require_perm(request, db, "edit")
    return _guard(lambda: credit_notes.update_customer_cn_meta(db, cn_id, payload, actor=actor, reason=reason or None))


@router.post("/sales/credit-notes/{cn_id}/post", tags=["sales", "credit-notes"])
def post_customer_cn(cn_id: str, db: Session = Depends(get_db)) -> dict:
    return _guard(lambda: credit_notes.post_customer_cn(db, cn_id))


@router.post("/sales/credit-notes/{cn_id}/void", tags=["sales", "credit-notes"])
def void_customer_cn(cn_id: str, db: Session = Depends(get_db)) -> dict:
    return _guard(lambda: credit_notes.void_customer_cn(db, cn_id))


@router.post("/purchases/credit-notes", tags=["purchases", "credit-notes"])
def create_vendor_cn(payload: VendorCreditNoteIn, db: Session = Depends(get_db)) -> dict:
    return _guard(lambda: credit_notes.create_vendor_cn(db, payload))


@router.get("/purchases/credit-notes", tags=["purchases", "credit-notes"])
def list_vendor_cn(vendor_id: str | None = Query(None), db: Session = Depends(get_db)) -> list[dict]:
    return credit_notes.list_vendor_cn(db, vendor_id=vendor_id)


@router.get("/purchases/credit-notes/{cn_id}", tags=["purchases", "credit-notes"])
def get_vendor_cn(cn_id: str, db: Session = Depends(get_db)) -> dict:
    return _guard(lambda: credit_notes.get_vendor_cn(db, cn_id))


@router.put("/purchases/credit-notes/{cn_id}", tags=["purchases", "credit-notes"])
def update_vendor_cn(cn_id: str, payload: VendorCreditNoteIn, request: Request,
                     reason: str = Query(""), db: Session = Depends(get_db)) -> dict:
    actor = _require_perm(request, db, "edit")
    if not reason.strip():
        raise HTTPException(status_code=400, detail="A reason for the change is required.")
    return _guard(lambda: credit_notes.update_vendor_cn(db, cn_id, payload, actor=actor, reason=reason))


@router.get("/purchases/credit-notes/{cn_id}/audit", tags=["purchases", "credit-notes"])
def vendor_cn_audit(cn_id: str, request: Request, db: Session = Depends(get_db)) -> list[dict]:
    _require_perm(request, db, "view_audit")
    return _guard(lambda: credit_notes.vendor_cn_audit(db, cn_id))


@router.put("/purchases/credit-notes/{cn_id}/details", tags=["purchases", "credit-notes"])
def update_vendor_cn_details(cn_id: str, payload: CnMetaIn, request: Request,
                             reason: str = Query(""), db: Session = Depends(get_db)) -> dict:
    actor = _require_perm(request, db, "edit")
    return _guard(lambda: credit_notes.update_vendor_cn_meta(db, cn_id, payload, actor=actor, reason=reason or None))


@router.post("/purchases/credit-notes/{cn_id}/post", tags=["purchases", "credit-notes"])
def post_vendor_cn(cn_id: str, db: Session = Depends(get_db)) -> dict:
    return _guard(lambda: credit_notes.post_vendor_cn(db, cn_id))


@router.post("/purchases/credit-notes/{cn_id}/void", tags=["purchases", "credit-notes"])
def void_vendor_cn(cn_id: str, db: Session = Depends(get_db)) -> dict:
    return _guard(lambda: credit_notes.void_vendor_cn(db, cn_id))


@router.post("/credit-notes/apply", tags=["credit-notes"])
def apply_credit_note(payload: CreditNoteApplyIn, db: Session = Depends(get_db)) -> dict:
    return _guard(lambda: credit_notes.apply_credit_note(db, payload))


@router.get("/credit-notes/{cn_type}/{cn_id}/applications", tags=["credit-notes"])
def credit_note_applications(cn_type: str, cn_id: str, db: Session = Depends(get_db)) -> list[dict]:
    return credit_notes.list_applications(db, cn_type, cn_id)


@router.get("/reports/credit-notes", tags=["credit-notes", "reports"])
def credit_note_report(side: str = Query("customer"), db: Session = Depends(get_db)) -> dict:
    return credit_notes.credit_note_report(db, side=side)


# ── Advances (customer / vendor) + application ──────────────────────────────────────────────
@router.post("/sales/advances", tags=["sales", "advances"])
def create_customer_advance(payload: CustomerAdvanceIn, db: Session = Depends(get_db)) -> dict:
    return _guard(lambda: advances.create_customer_advance(db, payload))


@router.get("/sales/advances", tags=["sales", "advances"])
def list_customer_advances(customer_id: str | None = Query(None), available: bool = Query(False),
                           db: Session = Depends(get_db)) -> list[dict]:
    return advances.list_customer_advances(db, customer_id=customer_id, only_available=available)


@router.get("/sales/advances/{advance_id}", tags=["sales", "advances"])
def get_customer_advance(advance_id: str, db: Session = Depends(get_db)) -> dict:
    return _guard(lambda: advances.get_customer_advance(db, advance_id))


@router.put("/sales/advances/{advance_id}", tags=["sales", "advances"])
def update_customer_advance(advance_id: str, payload: CustomerAdvanceIn, request: Request,
                            reason: str = Query(""), db: Session = Depends(get_db)) -> dict:
    actor = _require_perm(request, db, "edit")
    if not reason.strip():
        raise HTTPException(status_code=400, detail="A reason for the change is required.")
    return _guard(lambda: advances.update_customer_advance(db, advance_id, payload, actor=actor, reason=reason))


@router.post("/purchases/advances", tags=["purchases", "advances"])
def create_vendor_advance(payload: VendorAdvanceIn, db: Session = Depends(get_db)) -> dict:
    return _guard(lambda: advances.create_vendor_advance(db, payload))


@router.get("/purchases/advances", tags=["purchases", "advances"])
def list_vendor_advances(vendor_id: str | None = Query(None), available: bool = Query(False),
                         db: Session = Depends(get_db)) -> list[dict]:
    return advances.list_vendor_advances(db, vendor_id=vendor_id, only_available=available)


@router.get("/purchases/advances/{advance_id}", tags=["purchases", "advances"])
def get_vendor_advance(advance_id: str, db: Session = Depends(get_db)) -> dict:
    return _guard(lambda: advances.get_vendor_advance(db, advance_id))


@router.put("/purchases/advances/{advance_id}", tags=["purchases", "advances"])
def update_vendor_advance(advance_id: str, payload: VendorAdvanceIn, request: Request,
                          reason: str = Query(""), db: Session = Depends(get_db)) -> dict:
    actor = _require_perm(request, db, "edit")
    if not reason.strip():
        raise HTTPException(status_code=400, detail="A reason for the change is required.")
    return _guard(lambda: advances.update_vendor_advance(db, advance_id, payload, actor=actor, reason=reason))


@router.post("/advances/apply", tags=["advances"])
def apply_advance(payload: AdvanceApplyIn, request: Request, db: Session = Depends(get_db)) -> dict:
    actor = _require_perm(request, db, "edit")
    return _guard(lambda: advances.apply_advance(db, payload, actor=actor))


@router.post("/advances/applications/{application_id}/unapply", tags=["advances"])
def unapply_advance(application_id: str, request: Request, reason: str = Query(""),
                    db: Session = Depends(get_db)) -> dict:
    actor = _require_perm(request, db, "reverse")
    return _guard(lambda: advances.unapply_advance(db, application_id, actor=actor, reason=reason or None))


@router.get("/advances/{advance_type}/{advance_id}/applications", tags=["advances"])
def advance_applications(advance_type: str, advance_id: str, db: Session = Depends(get_db)) -> list[dict]:
    return advances.list_applications(db, advance_type, advance_id)


@router.get("/advances/{advance_type}/{advance_id}/audit", tags=["advances"])
def advance_audit(advance_type: str, advance_id: str, request: Request,
                  db: Session = Depends(get_db)) -> list[dict]:
    _require_perm(request, db, "view_audit")
    return _guard(lambda: advances.advance_audit(db, advance_type, advance_id))


@router.get("/targets/{target_type}/{target_id}/advances", tags=["advances"])
def advances_for_target(target_type: str, target_id: str, db: Session = Depends(get_db)) -> list[dict]:
    """Advances applied to an invoice (sales_invoice) or bill (vendor_bill) — for drill-down."""
    return advances.applications_for_target(db, target_type, target_id)


@router.get("/reports/advances", tags=["advances", "reports"])
def advance_report(side: str = Query("customer"), db: Session = Depends(get_db)) -> dict:
    return advances.advance_report(db, side=side)


@router.get("/reports/advance-applications", tags=["advances", "reports"])
def advance_application_details(side: str = Query("customer"), db: Session = Depends(get_db)) -> dict:
    return advances.advance_application_details(db, side=side)


@router.get("/reports/advance-aging", tags=["advances", "reports"])
def advance_aging(side: str = Query("customer"), as_of: date | None = Query(None),
                  db: Session = Depends(get_db)) -> dict:
    return advances.advance_aging(db, side=side, as_of=as_of)


# ── Direct Expenses ───────────────────────────────────────────────────────────────────────
@router.post("/expenses", tags=["expenses"])
def create_expense(payload: ExpenseIn, db: Session = Depends(get_db)) -> dict:
    return _guard(lambda: expenses.create_expense(db, payload))


@router.get("/expenses", tags=["expenses"])
def list_expenses(status: str | None = Query(None), project: str | None = Query(None),
                  vendor_id: str | None = Query(None), db: Session = Depends(get_db)) -> list[dict]:
    return expenses.list_expenses(db, status=status, project=project, vendor_id=vendor_id)


@router.get("/expenses/report", tags=["expenses", "reports"])
def expense_report(group_by: str = Query("category"), start: date | None = Query(None),
                   end: date | None = Query(None), direct_only: bool = Query(False),
                   db: Session = Depends(get_db)) -> dict:
    return expenses.expense_report(db, group_by=group_by, start=start, end=end, direct_only=direct_only)


@router.get("/expenses/{expense_id}", tags=["expenses"])
def get_expense(expense_id: str, db: Session = Depends(get_db)) -> dict:
    return _guard(lambda: expenses.get_expense(db, expense_id))


@router.put("/expenses/{expense_id}", tags=["expenses"])
def update_expense(expense_id: str, payload: ExpenseIn, request: Request,
                   reason: str = Query(""), db: Session = Depends(get_db)) -> dict:
    actor = _require_perm(request, db, "edit")
    if not reason.strip():
        raise HTTPException(status_code=400, detail="A reason for the change is required.")
    return _guard(lambda: expenses.update_expense(db, expense_id, payload, actor=actor, reason=reason))


@router.get("/expenses/{expense_id}/audit", tags=["expenses"])
def expense_audit(expense_id: str, request: Request, db: Session = Depends(get_db)) -> list[dict]:
    _require_perm(request, db, "view_audit")
    return _guard(lambda: expenses.expense_audit(db, expense_id))


@router.post("/expenses/{expense_id}/post", tags=["expenses"])
def post_expense(expense_id: str, db: Session = Depends(get_db)) -> dict:
    return _guard(lambda: expenses.post_expense(db, expense_id))


@router.post("/expenses/{expense_id}/void", tags=["expenses"])
def void_expense(expense_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
    _require_perm(request, db, "reverse")
    return _guard(lambda: expenses.void_expense(db, expense_id))


@router.post("/calc/preview", tags=["calc"])
def calc_preview(payload: CalcPreviewIn, db: Session = Depends(get_db)) -> dict:
    s = calc.document_summary(
        subtotal=payload.subtotal, discount=payload.discount, vat_rate=payload.vat_rate,
        vat_amount=payload.vat_amount, retention_basis=payload.retention_basis,
        retention_percent=payload.retention_percent, retention_amount=payload.retention_amount,
        advance_recovery=payload.advance_recovery)
    return {k: str(v) for k, v in s.items()}


# ── Transaction document attachments ──────────────────────────────────────────────────────
@router.post("/attachments", response_model=AttachmentOut, tags=["attachments"])
async def upload_attachment(
    request: Request, entity_type: str = Form(...), entity_id: str = Form(...),
    file: UploadFile = File(...), db: Session = Depends(get_db),
) -> AttachmentOut:
    actor = _require_editor(request, db)
    data = await file.read()
    att = _guard(lambda: attachments.save_upload(db, entity_type, entity_id, file.filename or "file", data, actor))
    return attachments.serialize(db, att)


@router.get("/attachments", response_model=list[AttachmentOut], tags=["attachments"])
def list_attachments(entity_type: str = Query(...), entity_id: str = Query(...),
                     db: Session = Depends(get_db)) -> list[AttachmentOut]:
    return [attachments.serialize(db, a) for a in attachments.list_for(db, entity_type, entity_id)]


@router.get("/attachments/status", response_model=AttachmentStatusOut, tags=["attachments"])
def attachment_status(entity_type: str = Query(...), entity_id: str = Query(...),
                      db: Session = Depends(get_db)) -> AttachmentStatusOut:
    return attachments.status_for(db, entity_type, entity_id)


@router.get("/attachments/status-bulk", tags=["attachments"])
def attachment_status_bulk(entity_type: str = Query(...), ids: str = Query(""),
                           db: Session = Depends(get_db)) -> dict:
    """Attachment status for many transactions at once (ids = comma-separated)."""
    id_list = [i for i in ids.split(",") if i]
    return attachments.status_bulk(db, entity_type, id_list)


@router.get("/attachments/{att_id}", response_model=AttachmentOut, tags=["attachments"])
def get_attachment(att_id: str, db: Session = Depends(get_db)) -> AttachmentOut:
    return _guard(lambda: attachments.serialize(db, attachments.get(db, att_id)))


@router.get("/attachments/{att_id}/download", tags=["attachments"])
def download_attachment(att_id: str, request: Request, disposition: str = Query("attachment"),
                        db: Session = Depends(get_db)):
    from fastapi import Response
    from ..services.storage import StorageError
    actor, _ = _actor_role(request, db)
    try:
        att = attachments.get(db, att_id)
    except AttachmentError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    try:
        data = attachments.read_bytes(att)   # durable storage (filesystem path or Vercel Blob)
    except StorageError as e:
        raise HTTPException(status_code=410, detail=str(e)) from e
    disp = "inline" if disposition == "inline" else "attachment"
    attachments.record_access(db, att_id, actor, download=(disp == "attachment"))
    safe = att.display_name.replace('"', "")
    return Response(content=data, media_type=att.mime_type,
                    headers={"Content-Disposition": f'{disp}; filename="{safe}"'})


@router.get("/attachments/{att_id}/events", response_model=list[AttachmentEventOut], tags=["attachments"])
def attachment_events(att_id: str, db: Session = Depends(get_db)) -> list[AttachmentEventOut]:
    evs = _guard(lambda: attachments.events_for(db, att_id))
    return [AttachmentEventOut(at=(e.at.isoformat() if e.at else None), actor=e.actor,
                               action=e.action, note=e.note) for e in evs]


@router.patch("/attachments/{att_id}", response_model=AttachmentOut, tags=["attachments"])
def rename_attachment(att_id: str, body: AttachmentRenameIn, request: Request,
                      db: Session = Depends(get_db)) -> AttachmentOut:
    actor = _require_editor(request, db)
    return _guard(lambda: attachments.serialize(db, attachments.rename(db, att_id, body.display_name, actor)))


@router.post("/attachments/{att_id}/replace", response_model=AttachmentOut, tags=["attachments"])
async def replace_attachment(att_id: str, request: Request, file: UploadFile = File(...),
                             db: Session = Depends(get_db)) -> AttachmentOut:
    actor = _require_editor(request, db)
    data = await file.read()
    att = _guard(lambda: attachments.replace(db, att_id, file.filename or "file", data, actor))
    return attachments.serialize(db, att)


@router.post("/attachments/{att_id}/review", response_model=AttachmentOut, tags=["attachments"])
def review_attachment(att_id: str, body: AttachmentReviewIn, request: Request,
                      db: Session = Depends(get_db)) -> AttachmentOut:
    actor = _require_editor(request, db)
    return _guard(lambda: attachments.serialize(db, attachments.mark_reviewed(db, att_id, actor, body.note)))


@router.post("/attachments/{att_id}/extract", response_model=AttachmentOut, tags=["attachments"])
def reextract_attachment(att_id: str, request: Request, db: Session = Depends(get_db)) -> AttachmentOut:
    actor = _require_editor(request, db)
    return _guard(lambda: attachments.serialize(db, attachments.reextract(db, att_id, actor)))


@router.delete("/attachments/{att_id}", tags=["attachments"])
def delete_attachment(att_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
    actor = _require_editor(request, db)
    _guard(lambda: attachments.soft_delete(db, att_id, actor))
    return {"ok": True}


# ── Sales ───────────────────────────────────────────────────────────────────────────────
@router.post("/sales/customers", response_model=CustomerOut, tags=["sales"])
def create_customer(payload: CustomerIn, db: Session = Depends(get_db)) -> CustomerOut:
    return _guard(lambda: sales.create_customer(db, payload))


@router.get("/sales/customers", response_model=list[CustomerOut], tags=["sales"])
def list_customers(active_only: bool = Query(True), db: Session = Depends(get_db)) -> list[CustomerOut]:
    return sales.list_customers(db, active_only=active_only)


@router.get("/sales/customers/{customer_id}", response_model=CustomerOut, tags=["sales"])
def get_customer(customer_id: str, db: Session = Depends(get_db)) -> CustomerOut:
    return _guard(lambda: sales.get_customer(db, customer_id))


@router.put("/sales/customers/{customer_id}", response_model=CustomerOut, tags=["sales"])
def update_customer(customer_id: str, payload: CustomerIn, request: Request,
                    db: Session = Depends(get_db)) -> CustomerOut:
    actor = _require_editor(request, db)
    return _guard(lambda: sales.update_customer(db, customer_id, payload, actor=actor))


@router.get("/sales/customers/{customer_id}/audit", tags=["sales"])
def customer_audit(customer_id: str, db: Session = Depends(get_db)) -> list[dict]:
    return _guard(lambda: sales.customer_audit(db, customer_id))


@router.post("/sales/invoices", response_model=InvoiceOut, tags=["sales"])
def create_invoice(payload: InvoiceIn, db: Session = Depends(get_db)) -> InvoiceOut:
    return _guard(lambda: sales.create_invoice(db, payload))


@router.get("/sales/invoices", response_model=list[InvoiceSummary], tags=["sales"])
def list_invoices(
    customer_id: str | None = Query(None), status: str | None = Query(None), db: Session = Depends(get_db)
) -> list[InvoiceSummary]:
    return sales.list_invoices(db, customer_id=customer_id, status=status)


@router.get("/sales/invoices/{invoice_id}", response_model=InvoiceOut, tags=["sales"])
def get_invoice(invoice_id: str, db: Session = Depends(get_db)) -> InvoiceOut:
    return _guard(lambda: sales.get_invoice(db, invoice_id))


@router.put("/sales/invoices/{invoice_id}", response_model=InvoiceOut, tags=["sales"])
def update_invoice(invoice_id: str, payload: InvoiceIn, request: Request,
                   reason: str = Query(""), db: Session = Depends(get_db)) -> InvoiceOut:
    actor = _require_perm(request, db, "edit")
    if not reason.strip():
        raise HTTPException(status_code=400, detail="A reason for the change is required.")
    return _guard(lambda: sales.update_invoice(db, invoice_id, payload, actor=actor, reason=reason))


@router.get("/sales/invoices/{invoice_id}/audit", tags=["sales"])
def invoice_audit(invoice_id: str, request: Request, db: Session = Depends(get_db)) -> list[dict]:
    _require_perm(request, db, "view_audit")
    return _guard(lambda: sales.invoice_audit(db, invoice_id))


@router.put("/sales/invoices/{invoice_id}/details", response_model=InvoiceOut, tags=["sales"])
def update_invoice_details(invoice_id: str, payload: InvoiceMetaIn, request: Request,
                           reason: str = Query(""), db: Session = Depends(get_db)) -> InvoiceOut:
    """Edit non-financial invoice fields (safe even when paid)."""
    actor = _require_perm(request, db, "edit")
    return _guard(lambda: sales.update_invoice_meta(db, invoice_id, payload, actor=actor, reason=reason or None))


@router.post("/sales/invoices/{invoice_id}/post", response_model=InvoiceOut, tags=["sales"])
def post_invoice(invoice_id: str, db: Session = Depends(get_db)) -> InvoiceOut:
    return _guard(lambda: sales.post_invoice(db, invoice_id))


@router.post("/sales/invoices/{invoice_id}/void", response_model=InvoiceOut, tags=["sales"])
def void_invoice(invoice_id: str, request: Request, db: Session = Depends(get_db)) -> InvoiceOut:
    _require_perm(request, db, "reverse")
    return _guard(lambda: sales.void_invoice(db, invoice_id))


@router.post("/sales/payments", response_model=PaymentOut, tags=["sales"])
def record_payment(payload: PaymentIn, db: Session = Depends(get_db)) -> PaymentOut:
    return _guard(lambda: sales.record_payment(db, payload))


@router.post("/sales/payments/{payment_id}/void", tags=["sales"])
def void_payment(payment_id: str, request: Request, reason: str = Query(""),
                 db: Session = Depends(get_db)) -> dict:
    actor = _require_perm(request, db, "reverse")
    return _guard(lambda: sales.void_payment(db, payment_id, actor=actor, reason=reason or None))


# ── Recurring / subscription invoicing ──────────────────────────────────────────────────
@router.get("/recurring", tags=["recurring"])
def list_recurring(active_only: bool = Query(False), db: Session = Depends(get_db)) -> list[dict]:
    return recurring.list_plans(db, active_only=active_only)


@router.post("/recurring", tags=["recurring"])
def create_recurring(payload: RecurringPlanIn, request: Request, db: Session = Depends(get_db)) -> dict:
    _require_perm(request, db, "create")
    return _guard(lambda: recurring.create_plan(db, payload))


@router.get("/recurring/{plan_id}", tags=["recurring"])
def get_recurring(plan_id: str, db: Session = Depends(get_db)) -> dict:
    return _guard(lambda: recurring.get_plan(db, plan_id))


@router.put("/recurring/{plan_id}", tags=["recurring"])
def update_recurring(plan_id: str, payload: RecurringPlanIn, request: Request,
                     db: Session = Depends(get_db)) -> dict:
    _require_perm(request, db, "edit")
    return _guard(lambda: recurring.update_plan(db, plan_id, payload))


@router.post("/recurring/{plan_id}/active", tags=["recurring"])
def set_recurring_active(plan_id: str, active: bool = Query(...), request: Request = None,
                         db: Session = Depends(get_db)) -> dict:
    _require_perm(request, db, "edit")
    return _guard(lambda: recurring.set_active(db, plan_id, active))


@router.get("/recurring/{plan_id}/runs", tags=["recurring"])
def recurring_runs(plan_id: str, db: Session = Depends(get_db)) -> list[dict]:
    return _guard(lambda: recurring.runs_for(db, plan_id))


@router.post("/recurring/{plan_id}/generate", tags=["recurring"])
def generate_recurring(plan_id: str, request: Request, on_date: date | None = Query(None),
                       db: Session = Depends(get_db)) -> dict:
    _require_perm(request, db, "create")
    return _guard(lambda: recurring.generate_now(db, plan_id, on_date))


@router.post("/recurring/run", tags=["recurring"])
def run_recurring_due(request: Request, as_of: date = Query(...), catch_up: bool = Query(False),
                      db: Session = Depends(get_db)) -> dict:
    _require_perm(request, db, "create")
    return _guard(lambda: recurring.run_due(db, as_of, catch_up=catch_up))


def _pdf_response(pair):
    data, fname = pair
    from fastapi.responses import Response as _Resp
    return _Resp(content=data, media_type="application/pdf",
                 headers={"Content-Disposition": f'inline; filename="{fname}"'})


# ── Document layout templates (invoice/receipt customization) ────────────────────────────────
@router.get("/document-templates", tags=["templates"])
def list_templates(doc_type: str | None = Query(None), db: Session = Depends(get_db)) -> list[dict]:
    return templates.list_templates(db, doc_type=doc_type)


@router.post("/document-templates", tags=["templates"])
def create_template(payload: DocumentTemplateIn, request: Request, db: Session = Depends(get_db)) -> dict:
    _require_editor(request, db)
    return _guard(lambda: templates.create(db, payload))


@router.get("/document-templates/{template_id}", tags=["templates"])
def get_template(template_id: str, db: Session = Depends(get_db)) -> dict:
    from ..services.templates import _out
    return _guard(lambda: _out(templates.get(db, template_id)))


@router.put("/document-templates/{template_id}", tags=["templates"])
def update_template(template_id: str, payload: DocumentTemplateIn, request: Request,
                    db: Session = Depends(get_db)) -> dict:
    _require_editor(request, db)
    return _guard(lambda: templates.update(db, template_id, payload))


@router.post("/document-templates/{template_id}/default", tags=["templates"])
def set_default_template(template_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
    _require_editor(request, db)
    return _guard(lambda: templates.set_default(db, template_id))


@router.delete("/document-templates/{template_id}", tags=["templates"])
def delete_template(template_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
    _require_editor(request, db)
    _guard(lambda: templates.delete(db, template_id))
    return {"ok": True}


@router.post("/document-templates/preview", tags=["templates"])
def preview_template(payload: DocumentTemplateIn, db: Session = Depends(get_db)):
    cfg = templates.config_from_payload(payload)
    data = doc_pdf.preview(db, payload.doc_type, cfg)
    from fastapi.responses import Response as _Resp
    return _Resp(content=data, media_type="application/pdf",
                 headers={"Content-Disposition": 'inline; filename="preview.pdf"'})


@router.get("/sales/invoices/{invoice_id}/pdf", tags=["sales"])
def invoice_pdf(invoice_id: str, request: Request, template_id: str | None = Query(None), db: Session = Depends(get_db)):
    r = _guard(lambda: _pdf_response(doc_pdf.invoice_pdf(db, invoice_id, template_id)))
    _log_export(db, request, kind="document", ref=f"invoice/{invoice_id}", fmt="pdf")
    return r


@router.get("/sales/credit-notes/{cn_id}/pdf", tags=["sales", "credit-notes"])
def customer_cn_pdf(cn_id: str, request: Request, template_id: str | None = Query(None), db: Session = Depends(get_db)):
    r = _guard(lambda: _pdf_response(doc_pdf.customer_cn_pdf(db, cn_id, template_id)))
    _log_export(db, request, kind="document", ref=f"customer_cn/{cn_id}", fmt="pdf")
    return r


@router.get("/sales/payments/{payment_id}/receipt", tags=["sales"])
def customer_receipt_pdf(payment_id: str, request: Request, template_id: str | None = Query(None), db: Session = Depends(get_db)):
    try:
        r = _pdf_response(doc_pdf.receipt_pdf(db, payment_id, "customer", template_id))
        _log_export(db, request, kind="document", ref=f"customer_receipt/{payment_id}", fmt="pdf")
        return r
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.get("/purchases/payments/{payment_id}/receipt", tags=["purchases"])
def vendor_receipt_pdf(payment_id: str, request: Request, template_id: str | None = Query(None), db: Session = Depends(get_db)):
    try:
        r = _pdf_response(doc_pdf.receipt_pdf(db, payment_id, "vendor", template_id))
        _log_export(db, request, kind="document", ref=f"vendor_receipt/{payment_id}", fmt="pdf")
        return r
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.get("/purchases/bills/{bill_id}/pdf", tags=["purchases"])
def bill_pdf(bill_id: str, request: Request, template_id: str | None = Query(None), db: Session = Depends(get_db)):
    r = _guard(lambda: _pdf_response(doc_pdf.bill_pdf(db, bill_id, template_id)))
    _log_export(db, request, kind="document", ref=f"bill/{bill_id}", fmt="pdf")
    return r


@router.get("/purchases/credit-notes/{cn_id}/pdf", tags=["purchases", "credit-notes"])
def vendor_cn_pdf(cn_id: str, request: Request, template_id: str | None = Query(None), db: Session = Depends(get_db)):
    r = _guard(lambda: _pdf_response(doc_pdf.vendor_cn_pdf(db, cn_id, template_id)))
    _log_export(db, request, kind="document", ref=f"vendor_cn/{cn_id}", fmt="pdf")
    return r


@router.get("/expenses/{expense_id}/pdf", tags=["expenses"])
def expense_pdf(expense_id: str, request: Request, db: Session = Depends(get_db)):
    r = _guard(lambda: _pdf_response(doc_pdf.expense_pdf(db, expense_id)))
    _log_export(db, request, kind="document", ref=f"expense/{expense_id}", fmt="pdf")
    return r


@router.get("/journal-entries/{entry_id}/pdf", tags=["journal"])
def journal_entry_pdf(entry_id: str, request: Request, db: Session = Depends(get_db)):
    r = _guard(lambda: _pdf_response(doc_pdf.journal_entry_pdf(db, entry_id)))
    _log_export(db, request, kind="document", ref=f"journal/{entry_id}", fmt="pdf")
    return r


@router.get("/sales/advances/{advance_id}/pdf", tags=["sales", "advances"])
def customer_advance_pdf(advance_id: str, request: Request, db: Session = Depends(get_db)):
    r = _guard(lambda: _pdf_response(doc_pdf.advance_pdf(db, "customer", advance_id)))
    _log_export(db, request, kind="document", ref=f"customer_advance/{advance_id}", fmt="pdf")
    return r


@router.get("/purchases/advances/{advance_id}/pdf", tags=["purchases", "advances"])
def vendor_advance_pdf(advance_id: str, request: Request, db: Session = Depends(get_db)):
    r = _guard(lambda: _pdf_response(doc_pdf.advance_pdf(db, "vendor", advance_id)))
    _log_export(db, request, kind="document", ref=f"vendor_advance/{advance_id}", fmt="pdf")
    return r


# Centralized PDF builders (kind → builder + attachment entity type) for Archive integration.
_DOC_KINDS = {
    "invoice": (doc_pdf.invoice_pdf, "sales_invoice"),
    "customer_cn": (doc_pdf.customer_cn_pdf, "customer_credit_note"),
    "bill": (doc_pdf.bill_pdf, "vendor_bill"),
    "vendor_cn": (doc_pdf.vendor_cn_pdf, "vendor_credit_note"),
    "expense": (doc_pdf.expense_pdf, "expense"),
    "journal": (doc_pdf.journal_entry_pdf, "journal_entry"),
    "customer_advance": (lambda db, i: doc_pdf.advance_pdf(db, "customer", i), "customer_advance"),
    "vendor_advance": (lambda db, i: doc_pdf.advance_pdf(db, "vendor", i), "vendor_advance"),
    "einvoice": (doc_pdf.einvoice_pdf, "einvoice"),
}


@router.post("/documents/{kind}/{doc_id}/archive-pdf", tags=["attachments"])
def archive_document_pdf(kind: str, doc_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
    """Render a document's PDF and save it to the Archive (as an attachment linked to the
    transaction), so it can later be viewed / downloaded / re-exported with full history."""
    actor = _require_editor(request, db)
    if kind not in _DOC_KINDS:
        raise HTTPException(status_code=400, detail=f"Unknown document kind '{kind}'.")
    builder, entity_type = _DOC_KINDS[kind]
    data, fname = _guard(lambda: builder(db, doc_id))
    att = _guard(lambda: attachments.save_upload(db, entity_type, doc_id, fname, data, actor))
    _log_export(db, request, kind="document", ref=f"{kind}/{doc_id}", fmt="pdf-archive", filename=fname)
    return attachments.serialize(db, att)


@router.get("/sales/invoices/{invoice_id}/payments", tags=["sales"])
def list_invoice_payments(invoice_id: str, db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(select(CustomerPayment).where(CustomerPayment.invoice_id == invoice_id)
                      .order_by(CustomerPayment.date)).scalars()
    return [{"id": p.id, "date": str(p.date), "amount": str(p.amount), "reference": p.reference,
             "method": p.method, "journal_entry_id": p.journal_entry_id} for p in rows]


@router.post("/sales/invoices/{invoice_id}/release-retention", response_model=InvoiceOut, tags=["sales"])
def release_invoice_retention(invoice_id: str, payload: RetentionReleaseIn,
                             db: Session = Depends(get_db)) -> InvoiceOut:
    return _guard(lambda: sales.release_retention(db, invoice_id, payload))


@router.post("/sales/invoices/{invoice_id}/hold-retention", response_model=InvoiceOut, tags=["sales"])
def hold_invoice_retention(invoice_id: str, payload: RetentionReleaseIn, request: Request,
                           db: Session = Depends(get_db)) -> InvoiceOut:
    actor = _require_perm(request, db, "edit")
    return _guard(lambda: sales.hold_retention(db, invoice_id, payload, actor=actor))


@router.get("/reports/retention", tags=["reports"])
def retention_report(side: str = Query("customer"), db: Session = Depends(get_db)) -> dict:
    return reports.retention_report(db, side=side)


@router.get("/sales/aging", response_model=ArAgingReport, tags=["sales", "reports"])
def ar_aging(as_of: date | None = Query(None), db: Session = Depends(get_db)) -> ArAgingReport:
    return reports.ar_aging(db, as_of=as_of)


# ── Purchases / Accounts Payable ─────────────────────────────────────────────────────────
@router.post("/purchases/vendors", response_model=VendorOut, tags=["purchases"])
def create_vendor(payload: VendorIn, db: Session = Depends(get_db)) -> VendorOut:
    return _guard(lambda: purchases.create_vendor(db, payload))


@router.get("/purchases/vendors", response_model=list[VendorOut], tags=["purchases"])
def list_vendors(active_only: bool = Query(True), db: Session = Depends(get_db)) -> list[VendorOut]:
    return purchases.list_vendors(db, active_only=active_only)


@router.get("/purchases/vendors/{vendor_id}", response_model=VendorOut, tags=["purchases"])
def get_vendor(vendor_id: str, db: Session = Depends(get_db)) -> VendorOut:
    return _guard(lambda: purchases.get_vendor(db, vendor_id))


@router.put("/purchases/vendors/{vendor_id}", response_model=VendorOut, tags=["purchases"])
def update_vendor(vendor_id: str, payload: VendorIn, request: Request,
                  db: Session = Depends(get_db)) -> VendorOut:
    actor = _require_editor(request, db)
    return _guard(lambda: purchases.update_vendor(db, vendor_id, payload, actor=actor))


@router.get("/purchases/vendors/{vendor_id}/audit", tags=["purchases"])
def vendor_audit(vendor_id: str, db: Session = Depends(get_db)) -> list[dict]:
    return _guard(lambda: purchases.vendor_audit(db, vendor_id))


@router.post("/purchases/bills", response_model=BillOut, tags=["purchases"])
def create_bill(payload: BillIn, db: Session = Depends(get_db)) -> BillOut:
    return _guard(lambda: purchases.create_bill(db, payload))


@router.get("/purchases/bills", response_model=list[BillSummary], tags=["purchases"])
def list_bills(
    vendor_id: str | None = Query(None), status: str | None = Query(None), db: Session = Depends(get_db)
) -> list[BillSummary]:
    return purchases.list_bills(db, vendor_id=vendor_id, status=status)


@router.get("/purchases/bills/{bill_id}", response_model=BillOut, tags=["purchases"])
def get_bill(bill_id: str, db: Session = Depends(get_db)) -> BillOut:
    return _guard(lambda: purchases.get_bill(db, bill_id))


@router.put("/purchases/bills/{bill_id}", response_model=BillOut, tags=["purchases"])
def update_bill(bill_id: str, payload: BillIn, request: Request,
                reason: str = Query(""), db: Session = Depends(get_db)) -> BillOut:
    actor = _require_perm(request, db, "edit")
    if not reason.strip():
        raise HTTPException(status_code=400, detail="A reason for the change is required.")
    return _guard(lambda: purchases.update_bill(db, bill_id, payload, actor=actor, reason=reason))


@router.get("/purchases/bills/{bill_id}/audit", tags=["purchases"])
def bill_audit(bill_id: str, request: Request, db: Session = Depends(get_db)) -> list[dict]:
    _require_perm(request, db, "view_audit")
    return _guard(lambda: purchases.bill_audit(db, bill_id))


@router.put("/purchases/bills/{bill_id}/details", response_model=BillOut, tags=["purchases"])
def update_bill_details(bill_id: str, payload: BillMetaIn, request: Request,
                        reason: str = Query(""), db: Session = Depends(get_db)) -> BillOut:
    """Edit non-financial bill fields (safe even when paid)."""
    actor = _require_perm(request, db, "edit")
    return _guard(lambda: purchases.update_bill_meta(db, bill_id, payload, actor=actor, reason=reason or None))


@router.post("/purchases/bills/{bill_id}/post", response_model=BillOut, tags=["purchases"])
def post_bill(bill_id: str, db: Session = Depends(get_db)) -> BillOut:
    return _guard(lambda: purchases.post_bill(db, bill_id))


@router.post("/purchases/bills/{bill_id}/void", response_model=BillOut, tags=["purchases"])
def void_bill(bill_id: str, request: Request, db: Session = Depends(get_db)) -> BillOut:
    _require_perm(request, db, "reverse")
    return _guard(lambda: purchases.void_bill(db, bill_id))


@router.post("/purchases/payments", response_model=BillPaymentOut, tags=["purchases"])
def pay_bill(payload: BillPaymentIn, db: Session = Depends(get_db)) -> BillPaymentOut:
    return _guard(lambda: purchases.pay_bill(db, payload))


@router.post("/purchases/payments/{payment_id}/void", tags=["purchases"])
def void_bill_payment(payment_id: str, request: Request, reason: str = Query(""),
                      db: Session = Depends(get_db)) -> dict:
    actor = _require_perm(request, db, "reverse")
    return _guard(lambda: purchases.void_bill_payment(db, payment_id, actor=actor, reason=reason or None))


@router.get("/purchases/bills/{bill_id}/payments", tags=["purchases"])
def list_bill_payments(bill_id: str, db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(select(BillPayment).where(BillPayment.bill_id == bill_id)
                      .order_by(BillPayment.date)).scalars()
    return [{"id": p.id, "date": str(p.date), "amount": str(p.amount), "reference": p.reference,
             "method": p.method, "journal_entry_id": p.journal_entry_id} for p in rows]


@router.post("/purchases/bills/{bill_id}/release-retention", response_model=BillOut, tags=["purchases"])
def release_bill_retention(bill_id: str, payload: RetentionReleaseIn,
                           db: Session = Depends(get_db)) -> BillOut:
    return _guard(lambda: purchases.release_retention(db, bill_id, payload))


@router.post("/purchases/bills/{bill_id}/hold-retention", response_model=BillOut, tags=["purchases"])
def hold_bill_retention(bill_id: str, payload: RetentionReleaseIn, request: Request,
                        db: Session = Depends(get_db)) -> BillOut:
    actor = _require_perm(request, db, "edit")
    return _guard(lambda: purchases.hold_retention(db, bill_id, payload, actor=actor))


@router.get("/purchases/aging", response_model=ApAgingReport, tags=["purchases", "reports"])
def ap_aging(as_of: date | None = Query(None), db: Session = Depends(get_db)) -> ApAgingReport:
    return reports.ap_aging(db, as_of=as_of)


# ── Cross-module document reports ────────────────────────────────────────────────────────
@router.get("/reports/invoice-by-vendor", response_model=DocReport, tags=["reports"])
def invoice_by_vendor(
    group_by: str = Query("vendor"),
    start: date | None = Query(None),
    end: date | None = Query(None),
    db: Session = Depends(get_db),
) -> DocReport:
    return reports.invoice_by_vendor(db, group_by=group_by, start=start, end=end)


@router.get("/reports/sales-by-customer", response_model=DocReport, tags=["reports"])
def sales_by_customer(
    group_by: str = Query("customer"),
    start: date | None = Query(None),
    end: date | None = Query(None),
    db: Session = Depends(get_db),
) -> DocReport:
    return reports.sales_by_customer(db, group_by=group_by, start=start, end=end)


# ── Fixed Assets ─────────────────────────────────────────────────────────────────────────
@router.post("/assets", response_model=AssetOut, tags=["assets"])
def create_asset(payload: AssetIn, db: Session = Depends(get_db)) -> AssetOut:
    return _guard(lambda: assets.create_asset(db, payload))


@router.get("/assets", response_model=list[AssetOut], tags=["assets"])
def list_assets(
    status: str | None = Query(None), category: str | None = Query(None), db: Session = Depends(get_db)
) -> list[AssetOut]:
    return assets.list_assets(db, status=status, category=category)


@router.get("/assets/register", response_model=AssetRegister, tags=["assets", "reports"])
def asset_register(
    category: str | None = Query(None),
    location: str | None = Query(None),
    department: str | None = Query(None),
    project: str | None = Query(None),
    status: str | None = Query(None),
    db: Session = Depends(get_db),
) -> AssetRegister:
    return reports.fixed_asset_register(db, category=category, location=location,
                                        department=department, project=project, status=status)


@router.get("/assets/dashboard", response_model=AssetDashboard, tags=["assets", "reports"])
def asset_dashboard(db: Session = Depends(get_db)) -> AssetDashboard:
    return reports.fixed_asset_dashboard(db)


@router.get("/assets/{asset_id}", response_model=AssetOut, tags=["assets"])
def get_asset(asset_id: str, db: Session = Depends(get_db)) -> AssetOut:
    return _guard(lambda: assets.get_asset(db, asset_id))


@router.put("/assets/{asset_id}", response_model=AssetOut, tags=["assets"])
def update_asset(asset_id: str, payload: AssetIn, request: Request,
                 reason: str = Query(""), db: Session = Depends(get_db)) -> AssetOut:
    actor = _require_perm(request, db, "edit")
    if not reason.strip():
        raise HTTPException(status_code=400, detail="A reason for the change is required.")
    return _guard(lambda: assets.update_asset(db, asset_id, payload, actor=actor, reason=reason))


@router.get("/assets/{asset_id}/audit", tags=["assets"])
def asset_audit(asset_id: str, request: Request, db: Session = Depends(get_db)) -> list[dict]:
    _require_perm(request, db, "view_audit")
    return _guard(lambda: assets.asset_audit(db, asset_id))


@router.get("/assets/{asset_id}/schedule", response_model=list[DepreciationScheduleRow], tags=["assets"])
def asset_schedule(asset_id: str, db: Session = Depends(get_db)) -> list[DepreciationScheduleRow]:
    return _guard(lambda: assets.depreciation_schedule(db, asset_id))


@router.post("/assets/{asset_id}/dispose", response_model=AssetOut, tags=["assets"])
def dispose_asset(asset_id: str, payload: DisposalIn, db: Session = Depends(get_db)) -> AssetOut:
    return _guard(lambda: assets.dispose_asset(db, asset_id, payload))


@router.post("/assets/depreciation/run", tags=["assets"])
def run_depreciation(payload: RunDepreciationIn, db: Session = Depends(get_db)) -> dict:
    return _guard(lambda: assets.run_depreciation(db, payload.as_of))


# ── Banking ──────────────────────────────────────────────────────────────────────────────
@router.post("/banking/accounts", response_model=BankAccountOut, tags=["banking"])
def create_bank_account(payload: BankAccountIn, db: Session = Depends(get_db)) -> BankAccountOut:
    return _guard(lambda: banking.create_bank_account(db, payload))


@router.get("/banking/accounts", response_model=list[BankAccountOut], tags=["banking"])
def list_bank_accounts(db: Session = Depends(get_db)) -> list[BankAccountOut]:
    return banking.list_bank_accounts(db)


@router.get("/banking/accounts/{bank_account_id}", response_model=BankAccountOut, tags=["banking"])
def get_bank_account(bank_account_id: str, db: Session = Depends(get_db)) -> BankAccountOut:
    return _guard(lambda: banking.get_bank_account(db, bank_account_id))


@router.put("/banking/accounts/{bank_account_id}", response_model=BankAccountOut, tags=["banking"])
def update_bank_account(bank_account_id: str, payload: BankAccountUpdateIn, request: Request,
                        db: Session = Depends(get_db)) -> BankAccountOut:
    actor = _require_editor(request, db)
    return _guard(lambda: banking.update_bank_account(db, bank_account_id, payload, actor=actor))


@router.get("/banking/accounts/{bank_account_id}/audit", tags=["banking"])
def bank_account_audit(bank_account_id: str, db: Session = Depends(get_db)) -> list[dict]:
    return _guard(lambda: banking.bank_account_audit(db, bank_account_id))


@router.post("/banking/transfers", tags=["banking"])
def bank_transfer(payload: TransferIn, db: Session = Depends(get_db)) -> dict:
    return _guard(lambda: banking.transfer(db, payload))


@router.post("/banking/statement/import", tags=["banking"])
def import_statement(payload: StatementImportIn, db: Session = Depends(get_db)) -> dict:
    return _guard(lambda: banking.import_statement(db, payload))


@router.get("/banking/statement-lines", response_model=list[StatementLineOut], tags=["banking"])
def list_statement_lines(
    bank_account_id: str = Query(...), status: str | None = Query(None), db: Session = Depends(get_db)
) -> list[StatementLineOut]:
    return banking.list_statement_lines(db, bank_account_id, status=status)


@router.get("/banking/statement-lines/{line_id}/candidates", response_model=list[MatchCandidate], tags=["banking"])
def match_candidates(line_id: str, db: Session = Depends(get_db)) -> list[MatchCandidate]:
    return _guard(lambda: banking.match_candidates(db, line_id))


@router.post("/banking/match", response_model=StatementLineOut, tags=["banking"])
def manual_match(payload: ManualMatchIn, db: Session = Depends(get_db)) -> StatementLineOut:
    return _guard(lambda: banking.manual_match(db, payload.statement_line_id, payload.entry_id))


@router.post("/banking/statement-lines/{line_id}/unmatch", response_model=StatementLineOut, tags=["banking"])
def unmatch(line_id: str, db: Session = Depends(get_db)) -> StatementLineOut:
    return _guard(lambda: banking.unmatch(db, line_id))


@router.post("/banking/auto-match", tags=["banking"])
def auto_match(payload: AutoMatchIn, db: Session = Depends(get_db)) -> dict:
    return _guard(lambda: banking.auto_match(db, payload.bank_account_id, payload.window_days))


@router.post("/banking/reconcile/summary", response_model=ReconcileSummary, tags=["banking"])
def reconcile_summary(payload: ReconcileSummaryIn, db: Session = Depends(get_db)) -> ReconcileSummary:
    return _guard(lambda: banking.reconcile_summary(db, payload))


@router.post("/banking/reconcile/complete", response_model=ReconciliationOut, tags=["banking"])
def complete_reconciliation(payload: ReconcileSummaryIn, db: Session = Depends(get_db)) -> ReconciliationOut:
    return _guard(lambda: banking.complete_reconciliation(db, payload))


@router.get("/banking/reconciliations", response_model=list[ReconciliationOut], tags=["banking"])
def list_reconciliations(
    bank_account_id: str | None = Query(None), db: Session = Depends(get_db)
) -> list[ReconciliationOut]:
    return banking.list_reconciliations(db, bank_account_id=bank_account_id)


# ── Inventory ────────────────────────────────────────────────────────────────────────────
@router.post("/inventory/warehouses", response_model=WarehouseOut, tags=["inventory"])
def create_warehouse(payload: WarehouseIn, db: Session = Depends(get_db)) -> WarehouseOut:
    return _guard(lambda: inventory.create_warehouse(db, payload))


@router.get("/inventory/warehouses", response_model=list[WarehouseOut], tags=["inventory"])
def list_warehouses(db: Session = Depends(get_db)) -> list[WarehouseOut]:
    return inventory.list_warehouses(db)


@router.post("/inventory/products", response_model=ProductOut, tags=["inventory"])
def create_product(payload: ProductIn, db: Session = Depends(get_db)) -> ProductOut:
    return _guard(lambda: inventory.create_product(db, payload))


@router.get("/inventory/products", response_model=list[ProductOut], tags=["inventory"])
def list_products(active_only: bool = Query(True), db: Session = Depends(get_db)) -> list[ProductOut]:
    return inventory.list_products(db, active_only=active_only)


@router.get("/inventory/products/{product_id}", response_model=ProductOut, tags=["inventory"])
def get_product(product_id: str, db: Session = Depends(get_db)) -> ProductOut:
    return _guard(lambda: inventory.get_product(db, product_id))


@router.put("/inventory/products/{product_id}", response_model=ProductOut, tags=["inventory"])
def update_product(product_id: str, payload: ProductIn, request: Request,
                   db: Session = Depends(get_db)) -> ProductOut:
    actor = _require_editor(request, db)
    return _guard(lambda: inventory.update_product(db, product_id, payload, actor=actor))


@router.get("/inventory/products/{product_id}/audit", tags=["inventory"])
def product_audit(product_id: str, db: Session = Depends(get_db)) -> list[dict]:
    return _guard(lambda: inventory.product_audit(db, product_id))


@router.post("/inventory/movements", response_model=MovementOut, tags=["inventory"])
def record_movement(payload: MovementIn, db: Session = Depends(get_db)) -> MovementOut:
    return _guard(lambda: inventory.record_movement(db, payload))


@router.get("/inventory/movements", response_model=list[MovementOut], tags=["inventory"])
def list_movements(
    product_id: str | None = Query(None), warehouse_id: str | None = Query(None), db: Session = Depends(get_db)
) -> list[MovementOut]:
    return inventory.list_movements(db, product_id=product_id, warehouse_id=warehouse_id)


@router.get("/inventory/valuation", response_model=InventoryValuation, tags=["inventory", "reports"])
def inventory_valuation(db: Session = Depends(get_db)) -> InventoryValuation:
    return inventory.valuation(db)


@router.get("/inventory/low-stock", response_model=list[LowStockRow], tags=["inventory", "reports"])
def low_stock(db: Session = Depends(get_db)) -> list[LowStockRow]:
    return inventory.low_stock(db)


# ── Currencies & exchange rates ──────────────────────────────────────────────────────────
@router.post("/currencies", response_model=CurrencyOut, tags=["currency"])
def create_currency(payload: CurrencyIn, db: Session = Depends(get_db)) -> CurrencyOut:
    return _guard(lambda: currency.create_currency(db, payload))


@router.get("/currencies", response_model=list[CurrencyOut], tags=["currency"])
def list_currencies(db: Session = Depends(get_db)) -> list[CurrencyOut]:
    return currency.list_currencies(db)


@router.post("/currencies/rates", response_model=ExchangeRateOut, tags=["currency"])
def set_rate(payload: ExchangeRateIn, db: Session = Depends(get_db)) -> ExchangeRateOut:
    return _guard(lambda: currency.set_rate(db, payload))


@router.get("/currencies/rates", response_model=list[ExchangeRateOut], tags=["currency"])
def list_rates(code: str | None = Query(None), db: Session = Depends(get_db)) -> list[ExchangeRateOut]:
    return currency.list_rates(db, code=code)


# ── Payroll ──────────────────────────────────────────────────────────────────────────────
@router.post("/payroll/employees", response_model=EmployeeOut, tags=["payroll"])
def create_employee(payload: EmployeeIn, db: Session = Depends(get_db)) -> EmployeeOut:
    return _guard(lambda: payroll.create_employee(db, payload))


@router.get("/payroll/employees", response_model=list[EmployeeOut], tags=["payroll"])
def list_employees(active_only: bool = Query(True), db: Session = Depends(get_db)) -> list[EmployeeOut]:
    return payroll.list_employees(db, active_only=active_only)


@router.get("/payroll/employees/{employee_id}", response_model=EmployeeOut, tags=["payroll"])
def get_employee(employee_id: str, db: Session = Depends(get_db)) -> EmployeeOut:
    return _guard(lambda: payroll.get_employee(db, employee_id))


@router.put("/payroll/employees/{employee_id}", response_model=EmployeeOut, tags=["payroll"])
def update_employee(employee_id: str, payload: EmployeeIn, request: Request,
                    db: Session = Depends(get_db)) -> EmployeeOut:
    actor = _require_editor(request, db)
    return _guard(lambda: payroll.update_employee(db, employee_id, payload, actor=actor))


@router.get("/payroll/employees/{employee_id}/audit", tags=["payroll"])
def employee_audit(employee_id: str, db: Session = Depends(get_db)) -> list[dict]:
    return _guard(lambda: payroll.employee_audit(db, employee_id))


@router.post("/payroll/runs", response_model=PayrollRunOut, tags=["payroll"])
def create_run(payload: PayrollRunIn, db: Session = Depends(get_db)) -> PayrollRunOut:
    return _guard(lambda: payroll.create_run(db, payload))


@router.get("/payroll/runs", response_model=list[PayrollRunSummary], tags=["payroll"])
def list_runs(db: Session = Depends(get_db)) -> list[PayrollRunSummary]:
    return payroll.list_runs(db)


@router.get("/payroll/runs/{run_id}", response_model=PayrollRunOut, tags=["payroll"])
def get_run(run_id: str, db: Session = Depends(get_db)) -> PayrollRunOut:
    return _guard(lambda: payroll.get_run(db, run_id))


@router.post("/payroll/runs/{run_id}/post", response_model=PayrollRunOut, tags=["payroll"])
def post_run(run_id: str, db: Session = Depends(get_db)) -> PayrollRunOut:
    return _guard(lambda: payroll.post_run(db, run_id))


@router.post("/payroll/runs/{run_id}/pay", response_model=PayrollRunOut, tags=["payroll"])
def pay_run(run_id: str, db: Session = Depends(get_db)) -> PayrollRunOut:
    return _guard(lambda: payroll.pay_run(db, run_id))


@router.post("/payroll/runs/{run_id}/void", response_model=PayrollRunOut, tags=["payroll"])
def void_run(run_id: str, db: Session = Depends(get_db)) -> PayrollRunOut:
    return _guard(lambda: payroll.void_run(db, run_id))


# ── Import (Trial Balance / General Ledger / Financial Report) ───────────────────────────
@router.post("/import/analyze", response_model=ImportAnalysis, tags=["import"])
async def import_analyze(
    kind: str = Form(...), file: UploadFile = File(...), db: Session = Depends(get_db)
) -> ImportAnalysis:
    """Read + validate + analyze an uploaded xlsx/xls/csv/pdf into a preview (no changes)."""
    data = await file.read()
    try:
        return importing.analyze(db, kind, file.filename or "upload", data)
    except ImportError_ as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/import/commit", response_model=ImportResult, tags=["import"])
def import_commit(payload: ImportCommitIn, db: Session = Depends(get_db)) -> ImportResult:
    """Import the analysed rows into the ledger as balanced journal entries."""
    return _guard(lambda: importing.commit(db, payload))


# ── UAE E-Invoicing (provisional, configuration-driven compliance layer) ──────────────────
from pydantic import BaseModel as _BaseModel


class EInvConfigIn(_BaseModel):
    enabled: bool | None = None
    environment: str | None = None
    provider: str | None = None
    provider_config: dict | None = None
    schema_id: str | None = None
    schema_version: str | None = None
    applicable_types: list[str] | None = None
    required_fields: list[str] | None = None
    ruleset_version: str | None = None
    ruleset_source: str | None = None
    ruleset_date: str | None = None
    provisional: bool | None = None
    require_buyer_trn_b2b: bool | None = None


class EInvGenerateIn(_BaseModel):
    source_type: str
    source_id: str


class EInvRulesetValidateIn(_BaseModel):
    firm: str
    validator: str | None = None
    note: str | None = None


class EInvStatusIn(_BaseModel):
    status: str
    detail: str | None = None
    provider_ref: str | None = None
    regulatory_confirmed: bool | None = None


class EInvCancelIn(_BaseModel):
    reason: str


@router.get("/einvoicing/config", tags=["e-invoicing"])
def einv_get_config(db: Session = Depends(get_db)) -> dict:
    return einvoicing.config_out(db)


@router.put("/einvoicing/config", tags=["e-invoicing"])
def einv_update_config(payload: EInvConfigIn, request: Request, db: Session = Depends(get_db)) -> dict:
    actor = _require_perm(request, db, "edit")
    return _guard(lambda: einvoicing.update_config(db, payload.model_dump(exclude_none=True), actor=actor))


@router.post("/einvoicing/ruleset/validate", tags=["e-invoicing"])
def einv_ruleset_validate(payload: EInvRulesetValidateIn, request: Request,
                          db: Session = Depends(get_db)) -> dict:
    """SME sign-off of the active ruleset/schema (e.g. by Deloitte). Clears PROVISIONAL."""
    _require_perm(request, db, "approve")
    return _guard(lambda: einvoicing.validate_ruleset(
        db, payload.firm, validator=payload.validator, note=payload.note))


@router.post("/einvoicing/ruleset/revoke", tags=["e-invoicing"])
def einv_ruleset_revoke(request: Request, db: Session = Depends(get_db)) -> dict:
    _require_perm(request, db, "approve")
    return _guard(lambda: einvoicing.revoke_ruleset_validation(db))


@router.get("/einvoicing/dashboard", tags=["e-invoicing"])
def einv_dashboard(db: Session = Depends(get_db)) -> dict:
    return einvoicing.dashboard(db)


@router.get("/einvoicing", tags=["e-invoicing"])
def einv_list(status: str | None = Query(None), source_type: str | None = Query(None),
              limit: int = Query(500, le=2000), db: Session = Depends(get_db)) -> list[dict]:
    return einvoicing.list_einvoices(db, status=status, source_type=source_type, limit=limit)


@router.post("/einvoicing/generate", tags=["e-invoicing"])
def einv_generate(payload: EInvGenerateIn, request: Request, db: Session = Depends(get_db)) -> dict:
    actor = _require_perm(request, db, "create")
    ei = _guard(lambda: einvoicing.generate(db, payload.source_type, payload.source_id, actor=actor))
    return einvoicing.detail(db, ei)


@router.get("/einvoicing/{ei_id}", tags=["e-invoicing"])
def einv_get(ei_id: str, db: Session = Depends(get_db)) -> dict:
    ei = _guard(lambda: einvoicing.get(db, ei_id))
    return einvoicing.detail(db, ei)


@router.post("/einvoicing/{ei_id}/validate", tags=["e-invoicing"])
def einv_validate(ei_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
    actor = _require_perm(request, db, "edit")
    ei = _guard(lambda: einvoicing.revalidate(db, ei_id, actor=actor))
    return einvoicing.detail(db, ei)


@router.post("/einvoicing/{ei_id}/submit", tags=["e-invoicing"])
def einv_submit(ei_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
    actor = _require_perm(request, db, "approve")
    ei = _guard(lambda: einvoicing.submit(db, ei_id, actor=actor))
    return einvoicing.detail(db, ei)


@router.post("/einvoicing/{ei_id}/status", tags=["e-invoicing"])
def einv_status(ei_id: str, payload: EInvStatusIn, request: Request, db: Session = Depends(get_db)) -> dict:
    """Record an acceptance/rejection/status update returned by the ASP / UAE network."""
    actor = _require_perm(request, db, "edit")
    ei = _guard(lambda: einvoicing.record_provider_status(
        db, ei_id, payload.status, detail=payload.detail, provider_ref=payload.provider_ref,
        regulatory_confirmed=payload.regulatory_confirmed, actor=actor))
    return einvoicing.detail(db, ei)


@router.post("/einvoicing/{ei_id}/cancel", tags=["e-invoicing"])
def einv_cancel(ei_id: str, payload: EInvCancelIn, request: Request, db: Session = Depends(get_db)) -> dict:
    actor = _require_perm(request, db, "reverse")
    ei = _guard(lambda: einvoicing.cancel(db, ei_id, payload.reason, actor=actor))
    return einvoicing.detail(db, ei)


@router.post("/einvoicing/{ei_id}/resubmit", tags=["e-invoicing"])
def einv_resubmit(ei_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
    actor = _require_perm(request, db, "approve")
    ei = _guard(lambda: einvoicing.resubmit(db, ei_id, actor=actor))
    return einvoicing.detail(db, ei)


@router.get("/einvoicing/{ei_id}/pdf", tags=["e-invoicing"])
def einv_pdf(ei_id: str, request: Request, db: Session = Depends(get_db)):
    r = _guard(lambda: _pdf_response(doc_pdf.einvoice_pdf(db, ei_id)))
    _log_export(db, request, kind="document", ref=f"einvoice/{ei_id}", fmt="pdf")
    return r


# ── Export (Excel / CSV) ─────────────────────────────────────────────────────────────────
@router.get("/export/{report}", tags=["reports"])
def export_report(
    report: str,
    format: str = Query("xlsx", pattern="^(xlsx|csv|pdf)$"),
    start: date | None = Query(None),
    end: date | None = Query(None),
    as_of: date | None = Query(None),
    group_by: str | None = Query(None),
    category: str | None = Query(None),
    location: str | None = Query(None),
    department: str | None = Query(None),
    project: str | None = Query(None),
    status: str | None = Query(None),
    account_id: str | None = Query(None),
    customer_id: str | None = Query(None),
    vendor_id: str | None = Query(None),
    budget_id: str | None = Query(None),
    side: str | None = Query(None),
    request: Request = None,
    db: Session = Depends(get_db),
):
    """Export a report as Excel (xlsx), PDF or CSV. Reports: chart-of-accounts, trial-balance,
    general-ledger (needs account_id), ap-aging, ar-aging, invoice-by-vendor, sales-by-customer,
    asset-register, vat-return, income-statement, balance-sheet, cash-flow, budget-vs-actual /
    budget-forecast (need budget_id), advance-applications / advance-aging (side)."""
    from ..export import export_response

    params = {
        "start": start, "end": end, "as_of": as_of, "group_by": group_by, "category": category,
        "location": location, "department": department, "project": project, "status": status,
        "account_id": account_id, "customer_id": customer_id, "vendor_id": vendor_id,
        "budget_id": budget_id, "side": side,
    }
    try:
        resp = export_response(db, report, format, params)
        _log_export(db, request, kind="report", ref=report, fmt=format, params=params)
        return resp
    except (LedgerError, SalesError, PurchaseError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def _log_export(db: Session, request, *, kind: str, ref: str, fmt: str, params: dict | None = None,
                filename: str | None = None) -> None:
    """Record an export/print in the audit log (best-effort; never blocks the download)."""
    import json as _json
    from ..models import ExportLog
    try:
        filt = None
        if params:
            filt = _json.dumps({k: str(v) for k, v in params.items() if v not in (None, "", "None")})
        db.add(ExportLog(actor=current_actor(request) if request else "local", kind=kind, ref=ref,
                         fmt=fmt, filters=filt, filename=filename))
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()


@router.get("/documents/{kind}/{doc_id}/excel", tags=["attachments"])
def document_excel(kind: str, doc_id: str, request: Request, db: Session = Depends(get_db)):
    """Per-transaction Excel workbook (invoice/bill/credit-note/expense/journal)."""
    from ..export import document_excel as _doc_xlsx
    resp = _guard(lambda: _doc_xlsx(db, kind, doc_id))
    _log_export(db, request, kind="document", ref=f"{kind}/{doc_id}", fmt="xlsx")
    return resp


@router.get("/export-log", tags=["settings"])
def export_log(limit: int = Query(200), request: Request = None, db: Session = Depends(get_db)) -> list[dict]:
    """Recent export/print activity (audit). Requires view-audit permission."""
    _require_perm(request, db, "view_audit")
    import json as _json
    from ..models import ExportLog
    rows = db.execute(select(ExportLog).order_by(ExportLog.at.desc()).limit(limit)).scalars()
    return [{"at": r.at.isoformat() if r.at else None, "actor": r.actor, "kind": r.kind, "ref": r.ref,
             "fmt": r.fmt, "filters": _json.loads(r.filters) if r.filters else {}, "filename": r.filename}
            for r in rows]
