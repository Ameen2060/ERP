"""Corporate Tax — 'Provisional → Requires SME Validation' review workflow.

Every CT computation produced by this app is PROVISIONAL: the accounting treatment is not
assumed to equal the tax treatment, and no figure may be treated as filing-ready until a
qualified tax specialist (SME) has reviewed each line and validated the whole computation.

This service enforces that control:

  draft ──submit──▶ provisional ──mark_reviewed──▶ sme_reviewed ──validate──▶ validated
    ▲                   │  (all lines signed off)        │  (SME named)           │
    └──────────── reopen ┴──────── reject ───────────────┴──── reopen ────────────┘

Filing is BLOCKED (`can_file` is False) unless status == validated. Every transition and
sign-off is written to an immutable audit trail (CTReviewEvent).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CTReview, CTReviewEvent, CTReviewItem
from ..schemas import (
    CTComputation,
    CTReviewDetail,
    CTReviewEventOut,
    CTReviewItemOut,
    CTReviewSummary,
)
from . import reports


class CTReviewError(ValueError):
    """Raised on an invalid workflow transition or missing precondition."""


STATUS_LABEL = {
    "draft": "Draft",
    "provisional": "Provisional — awaiting SME review",
    "sme_reviewed": "SME reviewed — awaiting validation",
    "validated": "Validated — filing-ready",
    "rejected": "Rejected — requires rework",
}

# Lines that are pure rate-band display and don't need an individual SME sign-off.
_NO_SIGNOFF_KINDS = {"rate"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


# ── Creation ────────────────────────────────────────────────────────────────────────────────
def create_review(db: Session, start: date | None, end: date | None,
                   prepared_by: str | None = None, actor: str = "local") -> CTReview:
    """Snapshot the current CT computation for a period and open a review at 'draft'."""
    comp = reports.ct_computation(db, start=start, end=end)
    review = CTReview(
        period_start=start, period_end=end, status="draft",
        snapshot_json=json.dumps(comp.model_dump(mode="json")),
        taxable_income=comp.taxable_income, ct_payable=comp.ct_payable,
        prepared_by=prepared_by or actor,
    )
    db.add(review)
    db.flush()
    for i, line in enumerate(comp.lines):
        db.add(CTReviewItem(
            review_id=review.id, ordinal=i, line_key=f"L{i:02d}",
            line_label=line.label, amount=line.amount,
            requires_signoff=line.kind not in _NO_SIGNOFF_KINDS,
        ))
    _log(db, review, "created", None, "draft", actor,
         note=f"Computation snapshotted (CT payable {comp.ct_payable}).")
    db.commit()
    db.refresh(review)
    return review


# ── Audit trail ───────────────────────────────────────────────────────────────────────────
def _log(db: Session, review: CTReview, action: str, frm: str | None,
         to: str | None, actor: str, note: str | None = None) -> None:
    db.add(CTReviewEvent(review_id=review.id, action=action, from_status=frm,
                         to_status=to, actor=actor or "local", note=note))


# ── Sign-off ──────────────────────────────────────────────────────────────────────────────
def sign_off_item(db: Session, review_id: str, item_id: str, signed: bool,
                  note: str | None = None, actor: str = "local") -> CTReview:
    review = _get(db, review_id)
    if review.status not in ("provisional", "sme_reviewed"):
        raise CTReviewError("Line sign-off is only allowed while the review is provisional or under SME review.")
    item = db.get(CTReviewItem, item_id)
    if not item or item.review_id != review.id:
        raise CTReviewError("Review line not found.")
    if not item.requires_signoff:
        raise CTReviewError("This line does not require sign-off.")
    item.signed_off = bool(signed)
    item.note = note
    item.signed_by = actor if signed else None
    item.signed_at = _now() if signed else None
    _log(db, review, "signed_off" if signed else "unsigned", review.status, review.status,
         actor, note=f"{item.line_label}" + (f" — {note}" if note else ""))
    review.updated_at = _now()
    db.commit()
    db.refresh(review)
    return review


# ── Transitions ───────────────────────────────────────────────────────────────────────────
def submit(db: Session, review_id: str, actor: str = "local", note: str | None = None) -> CTReview:
    review = _get(db, review_id)
    if review.status != "draft":
        raise CTReviewError("Only a draft review can be submitted for SME review.")
    return _transition(db, review, "submitted", "provisional", actor, note)


def mark_reviewed(db: Session, review_id: str, actor: str = "local", note: str | None = None) -> CTReview:
    review = _get(db, review_id)
    if review.status != "provisional":
        raise CTReviewError("Only a provisional review can be marked SME-reviewed.")
    if not _all_signed(review):
        raise CTReviewError("Every line requiring sign-off must be signed off before SME review is complete.")
    review.reviewed_by = actor
    return _transition(db, review, "reviewed", "sme_reviewed", actor, note)


def validate(db: Session, review_id: str, sme_name: str | None, actor: str = "local",
             note: str | None = None) -> CTReview:
    review = _get(db, review_id)
    if review.status != "sme_reviewed":
        raise CTReviewError("A review must be SME-reviewed (all lines signed off) before it can be validated.")
    if not _all_signed(review):
        raise CTReviewError("Not all lines are signed off.")
    if not (sme_name and sme_name.strip()):
        raise CTReviewError("Validation requires the name of the qualified tax specialist (SME).")
    review.validated_by = actor
    review.validated_at = _now()
    review.sme_name = sme_name.strip()
    review.sme_note = note
    return _transition(db, review, "validated", "validated", actor,
                       note=f"Validated by {sme_name.strip()}" + (f" — {note}" if note else ""))


def reject(db: Session, review_id: str, actor: str = "local", note: str | None = None) -> CTReview:
    review = _get(db, review_id)
    if review.status not in ("provisional", "sme_reviewed"):
        raise CTReviewError("Only a provisional or SME-reviewed computation can be rejected.")
    if not (note and note.strip()):
        raise CTReviewError("A rejection reason is required.")
    return _transition(db, review, "rejected", "rejected", actor, note)


def reopen(db: Session, review_id: str, actor: str = "local", note: str | None = None) -> CTReview:
    """Send a validated/rejected review back to draft — this INVALIDATES any prior validation."""
    review = _get(db, review_id)
    if review.status not in ("validated", "rejected", "sme_reviewed"):
        raise CTReviewError("This review cannot be reopened from its current state.")
    # reopening clears the validation stamp and all sign-offs
    review.validated_by = review.validated_at = review.sme_name = None
    review.reviewed_by = None
    for it in review.items:
        it.signed_off = False
        it.signed_by = it.signed_at = None
    return _transition(db, review, "reopened", "draft", actor, note)


def _transition(db: Session, review: CTReview, action: str, to: str,
                actor: str, note: str | None) -> CTReview:
    frm = review.status
    review.status = to
    review.updated_at = _now()
    _log(db, review, action, frm, to, actor, note)
    db.commit()
    db.refresh(review)
    return review


# ── Queries / serialisation ─────────────────────────────────────────────────────────────────
def _get(db: Session, review_id: str) -> CTReview:
    review = db.get(CTReview, review_id)
    if not review:
        raise CTReviewError("CT review not found.")
    return review


def get_review(db: Session, review_id: str) -> CTReview:
    return _get(db, review_id)


def list_reviews(db: Session) -> list[CTReview]:
    return list(db.execute(select(CTReview).order_by(CTReview.created_at.desc())).scalars())


def _all_signed(review: CTReview) -> bool:
    req = [it for it in review.items if it.requires_signoff]
    return bool(req) and all(it.signed_off for it in req)


def can_file(review: CTReview) -> bool:
    return review.status == "validated"


def _allowed_actions(review: CTReview) -> list[str]:
    s = review.status
    if s == "draft":
        return ["submit"]
    if s == "provisional":
        acts = ["sign_off", "reject"]
        if _all_signed(review):
            acts.insert(1, "mark_reviewed")
        return acts
    if s == "sme_reviewed":
        return ["sign_off", "validate", "reject", "reopen"]
    if s == "validated":
        return ["reopen"]
    if s == "rejected":
        return ["reopen"]
    return []


def summary(review: CTReview) -> CTReviewSummary:
    req = [it for it in review.items if it.requires_signoff]
    return CTReviewSummary(
        id=review.id, period_start=review.period_start, period_end=review.period_end,
        status=review.status, status_label=STATUS_LABEL.get(review.status, review.status),
        taxable_income=review.taxable_income, ct_payable=review.ct_payable,
        prepared_by=review.prepared_by, validated_by=review.validated_by,
        validated_at=_iso(review.validated_at), sme_name=review.sme_name,
        can_file=can_file(review),
        signed_count=sum(1 for it in req if it.signed_off), signoff_total=len(req),
        created_at=_iso(review.created_at), updated_at=_iso(review.updated_at),
    )


def detail(review: CTReview) -> CTReviewDetail:
    base = summary(review).model_dump()
    comp = CTComputation.model_validate(json.loads(review.snapshot_json))
    items = [CTReviewItemOut(
        id=it.id, ordinal=it.ordinal, line_key=it.line_key, line_label=it.line_label,
        amount=it.amount, requires_signoff=it.requires_signoff, signed_off=it.signed_off,
        signed_by=it.signed_by, signed_at=_iso(it.signed_at), note=it.note,
    ) for it in review.items]
    events = [CTReviewEventOut(
        at=_iso(e.at), actor=e.actor, action=e.action,
        from_status=e.from_status, to_status=e.to_status, note=e.note,
    ) for e in review.events]
    return CTReviewDetail(
        **base, sme_note=review.sme_note, computation=comp, items=items, events=events,
        allowed_actions=_allowed_actions(review), all_signed_off=_all_signed(review),
    )
