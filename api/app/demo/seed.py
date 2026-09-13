"""Seed the demo: Hallam & Pryce's leases, extracted, part-reviewed and asked about.

Run with ``python -m app.demo.seed`` from ``api/``. The API does not need to be
running; when it is, its worker thread and this process share one job queue
and whichever claims a job first runs it.

What the seed builds is the firm six weeks into using the product, not on its
first morning. The leases went in over those weeks in batches; the lease
administrator has confirmed the extracted terms on some of them, corrected a
few and not reached the rest; the managers have asked a fortnight's worth of
questions and left the odd thumbs-up or thumbs-down; the director has run one
evaluation. The timestamps say so, with enough jitter that no two review
events share a second -- generated data gives itself away in a screenshot when
every review landed at 12:00:00.

Idempotent: it does nothing when documents already exist, and ``--force``
clears everything except the people and the portfolios and builds it again.
"""

from __future__ import annotations

import argparse
import logging
import math
import random
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.core.security import hash_password
from app.demo import leases
from app.deps import visible_portfolio_ids
from app.models import (
    Document,
    EvalQuestion,
    EvalRun,
    ExtractedValue,
    Extraction,
    Feedback,
    Job,
    Portfolio,
    PortfolioMember,
    Question,
    User,
)
from app.models.enums import (
    DocumentStatus,
    DocumentType,
    EvalMode,
    EvalSource,
    ExtractionStatus,
    FeedbackReason,
    JobKind,
    JobStatus,
    ReviewStatus,
    Role,
    Verdict,
)
from app.services import answering, evals, extraction, ingestion
from app.services.extraction_fields import FIELDS
from app.services.pdf import extract_pages

log = logging.getLogger("seed")

DEMO_PASSWORD = "demo-password"
DEMO_DOMAIN = "hallampryce.demo"

#: (mailbox, full name, role, portfolios). The director is a member of nothing
#: and sees everything; Riverside is the confidential one, visible to the
#: director, the administrator and the manager who is selling it.
USERS: list[tuple[str, str, Role, tuple[str, ...]]] = [
    ("director", "Priya Hallam", Role.DIRECTOR, ()),
    ("admin", "Owen Pryce-Reid", Role.ADMIN, ("City Centre", "Northern Estates", "Riverside")),
    ("manager", "Sadia Mahmood", Role.MANAGER, ("City Centre", "Northern Estates")),
    ("riverside", "Tom Whitlock", Role.MANAGER, ("Riverside",)),
    ("finance", "Grace Adeyemi", Role.VIEWER, ("City Centre", "Northern Estates")),
]

PORTFOLIO_DESCRIPTIONS = {
    "City Centre": "Offices and ground-floor retail in and around the city centre.",
    "Northern Estates": "Industrial and trade units on the estates north and west of the city.",
    "Riverside": "The waterside holdings being marketed for sale; confidential until exchange.",
}

#: How far the administrator has got, per portfolio: documents confirmed in
#: full, documents with a correction on them, then the rest left pending. The
#: counts are for the full 48; a smaller run takes the same shape as far as it
#: goes.
REVIEW_PLAN: dict[str, tuple[int, int]] = {
    "City Centre": (14, 2),
    "Northern Estates": (10, 0),
    "Riverside": (0, 0),
}
#: Which values are corrected on the corrected documents: rent on both, the
#: deposit (a multiple of the rent) on the first. Three corrections in all.
CORRECTIONS: tuple[tuple[str, ...], ...] = (
    ("annual_rent_gbp", "deposit_gbp"),
    ("annual_rent_gbp",),
)
CORRECTION_NOTE = "Rent stated inclusive of service charge; corrected to the net figure"

#: The uploads are spread between these two points in the past. The newer
#: bound leaves room for a review a few working days after the last upload
#: to still be in the past.
HISTORY_DAYS = 42
HISTORY_END_DAYS = 8
#: The historical questions are spread over this many days.
QUESTION_DAYS = 14
#: Who asked the seeded questions, in order. The eighth is the director's
#: unanswerable one.
ASKERS: tuple[str, ...] = (
    "manager",
    "admin",
    "director",
    "manager",
    "admin",
    "director",
    "manager",
)
FEEDBACK_NOTE = "Answers half the question; the notice period is missing."

