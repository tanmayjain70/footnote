"""Shapes for asking, for stored questions, and for the events streamed back.

``AnswerOut`` is the whole record of one question: what was asked, what the
model was shown, what it said, and which of its citations survived checking.
A stored question is the same shape as a finished answer, so the screen that
renders a live stream renders history with the same code.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

QUESTION_MIN_CHARS = 3
QUESTION_MAX_CHARS = 2000

FeedbackVerdict = Literal["up", "down"]
FeedbackReason = Literal["wrong", "missing_citation", "incomplete", "should_have_refused", "other"]


class AskRequest(BaseModel):
    question: str = Field(min_length=QUESTION_MIN_CHARS, max_length=QUESTION_MAX_CHARS)
    #: Restrict retrieval to one portfolio. Absent means everything the
    #: caller can see.
    portfolio_id: uuid.UUID | None = None


class FeedbackRequest(BaseModel):
    verdict: FeedbackVerdict
    reason: FeedbackReason | None = None
    note: str | None = Field(default=None, max_length=2000)


class FeedbackOut(BaseModel):
    verdict: str
    reason: str | None
    note: str | None


class SourceOut(BaseModel):
    """One passage the model was shown, in the position it was shown."""

    index: int
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    page_number: int
    snippet: str
    fused_score: float
    vector_rank: int | None
    lexical_rank: int | None
    #: Whether a verified citation points at this passage. The screen uses it
    #: to show which of the eight passages actually carried the answer.
    cited: bool


class CitationOut(BaseModel):
    """A citation that resolved to a passage the model was shown."""

    ordinal: int
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    page_number: int
    cited_text: str
    source_index: int
    block_start: int
    block_end: int


class AnswerBlock(BaseModel):
    block: int
    text: str
    #: Ordinals of the citations attached to this block, in the order made.
    citations: list[int]


class AnswerOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID | None
    user_name: str | None
    portfolio_id: uuid.UUID | None
    text: str
    status: str
    answer_text: str | None
    #: Null until the answer has finished; the stream fills it in block by
    #: block and the stored row carries the final shape.
    answer_blocks: list[AnswerBlock] | None
    citations: list[CitationOut]
    sources: list[SourceOut]
    provider: str
    model: str
    latency_ms: int | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    #: Serialises as a decimal string with six places, never as a float.
    cost_usd: Decimal
    #: Citations the model made that did not match anything it was shown.
    #: Counted and never rendered: this is the number the client asked for.
    dropped_citations: int
    error: str | None
    #: The caller's own verdict on this answer, if they gave one.
    feedback: FeedbackOut | None
    created_at: datetime
    finished_at: datetime | None


#: A stored question is a finished answer.
QuestionOut = AnswerOut
