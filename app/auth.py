"""Authentication: PBKDF2 password hashing + HMAC-signed session tokens (stdlib only).

Kept deliberately simple for a self-hosted single-tenant app. Passwords are never stored in
plain text; tokens are signed with the app secret and carry an expiry.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db
from .models import PasswordResetToken, SecurityEvent, User


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    """SQLite may return naive datetimes; treat stored values as UTC for comparison."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

_ITER = 200_000


# ── Passwords ─────────────────────────────────────────────────────────────────────────────
def hash_password(pw: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), _ITER)
    return f"pbkdf2_sha256${_ITER}${salt}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _algo, iters, salt, digest = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), int(iters))
        return hmac.compare_digest(dk.hex(), digest)
    except Exception:  # noqa: BLE001
        return False


# ── Tokens (HMAC-signed "username:expiry") ─────────────────────────────────────────────────
def _sign(payload: str) -> str:
    key = get_settings().secret_key.encode()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def create_token(username: str) -> str:
    exp = int(time.time()) + get_settings().token_ttl_hours * 3600
    payload = f"{username}:{exp}"
    raw = f"{payload}:{_sign(payload)}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_token(token: str) -> str | None:
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        username, exp, sig = raw.rsplit(":", 2)
        if not hmac.compare_digest(sig, _sign(f"{username}:{exp}")):
            return None
        if int(exp) < int(time.time()):
            return None
        return username
    except Exception:  # noqa: BLE001
        return None


# ── User service ────────────────────────────────────────────────────────────────────────
def bootstrap_admin(db: Session, username: str, password: str) -> None:
    """Create the first admin from configured credentials — no-op if any user exists."""
    if not username or not password:
        return
    if db.execute(select(User)).scalars().first():
        return
    db.add(User(username=username, password_hash=hash_password(password), role="admin"))
    db.commit()


def authenticate(db: Session, username: str, password: str) -> User | None:
    user = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
    if user and user.is_active and verify_password(password, user.password_hash):
        return user
    return None


# ── Account / user management ───────────────────────────────────────────────────────────────
MIN_PW_LEN = 8
_ROLES = ("admin", "accountant", "viewer")


class UserError(ValueError):
    """Raised on an invalid user-management request (bad password, duplicate username, ...)."""


def _validate_pw(pw: str) -> None:
    """Strong-password policy: min length plus at least three of four character classes
    (lower, upper, digit, symbol)."""
    if not pw or len(pw) < MIN_PW_LEN:
        raise UserError(f"Password must be at least {MIN_PW_LEN} characters.")
    classes = sum([
        any(c.islower() for c in pw), any(c.isupper() for c in pw),
        any(c.isdigit() for c in pw), any(not c.isalnum() for c in pw),
    ])
    if classes < 3:
        raise UserError("Password must include at least three of: lowercase, uppercase, "
                        "number, and symbol.")


def change_password(db: Session, username: str, current: str, new: str,
                    ip: str | None = None) -> None:
    """Change the signed-in user's own password after verifying the current one."""
    user = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
    if not user or not verify_password(current, user.password_hash):
        raise UserError("Current password is incorrect.")
    if verify_password(new, user.password_hash):
        raise UserError("The new password must be different from the current one.")
    _validate_pw(new)
    user.password_hash = hash_password(new)
    user.must_change_password = False
    _invalidate_reset_tokens(db, user.id)
    log_security_event(db, "password_change", username=user.username, actor=user.username, ip=ip)
    db.commit()


def list_users(db: Session) -> list[User]:
    return list(db.execute(select(User).order_by(User.username)).scalars())


def create_user(db: Session, username: str, password: str, role: str = "accountant") -> User:
    username = (username or "").strip()
    if not username:
        raise UserError("Username is required.")
    if role not in _ROLES:
        raise UserError(f"Role must be one of: {', '.join(_ROLES)}.")
    if db.execute(select(User).where(User.username == username)).scalar_one_or_none():
        raise UserError("That username already exists.")
    _validate_pw(password)
    user = User(username=username, password_hash=hash_password(password), role=role)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def admin_reset_password(db: Session, user_id: str, new: str, actor: str | None = None) -> User:
    """Admin sets a specific new password for a user (never sees the old one)."""
    user = db.get(User, user_id)
    if not user:
        raise UserError("User not found.")
    _validate_pw(new)
    user.password_hash = hash_password(new)
    user.must_change_password = True
    _invalidate_reset_tokens(db, user.id)
    log_security_event(db, "admin_reset", username=user.username, actor=actor)
    db.commit()
    return user


# ── Security audit trail ────────────────────────────────────────────────────────────────
def log_security_event(db: Session, event: str, *, username: str | None = None,
                       actor: str | None = None, detail: str | None = None,
                       ip: str | None = None) -> None:
    """Append an auth/credential event. Caller commits (kept in the same txn as the action)."""
    db.add(SecurityEvent(event=event, username=username, actor=actor, detail=detail, ip=ip))


