"""
routes/supplier_emails.py — CRUD for the per-supplier Email Log.

Companion to routes/suppliers.py. Implements Phase 1 of the Email Log
feature locked in docs/email-log-feature.md. Exposes:

  GET    /api/suppliers/{supplier_id}/emails  — list a supplier's emails
  POST   /api/supplier-emails                  — create (Save to Log)
  PATCH  /api/supplier-emails/{id}             — state transition + edits
  DELETE /api/supplier-emails/{id}             — remove a row

Behaviour rules enforced here (from the design doc):

  * Field integrity:
      outbound rows: sent_at may be null or set; received_at MUST be null.
      inbound  rows: received_at MUST be set; sent_at MUST be null.

  * State is derived (no status column) from (direction, sent_at):
      direction='outbound' AND sent_at IS NULL      → "draft"
      direction='outbound' AND sent_at IS NOT NULL  → "sent"
      direction='inbound'                            → "inbound"

  * Drafts are user-action-only. The chat endpoint does NOT auto-POST
    when an EMAIL_DRAFT response is generated. POST fires only when the
    user clicks [Save to Log] / [Log inbound reply] in the UI.
"""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from db import get_db
from models import SavedSupplier, SupplierEmail


_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["supplier-emails"])


# ---------------------------------------------------------------------------
# State derivation
# ---------------------------------------------------------------------------

def _derive_state(direction: str, sent_at: Optional[datetime]) -> str:
    """Compute the displayed state from stored fields. No status column."""
    if direction == "inbound":
        return "inbound"
    if direction == "outbound" and sent_at is None:
        return "draft"
    return "sent"


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class EmailOut(BaseModel):
    """Response shape — what the frontend reads."""
    id:           int
    supplier_id:  int
    subject:      str
    body:         str
    direction:    str
    state:        str               # derived, not stored
    ai_generated: bool
    created_at:   datetime
    sent_at:      Optional[datetime] = None
    received_at:  Optional[datetime] = None
    thread_id:    Optional[str] = None


def _to_out(e: SupplierEmail) -> EmailOut:
    return EmailOut(
        id=e.id,
        supplier_id=e.supplier_id,
        subject=e.subject,
        body=e.body,
        direction=e.direction,
        state=_derive_state(e.direction, e.sent_at),
        ai_generated=e.ai_generated,
        created_at=e.created_at,
        sent_at=e.sent_at,
        received_at=e.received_at,
        thread_id=e.thread_id,
    )


