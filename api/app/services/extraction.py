"""Extraction: seventeen key terms per lease, each with evidence, each reviewed.

The model is asked for a value, a verbatim quote and the chunk it came from.
None of it is trusted on its own. The chunk must belong to the document, the
quote must be found in that chunk, and the value must parse as the type the
registry declares. Whatever fails is stored with an ``issue`` and left for a
person -- never dropped, because a reviewer who can see that the model pointed
at the wrong chunk fixes it in seconds, and one who sees a blank cannot.

A value counts in the register once somebody has confirmed or corrected it.
That is the rule of ``register_rows``, not of the screen that renders it, so
the CSV export and any future caller get the same answer.
"""

from __future__ import annotations

import csv
import io
import logging
import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core.errors import HTTP_422, AppError, Conflict
from app.models import Chunk, Document, ExtractedValue, Extraction, Job, Portfolio, User
from app.models.enums import (
    Confidence,
    DocumentStatus,
    ExtractionStatus,
    JobKind,
    JobStage,
    JobStatus,
    ReviewStatus,
    UsageKind,
    ValueIssue,
)
from app.providers import llm, pricing
from app.providers.llm import ExtractedField, Source
from app.services import usage
from app.services.chunking import split_sentences
from app.services.extraction_fields import FIELDS, SCHEMA_VERSION, FieldSpec, parse_value

logger = logging.getLogger(__name__)

FIELD_BY_KEY: dict[str, FieldSpec] = {spec.key: spec for spec in FIELDS}

#: The columns the register shows, in order. The other six terms (landlord,
#: permitted use, deposit, guarantor, VAT, term length) are on the document
#: screen; the register answers the two quarterly questions -- what expires
#: when, and where the breaks are -- and a table wider than that is a
#: spreadsheet nobody reads.
REGISTER_FIELDS = (
    "tenant_name",
    "unit",
    "property_address",
    "term_start",
    "term_end",
    "annual_rent_gbp",
    "rent_review_basis",
    "rent_review_date",
    "break_date",
    "break_notice_months",
    "repairing_obligation",
)

#: A value counts once one of these has been said about it.
REVIEWED = {ReviewStatus.CONFIRMED, ReviewStatus.CORRECTED}
CONFIDENCES = {c.value for c in Confidence}

#: ``value_text`` is a String(512); a model that answers with a paragraph
#: should produce a review, not a database error.
VALUE_TEXT_MAX = 512


class InvalidReview(AppError):
    """The review request cannot be applied as asked -- a correction that does
    not parse, or an action that needs a value and has none."""

    status_code = HTTP_422
    code = "invalid_review"


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------- request --


def request_extraction(db: Session, document: Document, user: User) -> Extraction:
    """Queue an extraction of ``document`` and return the pending row.

    One at a time per document: a second request while one is queued or
    running is a conflict, not a second job, because both would produce a
    full set of values and the reviewer would be shown the same lease twice.
    A finished or failed extraction can be run again.
    """
    # Extraction bills like any other model call, so it asks the same
    # question first. Checked again when the job runs: a job queued inside
    # the budget may not start inside it.
    usage.check_budget(db)

    if document.status != DocumentStatus.READY:
        raise Conflict(
            "This document is not ready to extract from yet.",
            detail={"status": document.status},
            code="document_not_ready",
        )
    active = db.execute(
        select(Extraction.id).where(
            Extraction.document_id == document.id,
            Extraction.status.in_([ExtractionStatus.QUEUED, ExtractionStatus.RUNNING]),
        )
    ).first()
    if active is not None:
        raise Conflict(
            "An extraction is already queued or running for this document.",
            detail={"extraction_id": str(active[0])},
            code="extraction_in_progress",
        )

    provider = llm.get_llm_provider()
    extraction = Extraction(
        document_id=document.id,
        status=ExtractionStatus.QUEUED,
        provider=provider.name,
        model=provider.model,
        schema_version=SCHEMA_VERSION,
        created_by=user.id,
    )
    db.add(extraction)
    db.flush()
    db.add(
        Job(
            kind=JobKind.EXTRACT,
            status=JobStatus.QUEUED,
            document_id=document.id,
            payload={"extraction_id": str(extraction.id)},
        )
    )
    db.commit()
    db.refresh(extraction)
    return extraction