def list_security_events(db: Session, limit: int = 200, username: str | None = None) -> list[SecurityEvent]:
    stmt = select(SecurityEvent).order_by(SecurityEvent.at.desc()).limit(limit)
    if username:
        stmt = select(SecurityEvent).where(SecurityEvent.username == username)\
            .order_by(SecurityEvent.at.desc()).limit(limit)
    return list(db.execute(stmt).scalars())


# ── Password reset (forgot-password) flow ───────────────────────────────────────────────
def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _invalidate_reset_tokens(db: Session, user_id: str) -> None:
    """Mark every still-active reset token for a user as used (single-use / one-at-a-time)."""
    now = _utcnow()
    for t in db.execute(select(PasswordResetToken).where(
            PasswordResetToken.user_id == user_id, PasswordResetToken.used_at.is_(None))).scalars():
        t.used_at = now


def _find_user_by_identifier(db: Session, identifier: str) -> User | None:
    ident = (identifier or "").strip()
    if not ident:
        return None
    u = db.execute(select(User).where(User.username == ident)).scalar_one_or_none()
    if u:
        return u
    return db.execute(select(User).where(User.email == ident)).scalars().first()


def _recent_request_count(db: Session, user_id: str) -> int:
    window = _utcnow() - timedelta(minutes=get_settings().reset_request_window_minutes)
    rows = db.execute(select(PasswordResetToken).where(
        PasswordResetToken.user_id == user_id, PasswordResetToken.kind == "self")).scalars()
    return sum(1 for r in rows if _aware(r.created_at) >= window)


def request_password_reset(db: Session, identifier: str, *, ip: str | None = None,
                           kind: str = "self", actor: str | None = None) -> str | None:
    """Create a single-use reset token for the account matching username/email.

    Returns the RAW token when a user is found (the caller decides whether to email it or, in
    dev, expose it), or None when no account matches — the route must respond identically in
    both cases to avoid account enumeration. Rate-limited per account.
    """
    user = _find_user_by_identifier(db, identifier)
    if not user or not user.is_active:
        log_security_event(db, "reset_request", username=identifier, ip=ip, detail="no-match")
        db.commit()
        return None
    if kind == "self" and _recent_request_count(db, user.id) >= get_settings().reset_request_max:
        log_security_event(db, "reset_request", username=user.username, ip=ip, detail="rate-limited")
        db.commit()
        raise UserError("Too many reset requests. Please try again later.")
    _invalidate_reset_tokens(db, user.id)  # supersede any pending token
    raw = secrets.token_urlsafe(32)
    ttl = get_settings().reset_token_ttl_minutes
    db.add(PasswordResetToken(
        user_id=user.id, token_hash=_hash_token(raw), kind=kind,
        expires_at=_utcnow() + timedelta(minutes=ttl), requested_ip=ip,
    ))
    log_security_event(db, "reset_request", username=user.username, actor=actor, ip=ip,
                       detail=f"issued ({kind})")
    db.commit()
    return raw


def _consume_reset_token(db: Session, raw: str) -> PasswordResetToken:
    rec = db.execute(select(PasswordResetToken).where(
        PasswordResetToken.token_hash == _hash_token(raw))).scalar_one_or_none()
    if not rec or rec.used_at is not None:
        raise UserError("This reset link is invalid or has already been used.")
    if _aware(rec.expires_at) < _utcnow():
        raise UserError("This reset link has expired. Please request a new one.")
    return rec


def complete_password_reset(db: Session, raw: str, new: str, *, ip: str | None = None) -> User:
    """Set a new password using a valid reset token. No current password required; on success
    the token (and any siblings) are invalidated so the previous password/link no longer work."""
    rec = _consume_reset_token(db, raw)
    user = db.get(User, rec.user_id)
    if not user:
        raise UserError("Account not found.")
    _validate_pw(new)
    user.password_hash = hash_password(new)
    user.must_change_password = False
    rec.used_at = _utcnow()
    _invalidate_reset_tokens(db, user.id)
    log_security_event(db, "reset_complete", username=user.username, ip=ip)
    db.commit()
    db.refresh(user)
    return user


def set_active(db: Session, user_id: str, active: bool, acting_user_id: str | None = None) -> User:
    user = db.get(User, user_id)
    if not user:
        raise UserError("User not found.")
    if not active and acting_user_id == user_id:
        raise UserError("You cannot deactivate your own account.")
    if not active and user.role == "admin":
        actives = db.execute(select(User).where(User.role == "admin", User.is_active.is_(True))).scalars().all()
        if len(actives) <= 1:
            raise UserError("Cannot deactivate the last active admin.")
    user.is_active = bool(active)
    db.commit()
    return user


# ── FastAPI dependency ─────────────────────────────────────────────────────────────────────
def _token_from_request(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return request.query_params.get("token")  # for <a>/download links that can't set headers


def require_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    """Gate a route. Pass-through when auth is disabled (tests/local). Otherwise a valid,
    unexpired token for an active user is required."""
    settings = get_settings()
    if not settings.auth_enabled:
        return None
    token = _token_from_request(request)
    username = decode_token(token) if token else None
    if not username:
        raise HTTPException(status_code=401, detail="Authentication required.")
    user = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User no longer active.")
    return user
