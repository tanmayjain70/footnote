"""Questions, what the model was shown, and what it cited.

Three tables rather than one JSON column, because the claim the product makes
is checkable only if the evidence is kept as rows: ``question_sources`` is the
exact list of passages the model saw, ``citations`` is what it pointed at, and
a citation that does not resolve to a source is dropped and counted. The count
is the number the client asked for -- how often the model points at something
it was not shown.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, uuid_pk
from app.models.document import Chunk
from app.models.user import User

#: Six decimal places because a single call on a small model costs fractions
#: of a cent, and rounding each one to pennies would make the daily total
#: drift from what the provider bills.
COST = Numeric(10, 6)


class Question(Base):
    __tablename__ = "questions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('answered','unanswered','failed','budget_exhausted')",
            name="status_valid",
        ),
        Index("ix_questions_user_created", "user_id", "created_at"),
        Index("ix_questions_created", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    #: Optional scope. NULL means "everything I can see".
    portfolio_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("portfolios.id", ondelete="SET NULL")
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)

    answer_text: Mapped[str | None] = mapped_column(Text)
    #: ``[{"block": i, "text": str, "citations": [ordinal, ...]}]`` -- the
    #: answer as blocks so the UI can place each [n] where the model put it.
    answer_blocks: Mapped[list[Any] | None] = mapped_column(JSONB)

    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    #: k, candidates, rrf_k and the embedding model at the time. Recorded per
    #: question so that a retrieval change is comparable, not just felt.
    retrieval_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )

    latency_ms: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(COST, nullable=False, default=Decimal("0"))
    #: Citations the model produced that did not resolve to a passage it was
    #: shown. Not rendered, but counted -- this is the number that matters.
    dropped_citations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User | None] = relationship()
    sources: Mapped[list[QuestionSource]] = relationship(
        back_populates="question",
        order_by="QuestionSource.source_index",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    citations: Mapped[list[Citation]] = relationship(
        back_populates="question",
        order_by="Citation.ordinal",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    feedback: Mapped[list[Feedback]] = relationship(
        back_populates="question", cascade="all, delete-orphan", passive_deletes=True
    )


class QuestionSource(Base):
    """One passage the model was shown, in the position it was shown."""

    __tablename__ = "question_sources"
    __table_args__ = (Index("ix_question_sources_chunk", "chunk_id"),)

    question_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("questions.id", ondelete="CASCADE"), primary_key=True
    )
    chunk_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("chunks.id", ondelete="CASCADE"), primary_key=True
    )
    #: 0-based position in the list. The model cites by this number.
    source_index: Mapped[int] = mapped_column(Integer, nullable=False)
    fused_score: Mapped[float] = mapped_column(Float, nullable=False)
    vector_rank: Mapped[int | None] = mapped_column(Integer)
    lexical_rank: Mapped[int | None] = mapped_column(Integer)

    question: Mapped[Question] = relationship(back_populates="sources")
    chunk: Mapped[Chunk] = relationship()


class Citation(Base):
    """A verified pointer from the answer into a passage the model was shown."""

    __tablename__ = "citations"
    __table_args__ = (
        Index("ix_citations_question", "question_id"),
        Index("ix_citations_chunk", "chunk_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    question_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("questions.id", ondelete="CASCADE"), nullable=False
    )
    chunk_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("chunks.id", ondelete="CASCADE"), nullable=False
    )
    #: 1-based label shown as [n]. The same chunk cited twice keeps one label.
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    source_index: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Sentence-block range within the chunk, end exclusive. Sentences are
    #: split the same way at ingest and at answer time, so these map back.
    block_start: Mapped[int] = mapped_column(Integer, nullable=False)
    block_end: Mapped[int] = mapped_column(Integer, nullable=False)
    cited_text: Mapped[str] = mapped_column(Text, nullable=False)

    question: Mapped[Question] = relationship(back_populates="citations")
    chunk: Mapped[Chunk] = relationship()


class Feedback(Base):
    __tablename__ = "feedback"
    __table_args__ = (
        UniqueConstraint("question_id", "user_id", name="uq_feedback_question_user"),
        CheckConstraint("verdict IN ('up','down')", name="verdict_valid"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    question_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("questions.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    verdict: Mapped[str] = mapped_column(String(8), nullable=False)
    #: wrong | missing_citation | incomplete | should_have_refused | other
    reason: Mapped[str | None] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    question: Mapped[Question] = relationship(back_populates="feedback")
    user: Mapped[User] = relationship()
