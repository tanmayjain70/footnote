"""Uploads in, searchable documents out: the queue and the worker that drains it.

An upload does three things inside the request -- hash the bytes, store them,
queue a job -- and nothing else. Parsing, chunking and embedding happen later
in a worker, because a 14-page lease takes seconds to embed and a browser
should not sit on an open connection while it does.

The queue is a table, and claiming is one ``UPDATE ... FOR UPDATE SKIP
LOCKED``. That is the whole concurrency story: two workers never take the same
job, a worker that dies mid-job leaves a ``running`` row with its name on it,
and the synchronous drain used by tests and the seeder is the same code path
with a loop around it.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import socket
import threading
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from sqlalchemy import String, bindparam, delete, func, select, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.errors import Terminal
from app.models import Chunk, Document, DocumentBlob, DocumentPage, Extraction, Job
from app.models.enums import DocumentStatus, DocumentType, JobKind, JobStage, JobStatus

logger = logging.getLogger(__name__)

#: Chunks embedded per provider call. bge-small is happiest around this size,
#: and it bounds how much work a failure in the embed stage throws away.
EMBED_BATCH = 32


def _now() -> datetime:
    return datetime.now(UTC)


def _worker_id(suffix: str | None = None) -> str:
    """``hostname:pid`` so a job stuck in ``running`` can be traced to the
    process that took it; the drain adds a suffix so a request-driven run is
    distinguishable from the worker thread in the same process."""
    base = f"{socket.gethostname()}:{os.getpid()}"
    return f"{base}:{suffix}" if suffix else base


def _title_from_filename(filename: str) -> str:
    # Browsers send a bare name, but a path pasted into curl on either
    # platform should still give "Lease - Unit 4" and not the whole path.
    name = PureWindowsPath(PurePosixPath(filename).name).name
    stem = name.rsplit(".", 1)[0] if "." in name else name
    # "irwell-bank-house-ground-floor" is how a file is named, not how a lease
    # is referred to. Joining hyphens and underscores become spaces, and words
    # that arrive all in lower case are capitalised; anything with its own
    # capitals or digits ("Unit 4B", "HP-CC-0007") is left as typed.
    words = re.sub(r"(?<=\w)[-_]+(?=\w)", " ", stem).split()
    title = " ".join(word.capitalize() if word.islower() else word for word in words)
    return (title.strip() or name or "Untitled")[:255]


# ============================================================== uploads ===


def create_document(
    db: Session,
    *,
    portfolio_id: uuid.UUID,
    uploaded_by: uuid.UUID | None,
    filename: str,
    title: str | None,
    doc_type: str | None,
    data: bytes,
    metadata: dict[str, Any] | None = None,
) -> tuple[Document, bool]:
    """Store an upload and queue its ingestion. Returns ``(document, created)``.

    The same bytes uploaded twice into one portfolio are one document, and
    the caller is told so rather than given an error: a lease administrator
    re-uploading a file they were not sure went through is the normal case,
    not a mistake to be corrected.
    """
    digest = hashlib.sha256(data).hexdigest()

    existing = _find_by_digest(db, portfolio_id, digest)
    if existing is not None:
        # The same bytes, but the last attempt at them failed. Uploading the
        # file again is how somebody asks for another go; answering "already
        # have it" would leave them holding a document that does not work.
        if existing.status == DocumentStatus.FAILED:
            reingest(db, existing)
            db.refresh(existing)
        return existing, False

    document = Document(
        portfolio_id=portfolio_id,
        title=(title or "").strip()[:255] or _title_from_filename(filename),
        filename=filename[:255],
        doc_type=doc_type or DocumentType.LEASE,
        content_sha256=digest,
        byte_size=len(data),
        status=DocumentStatus.QUEUED,
        uploaded_by=uploaded_by,
        metadata_=metadata or {},
    )
    try:
        # The savepoint is opened before the row is added so that a unique
        # violation -- two people uploading the same file at the same moment
        # -- is contained instead of poisoning the whole transaction.
        with db.begin_nested():
            db.add(document)
            db.flush()
    except IntegrityError:
        if document in db:
            db.expunge(document)
        db.rollback()
        winner = _find_by_digest(db, portfolio_id, digest)
        if winner is None:  # pragma: no cover - the violation says it exists
            raise
        return winner, False

    db.add(DocumentBlob(document_id=document.id, data=data))
    db.add(Job(kind=JobKind.INGEST, document_id=document.id))
    db.commit()
    db.refresh(document)
    return document, True


def _find_by_digest(db: Session, portfolio_id: uuid.UUID, digest: str) -> Document | None:
    return db.execute(
        select(Document).where(
            Document.portfolio_id == portfolio_id, Document.content_sha256 == digest
        )
    ).scalar_one_or_none()


def reingest(db: Session, document: Document) -> Job:
    """Throw away everything derived from the bytes and queue them again.

    Used when the chunking or the embedding model changes. Old citations
    pointed into chunks whose boundaries no longer exist, so they go too --
    a citation that cannot be mapped back to text is worse than none.
    """
    db.execute(delete(Chunk).where(Chunk.document_id == document.id))
    db.execute(delete(DocumentPage).where(DocumentPage.document_id == document.id))
    db.execute(delete(Extraction).where(Extraction.document_id == document.id))
    # An extraction still waiting in the queue would run against rows that
    # no longer exist and fail three times; it is withdrawn with them.
    db.execute(
        delete(Job).where(
            Job.document_id == document.id,
            Job.kind == JobKind.EXTRACT,
            Job.status == JobStatus.QUEUED,
        )
    )

    document.page_count = 0
    document.chunk_count = 0
    document.status = DocumentStatus.QUEUED
    document.error = None

    # A job already waiting -- or one a worker has in hand right now -- will
    # pick up the reset document. A second job would parse the same bytes
    # twice and the two would race over the same chunk rows.
    job = db.execute(
        select(Job)
        .where(
            Job.document_id == document.id,
            Job.kind == JobKind.INGEST,
            Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
        )
        .order_by(Job.created_at.desc())
        .limit(1)
    ).scalars().first()
    if job is None:
        job = Job(kind=JobKind.INGEST, document_id=document.id)
        db.add(job)
    db.commit()
    db.refresh(document)
    return job


def delete_document(db: Session, document: Document) -> None:
    """Pages, chunks, the blob, jobs, extractions and citations all hang off
    the document with ``ON DELETE CASCADE``; one row goes and the database
    takes the rest."""
    db.delete(document)
    db.commit()


# ============================================================== the queue ===

_CLAIM_SQL = """
UPDATE jobs
   SET status = 'running',
       claimed_at = now(),
       claimed_by = :worker_id,
       attempts = attempts + 1,
       started_at = now(),
       updated_at = now()
 WHERE id = (
        SELECT id
          FROM jobs
         WHERE status = 'queued'{kind_filter}
         ORDER BY created_at, id
         LIMIT 1
           FOR UPDATE SKIP LOCKED
       )
