"""The golden question set and the runs measured against it.

A question here has a known answer location -- a document and the pages the
canonical sentence sits on -- or is known to be unanswerable. A run asks each
one and records whether retrieval found the page, whether the answer cited it,
and whether an unanswerable question was refused. Retrieval changes are then
measured against these numbers rather than judged by feel.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, uuid_pk
from app.models.document import Document
from app.models.enums import EvalRunStatus, EvalSource
from app.models.question import COST, Question
from app.models.user import User


class EvalQuestion(Base, TimestampMixin):
    __tablename__ = "eval_questions"
    __table_args__ = (
        CheckConstraint("source IN ('generated','feedback','manual')", name="source_valid"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    question: Mapped[str] = mapped_column(Text, nullable=False)
    #: False for questions the documents cannot answer. The right result for
    #: those is a refusal, and the harness scores it as one.
    answerable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    expected_document_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL")
    )
    expected_pages: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), nullable=False, default=list
    )
    #: Scope the question is asked in, if not the whole visible set.
    portfolio_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("portfolios.id", ondelete="SET NULL")
    )
    #: generated (from the lease generator), feedback (promoted from a real
    #: question somebody marked wrong), or manual.
    source: Mapped[str] = mapped_column(String(16), nullable=False, default=EvalSource.MANUAL)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    expected_document: Mapped[Document | None] = relationship()


class EvalRun(Base):
    __tablename__ = "eval_runs"
    __table_args__ = (
        CheckConstraint("mode IN ('retrieval','end_to_end')", name="mode_valid"),
        CheckConstraint("status IN ('running','done','failed')", name="status_valid"),
        Index("ix_eval_runs_started", "started_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    #: ``retrieval`` costs nothing and runs in seconds; ``end_to_end`` asks
    #: the model and spends budget, so it goes through the job queue.
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=EvalRunStatus.RUNNING
    )
    started_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    #: The retrieval settings and providers in force, so two runs can be
    #: compared knowing what differed.
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    totals: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    starter: Mapped[User | None] = relationship()
    results: Mapped[list[EvalResult]] = relationship(
        back_populates="run", cascade="all, delete-orphan", passive_deletes=True
    )


class EvalResult(Base):
    __tablename__ = "eval_results"
    __table_args__ = (
        UniqueConstraint("run_id", "eval_question_id", name="uq_eval_results_run_question"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("eval_runs.id", ondelete="CASCADE"), nullable=False
    )
    eval_question_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("eval_questions.id", ondelete="CASCADE"), nullable=False
    )
    #: NULL for unanswerable questions, where "did retrieval find the page"
    #: has no meaning. A NULL is not a miss.
    retrieved_doc_hit: Mapped[bool | None] = mapped_column(Boolean)
    retrieved_page_hit: Mapped[bool | None] = mapped_column(Boolean)
    hit_rank: Mapped[int | None] = mapped_column(Integer)
    answered: Mapped[bool | None] = mapped_column(Boolean)
    cited_doc_hit: Mapped[bool | None] = mapped_column(Boolean)
    cited_page_hit: Mapped[bool | None] = mapped_column(Boolean)
    refused_correctly: Mapped[bool | None] = mapped_column(Boolean)
    question_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("questions.id", ondelete="SET NULL")
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal] = mapped_column(COST, nullable=False, default=Decimal("0"))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    run: Mapped[EvalRun] = relationship(back_populates="results")
    eval_question: Mapped[EvalQuestion] = relationship()
    question: Mapped[Question | None] = relationship()