class EmailCreate(BaseModel):
    """POST body. supplier_id comes from the body, not the URL, so the same
    endpoint serves both outbound (drafts) and inbound (logged replies)."""
    supplier_id:  int
    subject:      str = Field(min_length=1, max_length=500)
    body:         str = Field(min_length=1)
    direction:    str = Field(pattern=r"^(outbound|inbound)$")
    ai_generated: bool = False
    sent_at:      Optional[datetime] = None
    received_at:  Optional[datetime] = None
    thread_id:    Optional[str] = Field(default=None, max_length=50)

    @field_validator("subject", "body")
    @classmethod
    def _strip_and_check(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must be non-empty after stripping whitespace")
        return v


class EmailUpdate(BaseModel):
    """PATCH body. All fields optional. direction / supplier_id are
    intentionally NOT updatable — those are identity, not state."""
    subject:     Optional[str]      = Field(default=None, min_length=1, max_length=500)
    body:        Optional[str]      = Field(default=None, min_length=1)
    sent_at:     Optional[datetime] = None
    received_at: Optional[datetime] = None
    thread_id:   Optional[str]      = Field(default=None, max_length=50)
    # Allow explicit clearing — "I marked it sent by mistake, undo".
    # Pydantic distinguishes "field not present" vs "field set to None"
    # via model_fields_set on the parsed instance, which we read below.

    @field_validator("subject", "body")
    @classmethod
    def _strip(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("must be non-empty after stripping whitespace")
        return v


# ---------------------------------------------------------------------------
# Field-integrity validation (doc rule)
# ---------------------------------------------------------------------------

def _validate_integrity(direction: str,
                        sent_at: Optional[datetime],
                        received_at: Optional[datetime]) -> None:
    """Raise HTTP 400 if the (direction, sent_at, received_at) trio violates
    the rules from docs/email-log-feature.md."""
    if direction == "outbound":
        if received_at is not None:
            raise HTTPException(
                status_code=400,
                detail="outbound emails must not carry received_at",
            )
        # sent_at may be null (draft) or set (sent) — both valid.
    elif direction == "inbound":
        if sent_at is not None:
            raise HTTPException(
                status_code=400,
                detail="inbound emails must not carry sent_at",
            )
        if received_at is None:
            raise HTTPException(
                status_code=400,
                detail="inbound emails must carry received_at",
            )
    else:
        # Pydantic's regex should have caught this, but defense in depth.
        raise HTTPException(
            status_code=400,
            detail=f"direction must be 'outbound' or 'inbound', got {direction!r}",
        )


def _get_email_or_404(db: Session, email_id: int) -> SupplierEmail:
    e = db.query(SupplierEmail).filter(SupplierEmail.id == email_id).first()
    if not e:
        raise HTTPException(status_code=404, detail="Email not found")
    return e


def _supplier_exists_and_active(db: Session, supplier_id: int) -> None:
    """Ensure the supplier exists and is not soft-deleted. Mirrors
    routes/suppliers.py._get_active_supplier semantics."""
    s = (db.query(SavedSupplier)
           .filter(SavedSupplier.id == supplier_id,
                   SavedSupplier.is_saved == True)  # noqa: E712
           .first())
    if not s:
        raise HTTPException(status_code=404, detail="Supplier not found")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/suppliers/{supplier_id}/emails", response_model=list[EmailOut])
def list_supplier_emails(supplier_id: int, db: Session = Depends(get_db)):
    """List all emails for a supplier, newest event first.

    Order rule:
      * outbound sent       → sort by sent_at desc
      * outbound draft      → sort by created_at desc (no event time yet)
      * inbound             → sort by received_at desc
    All collapsed into a single "effective_time" expression at SQL level
    so a chronological mixed list comes out correctly.
    """
    _supplier_exists_and_active(db, supplier_id)

    rows = (
        db.query(SupplierEmail)
          .filter(SupplierEmail.supplier_id == supplier_id)
          .all()
    )

    def _effective_time(e: SupplierEmail) -> datetime:
        return e.sent_at or e.received_at or e.created_at

    rows.sort(key=_effective_time, reverse=True)
    return [_to_out(e) for e in rows]


@router.post("/supplier-emails", response_model=EmailOut, status_code=201)
def create_supplier_email(payload: EmailCreate, db: Session = Depends(get_db)):
    """Create a new email row. Triggered by user action only:
    [Save to Log] for outbound, [Log inbound reply] for inbound.
    Never called automatically by the chat endpoint."""
    _supplier_exists_and_active(db, payload.supplier_id)
    _validate_integrity(payload.direction, payload.sent_at, payload.received_at)

    e = SupplierEmail(
        supplier_id=payload.supplier_id,
        subject=payload.subject,
        body=payload.body,
        direction=payload.direction,
        ai_generated=payload.ai_generated,
        sent_at=payload.sent_at,
        received_at=payload.received_at,
        thread_id=payload.thread_id,
    )
    db.add(e)
    db.commit()
    db.refresh(e)

    _log.info(
        "[email] created id=%d supplier=%d direction=%s state=%s",
        e.id, e.supplier_id, e.direction, _derive_state(e.direction, e.sent_at),
    )
    return _to_out(e)


@router.patch("/supplier-emails/{email_id}", response_model=EmailOut)
def patch_supplier_email(email_id: int,
                         payload: EmailUpdate,
                         db: Session = Depends(get_db)):
    """Update an existing email. Primary use case: draft → sent state
    transition (set sent_at). Secondary: edit subject/body, correct
    received_at on a logged inbound reply.

    Cannot change direction or supplier_id — those are immutable identity.
    """
    e = _get_email_or_404(db, email_id)
    fields_set = payload.model_fields_set

    # Apply changes (model_fields_set distinguishes "not provided" from
    # "explicitly set to None" — we want to honour explicit clears).
    if "subject" in fields_set:
        e.subject = payload.subject
    if "body" in fields_set:
        e.body = payload.body
    if "sent_at" in fields_set:
        e.sent_at = payload.sent_at
    if "received_at" in fields_set:
        e.received_at = payload.received_at
    if "thread_id" in fields_set:
        e.thread_id = payload.thread_id

    # Re-validate the resulting (direction, sent_at, received_at) tuple.
    _validate_integrity(e.direction, e.sent_at, e.received_at)

    db.commit()
    db.refresh(e)
    _log.info(
        "[email] patched id=%d state=%s (changed: %s)",
        e.id, _derive_state(e.direction, e.sent_at), sorted(fields_set),
    )
    return _to_out(e)


@router.delete("/supplier-emails/{email_id}", status_code=204)
def delete_supplier_email(email_id: int, db: Session = Depends(get_db)):
    """Hard delete. Soft-delete is intentionally not used — emails are
    user-controlled records and the user explicitly asked for removal."""
    e = _get_email_or_404(db, email_id)
    db.delete(e)
    db.commit()
    _log.info("[email] deleted id=%d", email_id)
    return None
