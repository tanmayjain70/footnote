"""Enumerations stored as text with CHECK constraints.

Not Postgres ENUM types: adding a value to a PG enum inside a transaction is
awkward enough that people avoid doing it properly, and a register that grows
a new document type or review outcome should be a one-line change.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    #: The managing director. Sees every portfolio, including the one being
    #: sold, and is the only role that can see spend and run evaluations.
    DIRECTOR = "director"
    #: Lease administrator. Uploads, runs extraction, confirms values.
    ADMIN = "admin"
    #: Property manager. Asks questions about the portfolios they manage.
    MANAGER = "manager"
    #: Finance. Reads the register and the documents; spends nothing.
    VIEWER = "viewer"


#: Who may spend the model budget, and who may make a value count. The
#: distinction that matters most is the last one: a manager can ask about a
#: break clause, but only an administrator or the director can confirm the
#: extracted date -- see docs/brief.md.
UPLOAD_ROLES = {Role.DIRECTOR, Role.ADMIN}
ASK_ROLES = {Role.DIRECTOR, Role.ADMIN, Role.MANAGER}
REVIEW_ROLES = {Role.DIRECTOR, Role.ADMIN}
EVAL_ROLES = {Role.DIRECTOR}
USAGE_ROLES = {Role.DIRECTOR}

ROLE_LABELS = {
    Role.DIRECTOR: "Managing director",
    Role.ADMIN: "Lease administrator",
    Role.MANAGER: "Property manager",
    Role.VIEWER: "Finance",
}


class DocumentStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class DocumentType(StrEnum):
    LEASE = "lease"
    DEED_OF_VARIATION = "deed_of_variation"
    SIDE_LETTER = "side_letter"
    NOTICE = "notice"
    OTHER = "other"


class JobKind(StrEnum):
    INGEST = "ingest"
    EXTRACT = "extract"
    EVAL_RUN = "eval_run"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class JobStage(StrEnum):
    PARSE = "parse"
    CHUNK = "chunk"
    EMBED = "embed"
    EXTRACT = "extract"
    RUN = "run"


class QuestionStatus(StrEnum):
    ANSWERED = "answered"
    #: The documents did not contain the answer and the system said so.
    #: Deliberately distinct from ``failed``: a refusal is the product
    #: working, not the product breaking.
    UNANSWERED = "unanswered"
    FAILED = "failed"
    BUDGET_EXHAUSTED = "budget_exhausted"


class ReviewStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CORRECTED = "corrected"
    REJECTED = "rejected"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ValueIssue(StrEnum):
    NO_EVIDENCE = "no_evidence"
    INVALID_CHUNK = "invalid_chunk"
    QUOTE_NOT_FOUND = "quote_not_found"
    UNPARSEABLE = "unparseable"


class ExtractionStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class EvalMode(StrEnum):
    RETRIEVAL = "retrieval"
    END_TO_END = "end_to_end"


class EvalRunStatus(StrEnum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class EvalSource(StrEnum):
    GENERATED = "generated"
    FEEDBACK = "feedback"
    MANUAL = "manual"


class UsageKind(StrEnum):
    ANSWER = "answer"
    EXTRACTION = "extraction"
    EVAL = "eval"
    EMBEDDING = "embedding"


class Verdict(StrEnum):
    UP = "up"
    DOWN = "down"


class FeedbackReason(StrEnum):
    WRONG = "wrong"
    MISSING_CITATION = "missing_citation"
    INCOMPLETE = "incomplete"
    SHOULD_HAVE_REFUSED = "should_have_refused"
    OTHER = "other"


def sql_in(members: type[StrEnum]) -> str:
    """Render an enum as the list for a CHECK constraint, e.g. ``'a','b'``."""
    return ",".join(f"'{m.value}'" for m in members)
