"""The demo seed, at a size the suite can afford.

Three leases instead of forty-eight, under the stub answerer and the hashed
embeddings the rest of the suite uses. What is being checked is the shape --
every step runs, the result is complete, and running it again changes
nothing -- not the demo's numbers, which the real seed prints.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.demo.seed import DEMO_DOMAIN, DEMO_PASSWORD, seed
from app.models import (
    Chunk,
    Document,
    EvalQuestion,
    EvalRun,
    Extraction,
    Portfolio,
    Question,
    User,
)
from app.models.enums import DocumentStatus, ExtractionStatus

API = "/api/v1"


def _count(db: Session, model) -> int:
    return db.execute(select(func.count()).select_from(model)).scalar_one()


def _login(client: TestClient, key: str) -> dict[str, str]:
    response = client.post(
        f"{API}/auth/login",
        json={"email": f"{key}@{DEMO_DOMAIN}", "password": DEMO_PASSWORD},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_the_seed_builds_a_complete_small_demo(client: TestClient, db: Session):
    summary = seed(limit=3)

    assert summary["skipped"] is False
    assert _count(db, User) == 5
    assert _count(db, Portfolio) == 3

    documents = db.execute(select(Document)).scalars().all()
    assert len(documents) == 3
    assert all(d.status == DocumentStatus.READY for d in documents)
    assert all(d.page_count > 0 and d.chunk_count > 0 for d in documents)
    assert _count(db, Chunk) == sum(d.chunk_count for d in documents)
    embedded = db.execute(
        select(func.count()).select_from(Chunk).where(Chunk.embedding.is_not(None))
    ).scalar_one()
    assert embedded == _count(db, Chunk)

    extractions = db.execute(select(Extraction)).scalars().all()
    assert len(extractions) == 3
    assert all(x.status == ExtractionStatus.DONE for x in extractions)

    # Three or four golden questions per lease plus the twelve unanswerable ones.
    answerable = db.execute(
        select(func.count()).select_from(EvalQuestion).where(EvalQuestion.answerable.is_(True))
    ).scalar_one()
    unanswerable = _count(db, EvalQuestion) - answerable
    assert 9 <= answerable <= 12
    assert unanswerable == 12

    assert _count(db, Question) > 0
    run = db.execute(select(EvalRun)).scalars().one()
    assert run.status == "done"
    assert run.totals["questions"] == _count(db, EvalQuestion)


def test_running_the_seed_twice_changes_nothing(client: TestClient, db: Session):
    models = (User, Portfolio, Document, Chunk, Extraction, EvalQuestion, Question, EvalRun)
    seed(limit=3)
    before = {model.__name__: _count(db, model) for model in models}

    summary = seed(limit=3)

    assert summary["skipped"] is True
    after = {model.__name__: _count(db, model) for model in models}
    assert after == before


def test_the_seeded_manager_cannot_see_riverside(client: TestClient, db: Session):
    seed(limit=3, extraction=False)
    riverside = db.execute(select(Portfolio).where(Portfolio.name == "Riverside")).scalar_one()
    hidden = db.execute(
        select(Document).where(Document.portfolio_id == riverside.id)
    ).scalars().first()
    assert hidden is not None, "the three-lease seed should include one Riverside lease"

    director = _login(client, "director")
    manager = _login(client, "manager")

    assert client.get(f"{API}/documents/{hidden.id}", headers=director).status_code == 200
    # 404, not 403: confirming the document exists would leak that it does.
    assert client.get(f"{API}/documents/{hidden.id}", headers=manager).status_code == 404
    listed = client.get(f"{API}/documents?limit=50", headers=manager).json()
    assert all(item["id"] != str(hidden.id) for item in listed["items"])


def test_the_seeded_viewer_cannot_ask(client: TestClient):
    seed(limit=3, extraction=False)
    viewer = _login(client, "finance")

    response = client.post(
        f"{API}/ask?stream=false", json={"question": "When does the term expire?"}, headers=viewer
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_a_half_finished_seed_is_finished_rather_than_believed(
    client: TestClient, db: Session
):
    """A seeder killed part-way leaves a handful of leases behind. Treating
    that as "already seeded" is how a demo ends up with eight of forty-eight
    documents for ever, which is what the first deployment did."""
    seed(limit=3, extraction=False)
    # Lose the last document, as a container running out of memory would.
    victim = db.execute(select(Document).order_by(Document.created_at.desc())).scalars().first()
    db.execute(delete(Document).where(Document.id == victim.id))
    db.commit()
    assert _count(db, Document) == 2

    summary = seed(limit=3, extraction=False)

    assert summary["skipped"] is False, "a partial seed is not a finished one"
    assert _count(db, Document) == 3
    assert all(d.status == DocumentStatus.READY for d in db.execute(select(Document)).scalars())
    # And the stages that had already run did not run twice.
    questions = db.execute(
        select(EvalQuestion.expected_document_id).where(
            EvalQuestion.expected_document_id.is_not(None)
        )
    ).scalars()
    counted: dict[str, int] = {}
    for document_id in questions:
        counted[str(document_id)] = counted.get(str(document_id), 0) + 1
    assert counted and max(counted.values()) <= 4, "one set of golden questions per lease"
