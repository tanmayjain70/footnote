"""The register: key lease terms, each with its evidence and its reviewer.

A value has two halves. What the model produced (``value_text``, ``quote``,
``chunk_id``, ``confidence``) is never edited. What a person decided
(``review_status``, ``corrected_json``, ``reviewed_by``) sits beside it. The
register reads the second half, and a value that nobody has confirmed does not
count -- that is the third requirement in the brief, and it is enforced by the
query, not by a checkbox.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, uuid_pk
from app.models.document import Chunk, Document
from app.models.enums import ExtractionStatus, ReviewStatus
from app.models.question import COST
from app.models.user import User


class Extraction(Base, TimestampMixin):
    """One run of the extractor over one document."""

    __tablename__ = "extractions"
    __table_args__ = (
        CheckConstraint("status IN ('queued','running','done','failed')", name="status_valid"),
        Index("ix_extractions_document_created", "document_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ExtractionStatus.QUEUED
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Which version of the field registry produced these values, so an
    #: added field is visibly missing rather than silently null.
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(COST, nullable=False, default=Decimal("0"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    document: Mapped[Document] = relationship()
    creator: Mapped[User | None] = relationship()
    values: Mapped[list[ExtractedValue]] = relationship(
        back_populates="extraction",
        order_by="ExtractedValue.field_key",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class ExtractedValue(Base, TimestampMixin):
    __tablename__ = "extracted_values"
    __table_args__ = (
        UniqueConstraint(
            "extraction_id", "field_key", name="uq_extracted_values_extraction_field"
        ),
        CheckConstraint("confidence IN ('high','medium','low')", name="confidence_valid"),
        CheckConstraint(
            "review_status IN ('pending','confirmed','corrected','rejected')",
            name="review_status_valid",
        ),
        Index("ix_extracted_values_document_field", "document_id", "field_key"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    extraction_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("extractions.id", ondelete="CASCADE"), nullable=False
    )
    #: Denormalised from the extraction so the register can join documents
    #: to values without going through extractions.
    document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    field_key: Mapped[str] = mapped_column(String(48), nullable=False)

    #: The typed value: ISO date string, number, bool, string, or null.
    value_json: Mapped[Any | None] = mapped_column(JSONB)
    #: As the model wrote it, before parsing. Kept so a parse failure can be
    #: read and corrected by a person rather than guessed at.
    value_text: Mapped[str | None] = mapped_column(String(512))
    quote: Mapped[str | None] = mapped_column(Text)
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("chunks.id", ondelete="SET NULL")
    )
    page_number: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[str] = mapped_column(String(8), nullable=False)
    #: no_evidence | invalid_chunk | quote_not_found | unparseable. Set when
    #: the evidence the model gave could not be verified against the text.
    issue: Mapped[str | None] = mapped_column(String(32))

    review_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ReviewStatus.PENDING
    )
    #: The reviewer's replacement, parsed the same way as the model's value.
    #: Wins over ``value_json`` everywhere a value is read.
    corrected_json: Mapped[Any | None] = mapped_column(JSONB)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_note: Mapped[str | None] = mapped_column(Text)

    extraction: Mapped[Extraction] = relationship(back_populates="values")
    chunk: Mapped[Chunk | None] = relationship()
    reviewer: Mapped[User | None] = relationship()