# -------------------------------------------------------------------- run --


def run_extract_job(db: Session, job: Job) -> None:
    """Run one queued extraction. Called by the worker with a claimed job.

    The job's own bookkeeping -- attempts, requeue, final failure -- belongs
    to ``ingestion.run_job``, so on error this records the extraction's state
    and re-raises. Until the last attempt the extraction goes back to
    ``queued`` with the error kept, so the document screen can say why it is
    waiting; on the last it is ``failed`` and a person can ask again.
    """
    extraction = _extraction_for(db, job)
    if extraction is None:
        raise RuntimeError("The job names no extraction that exists.")

    job.stage = JobStage.EXTRACT
    extraction.status = ExtractionStatus.RUNNING
    extraction.started_at = _now()
    # Committed before the model is called so the document screen shows
    # "running" for the seconds or minutes it takes.
    db.commit()

    try:
        usage.check_budget(db)

        document = db.get(Document, extraction.document_id)
        if document is None:
            raise RuntimeError("The document was deleted while its extraction was queued.")

        chunks = list(
            db.execute(
                select(Chunk).where(Chunk.document_id == document.id).order_by(Chunk.ordinal)
            ).scalars()
        )
        sources = [
            Source(
                index=i,
                chunk_id=chunk.id,
                document_id=chunk.document_id,
                document_title=document.title,
                page_number=chunk.page_number,
                blocks=split_sentences(chunk.text),
            )
            for i, chunk in enumerate(chunks)
        ]

        provider = llm.get_llm_provider()
        output = provider.extract(document.title, FIELDS, sources)

        # The first answer for a key wins. The schema asks for one per field;
        # a model that repeats itself gets the benefit of its first thought.
        by_key: dict[str, ExtractedField] = {}
        for extracted in output.fields:
            by_key.setdefault(extracted.key, extracted)
        chunk_by_id = {chunk.id: chunk for chunk in chunks}

        for spec in FIELDS:
            db.add(_build_value(extraction, spec, by_key.get(spec.key), chunk_by_id))

        model = output.model or provider.model
        _record_usage(db, extraction, provider.name, model, output.usage)
        extraction.input_tokens = output.usage.input_tokens
        extraction.output_tokens = output.usage.output_tokens
        extraction.cost_usd = pricing.cost_usd(model, output.usage)
        extraction.status = ExtractionStatus.DONE
        extraction.finished_at = _now()
        extraction.error = None

        job.status = JobStatus.DONE
        job.finished_at = _now()
        job.error = None
        db.commit()
    except Exception as exc:
        db.rollback()
        final = job.attempts >= job.max_attempts
        extraction.status = ExtractionStatus.FAILED if final else ExtractionStatus.QUEUED
        extraction.error = _error_text(exc)
        extraction.finished_at = _now() if final else None
        db.commit()
        logger.warning(
            "extraction %s %s: %s", extraction.id, "failed" if final else "requeued", exc
        )
        raise


def _extraction_for(db: Session, job: Job) -> Extraction | None:
    try:
        extraction_id = uuid.UUID(str((job.payload or {}).get("extraction_id")))
    except ValueError:
        return None
    return db.get(Extraction, extraction_id)


def _error_text(exc: BaseException) -> str:
    message = str(exc).strip() or type(exc).__name__
    return message[:2000]


def _record_usage(
    db: Session, extraction: Extraction, provider: str, model: str, usage: Any
) -> None:
    # Imported here rather than at the top: the usage service is the metering
    # layer the answering path is built on, and this module is imported by
    # the worker at startup before every service is needed.
    from app.services import usage as usage_service

    usage_service.record(
        db,
        user_id=extraction.created_by,
        kind=UsageKind.EXTRACTION,
        provider=provider,
        model=model,
        usage=usage,
        reference_id=extraction.id,
    )


