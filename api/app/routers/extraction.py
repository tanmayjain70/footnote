"""Extraction, review, the review queue, and the field registry.

Running an extraction and reviewing its values are the two things that make a
value count, so both need a review role. Reading the result, the queue and the
registry is open to anyone who can see the document: the register is meant to
be read by the whole firm, and the queue is only the register's to-do list.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import NotFound
from app.deps import current_user, get_visible_document, require_review, visible_portfolio_ids
from app.models import ExtractedValue, Extraction, User
from app.schemas.extraction import (
    ExtractedValueOut,
    ExtractionOut,
    FieldOut,
    ReviewQueueItem,
    ReviewRequest,
)
from app.services import extraction as extraction_service
from app.services.extraction_fields import FIELDS

router = APIRouter(tags=["extraction"])

_SIX_DP = Decimal("0.000001")


def value_out(value: ExtractedValue) -> ExtractedValueOut:
    spec = extraction_service.FIELD_BY_KEY.get(value.field_key)
    return ExtractedValueOut(
        id=value.id,
        field_key=value.field_key,
        # A key that has left the registry still renders, labelled by its key,
        # rather than taking the whole document screen down.
        label=spec.label if spec else value.field_key,
        type=spec.type if spec else "text",
        value_json=value.value_json,
        value_text=value.value_text,
        effective_value=extraction_service.effective_value(value),
        quote=value.quote,
        chunk_id=value.chunk_id,
        page_number=value.page_number,
        confidence=value.confidence,
        issue=value.issue,
        review_status=value.review_status,
        corrected_json=value.corrected_json,
        reviewed_by_name=value.reviewer.full_name if value.reviewer else None,
        reviewed_at=value.reviewed_at,
        review_note=value.review_note,
    )


def extraction_out(extraction: Extraction) -> ExtractionOut:
    # Registry order rather than alphabetical: the reviewer reads the values
    # in the order the lease states them, parties first.
    order = {key: i for i, key in enumerate(extraction_service.FIELD_BY_KEY)}
    values = sorted(
        extraction.values, key=lambda v: (order.get(v.field_key, len(order)), v.field_key)
    )
    return ExtractionOut(
        id=extraction.id,
        document_id=extraction.document_id,
        status=extraction.status,
        provider=extraction.provider,
        model=extraction.model,
        schema_version=extraction.schema_version,
        started_at=extraction.started_at,
        finished_at=extraction.finished_at,
        error=extraction.error,
        cost_usd=str(Decimal(extraction.cost_usd or 0).quantize(_SIX_DP)),
        values=[value_out(value) for value in values],
    )


@router.post(
    "/documents/{document_id}/extract",
    response_model=ExtractionOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def request_extraction(
    document_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_review),
) -> ExtractionOut:
    """Queue an extraction of the document's key terms. 409 while one is
    already queued or running, or when the document is not ready."""
    document = get_visible_document(db, user, document_id)
    extraction = extraction_service.request_extraction(db, document, user)
    return extraction_out(extraction)


@router.get("/documents/{document_id}/extraction", response_model=ExtractionOut)
def get_extraction(
    document_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> ExtractionOut:
    """The newest extraction of the document, whatever its status."""
    document = get_visible_document(db, user, document_id)
    extraction = extraction_service.latest_extraction(db, document)
    if extraction is None:
        raise NotFound("This document has not been extracted yet.")
    return extraction_out(extraction)


@router.post("/extracted-values/{value_id}/review", response_model=ExtractedValueOut)
def review_value(
    value_id: uuid.UUID,
    body: ReviewRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_review),
) -> ExtractedValueOut:
    """Confirm, correct or reject one value. A correction is parsed with the
    same rules as the extracted value and refused (422) when it is not one."""
    value = db.get(ExtractedValue, value_id)
    if value is None:
        raise NotFound("No such value.")
    # The same 404 as for the document itself: a value id must not confirm
    # the existence of a lease the caller cannot see.
    get_visible_document(db, user, value.document_id)
    value = extraction_service.review(
        db,
        value,
        user,
        action=body.action,
        corrected_text=body.corrected_value,
        note=body.note,
    )
    return value_out(value)


@router.get("/review-queue", response_model=list[ReviewQueueItem])
def review_queue(
    portfolio_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[ReviewQueueItem]:
    """Documents with values still waiting for a person, scoped to what the
    caller can see."""
    visible = visible_portfolio_ids(db, user)
    if portfolio_id is not None and portfolio_id not in set(visible):
        raise NotFound("No such portfolio.")
    return [
        ReviewQueueItem(**item)
        for item in extraction_service.review_queue(
            db, visible_ids=visible, portfolio_id=portfolio_id
        )
    ]


@router.get("/fields", response_model=list[FieldOut])
def list_fields(_: User = Depends(current_user)) -> list[FieldOut]:
    """The field registry: what is extracted from every lease, in order."""
    return [
        FieldOut(
            key=spec.key,
            label=spec.label,
            type=spec.type,
            description=spec.description,
            enum_values=list(spec.enum_values),
        )
        for spec in FIELDS
    ]
