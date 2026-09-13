"""Ask a question, read questions back, say whether an answer was any good.

``POST /ask`` streams. Server-sent events rather than a websocket because the
traffic is one way and a proxy that understands HTTP understands SSE; the
``X-Accel-Buffering`` header is there so one that buffers by default does
not hold the whole answer until it is finished, which would defeat the point.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.core.db import SessionLocal, get_db
from app.deps import current_user, require_ask, visible_portfolio_ids
from app.models import Feedback, Question, User
from app.models.enums import Role
from app.schemas.ask import AnswerOut, AskRequest, FeedbackRequest, QuestionOut
from app.schemas.common import Page
from app.services import answering

router = APIRouter(tags=["ask"])

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _sse(events: Iterator[dict[str, Any]], db: Session) -> Iterator[str]:
    """Serialise events as ``event: name\\ndata: json\\n\\n`` and close the
    session when the stream ends -- including when the browser goes away
    mid-answer, which closes the generator."""
    try:
        for event in events:
            yield f"event: {event['event']}\ndata: {json.dumps(event['data'])}\n\n"
    finally:
        db.close()


@router.post("/ask", response_model=None)
def ask(
    body: AskRequest,
    stream: bool = Query(default=True),
    db: Session = Depends(get_db),
    user: User = Depends(require_ask),
) -> AnswerOut | StreamingResponse:
    """Ask. Streams by default; ``?stream=false`` waits and returns the answer.

    The budget check, the scope check and the question row all happen before
    the response starts, so a 429 or a 404 is a plain JSON error and never
    half a stream.
    """
    if not stream:
        return answering.ask_sync(db, user=user, text=body.question, portfolio_id=body.portfolio_id)

    # The stream outlives the request's own session, so it gets one of its
    # own, closed by the generator when the last event has gone.
    session = SessionLocal()
    try:
        events = answering.ask(
            session, user=user, text=body.question, portfolio_id=body.portfolio_id
        )
    except BaseException:
        session.close()
        raise
    return StreamingResponse(
        _sse(events, session), media_type="text/event-stream", headers=SSE_HEADERS
    )


@router.get("/questions", response_model=Page[QuestionOut])
def list_questions(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user_id: uuid.UUID | None = None,
) -> Page[QuestionOut]:
    """Newest first. The director sees everybody's and may filter by person;
    everybody else sees their own, whatever ``user_id`` says."""
    if user.role == Role.DIRECTOR:
        scope = Question.user_id == user_id if user_id is not None else None
    else:
        # Own questions, and only while their passages are still visible: an
        # answer quotes the lease it came from.
        scope = and_(
            Question.user_id == user.id,
            answering.evidence_still_visible(visible_portfolio_ids(db, user)),
        )

    # Questions an evaluation run produced are excluded: they go through the
    # same path on purpose, and there are hundreds of them.
    human = answering.asked_by_a_person()
    count = select(func.count()).select_from(Question).where(human)
    query = answering.question_query().where(human)
    if scope is not None:
        count = count.where(scope)
        query = query.where(scope)

    total = db.execute(count).scalar_one()
    rows = db.execute(
        query.order_by(Question.created_at.desc(), Question.id).limit(limit).offset(offset)
    ).scalars()
    return Page(
        items=[answering.question_out(db, q, viewer=user) for q in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/questions/{question_id}", response_model=QuestionOut)
def get_question(
    question_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> QuestionOut:
    question = answering.load_question(db, question_id, viewer=user)
    return answering.question_out(db, question, viewer=user)


@router.post("/questions/{question_id}/feedback", response_model=QuestionOut)
def give_feedback(
    question_id: uuid.UUID,
    body: FeedbackRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_ask),
) -> QuestionOut:
    """One verdict per person per question; a second one replaces the first.

    Feedback is how the golden question set grows: a thumbs-down with
    ``should_have_refused`` is a candidate unanswerable question, and a
    thumbs-up is a candidate answerable one with its expected page known.
    """
    question = answering.load_question(db, question_id, viewer=user)
    own = next((f for f in question.feedback if f.user_id == user.id), None)
    if own is None:
        own = Feedback(user_id=user.id, verdict=body.verdict)
        question.feedback.append(own)
    own.verdict = body.verdict
    own.reason = body.reason
    own.note = body.note
    db.commit()
    return answering.question_out(db, question, viewer=user)