def _build_value(
    extraction: Extraction,
    spec: FieldSpec,
    extracted: ExtractedField | None,
    chunk_by_id: dict[uuid.UUID, Chunk],
) -> ExtractedValue:
    """Check the model's evidence for one field and build the row.

    One ``issue`` per value, the first that applies:

    - ``no_evidence``: no value, or a value with no chunk to look in.
    - ``invalid_chunk``: the chunk named is not in this document. The chunk
      id is dropped -- a foreign chunk must never be stored as evidence --
      but the value and quote are kept for the reviewer.
    - ``quote_not_found``: the quote is not in the chunk, compared without
      regard to case or spacing. Value and chunk are kept; the page is still
      the right place to look.
    - ``unparseable``: the value is not a valid date, sum, number, yes/no or
      registry option. ``value_text`` keeps what the model wrote so the
      reviewer can read it and type the correction.
    """
    value_text = _clean(extracted.value_text if extracted else None)
    if value_text and len(value_text) > VALUE_TEXT_MAX:
        value_text = value_text[:VALUE_TEXT_MAX]
    quote = _clean(extracted.quote if extracted else None)
    confidence = (
        extracted.confidence if extracted and extracted.confidence in CONFIDENCES else "low"
    )

    issue: str | None = None
    chunk: Chunk | None = None
    chunk_ref = _clean(extracted.chunk_id if extracted else None)
    if chunk_ref:
        chunk = chunk_by_id.get(_as_uuid(chunk_ref))
        if chunk is None:
            issue = ValueIssue.INVALID_CHUNK

    if value_text is None:
        issue = ValueIssue.NO_EVIDENCE
    elif chunk is None:
        issue = issue or ValueIssue.NO_EVIDENCE
    elif quote and not _contains(chunk.text, quote):
        issue = ValueIssue.QUOTE_NOT_FOUND

    value_json: Any = None
    if value_text is not None:
        value_json, parse_issue = parse_value(spec, value_text)
        if parse_issue is not None:
            value_json = None
            issue = issue or ValueIssue.UNPARSEABLE

    return ExtractedValue(
        extraction_id=extraction.id,
        document_id=extraction.document_id,
        field_key=spec.key,
        value_json=value_json,
        value_text=value_text,
        quote=quote,
        chunk_id=chunk.id if chunk is not None else None,
        page_number=chunk.page_number if chunk is not None else None,
        confidence=confidence,
        issue=issue,
        review_status=ReviewStatus.PENDING,
    )


def _clean(text: str | None) -> str | None:
    if text is None:
        return None
    text = text.strip()
    return text or None


