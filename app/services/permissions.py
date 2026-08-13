"""Role-based permissions for financial actions — DB-backed and editable.

A `RolePermission` row per (role, action) holds the toggle; the matrix is seeded from
`DEFAULT_PERMISSIONS` on first startup and can be edited by admins in the UI. `can()` reads
the stored matrix (falling back to defaults if a role has no rows yet). Admin always retains
every action so it can't be locked out.

Actions: view, create, edit, delete, approve, reverse (void), view_audit.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import RolePermission

ACTIONS = ("view", "create", "edit", "delete", "approve", "reverse", "view_audit")
ROLES = ("admin", "accountant", "viewer")

DEFAULT_PERMISSIONS: dict[str, set[str]] = {
    "admin": set(ACTIONS),
    "accountant": {"view", "create", "edit", "approve", "reverse", "view_audit"},
    "viewer": {"view", "view_audit"},
}


class PermissionError_(PermissionError):
    """Raised when a role lacks a required action → HTTP 403."""


def ensure_seed(db: Session) -> None:
    """Populate the matrix from defaults for any (role, action) not yet present (idempotent)."""
    existing = {(r.role, r.action) for r in db.execute(select(RolePermission)).scalars()}
    added = False
    for role in ROLES:
        for action in ACTIONS:
            if (role, action) not in existing:
                db.add(RolePermission(role=role, action=action,
                                      allowed=action in DEFAULT_PERMISSIONS[role]))
                added = True
    if added:
        db.commit()


def can(db: Session, role: str | None, action: str) -> bool:
    if role is None:                 # auth disabled → local single-user tool
        return True
    if role == "admin":              # admin is never locked out
        return True
    row = db.execute(select(RolePermission).where(
        RolePermission.role == role, RolePermission.action == action)).scalar_one_or_none()
    if row is not None:
        return bool(row.allowed)
    return action in DEFAULT_PERMISSIONS.get(role or "", set())   # fallback for unseeded roles


def require(db: Session, role: str | None, action: str) -> None:
    if not can(db, role, action):
        raise PermissionError_(f"Your role does not have '{action}' permission.")


def permissions_for(db: Session, role: str | None) -> dict[str, bool]:
    """Full action→bool map for a role, for the UI to show/hide controls."""
    return {a: can(db, role, a) for a in ACTIONS}


def matrix(db: Session) -> dict:
    """The full editable matrix: {role: {action: bool}} for every role."""
    return {"actions": list(ACTIONS), "roles": list(ROLES),
            "matrix": {r: permissions_for(db, r) for r in ROLES}}


def set_matrix(db: Session, data: dict) -> dict:
    """Persist toggles from {role: {action: bool}}. Admin stays fully enabled regardless."""
    for role, actions in (data or {}).items():
        if role not in ROLES:
            continue
        for action, allowed in (actions or {}).items():
            if action not in ACTIONS:
                continue
            val = True if role == "admin" else bool(allowed)
            row = db.execute(select(RolePermission).where(
                RolePermission.role == role, RolePermission.action == action)).scalar_one_or_none()
            if row:
                row.allowed = val
            else:
                db.add(RolePermission(role=role, action=action, allowed=val))
    db.commit()
    return matrix(db)
