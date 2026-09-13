"""Asking: the stream, the citation check, the refusal, and who sees what.

The stub answerer is real enough for most of this -- it cites what it quotes
-- and a scripted provider stands in where the behaviour under test is what
the service does with a model that cites something it was not shown.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import Citation, Feedback, PortfolioMember, Question, QuestionSource, UsageEvent
from app.models.enums import QuestionStatus
from app.providers import llm
from app.providers.llm import CitationDelta, Finished, Source, TextDelta, Usage
from app.providers.stub_llm import REFUSAL_TEXT
from app.services import answering
from tests.conftest import API
from tests.test_retrieval import BREAK_CLAUSE, RENT_CLAUSE, TERM_CLAUSE, ready_document

BREAK_QUESTION = "What notice must the tenant give to determine the lease on 31 March 2029?"
UNRELATED_QUESTION = "How many employees does the tenant have on the payroll?"


class ScriptedLLM:
    """A provider that says exactly what the test tells it to."""

    name = "scripted"
    model = "extractive-v1"

    def __init__(self, events: list[Any]):
        self.events = events
        self.calls = 0
        self.seen: list[Source] = []

    def answer(self, question: str, sources: list[Source]) -> Iterator[Any]:
        self.calls += 1
        self.seen = sources
        yield from self.events

    def extract(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


def parse_sse(body: str) -> list[tuple[str, Any]]:
    events = []
    for block in body.strip().split("\n\n"):
        name, data = None, []
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data.append(line[len("data: ") :])
        events.append((name, json.loads("\n".join(data))))
    return events


@pytest.fixture
def lease(db: Session, portfolios):
    return ready_document(
        db,
        portfolios["city"],
        "Meridian House",
        [[TERM_CLAUSE, RENT_CLAUSE], [BREAK_CLAUSE]],
    )


def _ask(client: TestClient, headers: dict[str, str], question: str, **params: Any):
    return client.post(f"{API}/ask", headers=headers, json={"question": question}, params=params)


# --- the stream ---------------------------------------------------------


def test_the_stream_arrives_in_order_and_matches_the_stored_question(
    client: TestClient, auth, lease
):
    response = _ask(client, auth("manager"), BREAK_QUESTION)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"

    events = parse_sse(response.text)
    names = [name for name, _ in events]
    assert names[0] == "retrieval"
    assert names[-1] == "done"
    assert "text" in names and "citation" in names
    # Text is streamed before its citation, and nothing follows done.
    assert names.index("text") < names.index("citation")

    retrieval_event = events[0][1]
    assert retrieval_event["sources"], "the passages shown to the model come first"
    assert {"index", "chunk_id", "document_title", "page_number", "snippet"} <= set(
        retrieval_event["sources"][0]
    )

    done = events[-1][1]
    assert done["id"] == retrieval_event["question_id"]
    stored = client.get(f"{API}/questions/{done['id']}", headers=auth("manager")).json()
    assert stored == done


def test_the_stub_answers_a_matching_question_with_a_citation(
    client: TestClient, auth, lease, db: Session
):
    events = parse_sse(_ask(client, auth("manager"), BREAK_QUESTION).text)
    done = dict(events)["done"]

    assert done["status"] == QuestionStatus.ANSWERED
    assert done["citations"], "an answer without a citation is not an answer"
    citation = done["citations"][0]
    assert citation["ordinal"] == 1
    assert citation["document_title"] == "Meridian House"
    assert citation["page_number"] == 2
    assert citation["cited_text"] == BREAK_CLAUSE
    assert done["answer_blocks"][0]["citations"] == [1]
    assert BREAK_CLAUSE in done["answer_text"]

    # The term clause shares "31 March" with the question, so the stub cites
    # it too; what matters is that "cited" on a source means exactly that.
    cited = {s["chunk_id"] for s in done["sources"] if s["cited"]}
    assert cited == {c["chunk_id"] for c in done["citations"]}
    assert citation["chunk_id"] in cited
    assert done["dropped_citations"] == 0
    assert done["provider"] == "stub" and done["model"] == "extractive-v1"
    assert done["cost_usd"] == "0.000000"
    assert done["latency_ms"] is not None and done["finished_at"] is not None

    citation_events = [data for name, data in events if name == "citation"]
    assert citation_events[0]["ordinal"] == 1
    assert citation_events[0]["cited_text"] == BREAK_CLAUSE

    assert db.execute(select(func.count()).select_from(Citation)).scalar_one() == len(
        done["citations"]
    )
    assert db.execute(select(func.count()).select_from(QuestionSource)).scalar_one() == len(
        done["sources"]
    )


def test_an_unrelated_question_is_unanswered_with_no_citations(client: TestClient, auth, lease):
    """The passages are still shown -- that is the 'closest passages' panel --
    but nothing is cited and the status says so."""
    events = parse_sse(_ask(client, auth("manager"), UNRELATED_QUESTION).text)
    done = dict(events)["done"]

    assert done["status"] == QuestionStatus.UNANSWERED
    assert done["citations"] == []
    assert done["answer_text"] == REFUSAL_TEXT
    assert done["sources"], "the closest passages are still recorded"
    assert not any(s["cited"] for s in done["sources"])
    assert "citation" not in dict(events)


def test_no_documents_in_scope_finishes_without_calling_the_model(
    client: TestClient, auth, portfolios, db: Session, monkeypatch
):
    scripted = ScriptedLLM([])
    monkeypatch.setattr(llm, "get_llm_provider", lambda: scripted)

    events = parse_sse(_ask(client, auth("manager"), BREAK_QUESTION).text)
    done = dict(events)["done"]

    assert done["status"] == QuestionStatus.UNANSWERED
    assert done["answer_text"] == answering.NO_DOCUMENTS_TEXT
    assert done["sources"] == [] and done["citations"] == []
    assert scripted.calls == 0
    assert db.execute(select(func.count()).select_from(UsageEvent)).scalar_one() == 0


# --- the citation check -------------------------------------------------


def test_citations_the_model_was_not_shown_are_dropped_and_counted(
    client: TestClient, auth, lease, monkeypatch
):
    """One citation to a source that does not exist, one to a block range
    outside the source, one good. The good one survives with ordinal 1."""
    scripted = ScriptedLLM(
        [
            TextDelta(0, "The break needs six months' notice. "),
            CitationDelta(0, 99, 0, 1, "invented"),
            CitationDelta(0, 0, 0, 50, "too long"),
            CitationDelta(0, 0, 0, 1, "a real sentence"),
            Finished("end_turn", Usage(input_tokens=100, output_tokens=20), "extractive-v1"),
        ]
    )
    monkeypatch.setattr(llm, "get_llm_provider", lambda: scripted)

    events = parse_sse(_ask(client, auth("manager"), BREAK_QUESTION).text)
    done = dict(events)["done"]

    assert done["status"] == QuestionStatus.ANSWERED
    assert done["dropped_citations"] == 2
    assert [c["ordinal"] for c in done["citations"]] == [1]
    assert done["citations"][0]["chunk_id"] == str(scripted.seen[0].chunk_id)
    assert len([name for name, _ in events if name == "citation"]) == 1


def test_an_answer_whose_every_citation_was_dropped_is_unanswered(
    client: TestClient, auth, lease, monkeypatch
):
    scripted = ScriptedLLM(
        [
            TextDelta(0, "Confidently wrong. "),
            CitationDelta(0, 7, 0, 1, "nothing"),
            Finished("end_turn", Usage(), "extractive-v1"),
        ]
    )
    monkeypatch.setattr(llm, "get_llm_provider", lambda: scripted)

    done = dict(parse_sse(_ask(client, auth("manager"), BREAK_QUESTION).text))["done"]

    assert done["status"] == QuestionStatus.UNANSWERED
    assert done["citations"] == []
    assert done["dropped_citations"] == 1
    assert done["answer_text"] == "Confidently wrong."


def test_the_same_chunk_cited_twice_keeps_one_label(client: TestClient, auth, lease, monkeypatch):
    scripted = ScriptedLLM(
        [
            TextDelta(0, "Six months. "),
            CitationDelta(0, 0, 0, 1, "first"),
            TextDelta(1, "In writing. "),
            CitationDelta(1, 0, 0, 1, "again"),
            Finished("end_turn", Usage(), "extractive-v1"),
        ]
    )
    monkeypatch.setattr(llm, "get_llm_provider", lambda: scripted)

    done = dict(parse_sse(_ask(client, auth("manager"), BREAK_QUESTION).text))["done"]

    assert [c["ordinal"] for c in done["citations"]] == [1, 1]
    assert [b["citations"] for b in done["answer_blocks"]] == [[1], [1]]
    assert done["answer_text"] == "Six months.\nIn writing."


def test_a_refusal_is_recorded_as_failed(client: TestClient, auth, lease, monkeypatch):
    scripted = ScriptedLLM([Finished("refusal", Usage(input_tokens=50), "extractive-v1")])
    monkeypatch.setattr(llm, "get_llm_provider", lambda: scripted)

    done = dict(parse_sse(_ask(client, auth("manager"), BREAK_QUESTION).text))["done"]

    assert done["status"] == QuestionStatus.FAILED
    assert done["error"]
    assert done["citations"] == []
    assert done["input_tokens"] == 50, "a refused call still cost tokens, and they are recorded"


def test_a_provider_that_blows_up_yields_an_error_event_and_a_failed_question(
    client: TestClient, auth, lease, db: Session, monkeypatch
):
    def explode(question: str, sources: list[Source]) -> Iterator[Any]:
        yield TextDelta(0, "Starting... ")
        raise RuntimeError("connection reset")

    scripted = ScriptedLLM([])
    scripted.answer = explode  # type: ignore[method-assign]
    monkeypatch.setattr(llm, "get_llm_provider", lambda: scripted)

    events = parse_sse(_ask(client, auth("manager"), BREAK_QUESTION).text)

    assert events[-1][0] == "error"
    error = events[-1][1]
    assert error["code"] == "answer_failed"
    question = db.get(Question, uuid.UUID(error["question_id"]))
    assert question.status == QuestionStatus.FAILED
    assert "connection reset" in question.error


# --- the non-streaming variant and the service ---------------------------


def test_stream_false_returns_the_answer_as_json(client: TestClient, auth, lease):
    response = _ask(client, auth("manager"), BREAK_QUESTION, stream="false")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["status"] == QuestionStatus.ANSWERED
    assert body["citations"]
    assert isinstance(body["cost_usd"], str)


def test_ask_sync_returns_the_same_record(db: Session, users, lease):
    answer = answering.ask_sync(db, user=users["manager"], text=BREAK_QUESTION)

    assert answer.status == QuestionStatus.ANSWERED
    assert answer.citations[0].document_id == lease.id
    assert answer.citations[0].page_number == 2
    assert db.get(Question, answer.id) is not None


def test_a_question_that_is_too_short_or_too_long_is_refused(
    client: TestClient, auth, lease, db: Session, users
):
    assert _ask(client, auth("manager"), "hi").status_code == 422
    assert _ask(client, auth("manager"), "x" * 2001).status_code == 422
    with pytest.raises(answering.InvalidQuestion):
        answering.ask(db, user=users["manager"], text="  ")
    assert db.execute(select(func.count()).select_from(Question)).scalar_one() == 0


# --- who may ask, who may see -------------------------------------------


def test_a_viewer_cannot_ask(client: TestClient, auth, lease):
    response = _ask(client, auth("viewer"), BREAK_QUESTION)

    assert response.status_code == 403
    assert response.json()["error"]["detail"]["your_role"] == "viewer"


def test_a_scope_the_caller_cannot_see_is_404_not_403(client: TestClient, auth, portfolios, lease):
    response = client.post(
        f"{API}/ask",
        headers=auth("manager"),
        json={"question": BREAK_QUESTION, "portfolio_id": str(portfolios["riverside"].id)},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_scoping_to_a_portfolio_searches_only_that_portfolio(
    client: TestClient, auth, portfolios, db: Session
):
    ready_document(db, portfolios["city"], "Meridian House", [[BREAK_CLAUSE]])
    ready_document(db, portfolios["north"], "Whitworth Court", [[BREAK_CLAUSE]])

    response = client.post(
        f"{API}/ask",
        headers=auth("manager"),
        json={"question": BREAK_QUESTION, "portfolio_id": str(portfolios["north"].id)},
        params={"stream": "false"},
    )

    body = response.json()
    assert body["portfolio_id"] == str(portfolios["north"].id)
    assert {s["document_title"] for s in body["sources"]} == {"Whitworth Court"}


def test_questions_are_own_for_a_manager_and_everybody_s_for_the_director(
    client: TestClient, auth, lease, users
):
    _ask(client, auth("manager"), BREAK_QUESTION, stream="false")
    _ask(client, auth("admin"), UNRELATED_QUESTION, stream="false")

    manager = client.get(f"{API}/questions", headers=auth("manager")).json()
    director = client.get(f"{API}/questions", headers=auth("director")).json()
    filtered = client.get(
        f"{API}/questions", headers=auth("director"), params={"user_id": str(users["admin"].id)}
    ).json()
    # A manager asking for somebody else's still gets their own.
    sneaky = client.get(
        f"{API}/questions", headers=auth("manager"), params={"user_id": str(users["admin"].id)}
    ).json()

    assert manager["total"] == 1
    assert manager["items"][0]["user_name"] == "Sadia Mahmood"
    assert director["total"] == 2
    assert [q["text"] for q in director["items"]] == [UNRELATED_QUESTION, BREAK_QUESTION]
    assert filtered["total"] == 1 and filtered["items"][0]["user_name"] == "Owen Pryce-Reid"
    assert sneaky["total"] == 1 and sneaky["items"][0]["user_name"] == "Sadia Mahmood"


def test_another_users_question_is_404(client: TestClient, auth, lease):
    asked = _ask(client, auth("admin"), BREAK_QUESTION, stream="false").json()

    as_manager = client.get(f"{API}/questions/{asked['id']}", headers=auth("manager"))
    as_director = client.get(f"{API}/questions/{asked['id']}", headers=auth("director"))
    missing = client.get(f"{API}/questions/{uuid.uuid4()}", headers=auth("director"))

    assert as_manager.status_code == 404
    assert as_director.status_code == 200
    assert missing.status_code == 404
    assert as_manager.json() == missing.json(), "the same answer whether it exists or not"


# --- feedback -----------------------------------------------------------


def test_feedback_is_one_verdict_per_person_and_replaces_itself(
    client: TestClient, auth, lease, db: Session
):
    asked = _ask(client, auth("manager"), BREAK_QUESTION, stream="false").json()
    url = f"{API}/questions/{asked['id']}/feedback"

    first = client.post(url, headers=auth("manager"), json={"verdict": "up"})
    second = client.post(
        url,
        headers=auth("manager"),
        json={"verdict": "down", "reason": "incomplete", "note": "Missed the notice period."},
    )
    by_director = client.post(url, headers=auth("director"), json={"verdict": "up"})

    assert first.status_code == 200, first.text
    assert first.json()["feedback"] == {"verdict": "up", "reason": None, "note": None}
    assert second.json()["feedback"] == {
        "verdict": "down",
        "reason": "incomplete",
        "note": "Missed the notice period.",
    }
    # The director's verdict is their own; the manager still sees theirs.
    assert by_director.json()["feedback"]["verdict"] == "up"
    mine = client.get(f"{API}/questions/{asked['id']}", headers=auth("manager")).json()
    assert mine["feedback"]["verdict"] == "down"
    assert db.execute(select(func.count()).select_from(Feedback)).scalar_one() == 2


def test_feedback_needs_the_ask_role_and_a_visible_question(client: TestClient, auth, lease):
    asked = _ask(client, auth("admin"), BREAK_QUESTION, stream="false").json()
    url = f"{API}/questions/{asked['id']}/feedback"

    assert client.post(url, headers=auth("viewer"), json={"verdict": "up"}).status_code == 403
    assert client.post(url, headers=auth("manager"), json={"verdict": "up"}).status_code == 404
    assert client.post(url, headers=auth("admin"), json={"verdict": "sideways"}).status_code == 422


def test_a_question_goes_where_its_evidence_went(
    client: TestClient, db: Session, auth, users, portfolios, lease
):
    """An answer quotes the lease it came from -- titles, pages, sentences --
    so a stored question carries lease text with it. When somebody is taken
    off a portfolio, their old questions about it have to go the same way the
    documents did, or the history is the leak the access rules prevent."""
    manager = auth("manager")
    answer = _ask(client, manager, BREAK_QUESTION, stream="false").json()
    assert answer["status"] == QuestionStatus.ANSWERED
    question_id = answer["id"]

    assert client.get(f"{API}/questions/{question_id}", headers=manager).status_code == 200
    assert client.get(f"{API}/questions", headers=manager).json()["total"] == 1

    # The lease was in City Centre; the manager is taken off it.
    db.execute(
        delete(PortfolioMember).where(
            PortfolioMember.user_id == users["manager"].id,
            PortfolioMember.portfolio_id == portfolios["city"].id,
        )
    )
    db.commit()

    assert client.get(f"{API}/questions/{question_id}", headers=manager).status_code == 404
    assert client.get(f"{API}/questions", headers=manager).json()["total"] == 0
    # The director, who sees every portfolio, still sees the question.
    assert client.get(f"{API}/questions/{question_id}", headers=auth("director")).status_code == 200