#: How long to wait for a queue that another process may be draining too.
DRAIN_TIMEOUT_SECONDS = 1800

FIELD_ORDER = {spec.key: index for index, spec in enumerate(FIELDS)}


# ------------------------------------------------------------- plumbing --


@contextmanager
def _step(title: str) -> Iterator[None]:
    log.info("%s", title)
    started = time.perf_counter()
    yield
    log.info("  (%.1fs)", time.perf_counter() - started)


def _document_count(db: Session) -> int:
    return db.execute(select(func.count()).select_from(Document)).scalar_one()


def _clear(db: Session) -> None:
    """Everything the seed makes, in an order that needs no cascades. People
    and portfolios stay; their memberships are rebuilt from the plan."""
    for table in (
        "eval_results",
        "eval_runs",
        "eval_questions",
        "feedback",
        "citations",
        "question_sources",
        "questions",
        "usage_events",
        "extracted_values",
        "extractions",
        "jobs",
        "chunks",
        "document_pages",
        "document_blobs",
        "documents",
        "portfolio_members",
    ):
        db.execute(text(f"DELETE FROM {table}"))
    db.commit()


def _drain(db: Session, kinds: list[str]) -> None:
    """Run queued jobs of these kinds until none are queued or running.

    The API's worker may be claiming from the same table (both sides use
    ``SKIP LOCKED``), so the loop waits on the table rather than assuming
    this process ran every job itself.
    """
    deadline = time.monotonic() + DRAIN_TIMEOUT_SECONDS
    outstanding = (
        select(func.count())
        .select_from(Job)
        .where(Job.kind.in_(kinds), Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]))
    )
    while True:
        ingestion.process_pending(db, kinds=kinds)
        if db.execute(outstanding).scalar_one() == 0:
            return
        if time.monotonic() > deadline:
            raise RuntimeError(f"{', '.join(kinds)} jobs were still outstanding after the timeout.")
        time.sleep(0.5)


# ---------------------------------------------------------------- people --


def _ensure_people(
    db: Session, *, reset_passwords: bool
) -> tuple[dict[str, User], dict[str, Portfolio]]:
    portfolios: dict[str, Portfolio] = {}
    for name, _count, confidential in leases.PORTFOLIO_PLAN:
        portfolio = db.execute(select(Portfolio).where(Portfolio.name == name)).scalar_one_or_none()
        if portfolio is None:
            portfolio = Portfolio(name=name, slug=leases.slugify(name))
            db.add(portfolio)
        portfolio.confidential = confidential
        portfolio.description = PORTFOLIO_DESCRIPTIONS[name]
        portfolios[name] = portfolio
    db.flush()

    people: dict[str, User] = {}
    for key, full_name, role, _memberships in USERS:
        email = f"{key}@{DEMO_DOMAIN}"
        user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if user is None:
            user = User(email=email, full_name=full_name, role=role, is_active=True)
            user.password_hash = hash_password(DEMO_PASSWORD)
            db.add(user)
        elif reset_passwords:
            user.password_hash = hash_password(DEMO_PASSWORD)
        user.full_name = full_name
        user.role = role
        user.is_active = True
        people[key] = user
        log.info("  %-32s %s", email, role.value)
    db.flush()

    existing = {
        (member.user_id, member.portfolio_id)
        for member in db.execute(select(PortfolioMember)).scalars()
    }
    for key, _name, _role, memberships in USERS:
        for portfolio_name in memberships:
            pair = (people[key].id, portfolios[portfolio_name].id)
            if pair not in existing:
                db.add(PortfolioMember(user_id=pair[0], portfolio_id=pair[1]))
    db.commit()
    return people, portfolios


# ---------------------------------------------------------------- leases --


