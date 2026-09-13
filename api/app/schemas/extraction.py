"""Response shapes for extraction, review, the review queue and the register.

``ExtractedValueOut`` carries both halves of a value side by side -- what the
model produced and what a person decided -- and ``effective_value`` resolves
them, so the frontend never has to know that a correction wins.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class FieldOut(BaseModel):
    """One entry of the field registry, as ``GET /fields`` lists it."""

    key: str
    label: str
    type: str
    description: str
    enum_values: list[str] = Field(default_factory=list)


class ExtractedValueOut(ORMModel):
    id: uuid.UUID
    field_key: str
    label: str
    type: str
    #: The typed value: ISO date string, number, bool, text, or null.
    value_json: Any = None
    #: As the model wrote it, before parsing.
    value_text: str | None = None
    #: The correction when there is one, else the extracted value.
    effective_value: Any = None
    quote: str | None = None
    chunk_id: uuid.UUID | None = None
    page_number: int | None = None
    confidence: str
    issue: str | None = None
    review_status: str
    corrected_json: Any = None
    reviewed_by_name: str | None = None
    reviewed_at: datetime | None = None
    review_note: str | None = None


class ExtractionOut(ORMModel):
    id: uuid.UUID
    document_id: uuid.UUID
    status: str
    provider: str
    model: str
    schema_version: int
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    #: Six decimal places as a string; a JSON number would round them.
    cost_usd: str
    values: list[ExtractedValueOut] = Field(default_factory=list)


class ReviewRequest(BaseModel):
    action: Literal["confirm", "correct", "reject"]
    #: Required for ``correct``: parsed with the same rules as the model's
    #: value, so a correction can never be less well-formed than what it
    #: replaces.
    corrected_value: str | None = Field(default=None, max_length=512)
    note: str | None = Field(default=None, max_length=2000)


class ReviewQueueItem(BaseModel):
    document_id: uuid.UUID
    portfolio_id: uuid.UUID | None = None
    title: str
    portfolio_name: str
    #: Values still waiting for a person.
    pending: int
    #: Pending values that also carry an ``issue`` -- the ones to look at
    #: first, because the model's evidence did not check out.
    issues: int


class RegisterReview(BaseModel):
    confirmed: int = 0
    pending: int = 0
    corrected: int = 0
    rejected: int = 0


class RegisterRow(BaseModel):
    """One document's key terms.

    Unreviewed values are null unless the request asked for
    ``include_unreviewed``: a value nobody has confirmed does not count, and
    the shape makes that true rather than a rendering choice.
    """

    document_id: uuid.UUID
    portfolio_id: uuid.UUID | None = None
    title: str
    portfolio: str
    tenant_name: str | None = None
    unit: str | None = None
    property_address: str | None = None
    term_start: str | None = None
    term_end: str | None = None
    annual_rent_gbp: float | None = None
    rent_review_basis: str | None = None
    rent_review_date: str | None = None
    break_date: str | None = None
    break_notice_months: int | None = None
    repairing_obligation: str | None = None
    review: RegisterReview
    #: Every value the model found has been confirmed, corrected or rejected.
    complete: bool


class RegisterCounts(BaseModel):
    documents: int
    complete: int
    pending_values: int


class RegisterResponse(BaseModel):
    rows: list[RegisterRow]
    counts: RegisterCounts
