"""Documents, their pages, and the chunks retrieval runs over.

The page is the unit of citation: every chunk knows its page, every citation
resolves to a page, and the page text is stored so that the "open page" link in
the UI shows exactly what the model was shown. The original PDF bytes are kept
in their own table so listing documents never drags megabytes through the
session.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, uuid_pk
from app.models.enums import DocumentStatus, DocumentType
from app.models.portfolio import Portfolio
from app.models.user import User

#: bge-small and the hashed fallback both produce this many dimensions. It is
#: baked into the column type, so changing the model means a migration.
EMBEDDING_DIMENSIONS = 384


class Document(Base, TimestampMixin):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "content_sha256", name="uq_documents_portfolio_sha256"),
        CheckConstraint(
            "doc_type IN ('lease','deed_of_variation','side_letter','notice','other')",
            name="doc_type_valid",
        ),
        CheckConstraint(
            "status IN ('queued','processing','ready','failed')", name="status_valid"
        ),
        Index("ix_documents_portfolio_status", "portfolio_id", "status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    #: RESTRICT, not CASCADE: deleting a portfolio with documents in it should
    #: be a deliberate act, not a side effect.
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("portfolios.id", ondelete="RESTRICT"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    doc_type: Mapped[str] = mapped_column(String(32), nullable=False, default=DocumentType.LEASE)

    #: Identity of the bytes. The same file uploaded twice into one portfolio
    #: is one document, and the second upload says so rather than failing.
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DocumentStatus.QUEUED
    )
    error: Mapped[str | None] = mapped_column(Text)

    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    #: Property, unit and tenant hints supplied at upload. Free-form on
    #: purpose: the register is the structured record, this is a label.
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )

    portfolio: Mapped[Portfolio] = relationship()
    uploader: Mapped[User | None] = relationship()
    pages: Mapped[list[DocumentPage]] = relationship(
        back_populates="document",
        order_by="DocumentPage.page_number",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    chunks: Mapped[list[Chunk]] = relationship(
        back_populates="document",
        order_by="Chunk.ordinal",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class DocumentBlob(Base):
    """The uploaded bytes, kept so a document can be re-ingested when the
    chunking changes without asking anybody to find the file again."""

    __tablename__ = "document_blobs"

    document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DocumentPage(Base):
    __tablename__ = "document_pages"
    __table_args__ = (
        UniqueConstraint("document_id", "page_number", name="uq_document_pages_document_page"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    #: 1-based, as printed on the page and as a person would say it.
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)

    document: Mapped[Document] = relationship(back_populates="pages")


class Chunk(Base):
    """A passage the model can be shown, with both retrieval representations.

    Two indexes because two legs: the HNSW index serves the embedding search
    and the GIN index serves the lexical one. A lease is full of exact tokens
    a vector search is bad at -- "Unit 4B", "£42,500", "31 March 2029" -- and
    the fused ranking is what makes those questions land on the right page.
    """

    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "ordinal", name="uq_chunks_document_ordinal"),
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
        Index(
            "ix_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_chunks_document_page", "document_id", "page_number"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 0-based position within the whole document, not within the page.
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    #: Offsets into the page text, so the UI can highlight the passage.
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    token_estimate: Mapped[int] = mapped_column(Integer, nullable=False)

    #: NULL until the embedding stage has run. Retrieval skips NULLs, so a
    #: half-ingested document is simply not found rather than found wrongly.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    #: Maintained by the database, never written by the application.
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', text)", persisted=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    document: Mapped[Document] = relationship(back_populates="chunks")