def _ingest(
    db: Session,
    specs: list[leases.LeaseSpec],
    portfolios: dict[str, Portfolio],
    uploader: User,
) -> tuple[dict[str, Document], dict[str, list[str]]]:
    """Render every lease, store it as the administrator's upload and run
    the queue until each one is ready. Returns the documents and the page
    text the golden questions are located in, keyed by lease reference."""
    documents: dict[str, Document] = {}
    pages: dict[str, list[str]] = {}
    for spec in specs:
        pdf = leases.render_lease_pdf(spec)
        document, _created = ingestion.create_document(
            db,
            portfolio_id=portfolios[spec.portfolio].id,
            uploaded_by=uploader.id,
            filename=spec.filename,
            title=spec.title,
            doc_type=DocumentType.LEASE,
            data=pdf,
            metadata=spec.metadata,
        )
        documents[spec.reference] = document
        pages[spec.reference] = extract_pages(pdf)
    log.info("  %d leases rendered and queued", len(documents))

    _drain(db, [JobKind.INGEST.value])
    db.expire_all()
    not_ready = [d for d in documents.values() if d.status != DocumentStatus.READY]
    if not_ready:
        detail = "; ".join(f"{d.title}: {d.status} {d.error or ''}".strip() for d in not_ready)
        raise RuntimeError(f"{len(not_ready)} documents did not become ready: {detail}")
    log.info(
        "  %d ready, %d pages, %d chunks",
        len(documents),
        sum(d.page_count for d in documents.values()),
        sum(d.chunk_count for d in documents.values()),
    )
    return documents, pages


def _golden_set(
    db: Session,
    specs: list[leases.LeaseSpec],
    documents: dict[str, Document],
    pages: dict[str, list[str]],
) -> tuple[int, int]:
    answerable = 0
    for spec in specs:
        document = documents[spec.reference]
        for golden in leases.golden_questions(spec, pages[spec.reference]):
            evals.create_question(
                db,
                question=golden.question,
                answerable=True,
                expected_document_id=document.id,
                expected_pages=golden.expected_pages,
                portfolio_id=document.portfolio_id,
                source=EvalSource.GENERATED,
                notes=f"Field: {golden.field_key}",
            )
            answerable += 1
    unanswerable = leases.unanswerable_questions(specs)
    for golden in unanswerable:
        evals.create_question(
            db,
            question=golden.question,
            answerable=False,
            source=EvalSource.GENERATED,
            notes="A lease never states this.",
        )
    log.info("  %d answerable, %d unanswerable", answerable, len(unanswerable))
    return answerable, len(unanswerable)


# ------------------------------------------------------------ extraction --


def _extract(db: Session, documents: dict[str, Document], requester: User) -> None:
    for document in documents.values():
        extraction.request_extraction(db, document, requester)
    _drain(db, [JobKind.EXTRACT.value])
    db.expire_all()
    done = db.execute(
        select(func.count())
        .select_from(Extraction)
        .where(Extraction.status == ExtractionStatus.DONE)
    ).scalar_one()
    if done != len(documents):
        raise RuntimeError(f"{done} of {len(documents)} extractions finished.")
    log.info("  %d extractions done", done)


def _ordered_values(db: Session, document: Document) -> list[ExtractedValue]:
    latest = extraction.latest_extraction(db, document)
    if latest is None:
        return []
    return sorted(latest.values, key=lambda v: FIELD_ORDER.get(v.field_key, len(FIELD_ORDER)))


def _net_figure(amount: float) -> int:
    """A believable net sum for a figure that was read inclusive of service
    charge: lower, rounded the way a lease rounds, and never the same."""
    step = 250 if amount >= 20_000 else 50
    net = int(amount * 0.88) // step * step
    if net == int(amount):
        net -= step
    return max(net, step)