def _as_uuid(text: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(text)
    except ValueError:
        return None


def _fold(text: str) -> str:
    """Lower-case with whitespace runs collapsed, so a quote that differs from
    the chunk only in line breaks or capitals still counts as found."""
    return " ".join(text.split()).lower()


def _contains(haystack: str, needle: str) -> bool:
    return _fold(needle.strip("\"'“”‘’")) in _fold(haystack)


# ----------------------------------------------------------------- review --


def review(
    db: Session,
    value: ExtractedValue,
    user: User,
    *,
    action: str,
    corrected_text: str | None = None,
    note: str | None = None,
) -> ExtractedValue:
    """Confirm, correct or reject one value.

    The model's half of the row is never touched. A correction is parsed
    with the same rules as the model's value and stored beside it, so a
    corrected value still shows what the model read and how the person
    disagreed. Reviewing again replaces the earlier decision.
    """
    if action == "confirm":
        value.review_status = ReviewStatus.CONFIRMED
        value.corrected_json = None
    elif action == "correct":
        value.corrected_json = _parse_correction(value.field_key, corrected_text)
        value.review_status = ReviewStatus.CORRECTED
    elif action == "reject":
        value.review_status = ReviewStatus.REJECTED
        value.corrected_json = None
    else:
        raise InvalidReview(f"Unknown review action {action!r}.")

    value.reviewed_by = user.id
    value.reviewed_at = _now()
    value.review_note = _clean(note)
    db.commit()
    db.refresh(value)
    return value


def _parse_correction(field_key: str, corrected_text: str | None) -> Any:
    text = _clean(corrected_text)
    if text is None:
        raise InvalidReview("A correction needs a value. To remove the value, reject it instead.")
    spec = FIELD_BY_KEY.get(field_key)
    if spec is None:
        raise InvalidReview(
            f"The field {field_key!r} is no longer in the registry; re-run the extraction.",
            detail={"field_key": field_key},
        )
    parsed, issue = parse_value(spec, text)
    if issue is not None or parsed is None:
        raise InvalidReview(
            f"{text!r} is not a valid {spec.type} for {spec.label}.",
            detail={
                "field_key": spec.key,
                "type": spec.type,
                "enum_values": list(spec.enum_values),
                "issue": issue,
            },
        )
    return parsed


# ------------------------------------------------------------------- read --


def latest_extraction(db: Session, document: Document) -> Extraction | None:
    """The most recent extraction of any status, values and reviewers loaded."""
    return db.execute(
        select(Extraction)
        .where(Extraction.document_id == document.id)
        .options(selectinload(Extraction.values).selectinload(ExtractedValue.reviewer))
        .order_by(Extraction.created_at.desc(), Extraction.id)
        .limit(1)
    ).scalar_one_or_none()


def effective_value(value: ExtractedValue) -> Any:
    """The correction when there is one, else what the model extracted."""
    return value.corrected_json if value.corrected_json is not None else value.value_json


def _latest_done(
    db: Session, *, visible_ids: list[uuid.UUID], portfolio_id: uuid.UUID | None
) -> list[tuple[Document, Portfolio, uuid.UUID]]:
    """Ready, visible documents with their newest finished extraction.

    Older extractions keep their rows for the audit trail, but only the
    newest one is the register's view of a lease.
    """
    if not visible_ids:
        return []
    latest = (
        select(Extraction.id, Extraction.document_id)
        .where(Extraction.status == ExtractionStatus.DONE)
        .distinct(Extraction.document_id)
        .order_by(Extraction.document_id, Extraction.created_at.desc(), Extraction.id)
        .subquery()
    )
    query = (
        select(Document, Portfolio, latest.c.id)
        .join(Portfolio, Portfolio.id == Document.portfolio_id)
        .join(latest, latest.c.document_id == Document.id)
        .where(Document.status == DocumentStatus.READY, Document.portfolio_id.in_(visible_ids))
        .order_by(Portfolio.name, Document.title, Document.id)
    )
    if portfolio_id is not None:
        query = query.where(Document.portfolio_id == portfolio_id)
    return [tuple(row) for row in db.execute(query)]


def _values_by_extraction(
    db: Session, extraction_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[ExtractedValue]]:
    grouped: dict[uuid.UUID, list[ExtractedValue]] = {eid: [] for eid in extraction_ids}
    if not extraction_ids:
        return grouped
    for value in db.execute(
        select(ExtractedValue).where(ExtractedValue.extraction_id.in_(extraction_ids))
    ).scalars():
        grouped[value.extraction_id].append(value)
    return grouped


def _has_value(value: ExtractedValue) -> bool:
    return value.value_json is not None or bool(value.value_text)


def _shown(value: ExtractedValue, include_unreviewed: bool) -> Any:
    """What the register prints for a value.

    Confirmed and corrected values always. Pending ones only when asked for,
    and rejected ones never -- a person has said that value is wrong.
    """
    if value.review_status in REVIEWED:
        return effective_value(value)
    if include_unreviewed and value.review_status == ReviewStatus.PENDING:
        return value.value_json
    return None


def register_rows(
    db: Session,
    *,
    visible_ids: list[uuid.UUID],
    portfolio_id: uuid.UUID | None = None,
    expiring_before: date | None = None,
    has_break: bool | None = None,
    include_unreviewed: bool = False,
) -> list[dict[str, Any]]:
    """One row per ready, visible document with a finished extraction.

    The filters run over the values as shown, so with the default view an
    unconfirmed break date does not put a lease in the "has a break" list --
    which is the point of confirming it.
    """
    documents = _latest_done(db, visible_ids=visible_ids, portfolio_id=portfolio_id)
    values = _values_by_extraction(db, [eid for _, _, eid in documents])

    rows: list[dict[str, Any]] = []
    for document, portfolio, extraction_id in documents:
        extracted = values[extraction_id]
        by_key = {value.field_key: value for value in extracted}
        row: dict[str, Any] = {
            "document_id": document.id,
            "portfolio_id": portfolio.id,
            "title": document.title,
            "portfolio": portfolio.name,
        }
        for key in REGISTER_FIELDS:
            value = by_key.get(key)
            row[key] = _shown(value, include_unreviewed) if value is not None else None

        counts = {status.value: 0 for status in ReviewStatus}
        for value in extracted:
            counts[value.review_status] += 1
        row["review"] = counts
        row["complete"] = not any(
            value.review_status == ReviewStatus.PENDING and _has_value(value)
            for value in extracted
        )

        if expiring_before is not None and not _expires_before(row["term_end"], expiring_before):
            continue
        if has_break is not None and (row["break_date"] is not None) != has_break:
            continue
        rows.append(row)
    return rows


def _expires_before(term_end: Any, cutoff: date) -> bool:
    if not isinstance(term_end, str):
        return False
    try:
        return date.fromisoformat(term_end) < cutoff
    except ValueError:
        return False


def register_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "documents": len(rows),
        "complete": sum(1 for row in rows if row["complete"]),
        "pending_values": sum(row["review"]["pending"] for row in rows),
    }


