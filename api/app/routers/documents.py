"""Documents: upload, list, read pages and chunks, re-ingest, delete.

Everything here goes through ``get_visible_document`` or the visible-portfolio
list, so a document in a portfolio the caller is not a member of is a 404 on
every route -- including delete, where a 403 would confirm it exists.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import NotFound, PayloadTooLarge, UnprocessableUpload
from app.deps import current_user, get_visible_document, require_upload, visible_portfolio_ids
from app.models import (
    Chunk,
    Citation,
    Document,
    DocumentPage,
    ExtractedValue,
    Extraction,
    Question,
    User,
)
from app.models.enums import DocumentStatus, DocumentType, ReviewStatus, Role
from app.schemas.common import Page
from app.schemas.documents import (
    ChunkDetail,
    ChunkOut,
    CitationOut,
    DocumentOut,
    DocumentPageOut,
    DocumentQuestionOut,
    UploadResult,
)
from app.services import answering, ingestion

router = APIRouter(tags=["documents"])

PDF_MAGIC = b"%PDF"

#: A multipart body carries boundaries, headers and the other form fields
#: around the file. Allowing a little more than the file limit means a file
#: exactly at the limit is not refused for its envelope.
_MULTIPART_SLACK = 8 * 1024


# ============================================================== shaping ===


def _outs(db: Session, documents: list[Document]) -> list[DocumentOut]:
    """Serialise documents with the two figures the list screen sorts by.

    Both come from the extraction tables directly rather than through the
    extraction service: a document list must not depend on anything more
    than the rows, and two grouped queries over the page of ids is cheap.
    """
    if not documents:
        return []
    ids = [d.id for d in documents]

    latest = {
        document_id: (extraction_id, extraction_status)
        for document_id, extraction_id, extraction_status in db.execute(
            select(Extraction.document_id, Extraction.id, Extraction.status)
            .distinct(Extraction.document_id)
            .where(Extraction.document_id.in_(ids))
            .order_by(Extraction.document_id, Extraction.created_at.desc(), Extraction.id)
        ).all()
    }
    pending: dict[uuid.UUID, int] = {}
    if latest:
        pending = dict(
            db.execute(
                select(ExtractedValue.extraction_id, func.count())
                .where(
                    ExtractedValue.extraction_id.in_([e for e, _ in latest.values()]),
                    ExtractedValue.review_status == ReviewStatus.PENDING,
                )
                .group_by(ExtractedValue.extraction_id)
            ).all()
        )

    out = []
    for d in documents:
        extraction_id, extraction_status = latest.get(d.id, (None, "none"))
        out.append(
            DocumentOut(
                id=d.id,
                portfolio_id=d.portfolio_id,
                portfolio_name=d.portfolio.name,
                title=d.title,
                filename=d.filename,
                doc_type=d.doc_type,
                status=d.status,
                error=d.error,
                page_count=d.page_count,
                chunk_count=d.chunk_count,
                byte_size=d.byte_size,
                uploaded_by_name=d.uploader.full_name if d.uploader else None,
                metadata=d.metadata_,
                created_at=d.created_at,
                updated_at=d.updated_at,
                extraction_status=extraction_status,
                review_pending=pending.get(extraction_id, 0) if extraction_id else 0,
            )
        )
    return out


def _out(db: Session, document: Document) -> DocumentOut:
    return _outs(db, [document])[0]


# ============================================================== routes ===


@router.get("/documents", response_model=Page[DocumentOut])
def list_documents(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    portfolio_id: uuid.UUID | None = None,
    status: DocumentStatus | None = None,
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[DocumentOut]:
    visible = visible_portfolio_ids(db, user)
    if portfolio_id is not None and portfolio_id not in visible:
        raise NotFound("No such portfolio.")
    if not visible:
        return Page(items=[], total=0, limit=limit, offset=offset)

    filters: list[Any] = [Document.portfolio_id.in_([portfolio_id] if portfolio_id else visible)]
    if status is not None:
        filters.append(Document.status == status)
    if q:
        # ``%`` and ``_`` are wildcards in LIKE, and somebody searching for
        # "Unit_4" means the underscore. Escaped, with the escape character
        # named, or `q=%` would match the whole archive.
        escaped = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        filters.append(
            or_(
                Document.title.ilike(pattern, escape="\\"),
                Document.filename.ilike(pattern, escape="\\"),
            )
        )

    total = db.execute(select(func.count()).select_from(Document).where(*filters)).scalar_one()
    rows = (
        db.execute(
            select(Document)
            .options(selectinload(Document.portfolio), selectinload(Document.uploader))
            .where(*filters)
            .order_by(Document.created_at.desc(), Document.title)
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    return Page(items=_outs(db, list(rows)), total=total, limit=limit, offset=offset)


@router.post("/documents", response_model=UploadResult, status_code=status.HTTP_201_CREATED)
def upload_document(
    request: Request,
    response: Response,
    file: UploadFile = File(...),
    portfolio_id: uuid.UUID = Form(...),
    title: str | None = Form(default=None, max_length=255),
    doc_type: DocumentType = Form(default=DocumentType.LEASE),
    db: Session = Depends(get_db),
    user: User = Depends(require_upload),
) -> UploadResult:
    """Store a PDF and queue it. 201 with ``created: true``, or 200 with the
    document that already had these bytes."""
    if portfolio_id not in visible_portfolio_ids(db, user):
        raise NotFound("No such portfolio.")

    limit = get_settings().max_upload_bytes
    # The declared length first: by the time this handler runs the body has
    # already been received and spooled, so this only saves the reading --
    # the real ceiling belongs in front of the application, and the
    # deployment notes say so. Then one byte past the limit, because a
    # declared length is a claim.
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit + _MULTIPART_SLACK:
        raise PayloadTooLarge(
            f"That file is larger than the {get_settings().max_upload_mb} MB limit.",
            detail={"max_upload_mb": get_settings().max_upload_mb},
        )
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise PayloadTooLarge(
            f"That file is larger than the {get_settings().max_upload_mb} MB limit.",
            detail={"max_upload_mb": get_settings().max_upload_mb},
        )
    if not data.startswith(PDF_MAGIC):
        raise UnprocessableUpload(
            "Only PDF files can be uploaded. Word documents and images need converting first."
        )

    document, created = ingestion.create_document(
        db,
        portfolio_id=portfolio_id,
        uploaded_by=user.id,
        filename=file.filename or "upload.pdf",
        title=title,
        doc_type=doc_type,
        data=data,
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return UploadResult(document=_out(db, document), created=created)


@router.get("/documents/{document_id}", response_model=DocumentOut)
def get_document(
    document_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> DocumentOut:
    return _out(db, get_visible_document(db, user, document_id))


@router.get("/documents/{document_id}/pages", response_model=list[DocumentPageOut])
def list_pages(
    document_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[DocumentPageOut]:
    document = get_visible_document(db, user, document_id)
    pages = db.execute(
        select(DocumentPage)
        .where(DocumentPage.document_id == document.id)
        .order_by(DocumentPage.page_number)
    ).scalars()
    return [DocumentPageOut.model_validate(p) for p in pages]


@router.get("/documents/{document_id}/chunks", response_model=list[ChunkOut])
def list_chunks(
    document_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    page: int | None = Query(default=None, ge=1),
) -> list[ChunkOut]:
    document = get_visible_document(db, user, document_id)
    statement = select(Chunk).where(Chunk.document_id == document.id).order_by(Chunk.ordinal)
    if page is not None:
        statement = statement.where(Chunk.page_number == page)
    return [ChunkOut.model_validate(c) for c in db.execute(statement).scalars()]


@router.get("/chunks/{chunk_id}", response_model=ChunkDetail)
def get_chunk(
    chunk_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> ChunkDetail:
    """One passage with its sentence blocks -- what a citation's block range
    indexes into, split by the same function that split it for the model."""
    from app.services.chunking import split_sentences

    chunk = db.get(Chunk, chunk_id)
    if chunk is None:
        raise NotFound("No such passage.")
    document = get_visible_document(db, user, chunk.document_id)
    return ChunkDetail(
        id=chunk.id,
        page_number=chunk.page_number,
        ordinal=chunk.ordinal,
        text=chunk.text,
        document_id=document.id,
        document_title=document.title,
        blocks=split_sentences(chunk.text),
    )


@router.post(
    "/documents/{document_id}/reingest",
    response_model=DocumentOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def reingest_document(
    document_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_upload),
) -> DocumentOut:
    document = get_visible_document(db, user, document_id)
    ingestion.reingest(db, document)
    return _out(db, document)


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(
    document_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_upload),
) -> Response:
    document = get_visible_document(db, user, document_id)
    ingestion.delete_document(db, document)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/documents/{document_id}/questions", response_model=list[DocumentQuestionOut])
def list_document_questions(
    document_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[DocumentQuestionOut]:
    """Questions whose answer cited a passage of this document.

    The same visibility rule as ``/questions``: your own, or everything for
    the director. A manager who cannot see a colleague's question does not
    get to see it because it landed on a shared lease either.
    """
    document = get_visible_document(db, user, document_id)

    statement = (
        select(Question)
        .options(selectinload(Question.user))
        .where(answering.asked_by_a_person())
        .join(Citation, Citation.question_id == Question.id)
        .join(Chunk, Chunk.id == Citation.chunk_id)
        .where(Chunk.document_id == document.id)
        .distinct()
        .order_by(Question.created_at.desc())
        .limit(limit)
    )
    if user.role != Role.DIRECTOR:
        statement = statement.where(Question.user_id == user.id)
    questions = db.execute(statement).scalars().all()
    if not questions:
        return []

    citations: dict[uuid.UUID, list[CitationOut]] = {q.id: [] for q in questions}
    for citation, page_number in db.execute(
        select(Citation, Chunk.page_number)
        .join(Chunk, Chunk.id == Citation.chunk_id)
        .where(
            Citation.question_id.in_(list(citations)),
            Chunk.document_id == document.id,
        )
        .order_by(Citation.question_id, Citation.ordinal)
    ).all():
        citations[citation.question_id].append(
            CitationOut(
                ordinal=citation.ordinal,
                chunk_id=citation.chunk_id,
                document_id=document.id,
                document_title=document.title,
                page_number=page_number,
                cited_text=citation.cited_text,
                source_index=citation.source_index,
                block_start=citation.block_start,
                block_end=citation.block_end,
            )
        )

    return [
        DocumentQuestionOut(
            id=q.id,
            user_id=q.user_id,
            user_name=q.user.full_name if q.user else None,
            portfolio_id=q.portfolio_id,
            text=q.text,
            status=q.status,
            answer_text=q.answer_text,
            provider=q.provider,
            model=q.model,
            latency_ms=q.latency_ms,
            cost_usd=str(q.cost_usd),
            created_at=q.created_at,
            finished_at=q.finished_at,
            citations=citations[q.id],
        )
        for q in questions
    ]