def _review(
    db: Session,
    specs: list[leases.LeaseSpec],
    documents: dict[str, Document],
    reviewer: User,
) -> dict[str, int]:
    """Work through the plan in lease order: confirm whole documents, then
    correct a value or two on the next ones, and leave the rest."""
    by_portfolio: dict[str, list[leases.LeaseSpec]] = {}
    for spec in specs:
        by_portfolio.setdefault(spec.portfolio, []).append(spec)

    counts = {"confirmed": 0, "corrected": 0, "documents": 0}
    for portfolio, (confirm_docs, correct_docs) in REVIEW_PLAN.items():
        for index, spec in enumerate(by_portfolio.get(portfolio, [])):
            if index >= confirm_docs + correct_docs:
                break
            corrected_keys: tuple[str, ...] = ()
            if index >= confirm_docs:
                corrected_keys = CORRECTIONS[(index - confirm_docs) % len(CORRECTIONS)]
            counts["documents"] += 1
            for value in _ordered_values(db, documents[spec.reference]):
                if value.field_key in corrected_keys and isinstance(value.value_json, int | float):
                    extraction.review(
                        db,
                        value,
                        reviewer,
                        action="correct",
                        corrected_text=f"£{_net_figure(value.value_json):,}",
                        note=CORRECTION_NOTE,
                    )
                    counts["corrected"] += 1
                else:
                    extraction.review(db, value, reviewer, action="confirm")
                    counts["confirmed"] += 1
    log.info(
        "  %d documents reviewed: %d values confirmed, %d corrected",
        counts["documents"],
        counts["confirmed"],
        counts["corrected"],
    )
    return counts


# ------------------------------------------------------------- backdating --


class _Clock:
    """Hands out timestamps in the past that never repeat to the second.

    One instance covers the whole seed so the guarantee holds across
    documents, not just within one.
    """

    def __init__(self, rng: random.Random, now: datetime):
        self.rng = rng
        self.now = now
        self.latest = now - timedelta(hours=1)
        self._seconds: set[int] = set()

    def unique(self, moment: datetime) -> datetime:
        moment = min(moment, self.latest)
        while int(moment.timestamp()) in self._seconds:
            moment -= timedelta(seconds=self.rng.randint(1, 5))
        self._seconds.add(int(moment.timestamp()))
        return moment

    def working_moment(self, days_ago: int) -> datetime:
        """Some time during office hours on a weekday about that far back."""
        day = (self.now - timedelta(days=days_ago)).date()
        while day.weekday() >= 5:
            day -= timedelta(days=1)
        return datetime(
            day.year,
            day.month,
            day.day,
            self.rng.randint(8, 16),
            self.rng.randint(0, 59),
            self.rng.randint(0, 59),
            self.rng.randint(0, 999_999),
            tzinfo=UTC,
        )

    def after(self, moment: datetime, low: float, high: float) -> datetime:
        return moment + timedelta(seconds=self.rng.uniform(low, high))


def _upload_moments(clock: _Clock, count: int) -> list[datetime]:
    """When each lease was uploaded: in batches on a handful of working days,
    a few minutes apart within a batch, oldest first."""
    if count == 0:
        return []
    batches = max(2, math.ceil(count / 6))
    window = range(HISTORY_END_DAYS, HISTORY_DAYS + 1)
    days = sorted(clock.rng.sample(window, batches), reverse=True)
    moments: list[datetime] = []
    current_batch = -1
    for index in range(count):
        batch = index * batches // count
        if batch != current_batch:
            current_batch = batch
            moments.append(clock.working_moment(days[batch]))
        else:
            moments.append(clock.after(moments[-1], 90, 420))
    return moments


