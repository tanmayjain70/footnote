"""Answering: retrieve, show the model, check every citation, keep the evidence.

This is the product's promise in code. Three things are worth understanding
before reading it.

**The budget is checked before anything else.** Not after retrieval, not
when the model answers: before. A refused request costs nothing, and the
router turns the refusal into a plain 429 before any stream has begun.

**A citation is checked, never trusted.** The model cites by source index and
block range. Both are verified against the list it was actually shown; a
citation that points outside it is dropped and counted, and an answer whose
every citation was dropped is recorded as unanswered rather than answered.
``dropped_citations`` is the number the client asked for.

**Everything the model saw is kept as rows.** ``question_sources`` is the
exact list of passages, in the order shown; ``citations`` is what survived
checking. A stored question can therefore be re-read with the same screen
that rendered it live, and an evaluation can ask whether the cited page was
the right one.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, select, true
from sqlalchemy.orm import Session, joinedload, selectinload

from app.core.errors import AppError, NotFound
from app.deps import visible_portfolio_ids
from app.models import Chunk, Citation, Document, EvalResult, Question, QuestionSource, User
from app.models.enums import QuestionStatus, Role, UsageKind
from app.providers import llm
from app.providers.llm import CitationDelta, Finished, LLMProvider, Source, TextDelta, Usage
from app.schemas.ask import (
    QUESTION_MAX_CHARS,
    QUESTION_MIN_CHARS,
    AnswerBlock,
    AnswerOut,
    CitationOut,
    FeedbackOut,
    SourceOut,
)
from app.services import retrieval, usage

logger = logging.getLogger(__name__)

SNIPPET_CHARS = 240
SIX_DP = Decimal("0.000001")

#: Said when retrieval finds nothing at all in scope -- not the model's
#: refusal, which is a different situation: the model saw passages and none
#: of them helped.
NO_DOCUMENTS_TEXT = "There are no readable documents in scope."

#: Stop reasons that mean the answer is not one, whatever text arrived.
FAILED_STOP_REASONS = {
    "refusal": "The model declined to answer this question.",
    "error": "The model call failed before an answer was finished.",
}

Event = dict[str, Any]


class InvalidQuestion(AppError):
    code = "invalid_question"


# ------------------------------------------------------------- asking --


def ask(
    db: Session,
    *,
    user: User,
    text: str,
    portfolio_id: uuid.UUID | None = None,
    stream_events: bool = True,
) -> Iterator[Event]:
    """Ask a question and stream the answer as SSE-ready event dicts.

    Everything that can refuse the request happens here, eagerly, before the
    iterator is returned: the length check, the budget, the scope. The router
    can therefore answer those with an ordinary JSON error, and only once the
    question row exists does anything stream.

    With ``stream_events`` false only the terminal event (``done`` or
    ``error``) is yielded; ``ask_sync`` uses that.
    """
    question_text = _validated(text)
    usage.check_budget(db)

    visible = visible_portfolio_ids(db, user)
    if portfolio_id is not None and portfolio_id not in set(visible):
        # The same answer as for a portfolio that does not exist. Saying
        # "forbidden" would confirm there is one.
        raise NotFound("No such portfolio.")

    provider = llm.get_llm_provider()
    question = Question(
        user_id=user.id,
        portfolio_id=portfolio_id,
        text=question_text,
        # Nothing has been answered yet; the row is updated when the stream
        # ends, and a crash in between leaves it truthfully unanswered.
        status=QuestionStatus.UNANSWERED,
        provider=provider.name,
        model=provider.model,
        retrieval_config=retrieval.config(),
    )
    db.add(question)
    db.commit()

    return _answer(
        db, question, user=user, visible=visible, provider=provider, stream_events=stream_events
    )


def ask_sync(
    db: Session, *, user: User, text: str, portfolio_id: uuid.UUID | None = None
) -> AnswerOut:
    """Ask and wait. The non-streaming endpoint and the evaluation harness
    use it; a failed answer comes back as a failed question, not as an
    exception, so an evaluation can count it."""
    last: Event | None = None
    for event in ask(db, user=user, text=text, portfolio_id=portfolio_id, stream_events=False):
        last = event
    if last is None:
        raise RuntimeError("the answer stream ended without a terminal event")
    if last["event"] == "done":
        return AnswerOut.model_validate(last["data"])
    return question_out(db, load_question(db, last["data"]["question_id"], viewer=user))


def _validated(text: str) -> str:
    question = text.strip()
    if len(question) < QUESTION_MIN_CHARS:
        raise InvalidQuestion("Ask a question of at least three characters.")
    if len(question) > QUESTION_MAX_CHARS:
        raise InvalidQuestion(f"Keep the question under {QUESTION_MAX_CHARS} characters.")
    return question


@dataclass
class _Block:
    index: int
    parts: list[str] = field(default_factory=list)
    citations: list[int] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(self.parts)


@dataclass
class _Answer:
    """What has arrived so far, in the order it arrived."""

    blocks: dict[int, _Block] = field(default_factory=dict)
    citations: list[Citation] = field(default_factory=list)
    #: chunk id -> [n]. The same chunk cited twice keeps one label.
    ordinals: dict[uuid.UUID, int] = field(default_factory=dict)
    dropped: int = 0
    finished: Finished | None = None

    def block(self, index: int) -> _Block:
        if index not in self.blocks:
            self.blocks[index] = _Block(index)
        return self.blocks[index]


def _answer(
    db: Session,
    question: Question,
    *,
    user: User,
    visible: list[uuid.UUID],
    provider: LLMProvider,
    stream_events: bool,
) -> Iterator[Event]:
    started = time.perf_counter()
    try:
        retrieved = retrieval.search(
            db,
            query=question.text,
            visible_portfolio_ids=visible,
            portfolio_id=question.portfolio_id,
        )
        for r in retrieved:
            question.sources.append(
                QuestionSource(
                    chunk=r.chunk,
                    source_index=r.source_index,
                    fused_score=r.fused_score,
                    vector_rank=r.vector_rank,
                    lexical_rank=r.lexical_rank,
                )
            )
        db.commit()

        sources = retrieval.to_sources(retrieved)
        if stream_events:
            yield {
                "event": "retrieval",
                "data": {
                    "question_id": str(question.id),
                    "sources": [_retrieved_out(r) for r in retrieved],
                },
            }

        if not sources:
            answer = _Answer()
            answer.block(0).parts.append(NO_DOCUMENTS_TEXT)
            _store(db, question, answer, QuestionStatus.UNANSWERED, None, None, started)
            yield _done(db, question, user)
            return

        chunks_by_id = {r.chunk.id: r.chunk for r in retrieved}
        answer = _Answer()
        for event in provider.answer(question.text, sources):
            if isinstance(event, TextDelta):
                answer.block(event.block).parts.append(event.text)
                if stream_events:
                    yield {"event": "text", "data": {"block": event.block, "text": event.text}}
            elif isinstance(event, CitationDelta):
                citation = _verify(event, sources, answer, chunks_by_id, question.id)
                if citation is None:
                    continue
                if stream_events:
                    source = sources[event.source_index]
                    yield {
                        "event": "citation",
                        "data": {
                            "block": event.block,
                            "ordinal": citation.ordinal,
                            "source_index": event.source_index,
                            "chunk_id": str(source.chunk_id),
                            "document_id": str(source.document_id),
                            "document_title": source.document_title,
                            "page_number": source.page_number,
                            "cited_text": event.cited_text,
                        },
                    }
            elif isinstance(event, Finished):
                answer.finished = event
                break

        status, error = _outcome(answer)
        finished = answer.finished or Finished("error", Usage(), provider.model)
        spend = usage.record(
            db,
            user_id=question.user_id,
            kind=UsageKind.ANSWER,
            provider=provider.name,
            model=finished.model,
            usage=finished.usage,
            reference_id=question.id,
            # Committed on its own: what was spent is settled, and the
            # bookkeeping that follows must not be able to roll it back.
            commit=True,
        )
        _store(db, question, answer, status, error, spend, started)
        logger.info(
            "question %s %s: %d citation(s), %d dropped, $%s",
            question.id,
            status,
            len(answer.citations),
            answer.dropped,
            question.cost_usd,
        )
        yield _done(db, question, user)

    except Exception as exc:
        logger.exception("answering question %s failed", question.id)
        db.rollback()
        try:
            question.status = QuestionStatus.FAILED
            question.error = f"{type(exc).__name__}: {exc}"
            question.latency_ms = _elapsed_ms(started)
            question.finished_at = datetime.now(UTC)
            db.commit()
        except Exception:
            logger.exception("could not record the failure on question %s", question.id)
            db.rollback()
        yield {
            "event": "error",
            "data": {
                "code": "answer_failed",
                "message": "Something went wrong while answering. Nothing was cited.",
                "question_id": str(question.id),
            },
        }


def _verify(
    delta: CitationDelta,
    sources: list[Source],
    answer: _Answer,
    chunks_by_id: dict[uuid.UUID, Chunk],
    question_id: uuid.UUID,
) -> Citation | None:
    """A citation that resolves to a passage the model was shown, or None.

    Both halves are checked: the source index must be one of the passages
    sent, and the block range must lie within that passage's sentences. A
    failure on either is counted, logged, and never shown.
    """
    if not 0 <= delta.source_index < len(sources):
        answer.dropped += 1
        logger.warning(
            "question %s: dropped citation to source %d of %d",
            question_id,
            delta.source_index,
            len(sources),
        )
        return None
    source = sources[delta.source_index]
    if not 0 <= delta.block_start < delta.block_end <= len(source.blocks):
        answer.dropped += 1
        logger.warning(
            "question %s: dropped citation to blocks [%d, %d) of %d in source %d",
            question_id,
            delta.block_start,
            delta.block_end,
            len(source.blocks),
            delta.source_index,
        )
        return None

    ordinal = answer.ordinals.setdefault(source.chunk_id, len(answer.ordinals) + 1)
    citation = Citation(
        chunk=chunks_by_id[source.chunk_id],
        ordinal=ordinal,
        source_index=delta.source_index,
        block_start=delta.block_start,
        block_end=delta.block_end,
        cited_text=delta.cited_text,
    )
    answer.citations.append(citation)
    block = answer.block(delta.block)
    if ordinal not in block.citations:
        block.citations.append(ordinal)
    return citation


def _outcome(answer: _Answer) -> tuple[str, str | None]:
    finished = answer.finished
    if finished is None:
        return QuestionStatus.FAILED, "The model stream ended without finishing."
    if finished.stop_reason in FAILED_STOP_REASONS:
        return QuestionStatus.FAILED, FAILED_STOP_REASONS[finished.stop_reason]
    if finished.stop_reason == "max_tokens":
        logger.warning("answer hit the token limit; recording what arrived")
    if answer.citations:
        return QuestionStatus.ANSWERED, None
    # Text without a surviving citation is a refusal, or as good as one:
    # nothing in it can be pointed at.
    return QuestionStatus.UNANSWERED, None


def _store(
    db: Session,
    question: Question,
    answer: _Answer,
    status: str,
    error: str | None,
    spend: Any,
    started: float,
) -> None:
    blocks = list(answer.blocks.values())
    question.answer_blocks = [
        {"block": b.index, "text": b.text, "citations": list(b.citations)} for b in blocks
    ]
    joined = "\n".join(b.text.strip() for b in blocks if b.text.strip())
    question.answer_text = joined or None
    for citation in answer.citations:
        question.citations.append(citation)
    question.dropped_citations = answer.dropped
    if answer.finished is not None:
        # The model the provider actually answered with, which may be a dated
        # release of the alias that was requested.
        question.model = answer.finished.model
    if spend is not None:
        question.input_tokens = spend.input_tokens
        question.output_tokens = spend.output_tokens
        question.cache_read_tokens = spend.cache_read_tokens
        question.cache_write_tokens = spend.cache_write_tokens
        question.cost_usd = spend.cost_usd
    question.status = status
    question.error = error
    question.latency_ms = _elapsed_ms(started)
    question.finished_at = datetime.now(UTC)
    db.commit()


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _done(db: Session, question: Question, user: User) -> Event:
    return {
        "event": "done",
        "data": question_out(db, question, viewer=user).model_dump(mode="json"),
    }


def _retrieved_out(r: retrieval.Retrieved) -> dict[str, Any]:
    return {
        "index": r.source_index,
        "chunk_id": str(r.chunk.id),
        "document_id": str(r.chunk.document_id),
        "document_title": r.chunk.document.title,
        "page_number": r.chunk.page_number,
        "snippet": _snippet(r.chunk.text),
        "vector_rank": r.vector_rank,
        "lexical_rank": r.lexical_rank,
        "fused_score": r.fused_score,
    }


def _snippet(text: str) -> str:
    return text.strip()[:SNIPPET_CHARS]


# ------------------------------------------------------------- reading --


def question_query() -> Select[tuple[Question]]:
    """A select of questions with everything ``question_out`` reads loaded in
    a handful of queries rather than one per source."""
    return select(Question).options(
        joinedload(Question.user),
        selectinload(Question.sources).joinedload(QuestionSource.chunk).joinedload(Chunk.document),
        selectinload(Question.citations).joinedload(Citation.chunk).joinedload(Chunk.document),
        selectinload(Question.feedback),
    )


def asked_by_a_person() -> Any:
    """The filter that keeps an evaluation run out of the question history.

    An end-to-end run answers the whole golden set through the same path a
    person's question takes, which is the point -- it measures what people
    actually get. But two hundred of them landing in "recent questions" would
    bury the ones somebody asked, so the lists exclude any question an
    evaluation produced. They are still reachable from the run that made them.
    """
    return ~select(EvalResult.id).where(EvalResult.question_id == Question.id).exists()


def evidence_still_visible(visible: list[uuid.UUID]) -> Any:
    """The filter that retires a question when its evidence leaves.

    An answer quotes the passages it was built from -- the titles, the page
    numbers, the sentences -- so a stored question carries lease text with it.
    When somebody is taken off a portfolio, their old questions about it must
    go the same way the documents did, or the history becomes the leak the
    access rules were there to prevent.
    """
    hidden = (
        select(QuestionSource.question_id)
        .join(Chunk, Chunk.id == QuestionSource.chunk_id)
        .join(Document, Document.id == Chunk.document_id)
        .where(
            QuestionSource.question_id == Question.id,
            Document.portfolio_id.notin_(visible) if visible else true(),
        )
    )
    return ~hidden.exists()


def can_see(db: Session, user: User, question: Question) -> bool:
    """Own questions, or all of them for the director -- and only while every
    passage the answer was built from is still visible to the viewer."""
    if user.role == Role.DIRECTOR:
        return True
    if question.user_id != user.id:
        return False
    visible = set(visible_portfolio_ids(db, user))
    return all(
        source.chunk.document.portfolio_id in visible
        for source in question.sources
        if source.chunk is not None and source.chunk.document is not None
    )


def load_question(db: Session, question_id: uuid.UUID | str, *, viewer: User) -> Question:
    """A question the viewer may see, or 404 -- the same 404 whether it does
    not exist or belongs to somebody else."""
    question = db.execute(
        question_query().where(Question.id == uuid.UUID(str(question_id)))
    ).scalar_one_or_none()
    if question is None or not can_see(db, viewer, question):
        raise NotFound("No such question.")
    return question


def question_out(db: Session, question: Question, *, viewer: User) -> AnswerOut:
    """The full record of a question, with the viewer's own feedback."""
    cited = {c.chunk_id for c in question.citations}
    own = next((f for f in question.feedback if f.user_id == viewer.id), None)
    return AnswerOut(
        id=question.id,
        user_id=question.user_id,
        user_name=question.user.full_name if question.user else None,
        portfolio_id=question.portfolio_id,
        text=question.text,
        status=question.status,
        answer_text=question.answer_text,
        answer_blocks=(
            [AnswerBlock(**block) for block in question.answer_blocks]
            if question.answer_blocks is not None
            else None
        ),
        citations=[
            CitationOut(
                ordinal=c.ordinal,
                chunk_id=c.chunk_id,
                document_id=c.chunk.document_id,
                document_title=c.chunk.document.title,
                page_number=c.chunk.page_number,
                cited_text=c.cited_text,
                source_index=c.source_index,
                block_start=c.block_start,
                block_end=c.block_end,
            )
            for c in question.citations
        ],
        sources=[
            SourceOut(
                index=s.source_index,
                chunk_id=s.chunk_id,
                document_id=s.chunk.document_id,
                document_title=s.chunk.document.title,
                page_number=s.chunk.page_number,
                snippet=_snippet(s.chunk.text),
                fused_score=s.fused_score,
                vector_rank=s.vector_rank,
                lexical_rank=s.lexical_rank,
                cited=s.chunk_id in cited,
            )
            for s in question.sources
        ],
        provider=question.provider,
        model=question.model,
        latency_ms=question.latency_ms,
        input_tokens=question.input_tokens,
        output_tokens=question.output_tokens,
        cache_read_tokens=question.cache_read_tokens,
        cost_usd=Decimal(question.cost_usd).quantize(SIX_DP),
        dropped_citations=question.dropped_citations,
        error=question.error,
        feedback=FeedbackOut(verdict=own.verdict, reason=own.reason, note=own.note)
        if own
        else None,
        created_at=_utc(question.created_at),
        finished_at=_utc(question.finished_at),
    )


def _utc(value: datetime | None) -> datetime | None:
    # A timestamp assigned in this process is UTC; one read back from the
    # database arrives in the connection's zone. Same instant, different
    # string -- and the live answer and the stored one must serialise alike.
    return value.astimezone(UTC) if value is not None else None
