"""Response shapes for documents, pages, chunks and the questions that cited them."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.schemas.common import ORMModel


class DocumentOut(BaseModel):
    id: uuid.UUID
    portfolio_id: uuid.UUID
    portfolio_name: str
    title: str
    filename: str
    doc_type: str
    status: str
    error: str | None = None
    page_count: int
    chunk_count: int
    byte_size: int
    uploaded_by_name: str | None = None
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    #: ``none`` until somebody runs the extractor, then the latest run's
    #: status. The register only shows values from a ``done`` run.
    extraction_status: str
    #: Values from the latest extraction still waiting for a person. This is
    #: the number that decides whether the document counts in the register.
    review_pending: int


class UploadResult(BaseModel):
    document: DocumentOut
    #: False when the same bytes were already in this portfolio, in which
    #: case ``document`` is the one that was there.
    created: bool


class DocumentPageOut(ORMModel):
    page_number: int
    text: str


class ChunkOut(ORMModel):
    id: uuid.UUID
    page_number: int
    ordinal: int
    text: str


class ChunkDetail(ChunkOut):
    document_id: uuid.UUID
    document_title: str
    #: The sentence blocks a citation's ``block_start``/``block_end`` index
    #: into -- split exactly as they were when the model was shown them.
    blocks: list[str]


class CitationOut(BaseModel):
    ordinal: int
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    page_number: int
    cited_text: str
    source_index: int
    block_start: int
    block_end: int


class DocumentQuestionOut(BaseModel):
    """A question that cited this document, with only the citations that
    point into it. The full answer lives at ``/questions/{id}``."""

    id: uuid.UUID
    user_id: uuid.UUID | None = None
    user_name: str | None = None
    portfolio_id: uuid.UUID | None = None
    text: str
    status: str
    answer_text: str | None = None
    provider: str
    model: str
    latency_ms: int | None = None
    cost_usd: str
    created_at: datetime
    finished_at: datetime | None = None
    citations: list[CitationOut]