def _backdate(
    db: Session,
    specs: list[leases.LeaseSpec],
    documents: dict[str, Document],
    clock: _Clock,
) -> None:
    """Move every row the ingest, extraction and review wrote to a believable
    moment in the past. Plain SQL: the ORM would refresh ``updated_at`` to
    now on the way through, which is exactly the thing being undone."""
    uploads = _upload_moments(clock, len(specs))
    earliest = uploads[0] if uploads else clock.now
    for spec, uploaded in zip(specs, uploads, strict=True):
        document = documents[spec.reference]
        ingested = clock.after(uploaded, 25, 95)
        db.execute(
            text("UPDATE documents SET created_at = :c, updated_at = :u WHERE id = :id"),
            {"c": uploaded, "u": ingested, "id": document.id},
        )
        db.execute(
            text("UPDATE document_blobs SET created_at = :c WHERE document_id = :id"),
            {"c": uploaded, "id": document.id},
        )
        db.execute(
            text("UPDATE chunks SET created_at = :c WHERE document_id = :id"),
            {"c": clock.after(uploaded, 3, 12), "id": document.id},
        )
        db.execute(
            text(
                "UPDATE jobs SET created_at = :c, updated_at = :f, claimed_at = :s, "
                "started_at = :s, finished_at = :f WHERE document_id = :id AND kind = 'ingest'"
            ),
            {"c": uploaded, "s": clock.after(uploaded, 1, 4), "f": ingested, "id": document.id},
        )
        db.execute(
            text(
                "UPDATE eval_questions SET created_at = :c, updated_at = :c "
                "WHERE expected_document_id = :id"
            ),
            {"c": clock.after(ingested, 20, 80), "id": document.id},
        )

        latest = extraction.latest_extraction(db, document)
        if latest is None:
            continue
        requested = clock.after(ingested, 120, 2700)
        started = clock.after(requested, 1, 4)
        finished = clock.after(started, 5, 25)
        db.execute(
            text(
                "UPDATE extractions SET created_at = :c, updated_at = :f, started_at = :s, "
                "finished_at = :f WHERE id = :id"
            ),
            {"c": requested, "s": started, "f": finished, "id": latest.id},
        )
        db.execute(
            text(
                "UPDATE jobs SET created_at = :c, updated_at = :f, claimed_at = :s, "
                "started_at = :s, finished_at = :f "
                "WHERE kind = 'extract' AND payload->>'extraction_id' = :xid"
            ),
            {"c": requested, "s": started, "f": finished, "xid": str(latest.id)},
        )
        db.execute(
            text("UPDATE usage_events SET created_at = :c WHERE reference_id = :id"),
            {"c": finished, "id": latest.id},
        )
        db.execute(
            text(
                "UPDATE extracted_values SET created_at = :c, updated_at = :c "
                "WHERE extraction_id = :id"
            ),
            {"c": finished, "id": latest.id},
        )

        # The review happened in one sitting a few working days later, one
        # value after another; a correction took longer because the figure
        # had to be looked up.
        reviewed = [v for v in _ordered_values(db, document) if v.review_status != "pending"]
        if not reviewed:
            continue
        sitting = clock.working_moment(
            max(0, (clock.now - uploaded).days - clock.rng.randint(1, 4))
        )
        moment = max(sitting, clock.after(finished, 600, 1800))
        for value in reviewed:
            slow = value.review_status == ReviewStatus.CORRECTED
            moment = clock.unique(
                clock.after(moment, 60 if slow else 4, 180 if slow else 50)
            )
            db.execute(
                text(
                    "UPDATE extracted_values SET reviewed_at = :r, updated_at = :r WHERE id = :id"
                ),
                {"r": moment, "id": value.id},
            )

    db.execute(
        text(
            "UPDATE eval_questions SET created_at = :c, updated_at = :c "
            "WHERE expected_document_id IS NULL AND source = 'generated'"
        ),
        {"c": clock.after(earliest, 3600, 7200)},
    )
    db.commit()
    db.expire_all()
    log.info("  uploads spread from %s to %s", uploads[0].date(), uploads[-1].date())


# -------------------------------------------------------------- questions --


