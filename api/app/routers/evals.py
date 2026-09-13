"""The golden question set and the runs scored against it. Director only.

Every route requires the evaluation role, reads included: the set names the
documents a lease administrator may not be allowed to see, and a run's
results list which of them retrieval found.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.deps import require_evals
from app.models import EvalQuestion, EvalResult, EvalRun, User
from app.schemas.common import Page
from app.schemas.evals import (
    EvalQuestionCreate,
    EvalQuestionFromQuestion,
    EvalQuestionOut,
    EvalQuestionPatch,
    EvalResultOut,
    EvalRunDetail,
    EvalRunOut,
    EvalRunRequest,
)
from app.services import evals as evals_service

router = APIRouter(prefix="/evals", tags=["evals"])

_SIX_DP = Decimal("0.000001")


def question_out(question: EvalQuestion) -> EvalQuestionOut:
    return EvalQuestionOut(
        id=question.id,
        question=question.question,
        answerable=question.answerable,
        expected_document_id=question.expected_document_id,
        expected_document_title=(
            question.expected_document.title if question.expected_document else None
        ),
        expected_pages=list(question.expected_pages),
        portfolio_id=question.portfolio_id,
        source=question.source,
        active=question.active,
        notes=question.notes,
        created_at=question.created_at,
        updated_at=question.updated_at,
    )


def run_out(run: EvalRun) -> EvalRunOut:
    return EvalRunOut(
        id=run.id,
        mode=run.mode,
        status=run.status,
        started_by_name=run.starter.full_name if run.starter else None,
        config=run.config,
        totals=run.totals,
        started_at=run.started_at,
        finished_at=run.finished_at,
        error=run.error,
    )


def result_out(result: EvalResult) -> EvalResultOut:
    question = result.eval_question
    return EvalResultOut(
        eval_question_id=result.eval_question_id,
        question=question.question,
        answerable=question.answerable,
        expected_document_title=(
            question.expected_document.title if question.expected_document else None
        ),
        expected_pages=list(question.expected_pages),
        retrieved_doc_hit=result.retrieved_doc_hit,
        retrieved_page_hit=result.retrieved_page_hit,
        hit_rank=result.hit_rank,
        answered=result.answered,
        cited_doc_hit=result.cited_doc_hit,
        cited_page_hit=result.cited_page_hit,
        refused_correctly=result.refused_correctly,
        question_id=result.question_id,
        latency_ms=result.latency_ms,
        cost_usd=str(Decimal(result.cost_usd or 0).quantize(_SIX_DP)),
    )


# ------------------------------------------------------------- questions --


@router.get("/questions", response_model=Page[EvalQuestionOut])
def list_questions(
    db: Session = Depends(get_db),
    _: User = Depends(require_evals),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    answerable: bool | None = None,
) -> Page[EvalQuestionOut]:
    """The golden set, newest first, optionally only the answerable or
    only the unanswerable half."""
    rows, total = evals_service.list_questions(
        db, answerable=answerable, limit=limit, offset=offset
    )
    return Page(items=[question_out(q) for q in rows], total=total, limit=limit, offset=offset)


@router.post("/questions", response_model=EvalQuestionOut, status_code=status.HTTP_201_CREATED)
def create_question(
    body: EvalQuestionCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_evals),
) -> EvalQuestionOut:
    """Add a question by hand. An answerable one must name its document
    (422 otherwise); a document or portfolio that does not exist is a 404."""
    question = evals_service.create_question(
        db,
        question=body.question,
        answerable=body.answerable,
        expected_document_id=body.expected_document_id,
        expected_pages=body.expected_pages,
        portfolio_id=body.portfolio_id,
        notes=body.notes,
    )
    return question_out(evals_service.load_question(db, question.id))


@router.post(
    "/questions/from-question",
    response_model=EvalQuestionOut,
    status_code=status.HTTP_201_CREATED,
)
def create_question_from_question(
    body: EvalQuestionFromQuestion,
    db: Session = Depends(get_db),
    _: User = Depends(require_evals),
) -> EvalQuestionOut:
    """Promote a question somebody asked. The text and scope are copied;
    an answerable promotion with no document named takes the one the
    stored answer cited first."""
    question = evals_service.add_from_feedback(
        db,
        body.question_id,
        answerable=body.answerable,
        expected_document_id=body.expected_document_id,
        expected_pages=body.expected_pages,
    )
    return question_out(evals_service.load_question(db, question.id))


@router.patch("/questions/{question_id}", response_model=EvalQuestionOut)
def patch_question(
    question_id: uuid.UUID,
    body: EvalQuestionPatch,
    db: Session = Depends(get_db),
    _: User = Depends(require_evals),
) -> EvalQuestionOut:
    """Switch a question in or out of the set. Inactive questions are kept
    and skipped by runs; nothing is deleted."""
    question = evals_service.load_question(db, question_id)
    return question_out(evals_service.set_active(db, question, body.active))


# ------------------------------------------------------------------ runs --


@router.get("/runs", response_model=Page[EvalRunOut])
def list_runs(
    db: Session = Depends(get_db),
    _: User = Depends(require_evals),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> Page[EvalRunOut]:
    rows, total = evals_service.list_runs(db, limit=limit, offset=offset)
    return Page(items=[run_out(r) for r in rows], total=total, limit=limit, offset=offset)


@router.post("/runs", response_model=EvalRunOut, status_code=status.HTTP_201_CREATED)
def create_run(
    body: EvalRunRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_evals),
) -> EvalRunOut:
    """Start a run. A retrieval run is finished by the time this returns;
    an end-to-end run is queued for the worker and comes back ``running``."""
    run = evals_service.create_run(db, mode=body.mode, user=user, limit=body.limit)
    return run_out(evals_service.load_run(db, run.id))


@router.get("/runs/{run_id}", response_model=EvalRunDetail)
def get_run(
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_evals),
) -> EvalRunDetail:
    """The run with one row per question scored."""
    run = evals_service.load_run(db, run_id)
    return EvalRunDetail(
        **run_out(run).model_dump(),
        results=[result_out(r) for r in evals_service.run_results(db, run)],
    )
