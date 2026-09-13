"""The evaluation harness: a golden question set, and runs scored against it.

The client's complaint was a tool that got a break notice wrong and nobody
could say whether the next version would do better. This is the answer to
that: a set of questions with a known answer location -- a document and the
pages the canonical sentence sits on -- or known to be unanswerable, and a run
that asks every one and records what happened. A retrieval change is then a
number that moved, not an impression.

Two modes. A ``retrieval`` run costs nothing and takes seconds: did the right
page come back, and how far down. An ``end_to_end`` run asks the model as
well, so it spends budget and goes through the job queue; it additionally
records whether the answer cited the right page and whether an unanswerable
question was refused.

Unanswerable questions contribute nothing to the hit rates. Their hit fields
are null, and a null is not a miss: "did retrieval find the page" has no
meaning for a question with no page to find.
"""

from __future__ import annotations

import contextlib
import logging
import statistics
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, joinedload

from app.core.errors import HTTP_422, AppError, BudgetExhausted, NotFound, Terminal
from app.models import (
    Document,
    EvalQuestion,
    EvalResult,
    EvalRun,
    Job,
    Portfolio,
    Question,
    User,
)
from app.models.enums import (
    EvalMode,
    EvalRunStatus,
    EvalSource,
    JobKind,
    JobStage,
    JobStatus,
    QuestionStatus,
)
from app.providers import llm
from app.schemas.ask import QUESTION_MAX_CHARS, QUESTION_MIN_CHARS
from app.services import answering, retrieval

logger = logging.getLogger(__name__)

SIX_DP = Decimal("0.000001")
MODES = {mode.value for mode in EvalMode}

#: A ranked list of (document id, page number), best first -- what retrieval
#: returned, or what an answer cited, whichever mode produced it.
Ranked = list[tuple[uuid.UUID, int]]

#: ``(doc_hit, page_hit, hit_rank)``; see ``rank_hits``.
RankHits = tuple[bool | None, bool | None, int | None]


class InvalidEvalQuestion(AppError):
    status_code = HTTP_422
    code = "invalid_eval_question"


class InvalidEvalRun(AppError):
    status_code = HTTP_422
    code = "invalid_eval_run"


def _now() -> datetime:
    return datetime.now(UTC)


def _error_text(exc: BaseException) -> str:
    message = str(exc).strip() or type(exc).__name__
    return message[:2000]


# ------------------------------------------------------------ golden set --