def _history(
    db: Session,
    specs: list[leases.LeaseSpec],
    people: dict[str, User],
    clock: _Clock,
) -> tuple[int, int]:
    """Ask a fortnight's questions for real, then move them into the past."""
    pool = list(
        db.execute(
            select(EvalQuestion)
            .where(EvalQuestion.answerable.is_(True), EvalQuestion.active.is_(True))
            .order_by(EvalQuestion.created_at, EvalQuestion.id)
        ).scalars()
    )
    clock.rng.shuffle(pool)

    asked: list[tuple[uuid.UUID, User]] = []
    for key in ASKERS:
        user = people[key]
        visible = set(visible_portfolio_ids(db, user))
        pick = next((q for q in pool if q.portfolio_id in visible), None)
        if pick is None:
            continue
        pool.remove(pick)
        answer = answering.ask_sync(db, user=user, text=pick.question)
        asked.append((answer.id, user))
    unanswerable = leases.unanswerable_questions(specs)
    if unanswerable:
        answer = answering.ask_sync(db, user=people["director"], text=unanswerable[0].question)
        asked.append((answer.id, people["director"]))

    moments = sorted(
        clock.unique(clock.working_moment(clock.rng.randint(1, QUESTION_DAYS - 1)))
        for _ in asked
    )
    for (question_id, _user), created in zip(asked, moments, strict=True):
        question = db.get(Question, question_id)
        finished = created + timedelta(milliseconds=question.latency_ms or 1500)
        db.execute(
            text("UPDATE questions SET created_at = :c, finished_at = :f WHERE id = :id"),
            {"c": created, "f": finished, "id": question_id},
        )
        db.execute(
            text("UPDATE usage_events SET created_at = :c WHERE reference_id = :id"),
            {"c": finished, "id": question_id},
        )
    db.commit()

    # The asker's own verdicts: two people happy with what they got, and the
    # director saying an answer stopped short.
    plan = [
        (0, Verdict.UP, None, None),
        (1, Verdict.UP, None, None),
        (2, Verdict.DOWN, FeedbackReason.INCOMPLETE, FEEDBACK_NOTE),
    ]
    feedback = 0
    for index, verdict, reason, note in plan:
        if index >= len(asked):
            break
        question_id, user = asked[index]
        db.add(
            Feedback(
                question_id=question_id,
                user_id=user.id,
                verdict=verdict,
                reason=reason,
                note=note,
            )
        )
        db.flush()
        db.execute(
            text(
                "UPDATE feedback SET created_at = :c WHERE question_id = :q AND user_id = :u"
            ),
            {"c": clock.after(moments[index], 20, 360), "q": question_id, "u": user.id},
        )
        feedback += 1
    db.commit()
    db.expire_all()
    log.info("  %d questions asked, %d with feedback", len(asked), feedback)
    return len(asked), feedback


# ---------------------------------------------------------------- summary --


def _counts(db: Session, column: Any) -> dict[str, int]:
    return {
        str(key): count
        for key, count in db.execute(select(column, func.count()).group_by(column)).all()
    }


def _summary(db: Session, *, skipped: bool, seconds: float) -> dict[str, Any]:
    documents = _counts(db, Document.status)
    totals = db.execute(
        select(
            func.coalesce(func.sum(Document.page_count), 0),
            func.coalesce(func.sum(Document.chunk_count), 0),
        )
    ).one()
    eval_questions = _counts(db, EvalQuestion.answerable)
    values = _counts(db, ExtractedValue.review_status)
    latest_run = db.execute(
        select(EvalRun).order_by(EvalRun.started_at.desc()).limit(1)
    ).scalar_one_or_none()
    return {
        "skipped": skipped,
        "documents": {
            "ready": documents.get(DocumentStatus.READY.value, 0),
            "failed": documents.get(DocumentStatus.FAILED.value, 0),
            "total": sum(documents.values()),
        },
        "pages": int(totals[0]),
        "chunks": int(totals[1]),
        "eval_questions": {
            "answerable": eval_questions.get("True", 0),
            "unanswerable": eval_questions.get("False", 0),
        },
        "extracted_values": {status.value: values.get(status.value, 0) for status in ReviewStatus},
        "questions": _counts(db, Question.status),
        "feedback": _counts(db, Feedback.verdict),
        "eval_run": None
        if latest_run is None
        else {"mode": latest_run.mode, "status": latest_run.status, **latest_run.totals},
        "seconds": round(seconds, 1),
    }


def _rate(value: Any) -> str:
    return "-" if value is None else f"{float(value):.2f}"


