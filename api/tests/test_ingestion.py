"""Uploads, the queue, the worker, and the documents API.

The two things worth proving here are the ones that only a real database
can prove: that ``FOR UPDATE SKIP LOCKED`` hands each job to exactly one
claimant when two are racing, and that a document in a portfolio you cannot
see is absent -- not forbidden -- on every route, delete included.
"""

from __future__ import annotations

import sys
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models import (
    Chunk,
    Citation,
    Document,
    DocumentBlob,
    DocumentPage,
    Job,
    PortfolioMember,
    Question,
)
from app.models.enums import DocumentStatus, JobKind, JobStatus, QuestionStatus
from app.providers import embeddings
from app.services import ingestion
from tests.conftest import API

#: Three short pages with one canonical fact each, so a test can say which
#: page a passage came from without guessing at the chunker's boundaries.
LEASE_PAGES = [
    "1. Parties. This Lease is made between Meridian Estates Limited as Landlord and "
    "Whitworth Analytics Limited as Tenant.\n"
    "2. Term. The Term shall commence on 1 April 2024 and shall expire on 31 March 2034.",
    "3. Rent. The Initial Rent is £42,500 (forty-two thousand five hundred pounds) per "
    "annum, payable quarterly in advance on the usual quarter days.",
    "10. Break Clause. The Tenant may determine this Lease on 31 March 2029 by giving "
    "the Landlord not less than 6 months' prior written notice.",
]

CORRUPT_PDF = b"%PDF-1.7\nthis has the magic bytes and nothing else a parser could use\n"


def _long_page(number: int, sentences: int = 45) -> str:
    """About 3,600 characters: three or four chunks at the default target,
    and still comfortably one physical page in the test PDF builder."""
    return " ".join(
        f"Clause {number}.{i}. The Tenant shall keep the Premises in good repair at all times."
        for i in range(1, sentences + 1)
    )


def _jobs(db: Session, document_id: uuid.UUID | str) -> list[Job]:
    return list(
        db.execute(
            select(Job)
            .where(Job.document_id == uuid.UUID(str(document_id)))
            .order_by(Job.created_at, Job.id)
        ).scalars()
    )


def _count(db: Session, model, document_id: uuid.UUID | str) -> int:
    return db.execute(
        select(func.count())
        .select_from(model)
        .where(model.document_id == uuid.UUID(str(document_id)))
    ).scalar_one()