CSV_COLUMNS = (
    "document_id",
    "title",
    "portfolio",
    *REGISTER_FIELDS,
    "confirmed",
    "corrected",
    "pending",
    "rejected",
    "complete",
)


def register_csv(rows: list[dict[str, Any]]) -> str:
    """The register as a spreadsheet: a header row and one line per document.

    Same rows as the JSON, so what is missing from the screen is missing
    from the export too.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        review = row["review"]
        writer.writerow(
            [
                str(row["document_id"]),
                row["title"],
                row["portfolio"],
                *(_csv_cell(key, row[key]) for key in REGISTER_FIELDS),
                review["confirmed"],
                review["corrected"],
                review["pending"],
                review["rejected"],
                "yes" if row["complete"] else "no",
            ]
        )
    return buffer.getvalue()


#: Characters that make a spreadsheet treat a cell as a formula rather than
#: as text. Values here come from lease text by way of a model, and from
#: reviewers' corrections; this export is opened by a landlord.
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r", "\n")


def _csv_cell(key: str, value: Any) -> str:
    if value is None:
        return ""
    if key == "annual_rent_gbp" and isinstance(value, int | float):
        return f"{value:.2f}"
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = str(value)
    # A leading apostrophe is how every spreadsheet is told "this is text".
    return f"'{text}" if text.startswith(_FORMULA_START) else text


def review_queue(
    db: Session, *, visible_ids: list[uuid.UUID], portfolio_id: uuid.UUID | None = None
) -> list[dict[str, Any]]:
    """Documents with values still waiting on a person, most pending first.

    Counts come from the newest finished extraction only, like the register.
    ``issues`` is the number of those whose evidence did not check out --
    the ones worth opening first.
    """
    documents = _latest_done(db, visible_ids=visible_ids, portfolio_id=portfolio_id)
    if not documents:
        return []
    extraction_ids = [eid for _, _, eid in documents]
    counts = {
        extraction_id: (pending, issues)
        for extraction_id, pending, issues in db.execute(
            select(
                ExtractedValue.extraction_id,
                func.count(),
                func.count().filter(ExtractedValue.issue.is_not(None)),
            )
            .where(
                ExtractedValue.extraction_id.in_(extraction_ids),
                ExtractedValue.review_status == ReviewStatus.PENDING,
            )
            .group_by(ExtractedValue.extraction_id)
        )
    }
    queue = [
        {
            "document_id": document.id,
            "portfolio_id": portfolio.id,
            "title": document.title,
            "portfolio_name": portfolio.name,
            "pending": counts[extraction_id][0],
            "issues": counts[extraction_id][1],
        }
        for document, portfolio, extraction_id in documents
        if extraction_id in counts
    ]
    queue.sort(key=lambda item: (-item["issues"], -item["pending"], item["title"]))
    return queue
