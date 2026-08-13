"""Profile-change audit trail for master data (customer / vendor / bank account).

`record_profile_change` computes a field-level before→after diff and appends a ProfileAudit
row. Editing a master profile never rewrites posted transactions — this log is the record of
the master-data change itself (who / what / when), satisfying the audit requirement.
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ProfileAudit, TransactionAudit


def _norm(v) -> str | None:
    if v is None:
        return None
    return str(v)


def diff(before: dict, after: dict) -> list[dict]:
    """Field-level [{field, old, new}] for keys whose value changed (stringified compare)."""
    out: list[dict] = []
    for k in after:
        ov, nv = _norm(before.get(k)), _norm(after.get(k))
        if ov != nv:
            out.append({"field": k, "old": ov, "new": nv})
    return out


def record_profile_change(db: Session, *, entity_type: str, entity_id: str,
                          entity_label: str | None, actor: str | None,
                          changes: list[dict], action: str = "update") -> None:
    """Append a profile-audit row. Caller commits (kept in the same txn as the edit)."""
    db.add(ProfileAudit(
        entity_type=entity_type, entity_id=entity_id, entity_label=entity_label,
        actor=actor, action=action, changes=json.dumps(changes) if changes else None,
    ))


def list_profile_audit(db: Session, entity_type: str, entity_id: str) -> list[dict]:
    rows = db.execute(
        select(ProfileAudit)
        .where(ProfileAudit.entity_type == entity_type, ProfileAudit.entity_id == entity_id)
        .order_by(ProfileAudit.at.desc())
    ).scalars()
    return [{
        "at": r.at.isoformat() if r.at else None, "actor": r.actor, "action": r.action,
        "changes": json.loads(r.changes) if r.changes else [],
    } for r in rows]


# ── Transaction edit audit (reusable across every financial-transaction module) ──────────
def record_txn_audit(db: Session, *, entity_type: str, entity_id: str, doc_number: str | None,
                     actor: str | None, action: str = "edit", reason: str | None = None,
                     prev_status: str | None = None, new_status: str | None = None,
                     changes: list[dict] | None = None) -> None:
    """Append a transaction-edit audit row. Caller commits (same txn as the edit)."""
    db.add(TransactionAudit(
        entity_type=entity_type, entity_id=entity_id, doc_number=doc_number, actor=actor,
        action=action, reason=reason, prev_status=prev_status, new_status=new_status,
        changes=json.dumps(changes) if changes else None,
    ))


def list_txn_audit(db: Session, entity_type: str, entity_id: str) -> list[dict]:
    rows = db.execute(
        select(TransactionAudit)
        .where(TransactionAudit.entity_type == entity_type, TransactionAudit.entity_id == entity_id)
        .order_by(TransactionAudit.at.desc())
    ).scalars()
    return [{
        "at": r.at.isoformat() if r.at else None, "actor": r.actor, "action": r.action,
        "reason": r.reason, "prev_status": r.prev_status, "new_status": r.new_status,
        "doc_number": r.doc_number, "changes": json.loads(r.changes) if r.changes else [],
    } for r in rows]
