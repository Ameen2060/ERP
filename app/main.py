"""FastAPI application: serves both the JSON API and the built-in web UI on one port."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from . import __version__
from .api.routes import router as api_router
from .auth import (
    UserError,
    admin_reset_password,
    authenticate,
    change_password,
    complete_password_reset,
    create_token,
    create_user,
    list_security_events,
    list_users,
    log_security_event,
    request_password_reset,
    require_user,
    set_active,
)
from .models import User
from .config import get_settings
from .database import Base, SessionLocal, engine, get_db

settings = get_settings()
_WEB_DIR = Path(__file__).parent / "web"


@asynccontextmanager
async def lifespan(_: FastAPI):
    from . import models  # noqa: F401  (register models on the metadata)
    from .database import ensure_columns

    Base.metadata.create_all(bind=engine)
    # Additive migrations for new columns on pre-existing tables (new tables are handled
    # by create_all). Preserves any existing data.
    ensure_columns("sales_invoices", {
        "project": "VARCHAR(64)", "department": "VARCHAR(64)", "cost_center": "VARCHAR(64)",
        "salesperson": "VARCHAR(64)", "sales_category": "VARCHAR(64)",
        "exchange_rate": "NUMERIC(18,6) DEFAULT 1",
    })
    # Retention (holdback) columns for invoices and bills.
    _retention_cols = {
        "retention_applicable": "BOOLEAN DEFAULT 0", "retention_basis": "VARCHAR(8) DEFAULT 'net'",
        "retention_percent": "NUMERIC(9,4) DEFAULT 0", "retention_amount": "NUMERIC(18,2) DEFAULT 0",
        "retention_released": "NUMERIC(18,2) DEFAULT 0", "retention_release_date": "DATE",
        "retention_reference": "VARCHAR(64)", "retention_account_id": "VARCHAR(32)",
        "contract_reference": "VARCHAR(64)",
    }
    ensure_columns("sales_invoices", _retention_cols)
    ensure_columns("vendor_bills", _retention_cols)
    ensure_columns("projects", {
        "retention_amount": "NUMERIC(18,2) DEFAULT 0", "advance_percent": "NUMERIC(9,4) DEFAULT 0",
        "advance_amount": "NUMERIC(18,2) DEFAULT 0",
    })
    _cn_ret_cols = {
        "retention_applicable": "BOOLEAN DEFAULT 0", "retention_basis": "VARCHAR(8) DEFAULT 'net'",
        "retention_percent": "NUMERIC(9,4) DEFAULT 0", "retention_amount": "NUMERIC(18,2) DEFAULT 0",
        "retention_account_id": "VARCHAR(32)",
    }
    ensure_columns("customer_credit_notes", _cn_ret_cols)
    ensure_columns("vendor_credit_notes", _cn_ret_cols)
    _adv_vat_cols = {
        "vat_applicable": "BOOLEAN DEFAULT 0", "vat_rate": "NUMERIC(9,4) DEFAULT 0",
        "vat_amount": "NUMERIC(18,2) DEFAULT 0", "net_amount": "NUMERIC(18,2) DEFAULT 0",
    }
    _adv_vat_cols["tax_point_date"] = "DATE"
    ensure_columns("customer_advances", _adv_vat_cols)
    ensure_columns("vendor_advances", _adv_vat_cols)
    ensure_columns("system_settings", {
        "advance_vat_status": "VARCHAR(12) DEFAULT 'off'", "advance_vat_approved_by": "VARCHAR(64)",
        "advance_vat_approved_at": "DATETIME",
    })
    ensure_columns("sales_invoice_lines", {"vat_treatment": "VARCHAR(16) DEFAULT 'SR'"})
    ensure_columns("vendor_bill_lines", {"vat_treatment": "VARCHAR(16) DEFAULT 'SR'"})
    # Structured contact/address fields on the party masters (spec: TRN + billing/shipping).
    ensure_columns("customers", {
        "contact_name": "VARCHAR(128)", "billing_address": "TEXT", "shipping_address": "TEXT",
    })
    ensure_columns("vendors", {"contact_name": "VARCHAR(128)", "billing_address": "TEXT"})
    # Fixed assets: structured vendor link (TRN/address pull), beside the free-text supplier.
    ensure_columns("fixed_assets", {"vendor_id": "VARCHAR(32)"})
    # Users: recovery email + forced-change flag for the password-reset flow.
    ensure_columns("users", {"email": "VARCHAR(255)", "must_change_password": "BOOLEAN DEFAULT 0"})
    # Editable profiles: customer payment terms + credit limit; bank account name/SWIFT/branch.
    ensure_columns("customers", {"payment_terms": "VARCHAR(32)", "credit_limit": "NUMERIC(18,2) DEFAULT 0"})
    ensure_columns("bank_accounts", {"account_name": "VARCHAR(255)", "swift": "VARCHAR(32)", "branch": "VARCHAR(128)"})
    # Period lock for the edit-transaction framework.
    ensure_columns("system_settings", {"books_locked_before": "DATE"})
    # ── UAE E-Invoicing master data (organization + party) ──────────────────────────────
    ensure_columns("organization", {
        "trade_license": "VARCHAR(64)", "country": "VARCHAR(2) DEFAULT 'AE'",
        "einvoice_scheme": "VARCHAR(24)", "einvoice_id": "VARCHAR(64)",
        "bank_name": "VARCHAR(160)", "bank_iban": "VARCHAR(64)",
    })
    _einv_party_cols = {
        "country": "VARCHAR(2) DEFAULT 'AE'", "tax_status": "VARCHAR(16) DEFAULT 'unknown'",
        "party_type": "VARCHAR(4) DEFAULT 'b2b'", "einvoice_scheme": "VARCHAR(24)",
        "einvoice_id": "VARCHAR(64)",
    }
    ensure_columns("customers", _einv_party_cols)
    ensure_columns("vendors", _einv_party_cols)
    ensure_columns("einvoice_config", {
        "sme_firm": "VARCHAR(160)", "sme_validator": "VARCHAR(160)",
        "sme_validated_at": "DATETIME", "sme_note": "TEXT",
        "provider_config_json": "TEXT",
    })
    if settings.seed_on_startup:
        from .services.currency import ensure_base
        from .services.ledger import seed_chart_of_accounts

        with SessionLocal() as db:
            seed_chart_of_accounts(db)
            ensure_base(db)  # seed base currency (AED)
            from .auth import bootstrap_admin
            bootstrap_admin(db, settings.admin_username, settings.admin_password)
            from .services.vat_treatments import ensure_seed
            ensure_seed(db)  # seed the 5 standard UAE VAT treatments
            from .services import permissions as _perms
            _perms.ensure_seed(db)  # seed the role→action permission matrix
            from .services import einvoicing as _einv
            _einv.ensure_config(db)  # seed the provisional e-invoicing configuration
    yield


app = FastAPI(
    title=settings.app_name,
    version=__version__,
    description="Standalone double-entry accounting system (General Ledger, Sales, Financial Statements).",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Auth endpoints (login is public; the rest of the API is gated) ──────────────────────
auth_api = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    username: str
    password: str


def _client_ip(request: Request) -> str | None:
    return request.client.host if request and request.client else None


@auth_api.post("/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)) -> dict:
    ip = _client_ip(request)
    user = authenticate(db, body.username, body.password)
    if not user:
        log_security_event(db, "login_failed", username=body.username, ip=ip)
        db.commit()
        raise HTTPException(status_code=401, detail="Incorrect username or password.")
    log_security_event(db, "login", username=user.username, ip=ip)
    db.commit()
    return {"access_token": create_token(user.username), "token_type": "bearer",
            "user": {"username": user.username, "role": user.role,
                     "must_change_password": bool(user.must_change_password)}}


@auth_api.get("/me")
def me(user=Depends(require_user)) -> dict:
    if user is None:  # auth disabled
        return {"username": "local", "role": "admin", "auth_enabled": False}
    return {"username": user.username, "role": user.role, "auth_enabled": True}


@auth_api.get("/config")
def auth_config() -> dict:
    return {"auth_enabled": settings.auth_enabled}


# ── Account self-service + admin user management ────────────────────────────────────────
def require_admin(user: User | None = Depends(require_user)) -> User | None:
    """Admin-only gate. When auth is disabled the app is a local single-user tool → allow."""
    if user is None:
        return None
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Administrator access required.")
    return user


def _user_out(u: User) -> dict:
    return {"id": u.id, "username": u.username, "email": u.email, "role": u.role,
            "is_active": u.is_active, "must_change_password": bool(u.must_change_password),
            "created_at": u.created_at.isoformat() if u.created_at else None}


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str


class CreateUserIn(BaseModel):
    username: str
    password: str
    role: str = "accountant"
    email: str | None = None


class ResetPasswordIn(BaseModel):
    new_password: str


class SetActiveIn(BaseModel):
    is_active: bool


class ForgotPasswordIn(BaseModel):
    identifier: str  # username or registered email


class ResetWithTokenIn(BaseModel):
    token: str
    new_password: str


# Uniform response so callers can't tell whether an account exists (anti-enumeration).
_RESET_ACK = {"ok": True, "message": "If an account matches, a password-reset link has been sent."}


@auth_api.post("/forgot-password")
def forgot_password(body: ForgotPasswordIn, request: Request, db: Session = Depends(get_db)) -> dict:
    """Public: request a reset. Always returns the same acknowledgement regardless of whether
    the account exists. In dev (no SMTP) the raw token is included so the flow is usable."""
    try:
        raw = request_password_reset(db, body.identifier, ip=_client_ip(request))
    except UserError as e:  # rate limiting
        raise HTTPException(status_code=429, detail=str(e)) from e
    resp = dict(_RESET_ACK)
    if raw and settings.reset_expose_token:
        resp["reset_token"] = raw
        resp["expires_in_minutes"] = settings.reset_token_ttl_minutes
    return resp


@auth_api.post("/reset-password")
def reset_password(body: ResetWithTokenIn, request: Request, db: Session = Depends(get_db)) -> dict:
    """Public: complete a reset using a valid token. No current password required."""
    try:
        user = complete_password_reset(db, body.token, body.new_password, ip=_client_ip(request))
    except UserError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "username": user.username}


@auth_api.post("/change-password")
def change_pw(body: ChangePasswordIn, request: Request, user=Depends(require_user),
              db: Session = Depends(get_db)) -> dict:
    if user is None:
        raise HTTPException(status_code=400, detail="Authentication is disabled — there is no password to change.")
    try:
        change_password(db, user.username, body.current_password, body.new_password, ip=_client_ip(request))
    except UserError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True}


@auth_api.get("/users")
def users_list(admin=Depends(require_admin), db: Session = Depends(get_db)) -> list[dict]:
    return [_user_out(u) for u in list_users(db)]


@auth_api.post("/users")
def users_create(body: CreateUserIn, admin=Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    try:
        u = create_user(db, body.username, body.password, body.role)
        if body.email is not None:
            u.email = body.email.strip() or None
            db.commit()
        return _user_out(u)
    except UserError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@auth_api.post("/users/{user_id}/reset-password")
def users_reset_pw(user_id: str, body: ResetPasswordIn, admin=Depends(require_admin),
                   db: Session = Depends(get_db)) -> dict:
    """Admin sets a specific new password (forces the user to change it next login)."""
    try:
        actor = admin.username if admin else "system"
        return _user_out(admin_reset_password(db, user_id, body.new_password, actor=actor))
    except UserError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@auth_api.post("/users/{user_id}/reset-link")
def users_reset_link(user_id: str, request: Request, admin=Depends(require_admin),
                     db: Session = Depends(get_db)) -> dict:
    """Admin initiates a reset WITHOUT choosing/seeing a password: issues a single-use reset
    token the user redeems themselves. Returned in dev so the admin can hand it over."""
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    actor = admin.username if admin else "system"
    raw = request_password_reset(db, user.username, ip=_client_ip(request), kind="admin", actor=actor)
    resp = {"ok": True, "username": user.username}
    if raw and settings.reset_expose_token:
        resp["reset_token"] = raw
        resp["expires_in_minutes"] = settings.reset_token_ttl_minutes
    return resp


@auth_api.get("/security-events")
def security_events(limit: int = 200, admin=Depends(require_admin),
                    db: Session = Depends(get_db)) -> list[dict]:
    return [{"at": e.at.isoformat() if e.at else None, "event": e.event, "username": e.username,
             "actor": e.actor, "detail": e.detail, "ip": e.ip}
            for e in list_security_events(db, limit=limit)]


@auth_api.post("/users/{user_id}/active")
def users_set_active(user_id: str, body: SetActiveIn, admin=Depends(require_admin),
                     db: Session = Depends(get_db)) -> dict:
    try:
        return _user_out(set_active(db, user_id, body.is_active,
                                    acting_user_id=(admin.id if admin else None)))
    except UserError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


app.include_router(auth_api)
# Everything else requires a valid session (pass-through when auth is disabled).
app.include_router(api_router, dependencies=[Depends(require_user)])


@app.get("/health", tags=["system"])
def health() -> dict:
    return {"status": "ok", "app": settings.app_name, "version": __version__}


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> str:
    return (_WEB_DIR / "index.html").read_text(encoding="utf-8")