def _print_summary(summary: dict[str, Any]) -> None:
    documents = summary["documents"]
    values = summary["extracted_values"]
    questions = summary["questions"]
    feedback = summary["feedback"]
    run = summary["eval_run"]
    minutes, seconds = divmod(int(summary["seconds"]), 60)

    log.info("")
    log.info("Summary")
    log.info("  %-18s %d ready, %d failed", "documents", documents["ready"], documents["failed"])
    log.info("  %-18s %s", "pages", f"{summary['pages']:,}")
    log.info("  %-18s %s", "chunks", f"{summary['chunks']:,}")
    log.info(
        "  %-18s %d answerable, %d unanswerable",
        "eval questions",
        summary["eval_questions"]["answerable"],
        summary["eval_questions"]["unanswerable"],
    )
    log.info(
        "  %-18s %d confirmed, %d corrected, %d rejected, %d pending",
        "extracted values",
        values["confirmed"],
        values["corrected"],
        values["rejected"],
        values["pending"],
    )
    log.info(
        "  %-18s %s",
        "questions",
        ", ".join(f"{count} {status}" for status, count in sorted(questions.items())) or "none",
    )
    log.info(
        "  %-18s %d up, %d down", "feedback", feedback.get("up", 0), feedback.get("down", 0)
    )
    if run is None:
        log.info("  %-18s none", "eval run")
    else:
        log.info(
            "  %-18s %s %s: %s questions, doc hit %s, page hit %s, MRR %s",
            "eval run",
            run["mode"],
            run["status"],
            run.get("questions", 0),
            _rate(run.get("doc_hit_rate")),
            _rate(run.get("page_hit_rate")),
            _rate(run.get("mrr")),
        )
    log.info("  %-18s %dm %02ds", "wall time", minutes, seconds)


# ------------------------------------------------------------------- seed --


def seed(
    db_factory: Callable[[], Session] | None = None,
    *,
    force: bool = False,
    limit: int | None = None,
    extraction: bool = True,
) -> dict[str, Any]:
    """Build the demo and return the summary. ``limit`` caps the number of
    leases (the tests use three); ``extraction=False`` stops after the golden
    set, which is what a run against a paid model would want first."""
    started = time.perf_counter()
    db = (db_factory or SessionLocal)()
    try:
        present = _document_count(db)
        if present and not force:
            log.info("Demo data is already present (%d documents); nothing to do.", present)
            log.info("Re-run with --force to rebuild it.")
            return _summary(db, skipped=True, seconds=time.perf_counter() - started)

        if force:
            with _step("Clearing the previous demo data"):
                _clear(db)

        with _step("Users and portfolios"):
            people, portfolios = _ensure_people(db, reset_passwords=force)

        count = limit or leases.DEMO_LEASE_COUNT
        specs = leases.generate_specs(count)
        # Deterministic for the run: the same jitter every time the seed is
        # rebuilt, relative to whenever "now" is.
        clock = _Clock(random.Random(f"seed:{count}"), datetime.now(UTC))

        with _step(f"Leases ({count})"):
            documents, pages = _ingest(db, specs, portfolios, people["admin"])
        with _step("Golden question set"):
            _golden_set(db, specs, documents, pages)
        if extraction:
            with _step("Extraction"):
                _extract(db, documents, people["admin"])
            with _step("Review"):
                _review(db, specs, documents, people["admin"])
        with _step("Backdating"):
            _backdate(db, specs, documents, clock)
        with _step("Questions and feedback"):
            _history(db, specs, people, clock)
        with _step("Retrieval evaluation"):
            run = evals.create_run(db, mode=EvalMode.RETRIEVAL, user=people["director"])
            log.info("  %s: %s", run.status, run.totals or run.error)

        summary = _summary(db, skipped=False, seconds=time.perf_counter() - started)
        _print_summary(summary)
        log.info("")
        log.info("Sign in with any of:")
        for key, _name, role, _memberships in USERS:
            log.info("  %-32s %-9s password: %s", f"{key}@{DEMO_DOMAIN}", role.value, DEMO_PASSWORD)
        return summary
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the Footnote demo.")
    parser.add_argument("--force", action="store_true", help="Rebuild even if data exists.")
    parser.add_argument("--limit", type=int, default=None, help="Seed only the first N leases.")
    parser.add_argument(
        "--no-extraction", action="store_true", help="Stop after ingestion and the golden set."
    )
    parser.add_argument("--quiet", action="store_true", help="Print warnings only.")
    args = parser.parse_args(argv)

    # The seed's own lines only: the services log at INFO too, and the
    # embedding runtime is chatty, so the root stays at WARNING.
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    log.setLevel(logging.WARNING if args.quiet else logging.INFO)

    seed(force=args.force, limit=args.limit, extraction=not args.no_extraction)
    return 0


if __name__ == "__main__":
    sys.exit(main())