def create_question(
    db: Session,
    *,
    question: str,
    answerable: bool,
    expected_document_id: uuid.UUID | None = None,
    expected_pages: Sequence[int] = (),
    portfolio_id: uuid.UUID | None = None,
    notes: str | None = None,
    source: str = EvalSource.MANUAL,
) -> EvalQuestion:
    """Add one question to the golden set.

    An answerable question must name the document that answers it: a hit is
    measured against something, and a question with nothing to measure
    against would sit in the set counting for nothing. The pages are
    optional -- without them the run can still say whether the document
    came back, just not the page.
    """
    text = question.strip()
    if not QUESTION_MIN_CHARS <= len(text) <= QUESTION_MAX_CHARS:
        raise InvalidEvalQuestion(
            f"A question is between {QUESTION_MIN_CHARS} and {QUESTION_MAX_CHARS} characters."
        )
    if answerable and expected_document_id is None:
        raise InvalidEvalQuestion("An answerable question needs the document that answers it.")
    if expected_document_id is not None and db.get(Document, expected_document_id) is None:
        raise NotFound("No such document.")
    if portfolio_id is not None and db.get(Portfolio, portfolio_id) is None:
        raise NotFound("No such portfolio.")

    row = EvalQuestion(
        question=text,
        answerable=answerable,
        expected_document_id=expected_document_id,
        expected_pages=_pages(expected_pages),
        portfolio_id=portfolio_id,
        source=source,
        notes=(notes or "").strip() or None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def add_from_feedback(
    db: Session,
    question_id: uuid.UUID,
    *,
    answerable: bool,
    expected_document_id: uuid.UUID | None = None,
    expected_pages: Sequence[int] = (),
) -> EvalQuestion:
    """Promote a question somebody asked into the golden set.

    This is how the set grows past the generated one: a thumbs-up on a good
    answer becomes an answerable question whose expected page is the one
    the answer cited, and a thumbs-down with "should have refused" becomes
    an unanswerable one. When the caller names no document, an answerable
    promotion takes the first citation's document and the pages cited in
    it -- the answer a person just approved is the ground truth.
    """
    question = db.execute(
        answering.question_query().where(Question.id == question_id)
    ).scalar_one_or_none()
    if question is None:
        raise NotFound("No such question.")

    pages = list(expected_pages)
    if answerable and expected_document_id is None and question.citations:
        expected_document_id = question.citations[0].chunk.document_id
        if not pages:
            pages = [
                c.chunk.page_number
                for c in question.citations
                if c.chunk.document_id == expected_document_id
            ]

    asked_by = question.user.full_name if question.user else "a user"
    asked_on = question.created_at.astimezone(UTC).date().isoformat()
    return create_question(
        db,
        question=question.text,
        answerable=answerable,
        expected_document_id=expected_document_id,
        expected_pages=pages,
        portfolio_id=question.portfolio_id,
        notes=f"Promoted from a question asked by {asked_by} on {asked_on}.",
        source=EvalSource.FEEDBACK,
    )


def set_active(db: Session, question: EvalQuestion, active: bool) -> EvalQuestion:
    """Deactivated rather than deleted: past runs still refer to it, and a
    question that turned out to be badly phrased is worth keeping visible."""
    question.active = active
    db.commit()
    db.refresh(question)
    return question


def _pages(pages: Sequence[int]) -> list[int]:
    return sorted({int(p) for p in pages})


def question_query() -> Select[tuple[EvalQuestion]]:
    return select(EvalQuestion).options(joinedload(EvalQuestion.expected_document))


def load_question(db: Session, question_id: uuid.UUID) -> EvalQuestion:
    question = db.execute(
        question_query().where(EvalQuestion.id == question_id)
    ).scalar_one_or_none()
    if question is None:
        raise NotFound("No such evaluation question.")
    return question


def list_questions(
    db: Session, *, answerable: bool | None = None, limit: int = 50, offset: int = 0
) -> tuple[list[EvalQuestion], int]:
    """Newest first, with the total for paging."""
    count = select(func.count()).select_from(EvalQuestion)
    query = question_query()
    if answerable is not None:
        count = count.where(EvalQuestion.answerable == answerable)
        query = query.where(EvalQuestion.answerable == answerable)
    total = db.execute(count).scalar_one()
    rows = db.execute(
        query.order_by(EvalQuestion.created_at.desc(), EvalQuestion.id).limit(limit).offset(offset)
    ).scalars()
    return list(rows), total


def active_questions(db: Session, *, limit: int | None = None) -> list[EvalQuestion]:
    """The questions a run scores, oldest first so a ``limit`` is a stable
    prefix of the set rather than whichever were added last."""
    query = (
        question_query()
        .where(EvalQuestion.active.is_(True))
        .order_by(EvalQuestion.created_at, EvalQuestion.id)
    )
    if limit is not None:
        query = query.limit(limit)
    return list(db.execute(query).scalars())


# ------------------------------------------------------------------ runs --


def run_config(mode: str, limit: int | None) -> dict[str, Any]:
    """What was in force when the run started, so two runs can be compared
    knowing what differed. The model is recorded only when one was asked."""
    config: dict[str, Any] = {**retrieval.config(), "limit": limit}
    if mode == EvalMode.END_TO_END:
        provider = llm.get_llm_provider()
        config["llm_provider"] = provider.name
        config["llm_model"] = provider.model
    return config


def create_run(db: Session, *, mode: str, user: User, limit: int | None = None) -> EvalRun:
    """Start a run. Retrieval runs finish before this returns; end-to-end
    runs are queued for the worker and come back ``running``.

    A retrieval run that fails is returned as a failed run rather than
    raised: the row is the record of what happened, and the caller renders
    its error the same way it renders a failed end-to-end run.
    """
    if mode not in MODES:
        raise InvalidEvalRun(f"Unknown evaluation mode {mode!r}.", detail={"modes": sorted(MODES)})

    run = EvalRun(
        mode=mode,
        status=EvalRunStatus.RUNNING,
        started_by=user.id,
        config=run_config(mode, limit),
    )
    db.add(run)
    db.flush()
    if mode == EvalMode.END_TO_END:
        db.add(Job(kind=JobKind.EVAL_RUN, status=JobStatus.QUEUED, payload={"run_id": str(run.id)}))
    db.commit()
    db.refresh(run)

    if mode == EvalMode.RETRIEVAL:
        # A failure is already recorded on the row and logged; the caller
        # gets the failed run back rather than an exception.
        with contextlib.suppress(Exception):
            run_retrieval_eval(db, run)
    return run


def run_retrieval_eval(db: Session, run: EvalRun) -> EvalRun:
    """Score every active question by retrieval alone. Records the outcome
    on the run and re-raises on failure."""
    return _execute(db, run, end_to_end=False)


def run_end_to_end_eval(db: Session, run: EvalRun) -> EvalRun:
    """Ask every active question as the person who started the run, and
    score retrieval, citations and refusals. Records the outcome on the run
    and re-raises on failure."""
    return _execute(db, run, end_to_end=True)


def run_eval_job(db: Session, job: Job) -> None:
    """Run one queued evaluation. Called by the worker with a claimed job.

    The job's own bookkeeping -- attempts, requeue, final failure -- belongs
    to ``ingestion.run_job``, so on error this records the run's state and
    re-raises. A retry starts the run afresh; the results of the attempt
    that failed are cleared so the totals never mix two attempts.
    """
    run = _run_for(db, job)
    if run is None:
        raise RuntimeError("The job names no evaluation run that exists.")
    job.stage = JobStage.RUN
    db.commit()

    _execute(db, run, end_to_end=run.mode == EvalMode.END_TO_END)

    job.status = JobStatus.DONE
    job.finished_at = _now()
    job.error = None
    db.commit()


def _run_for(db: Session, job: Job) -> EvalRun | None:
    try:
        run_id = uuid.UUID(str((job.payload or {}).get("run_id")))
    except ValueError:
        return None
    return db.get(EvalRun, run_id)


def _execute(db: Session, run: EvalRun, *, end_to_end: bool) -> EvalRun:
    run.status = EvalRunStatus.RUNNING
    run.error = None
    run.finished_at = None
    run.totals = {}
    # A retry resumes rather than restarts. Results are unique per
    # (run, question) and committed one at a time, so what a failed attempt
    # measured is still there -- and for an end-to-end run it was paid for.
    measured = {
        question_id
        for question_id in db.execute(
            select(EvalResult.eval_question_id).where(EvalResult.run_id == run.id)
        ).scalars()
    }
    if measured:
        logger.info("run %s resuming: %d question(s) already measured", run.id, len(measured))
    db.commit()

    try:
        questions = active_questions(db, limit=run.config.get("limit"))
        starter: User | None = None
        if end_to_end:
            starter = db.get(User, run.started_by) if run.started_by else None
            if starter is None:
                raise RuntimeError(
                    "The person who started this run no longer exists, and an "
                    "end-to-end run asks its questions as them."
                )
        visible = _all_portfolio_ids(db)

        results: list[EvalResult] = list(
            db.execute(select(EvalResult).where(EvalResult.run_id == run.id)).scalars()
        )
        for question in questions:
            if question.id in measured:
                continue
            if end_to_end:
                result = _score_end_to_end(db, question, starter)  # type: ignore[arg-type]
            else:
                result = _score_retrieval(db, question, visible)
            result.run_id = run.id
            db.add(result)
            # Committed one at a time: a run that dies part-way keeps what
            # it measured, and an end-to-end run has already spent the
            # money on those questions.
            db.commit()
            results.append(result)

        run.totals = compute_totals(results, end_to_end=end_to_end)
        run.status = EvalRunStatus.DONE
        run.finished_at = _now()
        db.commit()
        logger.info("eval run %s (%s) done: %s", run.id, run.mode, run.totals)
    except BudgetExhausted as exc:
        # Not a fault to retry: every attempt would spend the rest of the
        # budget it has not got and delete nothing it measured. The run stops
        # where it is, with what it managed, and says why.
        db.rollback()
        run.status = EvalRunStatus.FAILED
        run.error = _error_text(exc)
        run.finished_at = _now()
        db.commit()
        logger.warning("eval run %s stopped on the daily budget", run.id)
        raise Terminal(str(exc)) from exc
    except Exception as exc:
        db.rollback()
        run.status = EvalRunStatus.FAILED
        run.error = _error_text(exc)
        run.finished_at = _now()
        db.commit()
        logger.warning("eval run %s (%s) failed: %s", run.id, run.mode, run.error)
        raise
    return run


def _all_portfolio_ids(db: Session) -> list[uuid.UUID]:
    # The director's view. Evaluations are director-only and measure the
    # whole corpus; a question narrows itself with its own portfolio scope.
    return list(db.execute(select(Portfolio.id).order_by(Portfolio.name)).scalars())


# --------------------------------------------------------------- scoring --


def _score_retrieval(
    db: Session, question: EvalQuestion, visible: list[uuid.UUID]
) -> EvalResult:
    retrieved = retrieval.search(
        db,
        query=question.question,
        visible_portfolio_ids=visible,
        portfolio_id=question.portfolio_id,
    )
    ranked: Ranked = [(r.chunk.document_id, r.chunk.page_number) for r in retrieved]
    result = EvalResult(
        eval_question=question,
        detail={
            "retrieved": [
                {
                    "rank": r.source_index + 1,
                    "document_id": str(r.chunk.document_id),
                    "document_title": r.chunk.document.title,
                    "page": r.chunk.page_number,
                }
                for r in retrieved
            ]
        },
    )
    _apply_retrieval(result, question, ranked)
    return result


def _score_end_to_end(db: Session, question: EvalQuestion, starter: User) -> EvalResult:
    """Ask the question for real and score what came back.

    Retrieval is scored from the sources stored on the question rather than
    by searching again: they are the exact list the model was shown, in
    rank order, and the point is to measure the answer that was given.
    """
    answer = answering.ask_sync(
        db, user=starter, text=question.question, portfolio_id=question.portfolio_id
    )
    ranked: Ranked = [(s.document_id, s.page_number) for s in answer.sources]
    answered = answer.status == QuestionStatus.ANSWERED
    result = EvalResult(
        eval_question=question,
        question_id=answer.id,
        answered=answered,
        latency_ms=answer.latency_ms,
        cost_usd=Decimal(answer.cost_usd).quantize(SIX_DP),
        detail={
            "status": answer.status,
            "retrieved": [
                {
                    "rank": s.index + 1,
                    "document_id": str(s.document_id),
                    "document_title": s.document_title,
                    "page": s.page_number,
                }
                for s in answer.sources
            ],
            "cited": [
                {
                    "ordinal": c.ordinal,
                    "document_id": str(c.document_id),
                    "document_title": c.document_title,
                    "page": c.page_number,
                }
                for c in answer.citations
            ],
        },
    )
    _apply_retrieval(result, question, ranked)

    if question.answerable:
        result.cited_doc_hit, result.cited_page_hit = citation_hits(
            question, [(c.document_id, c.page_number) for c in answer.citations]
        )
    else:
        # A refusal is the product working, and only an actual refusal
        # counts. A model call that failed did not decline to answer, it
        # broke; crediting it here would let a dead endpoint score a
        # perfect refusal rate.
        result.refused_correctly = answer.status == QuestionStatus.UNANSWERED
    return result


def _apply_retrieval(result: EvalResult, question: EvalQuestion, ranked: Ranked) -> None:
    result.retrieved_doc_hit, result.retrieved_page_hit, result.hit_rank = rank_hits(
        question, ranked
    )
    if question.answerable and question.expected_document_id is None:
        # The document was deleted after the question was written. Nothing
        # to measure against; left null so it does not count as a miss.
        result.detail = {**result.detail, "unmeasured": "expected document no longer exists"}


def _measurable(question: EvalQuestion) -> bool:
    return question.answerable and question.expected_document_id is not None


def rank_hits(question: EvalQuestion, ranked: Ranked) -> RankHits:
    """``(doc_hit, page_hit, hit_rank)`` for a ranked list, best first.

    All three are null for a question with nothing to measure against --
    unanswerable, or its expected document deleted since -- and
    ``page_hit`` alone is null when the question names no pages: the
    document can still be scored, the page cannot, and a null is not a
    miss.

    ``hit_rank`` is the rank of the first chunk on an expected page, or
    failing that the first chunk from the expected document: the page is
    the unit of citation, so a page hit at rank 3 beats a document hit at
    rank 1 for the purpose of the reciprocal rank.
    """
    if not _measurable(question):
        return None, None, None

    expected = question.expected_document_id
    pages = set(question.expected_pages)
    doc_rank: int | None = None
    page_rank: int | None = None
    for rank, (document_id, page) in enumerate(ranked, start=1):
        if document_id != expected:
            continue
        if doc_rank is None:
            doc_rank = rank
        if page in pages:
            page_rank = rank
            break
    page_hit = (page_rank is not None) if pages else None
    return doc_rank is not None, page_hit, page_rank or doc_rank


def citation_hits(question: EvalQuestion, cited: Ranked) -> tuple[bool | None, bool | None]:
    """``(doc_hit, page_hit)`` for the pages an answer cited, with the same
    nulls as ``rank_hits``. An answer that cited nothing is a miss on both,
    not a null: the question had an answer and the system did not give
    it.
    """
    if not _measurable(question):
        return None, None

    expected = question.expected_document_id
    pages = set(question.expected_pages)
    doc_hit = any(document_id == expected for document_id, _ in cited)
    page_hit = (
        any(document_id == expected and page in pages for document_id, page in cited)
        if pages
        else None
    )
    return doc_hit, page_hit


def compute_totals(results: Sequence[EvalResult], *, end_to_end: bool) -> dict[str, Any]:
    """The run's numbers. Every rate is over the questions it applies to
    and null when there are none -- an empty set has no hit rate, and a
    zero would say retrieval found nothing.

    - ``doc_hit_rate``, ``page_hit_rate``, ``mrr``: answerable questions.
    - ``answer_rate``, ``citation_*_hit_rate``: answerable questions, so a
      perfect system scores 1.0 on each.
    - ``correct_refusal_rate``: unanswerable questions.
    - ``false_answer_rate``: all questions -- an answerable one answered
      without citing the right document, or an unanswerable one answered
      at all. This is the number the client cares about most: how often
      the system says something confident and wrong.
    """
    answerable = [r for r in results if r.eval_question.answerable]
    unanswerable = [r for r in results if not r.eval_question.answerable]
    measured = [r for r in answerable if r.retrieved_doc_hit is not None]

    totals: dict[str, Any] = {
        "questions": len(results),
        "answerable": len(answerable),
        "unanswerable": len(unanswerable),
        "doc_hit_rate": _rate([r.retrieved_doc_hit for r in answerable]),
        "page_hit_rate": _rate([r.retrieved_page_hit for r in answerable]),
        "mrr": _mean([1.0 / r.hit_rank if r.hit_rank else 0.0 for r in measured]),
    }
    if not end_to_end:
        return totals

    false_answers = sum(1 for r in answerable if r.answered and r.cited_doc_hit is False) + sum(
        1 for r in unanswerable if r.answered
    )
    latencies = [r.latency_ms for r in results if r.latency_ms is not None]
    spent = sum((Decimal(r.cost_usd) for r in results), Decimal(0))
    totals.update(
        {
            "answer_rate": _rate([r.answered for r in answerable]),
            "citation_doc_hit_rate": _rate([r.cited_doc_hit for r in answerable]),
            "citation_page_hit_rate": _rate([r.cited_page_hit for r in answerable]),
            "correct_refusal_rate": _rate([r.refused_correctly for r in unanswerable]),
            "false_answer_rate": false_answers / len(results) if results else None,
            "cost_usd": str(spent.quantize(SIX_DP)),
            "latency_p50_ms": int(statistics.median(latencies)) if latencies else None,
        }
    )
    return totals


def _rate(values: Sequence[bool | None]) -> float | None:
    measured = [v for v in values if v is not None]
    return _mean([1.0 if v else 0.0 for v in measured])


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


# --------------------------------------------------------------- reading --


def run_query() -> Select[tuple[EvalRun]]:
    return select(EvalRun).options(joinedload(EvalRun.starter))


def load_run(db: Session, run_id: uuid.UUID) -> EvalRun:
    run = db.execute(run_query().where(EvalRun.id == run_id)).scalar_one_or_none()
    if run is None:
        raise NotFound("No such evaluation run.")
    return run


def list_runs(db: Session, *, limit: int = 20, offset: int = 0) -> tuple[list[EvalRun], int]:
    """Newest first, with the total for paging."""
    total = db.execute(select(func.count()).select_from(EvalRun)).scalar_one()
    rows = db.execute(
        run_query().order_by(EvalRun.started_at.desc(), EvalRun.id).limit(limit).offset(offset)
    ).scalars()
    return list(rows), total


def run_results(db: Session, run: EvalRun) -> list[EvalResult]:
    """The run's results in the order they were scored, each with its
    question and the expected document's title loaded."""
    rows = db.execute(
        select(EvalResult)
        .join(EvalQuestion, EvalQuestion.id == EvalResult.eval_question_id)
        .options(
            joinedload(EvalResult.eval_question).joinedload(EvalQuestion.expected_document)
        )
        .where(EvalResult.run_id == run.id)
        .order_by(EvalQuestion.created_at, EvalQuestion.id)
    ).scalars()
    return list(rows)