def _get(client: TestClient, headers: dict[str, str], document_id: str) -> dict:
    response = client.get(f"{API}/documents/{document_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


# --- upload and ingest ---------------------------------------------------


def test_an_upload_is_queued_then_ingested_to_ready(
    client: TestClient, auth, upload, ingest_all, db: Session
):
    doc = upload("admin", "city", LEASE_PAGES, title="Meridian House, Unit 4")
    assert doc["status"] == DocumentStatus.QUEUED
    assert doc["page_count"] == 0 and doc["chunk_count"] == 0
    assert doc["portfolio_name"] == "City Centre"
    assert doc["uploaded_by_name"] == "Owen Pryce-Reid"
    assert doc["extraction_status"] == "none" and doc["review_pending"] == 0
    assert doc["metadata"] == {}

    (job,) = _jobs(db, doc["id"])
    assert (job.kind, job.status, job.attempts) == (JobKind.INGEST, JobStatus.QUEUED, 0)

    assert ingest_all() == 1

    ready = _get(client, auth("admin"), doc["id"])
    assert ready["status"] == DocumentStatus.READY
    assert ready["page_count"] == 3
    assert ready["chunk_count"] >= 3
    assert ready["error"] is None

    db.refresh(job)
    assert job.status == JobStatus.DONE
    assert job.stage == "embed"
    assert job.attempts == 1
    assert job.claimed_by and job.claimed_by.endswith(":drain")
    assert job.finished_at is not None and job.error is None


def test_ingest_embeds_every_chunk_in_batches_of_32(upload, ingest_all, db: Session, monkeypatch):
    provider = embeddings.get_embedding_provider()
    original = provider.embed_documents
    batch_sizes: list[int] = []

    def spy(texts: list[str]) -> list[list[float]]:
        batch_sizes.append(len(texts))
        return original(texts)

    monkeypatch.setattr(provider, "embed_documents", spy)

    doc = upload("admin", "city", [_long_page(n) for n in range(1, 13)])
    ingest_all()

    document = db.get(Document, uuid.UUID(doc["id"]))
    assert document.status == DocumentStatus.READY
    assert document.page_count == 12
    assert document.chunk_count > 32, "the fixture must be big enough to need two batches"

    unembedded = db.execute(
        select(func.count())
        .select_from(Chunk)
        .where(Chunk.document_id == document.id, Chunk.embedding.is_(None))
    ).scalar_one()
    assert unembedded == 0
    assert sum(batch_sizes) == document.chunk_count
    assert max(batch_sizes) == 32
    assert len(batch_sizes) == -(-document.chunk_count // 32)

    first = db.execute(
        select(Chunk).where(Chunk.document_id == document.id).order_by(Chunk.ordinal).limit(1)
    ).scalar_one()
    assert len(first.embedding) == 384


def test_pages_endpoint_returns_text_in_page_order(client: TestClient, auth, upload, ingest_all):
    doc = upload("admin", "city", LEASE_PAGES)
    ingest_all()

    response = client.get(f"{API}/documents/{doc['id']}/pages", headers=auth("viewer"))
    assert response.status_code == 200, response.text
    pages = response.json()
    assert [p["page_number"] for p in pages] == [1, 2, 3]
    assert "Meridian Estates" in pages[0]["text"]
    assert "42,500" in pages[1]["text"]
    assert "31 March 2029" in pages[2]["text"]


def test_chunks_are_listed_per_page_and_a_chunk_carries_its_blocks(
    client: TestClient, auth, upload, ingest_all
):
    from app.services.chunking import split_sentences

    doc = upload("admin", "city", LEASE_PAGES, title="Meridian House")
    ingest_all()
    headers = auth("manager")

    everything = client.get(f"{API}/documents/{doc['id']}/chunks", headers=headers).json()
    assert [c["ordinal"] for c in everything] == list(range(len(everything)))
    assert {c["page_number"] for c in everything} == {1, 2, 3}

    page_three = client.get(
        f"{API}/documents/{doc['id']}/chunks?page=3", headers=headers
    ).json()
    assert page_three and all(c["page_number"] == 3 for c in page_three)
    assert any("31 March 2029" in c["text"] for c in page_three)

    chunk = page_three[0]
    response = client.get(f"{API}/chunks/{chunk['id']}", headers=headers)
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["document_id"] == doc["id"]
    assert detail["document_title"] == "Meridian House"
    assert detail["ordinal"] == chunk["ordinal"]
    # The blocks are what a citation's block range indexes into, so they
    # must be exactly what the same function gives at answer time.
    assert detail["blocks"] == split_sentences(chunk["text"])
    assert len(detail["blocks"]) >= 1


# --- duplicates, validation and roles ------------------------------------


def test_duplicate_bytes_in_the_same_portfolio_return_the_same_document(
    client: TestClient, auth, make_pdf, portfolios, db: Session
):
    pdf = make_pdf(LEASE_PAGES)
    headers = auth("admin")
    data = {"portfolio_id": str(portfolios["city"].id)}

    first = client.post(
        f"{API}/documents",
        headers=headers,
        files={"file": ("lease.pdf", pdf, "application/pdf")},
        data=data,
    )
    assert first.status_code == 201, first.text
    assert first.json()["created"] is True

    second = client.post(
        f"{API}/documents",
        headers=headers,
        files={"file": ("lease-again.pdf", pdf, "application/pdf")},
        data={**data, "title": "A different title changes nothing"},
    )
    assert second.status_code == 200, second.text
    assert second.json()["created"] is False
    assert second.json()["document"]["id"] == first.json()["document"]["id"]
    assert second.json()["document"]["filename"] == "lease.pdf"

    assert db.execute(select(func.count()).select_from(Document)).scalar_one() == 1
    assert db.execute(select(func.count()).select_from(Job)).scalar_one() == 1


def test_the_same_bytes_in_another_portfolio_are_a_second_document(upload, db: Session):
    city = upload("admin", "city", LEASE_PAGES)
    north = upload("admin", "north", LEASE_PAGES)
    assert city["id"] != north["id"]

    rows = db.execute(select(Document.content_sha256)).scalars().all()
    assert len(rows) == 2 and len(set(rows)) == 1
    assert db.execute(select(func.count()).select_from(Job)).scalar_one() == 2


def test_a_file_that_is_not_a_pdf_is_refused(client: TestClient, auth, portfolios, db: Session):
    response = client.post(
        f"{API}/documents",
        headers=auth("admin"),
        files={"file": ("lease.pdf", b"Not a PDF at all", "application/pdf")},
        data={"portfolio_id": str(portfolios["city"].id)},
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "unprocessable_upload"
    assert db.execute(select(func.count()).select_from(Document)).scalar_one() == 0


def test_an_oversized_upload_is_refused_before_it_is_stored(
    client: TestClient, auth, portfolios, db: Session, monkeypatch
):
    monkeypatch.setattr(get_settings(), "max_upload_mb", 1)
    too_big = b"%PDF-1.4\n" + b"0" * (1024 * 1024)

    response = client.post(
        f"{API}/documents",
        headers=auth("director"),
        files={"file": ("huge.pdf", too_big, "application/pdf")},
        data={"portfolio_id": str(portfolios["city"].id)},
    )
    assert response.status_code == 413, response.text
    assert response.json()["error"]["code"] == "file_too_large"
    assert db.execute(select(func.count()).select_from(Document)).scalar_one() == 0


def test_title_defaults_to_the_filename_and_doc_type_is_validated(
    client: TestClient, auth, make_pdf, portfolios
):
    headers = auth("admin")
    data = {"portfolio_id": str(portfolios["north"].id), "doc_type": "side_letter"}
    response = client.post(
        f"{API}/documents",
        headers=headers,
        files={"file": ("Deansgate Quays - Unit 7.pdf", make_pdf(LEASE_PAGES), "application/pdf")},
        data=data,
    )
    assert response.status_code == 201, response.text
    document = response.json()["document"]
    assert document["title"] == "Deansgate Quays - Unit 7"
    assert document["doc_type"] == "side_letter"

    bad = client.post(
        f"{API}/documents",
        headers=headers,
        files={"file": ("memo.pdf", make_pdf(["A memo."]), "application/pdf")},
        data={**data, "doc_type": "memo"},
    )
    assert bad.status_code == 422, bad.text
    assert bad.json()["error"]["code"] == "validation_error"


def test_a_manager_cannot_upload(client: TestClient, auth, make_pdf, portfolios):
    response = client.post(
        f"{API}/documents",
        headers=auth("manager"),
        files={"file": ("lease.pdf", make_pdf(LEASE_PAGES), "application/pdf")},
        data={"portfolio_id": str(portfolios["city"].id)},
    )
    assert response.status_code == 403, response.text
    body = response.json()["error"]
    assert body["code"] == "forbidden"
    assert body["detail"] == {"your_role": "manager", "required": ["admin", "director"]}


def test_a_viewer_can_list_but_not_upload(client: TestClient, auth, upload, make_pdf, portfolios):
    doc = upload("admin", "city", LEASE_PAGES, title="Whitworth Court, Unit 2B")

    listing = client.get(f"{API}/documents", headers=auth("viewer"))
    assert listing.status_code == 200, listing.text
    assert listing.json()["total"] == 1
    assert listing.json()["items"][0]["id"] == doc["id"]

    response = client.post(
        f"{API}/documents",
        headers=auth("viewer"),
        files={"file": ("lease.pdf", make_pdf(LEASE_PAGES), "application/pdf")},
        data={"portfolio_id": str(portfolios["city"].id)},
    )
    assert response.status_code == 403, response.text


def test_a_document_in_a_non_member_portfolio_is_404_on_every_route(
    client: TestClient, auth, upload, users, portfolios, db: Session
):
    doc = upload("director", "riverside", LEASE_PAGES, title="Riverside Wharf")

    # The admin is a member of every portfolio in the fixtures; take the
    # confidential one away so there is an upload role who cannot see it.
    db.execute(
        delete(PortfolioMember).where(
            PortfolioMember.user_id == users["admin"].id,
            PortfolioMember.portfolio_id == portfolios["riverside"].id,
        )
    )
    db.commit()

    for role in ("admin", "manager", "viewer"):
        headers = auth(role)
        assert client.get(f"{API}/documents/{doc['id']}", headers=headers).status_code == 404
        assert client.get(f"{API}/documents/{doc['id']}/pages", headers=headers).status_code == 404
        assert client.get(f"{API}/documents/{doc['id']}/chunks", headers=headers).status_code == 404
        listing = client.get(
            f"{API}/documents?portfolio_id={portfolios['riverside'].id}", headers=headers
        )
        assert listing.status_code == 404

    # Not 403: a 403 would tell the admin there is a lease they may not see.
    deletion = client.delete(f"{API}/documents/{doc['id']}", headers=auth("admin"))
    assert deletion.status_code == 404, deletion.text
    assert (
        client.post(f"{API}/documents/{doc['id']}/reingest", headers=auth("admin")).status_code
        == 404
    )
    assert db.get(Document, uuid.UUID(doc["id"])) is not None

    for role in ("riverside_manager", "director"):
        assert client.get(f"{API}/documents/{doc['id']}", headers=auth(role)).status_code == 200


def test_listing_filters_by_portfolio_status_and_query(
    client: TestClient, auth, upload, ingest_all, portfolios
):
    meridian = upload("admin", "city", LEASE_PAGES, title="Meridian House")
    upload("admin", "city", [_long_page(1)], title="Whitworth Court")
    upload("admin", "north", [_long_page(2)], title="Trafford Park, Unit 9")
    assert ingest_all(limit=1) == 1  # the oldest job first: Meridian is ready

    headers = auth("admin")

    def total(query: str = "") -> int:
        response = client.get(f"{API}/documents{query}", headers=headers)
        assert response.status_code == 200, response.text
        return response.json()["total"]

    assert total() == 3
    assert total("?status=ready") == 1
    assert total("?status=queued") == 2
    assert total("?q=whitworth") == 1
    assert total("?q=unit") == 1

    assert total(f"?portfolio_id={portfolios['city'].id}") == 2
    assert total(f"?portfolio_id={portfolios['north'].id}&status=queued") == 1
    page = client.get(f"{API}/documents?limit=1&offset=1", headers=headers).json()
    assert page["total"] == 3 and len(page["items"]) == 1 and page["limit"] == 1

    ready = client.get(f"{API}/documents?status=ready", headers=headers).json()["items"]
    assert [d["id"] for d in ready] == [meridian["id"]]

    assert client.get(f"{API}/documents", headers=auth("manager")).json()["total"] == 3
    assert client.get(f"{API}/documents", headers=auth("viewer")).json()["total"] == 2
    assert client.get(f"{API}/documents", headers=auth("riverside_manager")).json()["total"] == 0
    assert client.get(f"{API}/documents?status=bogus", headers=headers).status_code == 422


# --- failures and retries ------------------------------------------------


def _upload_corrupt(client: TestClient, headers: dict[str, str], portfolio_id: uuid.UUID) -> dict:
    response = client.post(
        f"{API}/documents",
        headers=headers,
        files={"file": ("broken.pdf", CORRUPT_PDF, "application/pdf")},
        data={"portfolio_id": str(portfolio_id)},
    )
    assert response.status_code == 201, response.text
    return response.json()["document"]


def test_a_corrupt_pdf_ends_failed_with_the_error_kept(
    client: TestClient, auth, portfolios, db: Session, ingest_all
):
    doc = _upload_corrupt(client, auth("admin"), portfolios["city"].id)
    (job,) = _jobs(db, doc["id"])
    job.max_attempts = 1
    db.commit()

    assert ingest_all() == 1

    db.refresh(job)
    assert job.status == JobStatus.FAILED
    assert job.attempts == 1
    assert job.stage == "parse"
    assert job.error and job.finished_at is not None

    failed = _get(client, auth("admin"), doc["id"])
    assert failed["status"] == DocumentStatus.FAILED
    assert failed["error"] == job.error


def test_a_failed_attempt_is_requeued_until_max_attempts(
    client: TestClient, auth, portfolios, db: Session, ingest_all
):
    doc = _upload_corrupt(client, auth("admin"), portfolios["city"].id)
    (job,) = _jobs(db, doc["id"])
    assert job.max_attempts == 3

    assert ingest_all(limit=1) == 1
    db.refresh(job)
    assert (job.status, job.attempts) == (JobStatus.QUEUED, 1)
    assert job.error and job.finished_at is None
    # Not failed yet: the document says what went wrong but stays queued,
    # because the next attempt might be the one that works.
    queued = _get(client, auth("admin"), doc["id"])
    assert queued["status"] == DocumentStatus.QUEUED
    assert queued["error"] == job.error

    assert ingest_all() == 2
    db.refresh(job)
    assert (job.status, job.attempts) == (JobStatus.FAILED, 3)
    assert _get(client, auth("admin"), doc["id"])["status"] == DocumentStatus.FAILED


def test_run_job_reports_a_missing_service_instead_of_crashing(db: Session, monkeypatch):
    import app.services as services_package

    # Simulate the service module being absent: None in sys.modules makes
    # the import raise, and the package attribute would otherwise short-cut
    # the lookup.
    monkeypatch.setitem(sys.modules, "app.services.extraction", None)
    monkeypatch.delattr(services_package, "extraction", raising=False)

    job = Job(kind=JobKind.EXTRACT, payload={"extraction_id": str(uuid.uuid4())}, max_attempts=1)
    db.add(job)
    db.commit()

    assert ingestion.process_pending(db) == 1
    db.refresh(job)
    assert job.status == JobStatus.FAILED
    assert "extraction service" in (job.error or "")


def test_process_pending_honours_kinds_and_limit(upload, ingest_all, db: Session):
    for n in range(3):
        upload("admin", "city", [_long_page(n)], title=f"Lease {n}")

    assert ingest_all(kinds=[JobKind.EXTRACT]) == 0
    assert ingest_all(limit=2) == 2
    assert ingestion.job_counts(db) == {"queued": 1, "running": 0, "done": 2, "failed": 0}
    assert ingestion.document_counts(db) == {
        "queued": 1,
        "processing": 0,
        "ready": 2,
        "failed": 0,
    }
    assert ingest_all() == 1
    assert ingestion.job_counts(db)["done"] == 3


# --- the queue under contention -------------------------------------------


def test_claim_next_job_never_hands_the_same_job_to_two_workers(db: Session):
    jobs = [Job(kind=JobKind.INGEST) for _ in range(20)]
    db.add_all(jobs)
    db.commit()
    expected = {job.id for job in jobs}

    claimed: dict[str, list[uuid.UUID]] = {"a": [], "b": []}
    barrier = threading.Barrier(2)

    def claim_everything(name: str) -> None:
        session = SessionLocal()
        try:
            barrier.wait()
            while True:
                job = ingestion.claim_next_job(session, f"test:{name}", [JobKind.INGEST])
                if job is None:
                    break
                claimed[name].append(job.id)
        finally:
            session.close()

    threads = [threading.Thread(target=claim_everything, args=(name,)) for name in claimed]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    a, b = set(claimed["a"]), set(claimed["b"])
    assert len(claimed["a"]) + len(claimed["b"]) == 20
    assert a & b == set(), "a job was claimed twice"
    assert a | b == expected

    rows = db.execute(select(Job.status, Job.attempts, Job.claimed_by)).all()
    assert all(status == JobStatus.RUNNING and attempts == 1 for status, attempts, _ in rows)
    assert {claimed_by for _, _, claimed_by in rows} <= {"test:a", "test:b"}
    assert ingestion.claim_next_job(db, "test:late", [JobKind.INGEST]) is None


def test_the_worker_thread_processes_a_queued_job(client: TestClient, auth, upload):
    doc = upload("admin", "north", LEASE_PAGES, title="Worker lease")
    headers = auth("director")

    ingestion.start_worker()
    try:
        assert ingestion.worker_alive()
        assert client.get(f"{API}/health", headers=headers).json()["worker_alive"] is True

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            current = _get(client, headers, doc["id"])
            if current["status"] in (DocumentStatus.READY, DocumentStatus.FAILED):
                break
            time.sleep(0.2)
    finally:
        ingestion.stop_worker()

    assert not ingestion.worker_alive()
    assert current["status"] == DocumentStatus.READY, current
    assert current["page_count"] == 3

    health = client.get(f"{API}/health", headers=headers).json()
    assert health["worker_alive"] is False
    assert health["jobs"] == {"queued": 0, "running": 0, "failed": 0}


# --- re-ingest and delete ------------------------------------------------


def test_reingest_resets_counts_and_queues_one_job(
    client: TestClient, auth, upload, ingest_all, db: Session
):
    doc = upload("admin", "city", LEASE_PAGES)
    ingest_all()
    before = _get(client, auth("admin"), doc["id"])
    assert before["status"] == DocumentStatus.READY

    path = f"{API}/documents/{doc['id']}/reingest"
    assert client.post(path, headers=auth("manager")).status_code == 403

    response = client.post(path, headers=auth("admin"))
    assert response.status_code == 202, response.text
    reset = response.json()
    assert reset["status"] == DocumentStatus.QUEUED
    assert reset["page_count"] == 0 and reset["chunk_count"] == 0
    assert reset["error"] is None
    assert _count(db, DocumentPage, doc["id"]) == 0
    assert _count(db, Chunk, doc["id"]) == 0

    # Pressing it twice queues one job, not two.
    assert client.post(path, headers=auth("admin")).status_code == 202
    statuses = [job.status for job in _jobs(db, doc["id"])]
    assert sorted(statuses) == [JobStatus.DONE, JobStatus.QUEUED]

    assert ingest_all() == 1
    after = _get(client, auth("admin"), doc["id"])
    assert after["status"] == DocumentStatus.READY
    assert after["page_count"] == 3
    assert after["chunk_count"] == before["chunk_count"]


def test_delete_removes_pages_chunks_blob_and_jobs(
    client: TestClient, auth, upload, ingest_all, db: Session
):
    doc = upload("admin", "city", LEASE_PAGES)
    ingest_all()
    assert _count(db, Chunk, doc["id"]) > 0

    assert client.delete(f"{API}/documents/{doc['id']}", headers=auth("viewer")).status_code == 403

    response = client.delete(f"{API}/documents/{doc['id']}", headers=auth("admin"))
    assert response.status_code == 204, response.text
    assert response.content == b""

    assert client.get(f"{API}/documents/{doc['id']}", headers=auth("admin")).status_code == 404
    assert _count(db, Chunk, doc["id"]) == 0
    assert _count(db, DocumentPage, doc["id"]) == 0
    assert _count(db, DocumentBlob, doc["id"]) == 0
    assert _jobs(db, doc["id"]) == []


# --- questions that cited a document ---------------------------------------


def test_document_questions_are_those_that_cited_it_and_only_your_own(
    client: TestClient, auth, upload, ingest_all, users, db: Session
):
    doc = upload("admin", "city", LEASE_PAGES, title="Meridian House")
    ingest_all()
    chunk = db.execute(
        select(Chunk).where(Chunk.document_id == uuid.UUID(doc["id"]), Chunk.page_number == 3)
    ).scalars().first()
    assert chunk is not None

    def question(user_key: str, text: str, cite: bool) -> Question:
        q = Question(
            user_id=users[user_key].id,
            text=text,
            status=QuestionStatus.ANSWERED if cite else QuestionStatus.UNANSWERED,
            answer_text="The break date is 31 March 2029." if cite else None,
            provider="stub",
            model="extractive-v1",
        )
        db.add(q)
        db.flush()
        if cite:
            db.add(
                Citation(
                    question_id=q.id,
                    chunk_id=chunk.id,
                    ordinal=1,
                    source_index=0,
                    block_start=0,
                    block_end=1,
                    cited_text="The Tenant may determine this Lease on 31 March 2029",
                )
            )
        return q

    mine = question("manager", "When is the break?", cite=True)
    question("admin", "What notice is needed?", cite=True)
    question("manager", "Who is the guarantor?", cite=False)
    db.commit()

    as_manager = client.get(f"{API}/documents/{doc['id']}/questions", headers=auth("manager"))
    assert as_manager.status_code == 200, as_manager.text
    (only_mine,) = as_manager.json()
    assert only_mine["id"] == str(mine.id)
    assert only_mine["user_name"] == "Sadia Mahmood"
    assert only_mine["status"] == QuestionStatus.ANSWERED
    assert only_mine["cost_usd"] == "0.000000"
    (citation,) = only_mine["citations"]
    assert citation["page_number"] == 3
    assert citation["document_title"] == "Meridian House"
    assert citation["chunk_id"] == str(chunk.id)

    as_director = client.get(f"{API}/documents/{doc['id']}/questions", headers=auth("director"))
    assert {q["text"] for q in as_director.json()} == {
        "When is the break?",
        "What notice is needed?",
    }

    assert client.get(f"{API}/documents/{doc['id']}/questions", headers=auth("viewer")).json() == []


def test_re_uploading_a_failed_document_gives_it_another_go(
    client: TestClient, auth, upload, db: Session, portfolios
):
    """The bytes are the same, so it is the same document -- but the last
    attempt at them failed, and "already have it" would leave the person
    holding a lease that does not work."""
    doc = upload("admin", "city", LEASE_PAGES, title="Meridian House")
    document = db.get(Document, uuid.UUID(doc["id"]))
    document.status = DocumentStatus.FAILED
    document.error = "The embedding service was unreachable."
    for job in _jobs(db, document.id):
        job.status = JobStatus.FAILED
    db.commit()

    again = upload("admin", "city", LEASE_PAGES, title="Meridian House")

    assert again["id"] == doc["id"]
    assert again["status"] == DocumentStatus.QUEUED
    assert again["error"] is None
    assert any(job.status == JobStatus.QUEUED for job in _jobs(db, document.id))


def test_reingesting_while_a_worker_holds_the_job_does_not_add_a_second(
    upload, db: Session
):
    """Two ingest jobs for one document would rebuild the same chunk rows
    underneath each other."""
    doc = upload("admin", "city", LEASE_PAGES)
    document = db.get(Document, uuid.UUID(doc["id"]))
    (job,) = _jobs(db, document.id)
    job.status = JobStatus.RUNNING
    job.claimed_by = "someone-else:1"
    job.claimed_at = datetime.now(UTC)
    db.commit()

    ingestion.reingest(db, document)

    assert len(_jobs(db, document.id)) == 1


def test_a_job_whose_worker_died_is_offered_again(upload, db: Session):
    """Claiming is what marks work as taken, and a killed process releases
    nothing: without this the job stays `running` and the document
    `processing` for ever."""
    doc = upload("admin", "city", LEASE_PAGES)
    document = db.get(Document, uuid.UUID(doc["id"]))
    (job,) = _jobs(db, document.id)
    job.status = JobStatus.RUNNING
    job.claimed_by = "a-worker-that-died:99"
    job.claimed_at = datetime.now(UTC) - timedelta(hours=2)
    document.status = DocumentStatus.PROCESSING
    db.commit()

    assert ingestion.requeue_stranded_jobs(db) == 1

    db.refresh(job)
    db.refresh(document)
    assert job.status == JobStatus.QUEUED
    assert job.claimed_by is None
    assert job.attempts == 0, "the job never got its turn; it does not lose an attempt"
    assert document.status == DocumentStatus.QUEUED
    # A job claimed a moment ago is somebody's work in progress.
    assert ingestion.requeue_stranded_jobs(db) == 0


def test_a_search_for_a_wildcard_is_a_search_for_that_character(
    client: TestClient, auth, upload
):
    upload("admin", "city", LEASE_PAGES, title="Unit 4B, Meridian House")
    upload("admin", "city", [p + " second" for p in LEASE_PAGES], title="100% Let, Whitworth Court")

    everything = client.get(f"{API}/documents?q=%", headers=auth("admin")).json()
    literal = client.get(f"{API}/documents?q=100%", headers=auth("admin")).json()

    assert everything["total"] == 1, "a lone % is a search for a per-cent sign, not for everything"
    assert [item["title"] for item in literal["items"]] == ["100% Let, Whitworth Court"]


def test_the_worker_sweeps_for_stranded_jobs_when_it_starts(upload, db: Session):
    """The recovery function is only worth having if something calls it. The
    first deployment proved that: a container killed for memory left eight
    leases half-ingested and nothing ever picked them up."""
    doc = upload("admin", "city", LEASE_PAGES)
    document = db.get(Document, uuid.UUID(doc["id"]))
    (job,) = _jobs(db, document.id)
    job.status = JobStatus.RUNNING
    job.claimed_by = "a-container-that-was-killed:1"
    job.claimed_at = datetime.now(UTC) - timedelta(minutes=ingestion.STRANDED_AFTER_MINUTES + 5)
    document.status = DocumentStatus.PROCESSING
    db.commit()

    worker = ingestion.Worker()
    worker.start()
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            db.expire_all()
            if db.get(Document, document.id).status == DocumentStatus.READY:
                break
            time.sleep(0.5)
    finally:
        worker.stop(timeout=10)

    db.expire_all()
    assert db.get(Document, document.id).status == DocumentStatus.READY
    assert db.get(Job, job.id).status == JobStatus.DONE
