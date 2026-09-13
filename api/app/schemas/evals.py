"""Shapes for the golden question set and the runs measured against it.

Rates are fractions in 0..1 and are null when there was nothing to measure:
a run over an empty set has no hit rate, and reporting 0.0 would read as
"retrieval found nothing", which is a different claim.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from app.schemas.ask import QUESTION_MAX_CHARS, QUESTION_MIN_CHARS
from app.schemas.common import ORMModel

EvalModeName = Literal["retrieval", "end_to_end"]

#: 1-based, as printed on the page; the golden set never refers to page 0.
PageNumber = Annotated[int, Field(ge=1)]


class EvalQuestionOut(ORMModel):
    id: uuid.UUID
    question: str
    answerable: bool
    expected_document_id: uuid.UUID | None
    #: Resolved for the screen, so a list of questions does not need a
    #: request per document to say which lease each one is about.
    expected_document_title: str | None
    expected_pages: list[int]
    portfolio_id: uuid.UUID | None
    source: str
    active: bool
    notes: str | None
    created_at: datetime
    updated_at: datetime


class EvalQuestionCreate(BaseModel):
    question: str = Field(min_length=QUESTION_MIN_CHARS, max_length=QUESTION_MAX_CHARS)
    answerable: bool
    #: Required when ``answerable``: a hit has to be measured against something.
    expected_document_id: uuid.UUID | None = None
    expected_pages: list[PageNumber] = Field(default_factory=list)
    portfolio_id: uuid.UUID | None = None
    notes: str | None = Field(default=None, max_length=2000)


class EvalQuestionFromQuestion(BaseModel):
    """Promote a question somebody actually asked into the golden set."""

    question_id: uuid.UUID
    answerable: bool
    #: Defaults to the document the stored answer cited first, so a
    #: thumbs-up can be promoted without looking the document up again.
    expected_document_id: uuid.UUID | None = None
    expected_pages: list[PageNumber] = Field(default_factory=list)


class EvalQuestionPatch(BaseModel):
    active: bool


class EvalRunRequest(BaseModel):
    mode: EvalModeName
    #: Score only the first ``limit`` active questions -- a quick check
    #: before spending the budget on the whole set.
    limit: int | None = Field(default=None, ge=1, le=1000)


class EvalRunOut(ORMModel):
    id: uuid.UUID
    mode: str
    status: str
    started_by_name: str | None
    config: dict[str, Any]
    totals: dict[str, Any]
    started_at: datetime
    finished_at: datetime | None
    error: str | None


class EvalResultOut(BaseModel):
    eval_question_id: uuid.UUID
    question: str
    answerable: bool
    expected_document_title: str | None
    expected_pages: list[int]
    #: Null for unanswerable questions: "did retrieval find the page" has no
    #: meaning there, and a null is not a miss.
    retrieved_doc_hit: bool | None
    retrieved_page_hit: bool | None
    hit_rank: int | None
    #: The end-to-end fields; null on a retrieval-only run.
    answered: bool | None
    cited_doc_hit: bool | None
    cited_page_hit: bool | None
    refused_correctly: bool | None
    question_id: uuid.UUID | None
    latency_ms: int | None
    #: Six decimal places as a string; a JSON number would round them.
    cost_usd: str


class EvalRunDetail(EvalRunOut):
    results: list[EvalResultOut] = Field(default_factory=list)