RETURNING id
"""


def claim_next_job(
    db: Session, worker_id: str, kinds: Sequence[str] | None = None
) -> Job | None:
    """Take the oldest queued job, or ``None``. One statement, committed.

    ``SKIP LOCKED`` is what makes two workers safe: each sees only the rows
    the other has not already locked, so a job is claimed exactly once
    without any coordination outside the database.
    """
    kind_values = [str(k) for k in kinds] if kinds else None
    kind_filter = " AND kind = ANY(:kinds)" if kind_values else ""
    statement = text(_CLAIM_SQL.format(kind_filter=kind_filter))
    params: dict[str, Any] = {"worker_id": worker_id[:128]}
    if kind_values:
        statement = statement.bindparams(bindparam("kinds", type_=ARRAY(String)))
        params["kinds"] = kind_values

    job_id = db.execute(statement, params).scalar_one_or_none()
    db.commit()
    if job_id is None:
        return None
    # populate_existing: the session may already hold this job from before it
    # was claimed, and a stale ``queued`` copy is exactly the wrong thing to
    # hand back.
    return db.execute(
        select(Job).where(Job.id == job_id).execution_options(populate_existing=True)
    ).scalar_one()


def requeue_stranded_jobs(db: Session, *, older_than_minutes: int = 30) -> int:
    """Return jobs whose worker never came back to the queue.

    A process killed mid-job leaves the row ``running`` and its document
    ``processing`` for ever: claiming is what marks work as taken, and a dead
    worker releases nothing. Anything claimed longer ago than a real job takes
    is assumed orphaned and offered again. Attempts are not spent -- the job
    never got its turn.
    """
    cutoff = _now() - timedelta(minutes=older_than_minutes)
    stranded = list(
        db.execute(
            select(Job).where(Job.status == JobStatus.RUNNING, Job.claimed_at < cutoff)
        ).scalars()
    )
    for job in stranded:
        job.status = JobStatus.QUEUED
        job.claimed_by = None
        job.claimed_at = None
        job.error = "The worker running this job stopped before it finished."
        if job.document_id is not None:
            document = db.get(Document, job.document_id)
            if document is not None and document.status == DocumentStatus.PROCESSING:
                document.status = DocumentStatus.QUEUED
    if stranded:
        db.commit()
        logger.warning("requeued %d stranded job(s)", len(stranded))
    return len(stranded)


def run_job(db: Session, job: Job) -> None:
    """Run one claimed job and record how it went.

    The outcome bookkeeping is here rather than in each handler so that every
    kind is retried and fails the same way: an exception counts one attempt;
    the job goes back to ``queued`` until ``max_attempts`` is reached, then
    ``failed`` with the message kept for the health screen.
    """
    try:
        if job.kind == JobKind.INGEST:
            run_ingest_job(db, job)
        elif job.kind == JobKind.EXTRACT:
            _extraction_service().run_extract_job(db, job)
        elif job.kind == JobKind.EVAL_RUN:
            _evals_service().run_eval_job(db, job)
        else:
            raise RuntimeError(f"Unknown job kind {job.kind!r}.")
    except Exception as exc:
        db.rollback()
        db.refresh(job)
        # ``Terminal`` means the handler knows a retry cannot help -- the
        # daily budget is gone, the payload names nothing. Retrying it would
        # spend attempts to reach the same place.
        final = job.attempts >= job.max_attempts or isinstance(exc, Terminal)
        job.error = _error_text(exc)
        job.status = JobStatus.FAILED if final else JobStatus.QUEUED
        job.finished_at = _now() if final else None
        db.commit()
        logger.warning(
            "job %s (%s) attempt %d/%d failed%s: %s",
            job.id,
            job.kind,
            job.attempts,
            job.max_attempts,
            "" if final else ", requeued",
            job.error,
        )
        return

    if job.status == JobStatus.RUNNING:
        job.status = JobStatus.DONE
    if job.finished_at is None:
        job.finished_at = _now()
    job.error = None
    db.commit()


def _error_text(exc: BaseException) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    return message[:2000]


def _extraction_service() -> Any:
    # Imported at call time: the service imports this module for the queue,
    # and a missing module should fail one job with a readable message, not
    # stop the API from starting.
    try:
        from app.services import extraction
    except ImportError as exc:
        raise RuntimeError(
            "The extraction service (app.services.extraction) is not available."
        ) from exc
    return extraction


def _evals_service() -> Any:
    try:
        from app.services import evals
    except ImportError as exc:
        raise RuntimeError(
            "The evaluation service (app.services.evals) is not available."
        ) from exc
    return evals


def process_pending(
    db: Session, *, kinds: Sequence[str] | None = None, limit: int | None = None
) -> int:
    """Claim and run queued jobs one after another until none are left (or
    ``limit`` is reached). What the worker thread does, without the thread:
    tests, the seeder and ``POST /jobs/drain`` call this."""
    worker_id = _worker_id("drain")
    processed = 0
    while limit is None or processed < limit:
        job = claim_next_job(db, worker_id, kinds)
        if job is None:
            break
        run_job(db, job)
        processed += 1
    return processed


def job_counts(db: Session) -> dict[str, int]:
    counts = dict(db.execute(select(Job.status, func.count()).group_by(Job.status)).all())
    return {status.value: counts.get(status.value, 0) for status in JobStatus}


def document_counts(db: Session) -> dict[str, int]:
    counts = dict(
        db.execute(select(Document.status, func.count()).group_by(Document.status)).all()
    )
    return {status.value: counts.get(status.value, 0) for status in DocumentStatus}


# ============================================================== ingest ===


def run_ingest_job(db: Session, job: Job) -> None:
    """Parse, chunk and embed one document. Stages are written to the job as
    it goes, so a failure says where it happened."""
    document = db.get(Document, job.document_id) if job.document_id else None
    if document is None:
        raise RuntimeError("The document this job was queued for no longer exists.")

    try:
        _ingest(db, job, document)
    except Exception as exc:
        db.rollback()
        db.refresh(job)
        db.refresh(document)
        # Only the last attempt fails the document; until then it goes back
        # to the queue with the error visible, so the screen says "retrying"
        # rather than "broken" for a transient embedding failure.
        final = job.attempts >= job.max_attempts
        document.status = DocumentStatus.FAILED if final else DocumentStatus.QUEUED
        document.error = _error_text(exc)
        db.commit()
        raise


def _ingest(db: Session, job: Job, document: Document) -> None:
    # Imported where used: the parser and chunker are part of this package,
    # but a broken one should fail the job it is running, not the import of
    # the module the health screen reads the worker from.
    from app.services import chunking, pdf

    document.status = DocumentStatus.PROCESSING
    document.error = None
    job.stage = JobStage.PARSE
    db.commit()

    blob = db.get(DocumentBlob, document.id)
    if blob is None:
        raise RuntimeError("The uploaded bytes for this document are missing.")
    pages = pdf.extract_pages(blob.data)

    # A retry after a failure part-way through must not leave two copies of
    # page 3 behind; the derived rows are rebuilt from scratch every time.
    db.execute(delete(Chunk).where(Chunk.document_id == document.id))
    db.execute(delete(DocumentPage).where(DocumentPage.document_id == document.id))
    db.add_all(
        DocumentPage(document_id=document.id, page_number=number, text=page_text)
        for number, page_text in enumerate(pages, start=1)
    )
    db.flush()

    job.stage = JobStage.CHUNK
    chunks = [
        Chunk(
            document_id=document.id,
            page_number=piece.page_number,
            ordinal=ordinal,
            text=piece.text,
            char_start=piece.char_start,
            char_end=piece.char_end,
            token_estimate=piece.token_estimate,
        )
        for ordinal, piece in enumerate(chunking.chunk_document(pages))
    ]
    db.add_all(chunks)
    db.commit()

    job.stage = JobStage.EMBED
    db.commit()
    from app.providers import embeddings

    provider = embeddings.get_embedding_provider()
    for start in range(0, len(chunks), EMBED_BATCH):
        batch = chunks[start : start + EMBED_BATCH]
        vectors = provider.embed_documents([chunk.text for chunk in batch])
        for chunk, vector in zip(batch, vectors, strict=True):
            chunk.embedding = vector
        db.flush()

    document.page_count = len(pages)
    document.chunk_count = len(chunks)
    document.status = DocumentStatus.READY
    document.error = None
    job.status = JobStatus.DONE
    job.finished_at = _now()
    db.commit()
    logger.info(
        "ingested %s: %d pages, %d chunks (%s)",
        document.id,
        len(pages),
        len(chunks),
        provider.model,
    )


# ============================================================== the worker ===


class Worker:
    """The in-process background thread.

    A thread rather than a separate process because the demo runs in one
    container on a free tier, and the claim statement already makes a second
    process safe the day one is wanted. Every job gets a fresh session; a
    session that has seen an exception is not one to keep using.
    """

    def __init__(self) -> None:
        self.worker_id = _worker_id()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.alive:
            return
        self._stop.clear()
        # A Thread can be started once, so each start gets a new one; the
        # worker object itself is the module-level singleton /health reads.
        self._thread = threading.Thread(target=self._run, name="footnote-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def _run(self) -> None:
        poll = get_settings().worker_poll_seconds
        while not self._stop.is_set():
            db = SessionLocal()
            try:
                job = claim_next_job(db, self.worker_id)
                if job is None:
                    self._stop.wait(poll)
                    continue
                run_job(db, job)
            except Exception:
                # Nothing that happens to one job may take the thread down;
                # the job's own error is recorded by run_job, this is for the
                # database going away underneath us.
                logger.exception("worker loop error")
                self._stop.wait(poll)
            finally:
                db.close()


worker = Worker()


def start_worker() -> None:
    worker.start()


def stop_worker() -> None:
    worker.stop(timeout=10)


def worker_alive() -> bool:
    return worker.alive
