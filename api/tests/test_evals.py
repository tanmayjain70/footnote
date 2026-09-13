"""The evaluation harness: what a run measures, and that the numbers are right.

The corpus is four generated leases with their golden questions and three
unanswerable ones, inserted directly as ready documents so the tests are
about scoring and not about the ingestion worker. The retrieval numbers are
checked against ``retrieval.search`` called by hand, and the end-to-end
numbers against the stored questions the run created -- the harness must
agree with the evidence it points at.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.demo.leases import (
    generate_specs,
    golden_questions,
    render_lease_pdf,
    unanswerable_questions,
)
from app.models import (
    Chunk,
    Document,
    DocumentPage,
    EvalQuestion,
    EvalResult,
    EvalRun,
    Job,
    Question,
)
from app.models.enums import (
    DocumentStatus,
    EvalMode,
    EvalRunStatus,
    EvalSource,
    JobKind,
    JobStatus,
    QuestionStatus,
)
from app.providers import llm
from app.providers.embeddings import get_embedding_provider
from app.providers.llm import CitationDelta, Finished, Source, TextDelta, Usage
from app.services import answering, evals, ingestion, retrieval, usage
from app.services.chunking import chunk_document
from app.services.pdf import extract_pages
from tests.conftest import API

#: Generated once per module: the specs are deterministic for a seed, and
#: rendering is the slow part, so the PDFs are cached below as well.
SPECS = generate_specs(4, seed=11)
UNANSWERABLE_COUNT = 3

_RENDERED: dict[str, tuple[bytes, list[str]]] = {}


def rendered(spec: Any) -> tuple[bytes, list[str]]:
    if spec.reference not in _RENDERED:
        data = render_lease_pdf(spec)
        _RENDERED[spec.reference] = (data, extract_pages(data))
    return _RENDERED[spec.reference]


def ready_lease(db: Session, portfolio_id: uuid.UUID, spec: Any, *, uploaded_by: uuid.UUID):
    """Insert a generated lease as a ready document with pages and chunks."""
    data, pages = rendered(spec)
    chunks = chunk_document(pages)
    vectors = get_embedding_provider().embed_documents([chunk.text for chunk in chunks])

    document = Document(
        portfolio_id=portfolio_id,
        title=spec.title,
        filename=spec.filename,
        content_sha256=hashlib.sha256(data).hexdigest(),
        byte_size=len(data),
        page_count=len(pages),
        chunk_count=len(chunks),
        status=DocumentStatus.READY,
        uploaded_by=uploaded_by,
    )
    db.add(document)
    db.flush()
    for number, text in enumerate(pages, start=1):
        db.add(DocumentPage(document_id=document.id, page_number=number, text=text))
    for ordinal, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True)):
        db.add(
            Chunk(
                document_id=document.id,
                page_number=chunk.page_number,
                ordinal=ordinal,
                text=chunk.text,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
                token_estimate=chunk.token_estimate,
                embedding=vector,
            )
        )
    db.commit()
    db.refresh(document)
    return document, pages


@pytest.fixture
def corpus(db: Session, users, portfolios) -> dict[str, Any]:
    """Four leases -- two in City Centre, two in Northern Estates -- with
    their golden questions and three unanswerable ones, all active."""
    documents: dict[str, Document] = {}
    questions: list[EvalQuestion] = []
    for index, spec in enumerate(SPECS):
        portfolio = portfolios["city"] if index < 2 else portfolios["north"]
        document, pages = ready_lease(db, portfolio.id, spec, uploaded_by=users["admin"].id)
        documents[spec.reference] = document
        for golden in golden_questions(spec, pages):
            questions.append(
                EvalQuestion(
                    question=golden.question,
                    answerable=True,
                    expected_document_id=document.id,
                    expected_pages=list(golden.expected_pages),
                    source=EvalSource.GENERATED,
                )
            )
    for golden in unanswerable_questions(SPECS)[:UNANSWERABLE_COUNT]:
        questions.append(
            EvalQuestion(question=golden.question, answerable=False, source=EvalSource.GENERATED)
        )
    db.add_all(questions)
    db.commit()
    for question in questions:
        db.refresh(question)
    answerable = [q for q in questions if q.answerable]
    assert len(answerable) >= 12, "four leases give three or four questions each"
    return {
        "documents": documents,
        "questions": questions,
        "answerable": answerable,
        "unanswerable": [q for q in questions if not q.answerable],
        "portfolio_ids": [p.id for p in portfolios.values()],
    }


def expected_retrieval(
    db: Session, question: EvalQuestion, portfolio_ids: list[uuid.UUID]
) -> tuple[bool | None, bool | None, int | None]:
    """What the harness should record for one question, computed by hand
    from the search it is meant to be measuring."""
    if not question.answerable:
        return None, None, None
    hits = retrieval.search(
        db,
        query=question.question,
        visible_portfolio_ids=portfolio_ids,
        portfolio_id=question.portfolio_id,
    )
    doc_rank = page_rank = None
    for rank, hit in enumerate(hits, start=1):
        if hit.chunk.document_id != question.expected_document_id:
            continue
        doc_rank = doc_rank or rank
        if hit.chunk.page_number in question.expected_pages:
            page_rank = rank
            break
    page_hit = (page_rank is not None) if question.expected_pages else None
    return doc_rank is not None, page_hit, page_rank or doc_rank


def post_run(client: TestClient, headers: dict[str, str], **body: Any) -> dict[str, Any]:
    response = client.post(f"{API}/evals/runs", headers=headers, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def get_run(client: TestClient, headers: dict[str, str], run_id: str) -> dict[str, Any]:
    response = client.get(f"{API}/evals/runs/{run_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


class ScriptedLLM:
    """A provider with one fixed behaviour -- refuse everything, cite the
    first passage for everything, or fail every call -- so the end-to-end
    numbers are pinned down independently of how good the stub is."""

    name = "scripted"
    model = "extractive-v1"

    def __init__(self, behaviour: str):
        assert behaviour in {"refuse", "cite_first", "error"}
        self.behaviour = behaviour

    def answer(self, question: str, sources: list[Source]) -> Iterator[Any]:
        if self.behaviour == "error":
            yield Finished("error", Usage(), self.model)
            return
        if self.behaviour == "cite_first" and sources:
            first = sources[0]
            yield TextDelta(0, first.blocks[0])
            yield CitationDelta(0, first.index, 0, 1, first.blocks[0])
        else:
            yield TextDelta(0, "I can't answer that from the documents I can see.")
        yield Finished("end_turn", Usage(input_tokens=400, output_tokens=40), self.model)

    def extract(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


# --- retrieval runs --------------------------------------------------------


def test_a_retrieval_run_scores_every_question_against_the_search(
    client: TestClient, db: Session, auth, corpus
):
    run = post_run(client, auth("director"), mode="retrieval")

    assert run["mode"] == "retrieval"
    assert run["status"] == EvalRunStatus.DONE
    assert run["started_by_name"] == "Priya Hallam"
    assert run["finished_at"] is not None and run["error"] is None
    assert run["config"]["embedding_provider"] == "hashed" and run["config"]["limit"] is None
    assert "llm_model" not in run["config"], "no model was asked"

    totals = run["totals"]
    assert totals["questions"] == len(corpus["questions"])
    assert totals["answerable"] == len(corpus["answerable"])
    assert totals["unanswerable"] == UNANSWERABLE_COUNT
    assert set(totals) == {
        "questions",
        "answerable",
        "unanswerable",
        "doc_hit_rate",
        "page_hit_rate",
        "mrr",
    }

    # The same numbers, computed by hand from the search the run measures.
    by_hand = {
        q.id: expected_retrieval(db, q, corpus["portfolio_ids"]) for q in corpus["answerable"]
    }
    n = len(by_hand)
    assert totals["doc_hit_rate"] == pytest.approx(sum(d for d, _, _ in by_hand.values()) / n)
    assert totals["page_hit_rate"] == pytest.approx(sum(p for _, p, _ in by_hand.values()) / n)
    assert totals["mrr"] == pytest.approx(
        sum(1 / r if r else 0 for _, _, r in by_hand.values()) / n
    )
    # Hybrid retrieval over four leases finds the right one for a question
    # naming its unit; a suite that passed with every hit false would be
    # measuring nothing.
    assert totals["doc_hit_rate"] > 0.5
    assert 0 < totals["mrr"] <= 1

    detail = get_run(client, auth("director"), run["id"])
    rows = {row["eval_question_id"]: row for row in detail["results"]}
    assert len(rows) == len(corpus["questions"])
    first = corpus["answerable"][0]
    doc_hit, page_hit, rank = by_hand[first.id]
    row = rows[str(first.id)]
    assert (row["retrieved_doc_hit"], row["retrieved_page_hit"], row["hit_rank"]) == (
        doc_hit,
        page_hit,
        rank,
    )
    assert row["question"] == first.question
    assert row["expected_document_title"] == first.expected_document.title
    assert row["expected_pages"] == first.expected_pages
    # Nothing was asked, so the answer fields are null rather than false.
    assert row["answered"] is None and row["cited_doc_hit"] is None
    assert row["refused_correctly"] is None and row["question_id"] is None


def test_unanswerable_questions_contribute_nothing_to_the_hit_rates(
    client: TestClient, db: Session, auth, corpus
):
    run = post_run(client, auth("director"), mode="retrieval")
    detail = get_run(client, auth("director"), run["id"])
    rows = {row["eval_question_id"]: row for row in detail["results"]}
    for question in corpus["unanswerable"]:
        row = rows[str(question.id)]
        assert row["answerable"] is False
        assert row["retrieved_doc_hit"] is None
        assert row["retrieved_page_hit"] is None
        assert row["hit_rank"] is None

    # With only the unanswerable questions active there is nothing to
    # measure, and the rates say so rather than reporting zero hits.
    for question in corpus["answerable"]:
        client.patch(
            f"{API}/evals/questions/{question.id}",
            headers=auth("director"),
            json={"active": False},
        )
    run = post_run(client, auth("director"), mode="retrieval")
    assert run["status"] == EvalRunStatus.DONE
    assert run["totals"]["questions"] == UNANSWERABLE_COUNT
    assert run["totals"]["answerable"] == 0
    assert run["totals"]["unanswerable"] == UNANSWERABLE_COUNT
    assert run["totals"]["doc_hit_rate"] is None
    assert run["totals"]["page_hit_rate"] is None
    assert run["totals"]["mrr"] is None


def test_a_question_scoped_to_a_portfolio_searches_only_that_portfolio(
    client: TestClient, db: Session, auth, corpus, portfolios
):
    # A City Centre lease asked about with Northern Estates as the scope:
    # the right document is invisible to the search, so this is a miss.
    original = corpus["answerable"][0]
    assert original.expected_document.portfolio_id == portfolios["city"].id
    response = client.post(
        f"{API}/evals/questions",
        headers=auth("director"),
        json={
            "question": original.question,
            "answerable": True,
            "expected_document_id": str(original.expected_document_id),
            "expected_pages": original.expected_pages,
            "portfolio_id": str(portfolios["north"].id),
        },
    )
    assert response.status_code == 201, response.text
    scoped_id = response.json()["id"]

    run = post_run(client, auth("director"), mode="retrieval")
    detail = get_run(client, auth("director"), run["id"])
    rows = {r["eval_question_id"]: r for r in detail["results"]}
    assert rows[scoped_id]["retrieved_doc_hit"] is False
    assert rows[scoped_id]["hit_rank"] is None
    # The unscoped original still finds it.
    assert rows[str(original.id)]["retrieved_doc_hit"] is True


def test_limit_scores_only_the_first_questions(client: TestClient, auth, corpus):
    run = post_run(client, auth("director"), mode="retrieval", limit=2)
    assert run["totals"]["questions"] == 2
    assert run["config"]["limit"] == 2
    detail = get_run(client, auth("director"), run["id"])
    assert len(detail["results"]) == 2
    # Oldest first with the id as the tiebreak, so the limit is a stable
    # prefix of the set. The corpus was inserted in one transaction, so
    # every question carries the same created_at and the tiebreak decides.
    ordered = sorted(corpus["questions"], key=lambda q: (q.created_at, q.id))
    assert [r["eval_question_id"] for r in detail["results"]] == [
        str(q.id) for q in ordered[:2]
    ]


def test_an_inactive_question_is_skipped(client: TestClient, auth, corpus):
    skipped = corpus["answerable"][1]
    response = client.patch(
        f"{API}/evals/questions/{skipped.id}", headers=auth("director"), json={"active": False}
    )
    assert response.status_code == 200, response.text
    assert response.json()["active"] is False

    run = post_run(client, auth("director"), mode="retrieval")
    assert run["totals"]["questions"] == len(corpus["questions"]) - 1
    detail = get_run(client, auth("director"), run["id"])
    assert str(skipped.id) not in {r["eval_question_id"] for r in detail["results"]}

    # And back in: nothing was deleted.
    response = client.patch(
        f"{API}/evals/questions/{skipped.id}", headers=auth("director"), json={"active": True}
    )
    assert response.json()["active"] is True
    assert post_run(client, auth("director"), mode="retrieval")["totals"]["questions"] == len(
        corpus["questions"]
    )


def test_a_run_with_no_active_questions_completes_with_zero_totals(
    client: TestClient, db: Session, auth, users
):
    run = post_run(client, auth("director"), mode="retrieval")
    assert run["status"] == EvalRunStatus.DONE
    assert run["error"] is None
    assert run["totals"] == {
        "questions": 0,
        "answerable": 0,
        "unanswerable": 0,
        "doc_hit_rate": None,
        "page_hit_rate": None,
        "mrr": None,
    }

    run = post_run(client, auth("director"), mode="end_to_end")
    assert run["status"] == EvalRunStatus.RUNNING
    assert ingestion.process_pending(db, kinds=[JobKind.EVAL_RUN]) == 1
    detail = get_run(client, auth("director"), run["id"])
    assert detail["status"] == EvalRunStatus.DONE
    assert detail["results"] == []
    assert detail["totals"]["questions"] == 0
    assert detail["totals"]["answer_rate"] is None
    assert detail["totals"]["false_answer_rate"] is None
    assert detail["totals"]["cost_usd"] == "0.000000"
    assert detail["totals"]["latency_p50_ms"] is None


def test_a_retrieval_run_that_raises_is_recorded_as_failed(
    client: TestClient, auth, corpus, monkeypatch
):
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the vector index is offline")

    monkeypatch.setattr(retrieval, "search", broken)
    run = post_run(client, auth("director"), mode="retrieval")
    assert run["status"] == EvalRunStatus.FAILED
    assert "vector index is offline" in run["error"]
    assert run["finished_at"] is not None
    assert run["totals"] == {}


# --- end-to-end runs ---------------------------------------------------------


def test_an_end_to_end_run_records_what_the_stored_questions_say(
    db: Session, users, corpus
):
    director = users["director"]
    run = evals.create_run(db, mode=EvalMode.END_TO_END, user=director)
    assert run.status == EvalRunStatus.RUNNING
    assert run.config["llm_provider"] == "stub" and run.config["llm_model"] == "extractive-v1"

    job = db.execute(select(Job).where(Job.kind == JobKind.EVAL_RUN)).scalar_one()
    assert job.status == JobStatus.QUEUED
    assert job.payload == {"run_id": str(run.id)}
    assert job.document_id is None

    evals.run_eval_job(db, job)
    db.expire_all()

    run = db.get(EvalRun, run.id)
    assert run.status == EvalRunStatus.DONE, run.error
    assert job.status == JobStatus.DONE and job.stage == "run"
    results = evals.run_results(db, run)
    assert len(results) == len(corpus["questions"])

    answered_count = 0
    for result in results:
        question = db.execute(
            answering.question_query().where(Question.id == result.question_id)
        ).scalar_one()
        # Asked for real, as the person who started the run.
        assert question.user_id == director.id
        assert question.text == result.eval_question.question
        assert question.portfolio_id == result.eval_question.portfolio_id
        assert result.answered == (question.status == QuestionStatus.ANSWERED)
        assert result.latency_ms == question.latency_ms
        assert result.cost_usd == question.cost_usd
        answered_count += result.answered

        # Retrieval is scored from the passages the model was shown.
        ranked = [(s.chunk.document_id, s.chunk.page_number) for s in question.sources]
        cited = [(c.chunk.document_id, c.chunk.page_number) for c in question.citations]
        expected = result.eval_question.expected_document_id
        pages = set(result.eval_question.expected_pages)
        if result.eval_question.answerable:
            assert result.retrieved_doc_hit == any(d == expected for d, _ in ranked)
            assert result.cited_doc_hit == any(d == expected for d, _ in cited)
            assert result.cited_page_hit == any(d == expected and p in pages for d, p in cited)
            assert result.refused_correctly is None
        else:
            assert result.retrieved_doc_hit is None and result.cited_doc_hit is None
            # Only an actual refusal counts, not a call that failed.
            assert result.refused_correctly == (question.status == QuestionStatus.UNANSWERED)

    # The stub answers a question that names the lease; a run where nothing
    # was answered would not be exercising the citation checks.
    assert answered_count > 0

    totals = run.totals
    assert set(totals) == {
        "questions",
        "answerable",
        "unanswerable",
        "doc_hit_rate",
        "page_hit_rate",
        "mrr",
        "answer_rate",
        "citation_doc_hit_rate",
        "citation_page_hit_rate",
        "correct_refusal_rate",
        "false_answer_rate",
        "cost_usd",
        "latency_p50_ms",
    }
    answerable = [r for r in results if r.eval_question.answerable]
    unanswerable = [r for r in results if not r.eval_question.answerable]
    assert totals["answer_rate"] == pytest.approx(
        sum(r.answered for r in answerable) / len(answerable)
    )
    assert totals["citation_doc_hit_rate"] == pytest.approx(
        sum(r.cited_doc_hit for r in answerable) / len(answerable)
    )
    assert totals["correct_refusal_rate"] == pytest.approx(
        sum(r.refused_correctly for r in unanswerable) / len(unanswerable)
    )
    false_answers = sum(1 for r in answerable if r.answered and not r.cited_doc_hit) + sum(
        1 for r in unanswerable if r.answered
    )
    assert totals["false_answer_rate"] == pytest.approx(false_answers / len(results))
    assert totals["cost_usd"] == "0.000000", "the stub is free"
    assert isinstance(totals["latency_p50_ms"], int)


def test_an_evaluation_does_not_flood_the_question_history(
    client: TestClient, db: Session, users, auth, corpus
):
    """An end-to-end run answers the whole golden set through the same path a
    person's question takes. Hundreds of them in "recent questions" would bury
    the ones somebody asked, so the lists leave them out -- while the run that
    made them still links to every one."""
    director = users["director"]
    asked = answering.ask_sync(db, user=director, text="When does the term expire?")

    run = evals.create_run(db, mode=EvalMode.END_TO_END, user=director)
    job = db.execute(select(Job).where(Job.kind == JobKind.EVAL_RUN)).scalar_one()
    evals.run_eval_job(db, job)
    db.expire_all()

    listed = client.get(f"{API}/questions?limit=100", headers=auth("director")).json()
    assert listed["total"] == 1
    assert [item["id"] for item in listed["items"]] == [str(asked.id)]

    # The evaluation's own questions are still there, reachable from the run.
    results = evals.run_results(db, db.get(EvalRun, run.id))
    measured = [r.question_id for r in results if r.question_id is not None]
    assert measured, "an end-to-end run records the question it asked"
    for question_id in measured:
        response = client.get(f"{API}/questions/{question_id}", headers=auth("director"))
        assert response.status_code == 200


def test_the_eval_run_job_runs_through_the_queue(
    client: TestClient, db: Session, auth, corpus
):
    run = post_run(client, auth("director"), mode="end_to_end", limit=3)
    assert run["status"] == EvalRunStatus.RUNNING
    assert run["totals"] == {}

    assert ingestion.process_pending(db, kinds=[JobKind.EVAL_RUN]) == 1

    detail = get_run(client, auth("director"), run["id"])
    assert detail["status"] == EvalRunStatus.DONE, detail["error"]
    assert detail["totals"]["questions"] == 3
    assert len(detail["results"]) == 3
    assert all(r["question_id"] is not None for r in detail["results"])
    assert all(r["answered"] is not None for r in detail["results"])
    job = db.execute(select(Job).where(Job.kind == JobKind.EVAL_RUN)).scalar_one()
    assert job.status == JobStatus.DONE and job.error is None
    assert len(db.execute(select(Question)).scalars().all()) == 3


def test_scripted_providers_pin_down_the_end_to_end_rates(
    db: Session, users, corpus, monkeypatch
):
    n_answerable = len(corpus["answerable"])
    n_total = len(corpus["questions"])

    # A model that refuses everything: no answers, every refusal correct.
    monkeypatch.setattr(llm, "get_llm_provider", lambda: ScriptedLLM("refuse"))
    run = evals.create_run(db, mode=EvalMode.END_TO_END, user=users["director"])
    evals.run_end_to_end_eval(db, run)
    assert run.status == EvalRunStatus.DONE
    assert run.totals["answer_rate"] == 0.0
    assert run.totals["citation_doc_hit_rate"] == 0.0
    assert run.totals["correct_refusal_rate"] == 1.0
    assert run.totals["false_answer_rate"] == 0.0

    # A model that always cites the first passage: everything answered,
    # nothing refused, and every unanswerable question is a false answer.
    monkeypatch.setattr(llm, "get_llm_provider", lambda: ScriptedLLM("cite_first"))
    run = evals.create_run(db, mode=EvalMode.END_TO_END, user=users["director"])
    evals.run_end_to_end_eval(db, run)
    assert run.status == EvalRunStatus.DONE
    assert run.totals["answer_rate"] == 1.0
    assert run.totals["correct_refusal_rate"] == 0.0
    assert run.totals["false_answer_rate"] >= UNANSWERABLE_COUNT / n_total
    # Citing the top passage is right exactly when the top passage is from
    # the expected document.
    results = evals.run_results(db, run)
    answerable = [r for r in results if r.eval_question.answerable]
    assert len(answerable) == n_answerable
    for result in answerable:
        top = result.detail["retrieved"][0]["document_id"]
        assert result.cited_doc_hit == (top == str(result.eval_question.expected_document_id))
    assert run.totals["cost_usd"] == "0.000000"

    # A model whose every call fails: nothing answered, but nothing refused
    # either. A dead endpoint is not the product declining to answer, and
    # the run itself still completes -- the failures are the finding.
    monkeypatch.setattr(llm, "get_llm_provider", lambda: ScriptedLLM("error"))
    run = evals.create_run(db, mode=EvalMode.END_TO_END, user=users["director"])
    evals.run_end_to_end_eval(db, run)
    assert run.status == EvalRunStatus.DONE
    assert run.totals["answer_rate"] == 0.0
    assert run.totals["correct_refusal_rate"] == 0.0
    assert run.totals["false_answer_rate"] == 0.0
    results = evals.run_results(db, run)
    assert all(r.detail["status"] == QuestionStatus.FAILED for r in results)
    assert all(r.answered is False for r in results)


def test_an_end_to_end_run_that_raises_fails_the_run_and_the_job(
    client: TestClient, db: Session, auth, corpus, monkeypatch
):
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the model endpoint is unreachable")

    monkeypatch.setattr(answering, "ask_sync", broken)
    run = post_run(client, auth("director"), mode="end_to_end")

    # The worker retries a failed job until its attempts run out; every
    # attempt fails the same way and the last one is final.
    assert ingestion.process_pending(db, kinds=[JobKind.EVAL_RUN]) == 3
    job = db.execute(select(Job).where(Job.kind == JobKind.EVAL_RUN)).scalar_one()
    assert job.status == JobStatus.FAILED
    assert job.attempts == 3
    assert "model endpoint is unreachable" in job.error

    detail = get_run(client, auth("director"), run["id"])
    assert detail["status"] == EvalRunStatus.FAILED
    assert "model endpoint is unreachable" in detail["error"]
    assert detail["results"] == []


def test_an_end_to_end_run_asks_a_scoped_question_in_its_scope(
    db: Session, users, corpus, portfolios
):
    # A City Centre lease asked about with Northern Estates as the scope.
    # The stored question must carry that scope, and every passage the
    # model was shown must come from it: the eval measures the product as
    # a manager of that portfolio would experience it, not as the director.
    original = corpus["answerable"][0]
    scoped = evals.create_question(
        db,
        question=original.question,
        answerable=True,
        expected_document_id=original.expected_document_id,
        expected_pages=original.expected_pages,
        portfolio_id=portfolios["north"].id,
    )
    for question in corpus["questions"]:
        evals.set_active(db, question, False)

    run = evals.create_run(db, mode=EvalMode.END_TO_END, user=users["director"])
    evals.run_end_to_end_eval(db, run)
    assert run.status == EvalRunStatus.DONE, run.error
    (result,) = evals.run_results(db, run)
    assert result.eval_question_id == scoped.id

    asked = db.execute(
        answering.question_query().where(Question.id == result.question_id)
    ).scalar_one()
    assert asked.portfolio_id == portfolios["north"].id
    assert asked.sources, "Northern Estates has two leases to search"
    assert all(s.chunk.document.portfolio_id == portfolios["north"].id for s in asked.sources)
    assert result.retrieved_doc_hit is False and result.hit_rank is None
    assert result.cited_doc_hit is False


def test_a_budget_exhausted_run_is_recorded_as_failed(
    client: TestClient, db: Session, auth, corpus
):
    # The budget is checked before every question, so a run started with
    # nothing left to spend asks nothing. It fails with the budget message
    # rather than recording a run of unanswered questions, which would read
    # as the model refusing everything.
    usage.set_budget(db, Decimal("0"))
    run = post_run(client, auth("director"), mode="end_to_end")

    # Once, not three times: a budget that is gone will still be gone on the
    # next attempt, so the job is failed rather than requeued.
    assert ingestion.process_pending(db, kinds=[JobKind.EVAL_RUN]) == 1
    job = db.execute(select(Job).where(Job.kind == JobKind.EVAL_RUN)).scalar_one()
    assert job.status == JobStatus.FAILED
    assert job.attempts == 1
    assert "daily limit" in job.error

    detail = get_run(client, auth("director"), run["id"])
    assert detail["status"] == EvalRunStatus.FAILED
    assert "daily limit" in detail["error"]
    assert detail["results"] == []
    assert db.execute(select(Question)).scalars().first() is None, "nothing was asked"


def test_a_job_naming_no_run_fails_without_touching_anything(
    client: TestClient, db: Session, auth, corpus
):
    db.add(Job(kind=JobKind.EVAL_RUN, payload={"run_id": str(uuid.uuid4())}))
    db.add(Job(kind=JobKind.EVAL_RUN, payload={"run_id": "not-a-uuid"}))
    db.commit()

    # Each job is retried to its limit; the message is the same every time.
    assert ingestion.process_pending(db, kinds=[JobKind.EVAL_RUN]) == 6
    jobs = db.execute(select(Job).where(Job.kind == JobKind.EVAL_RUN)).scalars().all()
    assert len(jobs) == 2
    for job in jobs:
        assert job.status == JobStatus.FAILED
        assert job.attempts == 3
        assert "names no evaluation run" in job.error
    assert client.get(f"{API}/evals/runs", headers=auth("director")).json()["total"] == 0
    assert db.execute(select(Question)).scalars().first() is None


def test_a_deleted_expected_document_leaves_its_questions_unmeasured(
    client: TestClient, db: Session, auth, corpus
):
    # The lease a golden question points at is deleted after the question
    # was written. The question stays in the set (its text is still worth
    # keeping) but there is nothing to score it against, so its fields are
    # null and it drops out of the rates rather than counting as a miss.
    gone = corpus["answerable"][0].expected_document
    orphaned = [q.id for q in corpus["answerable"] if q.expected_document_id == gone.id]
    ingestion.delete_document(db, gone)
    for question in corpus["questions"]:
        db.refresh(question)
    assert all(q.expected_document_id is None for q in corpus["answerable"] if q.id in orphaned)

    run = post_run(client, auth("director"), mode="retrieval")
    assert run["status"] == EvalRunStatus.DONE, run["error"]
    totals = run["totals"]
    assert totals["questions"] == len(corpus["questions"])
    assert totals["answerable"] == len(corpus["answerable"])

    detail = get_run(client, auth("director"), run["id"])
    rows = {r["eval_question_id"]: r for r in detail["results"]}
    for question_id in orphaned:
        row = rows[str(question_id)]
        assert row["answerable"] is True
        assert row["expected_document_title"] is None
        assert (row["retrieved_doc_hit"], row["retrieved_page_hit"], row["hit_rank"]) == (
            None,
            None,
            None,
        )
    stored = {r.eval_question_id: r for r in evals.run_results(db, db.get(EvalRun, run["id"]))}
    assert all("unmeasured" in stored[q].detail for q in orphaned)

    # The rates are over the questions that still have a document.
    measured = [q for q in corpus["answerable"] if q.id not in orphaned]
    by_hand = [expected_retrieval(db, q, corpus["portfolio_ids"]) for q in measured]
    assert totals["doc_hit_rate"] == pytest.approx(sum(d for d, _, _ in by_hand) / len(by_hand))
    assert totals["mrr"] == pytest.approx(
        sum(1 / r if r else 0 for _, _, r in by_hand) / len(by_hand)
    )


# --- the golden set ----------------------------------------------------------


def test_create_and_list_questions(client: TestClient, auth, corpus, portfolios):
    document = corpus["answerable"][0].expected_document
    response = client.post(
        f"{API}/evals/questions",
        headers=auth("director"),
        json={
            "question": "What is the annual rent?",
            "answerable": True,
            "expected_document_id": str(document.id),
            "expected_pages": [4, 2, 4],
            "portfolio_id": str(document.portfolio_id),
            "notes": "Added by hand.",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["question"] == "What is the annual rent?"
    assert body["expected_document_id"] == str(document.id)
    assert body["expected_document_title"] == document.title
    assert body["expected_pages"] == [2, 4], "sorted and deduplicated"
    assert body["portfolio_id"] == str(document.portfolio_id)
    assert body["source"] == EvalSource.MANUAL
    assert body["active"] is True
    assert body["notes"] == "Added by hand."
    assert body["created_at"] and body["updated_at"]
    assert set(body) == {
        "id",
        "question",
        "answerable",
        "expected_document_id",
        "expected_document_title",
        "expected_pages",
        "portfolio_id",
        "source",
        "active",
        "notes",
        "created_at",
        "updated_at",
    }

    page = client.get(f"{API}/evals/questions", headers=auth("director")).json()
    assert page["total"] == len(corpus["questions"]) + 1
    assert page["items"][0]["id"] == body["id"], "newest first"
    assert {"items", "total", "limit", "offset"} <= set(page)

    unanswerable = client.get(
        f"{API}/evals/questions", headers=auth("director"), params={"answerable": "false"}
    ).json()
    assert unanswerable["total"] == UNANSWERABLE_COUNT
    assert all(item["answerable"] is False for item in unanswerable["items"])
    assert all(item["expected_document_title"] is None for item in unanswerable["items"])

    paged = client.get(
        f"{API}/evals/questions", headers=auth("director"), params={"limit": 2, "offset": 1}
    ).json()
    assert len(paged["items"]) == 2 and paged["limit"] == 2 and paged["offset"] == 1


def test_an_answerable_question_needs_a_document_that_exists(
    client: TestClient, auth, users, portfolios
):
    response = client.post(
        f"{API}/evals/questions",
        headers=auth("director"),
        json={"question": "When does the lease expire?", "answerable": True},
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "invalid_eval_question"

    response = client.post(
        f"{API}/evals/questions",
        headers=auth("director"),
        json={
            "question": "When does the lease expire?",
            "answerable": True,
            "expected_document_id": str(uuid.uuid4()),
        },
    )
    assert response.status_code == 404, response.text

    response = client.post(
        f"{API}/evals/questions",
        headers=auth("director"),
        json={
            "question": "When does the lease expire?",
            "answerable": False,
            "expected_pages": [0],
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"

    # Unanswerable needs no document.
    response = client.post(
        f"{API}/evals/questions",
        headers=auth("director"),
        json={"question": "What is the tenant's VAT number?", "answerable": False},
    )
    assert response.status_code == 201, response.text


def test_from_question_copies_the_text_and_links_the_cited_document(
    client: TestClient, db: Session, auth, users, corpus
):
    golden = corpus["answerable"][0]
    answer = answering.ask_sync(db, user=users["director"], text=golden.question)
    assert answer.status == QuestionStatus.ANSWERED and answer.citations

    response = client.post(
        f"{API}/evals/questions/from-question",
        headers=auth("director"),
        json={"question_id": str(answer.id), "answerable": True},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["question"] == golden.question
    assert body["source"] == EvalSource.FEEDBACK
    first = answer.citations[0]
    assert body["expected_document_id"] == str(first.document_id)
    assert body["expected_document_title"] == first.document_title
    assert body["expected_pages"] == sorted(
        {c.page_number for c in answer.citations if c.document_id == first.document_id}
    )
    assert body["portfolio_id"] is None
    assert "Priya Hallam" in body["notes"]

    # Named explicitly, the caller's document and pages win.
    other = corpus["answerable"][-1].expected_document
    response = client.post(
        f"{API}/evals/questions/from-question",
        headers=auth("director"),
        json={
            "question_id": str(answer.id),
            "answerable": True,
            "expected_document_id": str(other.id),
            "expected_pages": [7],
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["expected_document_id"] == str(other.id)
    assert response.json()["expected_pages"] == [7]

    # An unanswerable promotion links nothing: the citation was the mistake.
    response = client.post(
        f"{API}/evals/questions/from-question",
        headers=auth("director"),
        json={"question_id": str(answer.id), "answerable": False},
    )
    assert response.status_code == 201, response.text
    assert response.json()["expected_document_id"] is None

    response = client.post(
        f"{API}/evals/questions/from-question",
        headers=auth("director"),
        json={"question_id": str(uuid.uuid4()), "answerable": True},
    )
    assert response.status_code == 404


def test_runs_list_newest_first(client: TestClient, auth, corpus):
    first = post_run(client, auth("director"), mode="retrieval", limit=1)
    second = post_run(client, auth("director"), mode="end_to_end", limit=1)

    page = client.get(f"{API}/evals/runs", headers=auth("director")).json()
    assert page["total"] == 2
    assert [item["id"] for item in page["items"]] == [second["id"], first["id"]]
    assert [item["status"] for item in page["items"]] == [
        EvalRunStatus.RUNNING,
        EvalRunStatus.DONE,
    ]
    assert page["items"][0]["started_by_name"] == "Priya Hallam"
    assert set(page["items"][0]) == {
        "id",
        "mode",
        "status",
        "started_by_name",
        "config",
        "totals",
        "started_at",
        "finished_at",
        "error",
    }
    missing = client.get(f"{API}/evals/runs/{uuid.uuid4()}", headers=auth("director"))
    assert missing.status_code == 404


def test_run_detail_has_the_documented_shape(client: TestClient, auth, corpus):
    run = post_run(client, auth("director"), mode="retrieval", limit=1)
    detail = get_run(client, auth("director"), run["id"])
    assert set(detail) == {
        "id",
        "mode",
        "status",
        "started_by_name",
        "config",
        "totals",
        "started_at",
        "finished_at",
        "error",
        "results",
    }
    (row,) = detail["results"]
    assert set(row) == {
        "eval_question_id",
        "question",
        "answerable",
        "expected_document_title",
        "expected_pages",
        "retrieved_doc_hit",
        "retrieved_page_hit",
        "hit_rank",
        "answered",
        "cited_doc_hit",
        "cited_page_hit",
        "refused_correctly",
        "question_id",
        "latency_ms",
        "cost_usd",
    }
    assert row["cost_usd"] == "0.000000"
    assert isinstance(row["expected_pages"], list)
    assert Decimal(row["cost_usd"]) == 0


# --- the scoring rules ---------------------------------------------------------


def _question(**fields: Any) -> EvalQuestion:
    # Transient rows: the rules are pure functions of the question and a
    # ranking, so nothing needs to reach the database.
    fields.setdefault("expected_pages", [])
    return EvalQuestion(question="q", **fields)


def test_the_ranking_rules_on_a_hand_built_ranking():
    doc, other = uuid.uuid4(), uuid.uuid4()
    question = _question(answerable=True, expected_document_id=doc, expected_pages=[4, 5])

    # A page hit at rank 3 outranks the document hit at rank 1.
    assert evals.rank_hits(question, [(doc, 2), (other, 4), (doc, 5)]) == (True, True, 3)
    # Right document, wrong pages: the document rank stands in.
    assert evals.rank_hits(question, [(other, 1), (doc, 9)]) == (True, False, 2)
    assert evals.rank_hits(question, [(other, 4)]) == (False, False, None)
    assert evals.rank_hits(question, []) == (False, False, None)

    # No pages named: the page is unmeasured, not missed.
    pageless = _question(answerable=True, expected_document_id=doc)
    assert evals.rank_hits(pageless, [(other, 1), (doc, 1)]) == (True, None, 2)

    # Unanswerable, or the expected document deleted since: nothing to
    # measure, and every field says so.
    assert evals.rank_hits(_question(answerable=False), [(doc, 4)]) == (None, None, None)
    orphan = _question(answerable=True, expected_document_id=None, expected_pages=[4])
    assert evals.rank_hits(orphan, [(doc, 4)]) == (None, None, None)

    assert evals.citation_hits(question, [(doc, 4)]) == (True, True)
    assert evals.citation_hits(question, [(doc, 1), (other, 4)]) == (True, False)
    assert evals.citation_hits(question, []) == (False, False), "nothing cited is a miss"
    assert evals.citation_hits(pageless, [(doc, 1)]) == (True, None)
    assert evals.citation_hits(_question(answerable=False), [(doc, 1)]) == (None, None)
    assert evals.citation_hits(orphan, [(doc, 4)]) == (None, None)


def test_totals_are_rates_over_the_questions_they_apply_to():
    answerable = _question(answerable=True, expected_document_id=uuid.uuid4(), expected_pages=[1])
    unanswerable = _question(answerable=False)

    def result(question: EvalQuestion, latency_ms: int, cost: str, **fields: Any) -> EvalResult:
        return EvalResult(
            eval_question=question, latency_ms=latency_ms, cost_usd=Decimal(cost), **fields
        )

    results = [
        # Found, answered and cited on the right page.
        result(
            answerable,
            100,
            "0.001",
            retrieved_doc_hit=True,
            retrieved_page_hit=True,
            hit_rank=1,
            answered=True,
            cited_doc_hit=True,
            cited_page_hit=True,
        ),
        # Found at rank 4, answered from the wrong document: a false answer.
        result(
            answerable,
            300,
            "0.002",
            retrieved_doc_hit=True,
            retrieved_page_hit=False,
            hit_rank=4,
            answered=True,
            cited_doc_hit=False,
            cited_page_hit=False,
        ),
        # Not found and not answered.
        result(
            answerable,
            200,
            "0.0005",
            retrieved_doc_hit=False,
            retrieved_page_hit=False,
            hit_rank=None,
            answered=False,
            cited_doc_hit=False,
            cited_page_hit=False,
        ),
        # One refusal, one unanswerable question answered anyway.
        result(unanswerable, 50, "0", answered=False, refused_correctly=True),
        result(unanswerable, 1000, "0.0015", answered=True, refused_correctly=False),
    ]

    totals = evals.compute_totals(results, end_to_end=True)
    assert (totals["questions"], totals["answerable"], totals["unanswerable"]) == (5, 3, 2)
    assert totals["doc_hit_rate"] == pytest.approx(2 / 3)
    assert totals["page_hit_rate"] == pytest.approx(1 / 3)
    assert totals["mrr"] == pytest.approx((1 + 1 / 4 + 0) / 3)
    assert totals["answer_rate"] == pytest.approx(2 / 3)
    assert totals["citation_doc_hit_rate"] == pytest.approx(1 / 3)
    assert totals["citation_page_hit_rate"] == pytest.approx(1 / 3)
    assert totals["correct_refusal_rate"] == 0.5
    # The wrong-document answer and the answered unanswerable: two of five.
    assert totals["false_answer_rate"] == pytest.approx(2 / 5)
    assert totals["cost_usd"] == "0.005000"
    assert totals["latency_p50_ms"] == 200

    # A retrieval run carries only the retrieval numbers.
    assert set(evals.compute_totals(results, end_to_end=False)) == {
        "questions",
        "answerable",
        "unanswerable",
        "doc_hit_rate",
        "page_hit_rate",
        "mrr",
    }

    # An unmeasured page (no expected pages) drops out of the page rate
    # rather than dragging it down.
    unmeasured = result(answerable, 10, "0", retrieved_doc_hit=True, retrieved_page_hit=None)
    assert evals.compute_totals([unmeasured], end_to_end=False)["page_hit_rate"] is None

    # Nothing scored: every rate null, not zero.
    empty = evals.compute_totals([], end_to_end=True)
    assert empty["questions"] == 0
    assert empty["doc_hit_rate"] is None and empty["false_answer_rate"] is None
    assert empty["cost_usd"] == "0.000000" and empty["latency_p50_ms"] is None


# --- access --------------------------------------------------------------------


@pytest.mark.parametrize("role", ["admin", "manager", "viewer"])
def test_only_the_director_may_touch_evals(client: TestClient, auth, users, role):
    headers = auth(role)
    some_id = uuid.uuid4()
    attempts = [
        client.get(f"{API}/evals/questions", headers=headers),
        client.post(
            f"{API}/evals/questions",
            headers=headers,
            json={"question": "What is the rent?", "answerable": False},
        ),
        client.post(
            f"{API}/evals/questions/from-question",
            headers=headers,
            json={"question_id": str(some_id), "answerable": False},
        ),
        client.patch(f"{API}/evals/questions/{some_id}", headers=headers, json={"active": False}),
        client.get(f"{API}/evals/runs", headers=headers),
        client.post(f"{API}/evals/runs", headers=headers, json={"mode": "retrieval"}),
        client.get(f"{API}/evals/runs/{some_id}", headers=headers),
    ]
    for response in attempts:
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "forbidden"

    # And nothing was created or started along the way.
    assert client.get(f"{API}/evals/runs", headers=auth("director")).json()["total"] == 0
    assert client.get(f"{API}/evals/questions", headers=auth("director")).json()["total"] == 0


def test_a_retried_run_keeps_what_it_already_measured(
    db: Session, users, corpus, monkeypatch
):
    """An end-to-end run pays for every question it asks. A retry used to
    delete the results of the attempt before it and ask them all again."""
    director = users["director"]
    run = evals.create_run(db, mode=EvalMode.END_TO_END, user=director)
    job = db.execute(select(Job).where(Job.kind == JobKind.EVAL_RUN)).scalar_one()

    # Fail once, part-way: the third question raises.
    asked: list[str] = []
    real_ask = answering.ask_sync
    fail_on: int | None = 3

    def counted(db_, *, user, text, portfolio_id=None):
        asked.append(text)
        if fail_on is not None and len(asked) == fail_on:
            raise RuntimeError("the provider fell over")
        return real_ask(db_, user=user, text=text, portfolio_id=portfolio_id)

    monkeypatch.setattr(answering, "ask_sync", counted)
    with pytest.raises(RuntimeError):
        evals.run_eval_job(db, job)
    db.expire_all()

    measured_first = {r.eval_question_id for r in evals.run_results(db, db.get(EvalRun, run.id))}
    assert len(measured_first) == 2, "what it measured before falling over is kept"

    # The retry asks only what is left.
    fail_on = None
    asked.clear()
    evals.run_eval_job(db, job)
    db.expire_all()

    run = db.get(EvalRun, run.id)
    assert run.status == EvalRunStatus.DONE, run.error
    results = evals.run_results(db, run)
    assert len(results) == len(corpus["questions"])
    assert len(asked) == len(corpus["questions"]) - 2, "the first two were not asked again"
