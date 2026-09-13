"""The work queue.

A table rather than a broker because the whole demo runs in one process on a
free tier, and because ``FOR UPDATE SKIP LOCKED`` gives a second worker
correct claiming for free the day one is added. Attempts are counted so a PDF
that crashes the parser fails after three tries and says so, instead of being
retried forever by a worker nobody is watching.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, uuid_pk
from app.models.document import Document
from app.models.enums import JobStatus


class Job(Base, TimestampMixin):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint("kind IN ('ingest','extract','eval_run')", name="kind_valid"),
        CheckConstraint("status IN ('queued','running','done','failed')", name="status_valid"),
        Index("ix_jobs_status_created", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=JobStatus.QUEUED)
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE")
    )
    #: Kind-specific arguments -- an extraction id, an eval run id.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: ``hostname:pid`` of whoever took it, so a job stuck in ``running``
    #: can be traced to the process that died holding it.
    claimed_by: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    #: How far a job got before it failed: parse, chunk, embed, extract, run.
    stage: Mapped[str | None] = mapped_column(String(32))

    document: Mapped[Document | None] = relationship()
