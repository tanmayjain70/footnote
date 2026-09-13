"""Extraction and review: the evidence checks, and who may make a value count.

The documents under test are generated leases, ingested by hand -- render,
parse, chunk, embed, insert -- rather than through the upload pipeline, so a
failure here is about extraction and not about the worker. The stub
extractor's patterns are written against the generator's canonical sentences,
which makes the round trip a real test: every one of the seventeen values
must come back with the right text, a quote that is in the chunk it names,
and the page that sentence is printed on.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.demo.leases import generate_specs, render_lease_pdf
from app.models import Chunk, Document, DocumentPage, ExtractedValue, Extraction, Job, UsageEvent
from app.models.enums import (
    DocumentStatus,
    ExtractionStatus,
    JobKind,
    JobStatus,
    ReviewStatus,
    UsageKind,
    ValueIssue,
)
from app.providers import llm
from app.providers.embeddings import get_embedding_provider
from app.providers.llm import ExtractedField, ExtractionOutput, ProviderError, Source, Usage
from app.services import extraction as extraction_service
from app.services.chunking import chunk_document
from app.services.extraction_fields import FIELDS
from app.services.pdf import extract_pages
from tests.conftest import API

FIELD_KEYS = [spec.key for spec in FIELDS]

#: Generated once per module: rendering a lease is the slow part of these
#: tests and the specs are deterministic for a seed.
SPECS = generate_specs(6, seed=7)


def _with_break(specs: Sequence[Any], wanted: bool) -> Any:
    for spec in specs:
        if bool(getattr(spec, "break_date", None)) == wanted:
            return spec
    pytest.fail(f"no generated lease {'with' if wanted else 'without'} a break clause")


def _truth(spec: Any, key: str) -> Any:
    """What the register should hold for ``key``, in its JSON form."""
    value = getattr(spec, key)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _fold(text: str) -> str:
    return " ".join(text.split()).lower()


def ready_lease(
    db: Session, portfolio_id: uuid.UUID, spec: Any, *, title: str, uploaded_by: uuid.UUID
) -> tuple[Document, list[str]]:
    """Insert a generated lease as a ready document with pages and chunks.

    Returns the document and the page texts, so a test can check that a
    value's page number is the page its quote is printed on.
    """
    data = render_lease_pdf(spec)
    pages = extract_pages(data)
    chunks = chunk_document(pages)
    vectors = get_embedding_provider().embed_documents([chunk.text for chunk in chunks])

    document = Document(
        portfolio_id=portfolio_id,
        title=title,
        filename=f"{title.lower().replace(' ', '-')}.pdf",
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


def extract(db: Session, document: Document, user: Any) -> Extraction:
    """Request and run an extraction the way the worker would, on one session."""
    extraction = extraction_service.request_extraction(db, document, user)
    # By payload rather than by document: a document extracted twice has two
    # jobs, and the one to run is the one this request queued.
    job = db.execute(
        select(Job).where(Job.payload["extraction_id"].astext == str(extraction.id))
    ).scalar_one()
    extraction_service.run_extract_job(db, job)
    db.expire_all()
    return db.get(Extraction, extraction.id)


def values_by_key(db: Session, extraction: Extraction) -> dict[str, ExtractedValue]:
    rows = db.execute(
        select(ExtractedValue).where(ExtractedValue.extraction_id == extraction.id)
    ).scalars()
    return {value.field_key: value for value in rows}


class ScriptedLLM:
    """A provider that says exactly what the test tells it to.

    For the evidence checks: the stub never points at a foreign chunk or
    invents a quote, so a provider that does is needed to prove the checks
    catch it.
    """

    name = "scripted"
    model = "extractive-v1"

    def __init__(self, fields: list[ExtractedField], *, error: Exception | None = None):
        self.fields = fields
        self.error = error
        self.calls = 0

    def answer(self, question: str, sources: list[Source]):  # pragma: no cover - unused
        raise NotImplementedError

    def extract(self, document_title: str, fields: Any, chunks: list[Source]) -> ExtractionOutput:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return ExtractionOutput(fields=self.fields, usage=Usage(1200, 300), model=self.model)


@pytest.fixture
def lease(db: Session, users, portfolios) -> tuple[Document, list[str], Any]:
    spec = _with_break(SPECS, True)
    document, pages = ready_lease(
        db,
        portfolios["city"].id,
        spec,
        title="Meridian House Unit 4",
        uploaded_by=users["admin"].id,
    )
    return document, pages, spec


# --- requesting -----------------------------------------------------------


def test_request_extraction_creates_the_row_and_the_job(
    client: TestClient, db: Session, auth, lease
):
    document, _, _ = lease
    response = client.post(f"{API}/documents/{document.id}/extract", headers=auth("admin"))
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == ExtractionStatus.QUEUED
    assert body["document_id"] == str(document.id)
    assert body["provider"] == "stub"
    assert body["values"] == []

    job = db.execute(select(Job).where(Job.kind == JobKind.EXTRACT)).scalar_one()
    assert job.status == JobStatus.QUEUED
    assert job.document_id == document.id
    assert job.payload == {"extraction_id": body["id"]}


def test_a_second_request_while_one_is_queued_is_a_conflict(client: TestClient, auth, lease):
    document, _, _ = lease
    first = client.post(f"{API}/documents/{document.id}/extract", headers=auth("admin"))
    assert first.status_code == 202
    second = client.post(f"{API}/documents/{document.id}/extract", headers=auth("admin"))
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "extraction_in_progress"


def test_extraction_needs_a_ready_document(client: TestClient, db: Session, auth, portfolios):
    document = Document(
        portfolio_id=portfolios["city"].id,
        title="Still parsing",
        filename="still-parsing.pdf",
        content_sha256="0" * 64,
        byte_size=10,
        status=DocumentStatus.PROCESSING,
    )
    db.add(document)
    db.commit()
    response = client.post(f"{API}/documents/{document.id}/extract", headers=auth("admin"))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "document_not_ready"


def test_manager_can_neither_extract_nor_review(
    client: TestClient, db: Session, auth, users, lease
):
    """A manager can ask about a break clause; only an administrator or the
    director can make the extracted date count."""
    document, _, _ = lease
    response = client.post(f"{API}/documents/{document.id}/extract", headers=auth("manager"))
    assert response.status_code == 403
    assert response.json()["error"]["detail"]["your_role"] == "manager"

    extraction = extract(db, document, users["admin"])
    value = values_by_key(db, extraction)["term_end"]
    response = client.post(
        f"{API}/extracted-values/{value.id}/review",
        headers=auth("manager"),
        json={"action": "confirm"},
    )
    assert response.status_code == 403
    db.refresh(value)
    assert value.review_status == ReviewStatus.PENDING


def test_a_document_outside_your_portfolios_does_not_exist(
    client: TestClient, db: Session, auth, users, portfolios
):
    """404, not 403: a 403 would confirm that the Riverside lease exists."""
    document, _ = ready_lease(
        db, portfolios["riverside"].id, SPECS[0], title="Riverside 1", uploaded_by=users["admin"].id
    )
    extraction = extract(db, document, users["admin"])
    value = next(iter(values_by_key(db, extraction).values()))

    response = client.get(f"{API}/documents/{document.id}/extraction", headers=auth("manager"))
    assert response.status_code == 404
    response = client.get(
        f"{API}/documents/{document.id}/extraction", headers=auth("riverside_manager")
    )
    assert response.status_code == 200, "the one manager who may see Riverside"
    assert (
        client.post(
            f"{API}/extracted-values/{value.id}/review",
            headers=auth("admin"),
            json={"action": "confirm"},
        ).status_code
        == 200
    ), "the admin is a member of Riverside"
    assert (
        client.post(
            f"{API}/extracted-values/{value.id}/review",
            headers=auth("director"),
            json={"action": "confirm"},
        ).status_code
        == 200
    ), "the director sees everything"


def test_get_extraction_is_404_until_one_exists(client: TestClient, auth, lease):
    document, _, _ = lease
    response = client.get(f"{API}/documents/{document.id}/extraction", headers=auth("viewer"))
    assert response.status_code == 404


# --- running with the stub ---------------------------------------------------


def test_the_stub_fills_every_field_with_verified_evidence(
    client: TestClient, db: Session, auth, users, lease
):
    """The round trip that the generator and the registry are coordinated on."""
    document, pages, spec = lease
    extraction = extract(db, document, users["admin"])
    assert extraction.status == ExtractionStatus.DONE, extraction.error
    assert extraction.finished_at is not None

    values = values_by_key(db, extraction)
    assert sorted(values) == sorted(FIELD_KEYS)
    assert len(values) == 17

    for key, value in values.items():
        assert value.issue is None, f"{key}: {value.issue} ({value.value_text!r})"
        assert value.value_json == _truth(spec, key), key
        assert value.chunk_id is not None and value.quote, key
        assert value.review_status == ReviewStatus.PENDING
        chunk = db.get(Chunk, value.chunk_id)
        assert chunk.document_id == document.id
        assert _fold(value.quote) in _fold(chunk.text), key
        # The page the quote is printed on, not the page the chunk started on.
        assert value.page_number == chunk.page_number
        assert _fold(value.quote) in _fold(pages[value.page_number - 1]), key

    job = db.execute(select(Job).where(Job.kind == JobKind.EXTRACT)).scalar_one()
    assert job.status == JobStatus.DONE

    body = client.get(f"{API}/documents/{document.id}/extraction", headers=auth("viewer")).json()
    assert body["status"] == "done"
    assert [v["field_key"] for v in body["values"]] == FIELD_KEYS, "registry order"
    assert all(v["label"] and v["type"] for v in body["values"])
    assert body["cost_usd"] == "0.000000", "the stub is free"


def test_usage_is_recorded_as_extraction(db: Session, users, lease):
    document, _, _ = lease
    extraction = extract(db, document, users["admin"])

    event = db.execute(select(UsageEvent)).scalar_one()
    assert event.kind == UsageKind.EXTRACTION
    assert event.reference_id == extraction.id
    assert event.user_id == users["admin"].id
    assert event.provider == "stub"
    assert event.input_tokens == extraction.input_tokens > 0
    assert event.output_tokens == extraction.output_tokens > 0


def test_fields_lists_the_registry(client: TestClient, auth):
    body = client.get(f"{API}/fields", headers=auth("viewer")).json()
    assert len(body) == 17
    assert [f["key"] for f in body] == FIELD_KEYS
    basis = next(f for f in body if f["key"] == "rent_review_basis")
    assert basis["type"] == "enum"
    assert "open_market" in basis["enum_values"]
    assert all(f["label"] and f["description"] for f in body)


# --- the evidence checks ---------------------------------------------------


def _scripted(monkeypatch, fields: list[ExtractedField], **kwargs) -> ScriptedLLM:
    provider = ScriptedLLM(fields, **kwargs)
    monkeypatch.setattr(llm, "get_llm_provider", lambda: provider)
    return provider


def test_a_chunk_from_another_document_is_an_invalid_chunk(
    db: Session, monkeypatch, users, portfolios, lease
):
    document, _, _ = lease
    other, _ = ready_lease(
        db, portfolios["north"].id, SPECS[1], title="Other lease", uploaded_by=users["admin"].id
    )
    foreign = db.execute(
        select(Chunk).where(Chunk.document_id == other.id).order_by(Chunk.ordinal)
    ).scalars().first()

    _scripted(
        monkeypatch,
        [
            ExtractedField(
                "tenant_name", "Acme Ltd", "Acme Ltd is the tenant.", str(foreign.id), "high"
            )
        ],
    )
    extraction = extract(db, document, users["admin"])
    assert extraction.status == ExtractionStatus.DONE

    value = values_by_key(db, extraction)["tenant_name"]
    assert value.issue == ValueIssue.INVALID_CHUNK
    assert value.chunk_id is None and value.page_number is None
    assert value.value_text == "Acme Ltd", "the value is kept for the reviewer"
    assert value.value_json == "Acme Ltd"


def test_a_quote_that_is_not_in_the_chunk_is_flagged(db: Session, monkeypatch, users, lease):
    document, _, _ = lease
    chunk = db.execute(
        select(Chunk).where(Chunk.document_id == document.id).order_by(Chunk.ordinal)
    ).scalars().first()

    _scripted(
        monkeypatch,
        [
            ExtractedField(
                "term_years", "10", "The Term is ten years, honest.", str(chunk.id), "high"
            )
        ],
    )
    extraction = extract(db, document, users["admin"])

    value = values_by_key(db, extraction)["term_years"]
    assert value.issue == ValueIssue.QUOTE_NOT_FOUND
    assert value.chunk_id == chunk.id and value.page_number == chunk.page_number
    assert value.value_json == 10, "value and chunk are kept; only the quote is in doubt"


def test_a_quote_is_matched_without_regard_to_case_or_spacing(
    db: Session, monkeypatch, users, lease
):
    document, _, _ = lease
    chunk = db.execute(
        select(Chunk).where(Chunk.document_id == document.id).order_by(Chunk.ordinal)
    ).scalars().first()
    words = chunk.text.split()[:6]
    quote = "  ".join(w.upper() for w in words)

    _scripted(
        monkeypatch, [ExtractedField("permitted_use", "offices", quote, str(chunk.id), "medium")]
    )
    extraction = extract(db, document, users["admin"])

    value = values_by_key(db, extraction)["permitted_use"]
    assert value.issue is None
    assert value.confidence == "medium"


def test_a_missing_value_is_no_evidence(db: Session, monkeypatch, users, lease):
    document, _, _ = lease
    _scripted(monkeypatch, [ExtractedField("guarantor", None, None, None, "low")])
    extraction = extract(db, document, users["admin"])

    values = values_by_key(db, extraction)
    assert len(values) == 17, "every registry field gets a row, answered or not"
    guarantor = values["guarantor"]
    assert guarantor.issue == ValueIssue.NO_EVIDENCE
    assert guarantor.value_json is None and guarantor.value_text is None
    # A field the provider did not mention at all is the same thing.
    assert values["deposit_gbp"].issue == ValueIssue.NO_EVIDENCE
    assert values["deposit_gbp"].confidence == "low"


def test_an_unparseable_date_keeps_what_the_model_wrote(db: Session, monkeypatch, users, lease):
    document, _, _ = lease
    chunk = db.execute(
        select(Chunk).where(Chunk.document_id == document.id).order_by(Chunk.ordinal)
    ).scalars().first()
    quote = " ".join(chunk.text.split()[:5])

    _scripted(
        monkeypatch,
        [ExtractedField("break_date", "the third anniversary", quote, str(chunk.id), "medium")],
    )
    extraction = extract(db, document, users["admin"])

    value = values_by_key(db, extraction)["break_date"]
    assert value.issue == ValueIssue.UNPARSEABLE
    assert value.value_json is None
    assert value.value_text == "the third anniversary"
    assert value.chunk_id == chunk.id


def test_a_provider_error_requeues_then_fails_the_extraction(
    db: Session, monkeypatch, users, lease
):
    """The job's retries belong to the worker; the extraction row must tell
    the truth at each step -- back to queued with the error, then failed."""
    document, _, _ = lease
    provider = _scripted(monkeypatch, [], error=ProviderError("overloaded"))

    extraction = extraction_service.request_extraction(db, document, users["admin"])
    job = db.execute(select(Job).where(Job.kind == JobKind.EXTRACT)).scalar_one()

    job.attempts = 1
    with pytest.raises(ProviderError):
        extraction_service.run_extract_job(db, job)
    db.refresh(extraction)
    assert extraction.status == ExtractionStatus.QUEUED
    assert extraction.error == "overloaded"
    assert extraction.finished_at is None

    job.attempts = job.max_attempts
    with pytest.raises(ProviderError):
        extraction_service.run_extract_job(db, job)
    db.refresh(extraction)
    assert extraction.status == ExtractionStatus.FAILED
    assert extraction.finished_at is not None
    assert provider.calls == 2
    assert db.execute(select(ExtractedValue)).first() is None, "no half-written values"
    assert db.execute(select(UsageEvent)).first() is None, "nothing was spent"


# --- review ----------------------------------------------------------------


def test_confirm_records_who_and_when(client: TestClient, db: Session, auth, users, lease):
    document, _, spec = lease
    extraction = extract(db, document, users["admin"])
    value = values_by_key(db, extraction)["term_end"]

    response = client.post(
        f"{API}/extracted-values/{value.id}/review",
        headers=auth("admin"),
        json={"action": "confirm", "note": "Checked against the signed copy."},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["review_status"] == "confirmed"
    assert body["reviewed_by_name"] == "Owen Pryce-Reid"
    assert body["reviewed_at"] is not None
    assert body["review_note"] == "Checked against the signed copy."
    assert body["effective_value"] == body["value_json"] == _truth(spec, "term_end")
    assert body["corrected_json"] is None


def test_correct_parses_the_correction_and_refuses_garbage(
    client: TestClient, db: Session, auth, users, lease
):
    document, _, spec = lease
    extraction = extract(db, document, users["admin"])
    values = values_by_key(db, extraction)

    rent = values["annual_rent_gbp"]
    response = client.post(
        f"{API}/extracted-values/{rent.id}/review",
        headers=auth("director"),
        json={
            "action": "correct",
            "corrected_value": "£41,000 per annum",
            "note": "Rent stated inclusive of service charge; corrected to net figure",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["review_status"] == "corrected"
    assert body["corrected_json"] == 41000.0
    assert body["effective_value"] == 41000.0
    assert body["value_json"] == _truth(spec, "annual_rent_gbp"), "the model's value is untouched"
    assert body["reviewed_by_name"] == "Priya Hallam"

    for field, garbage in (("term_start", "sometime next spring"), ("rent_review_basis", "vibes")):
        response = client.post(
            f"{API}/extracted-values/{values[field].id}/review",
            headers=auth("admin"),
            json={"action": "correct", "corrected_value": garbage},
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "invalid_review"
        db.refresh(values[field])
        assert values[field].review_status == ReviewStatus.PENDING

    # Correcting with nothing is not a correction either.
    response = client.post(
        f"{API}/extracted-values/{values['unit'].id}/review",
        headers=auth("admin"),
        json={"action": "correct", "corrected_value": "   "},
    )
    assert response.status_code == 422


def test_correct_accepts_the_same_forms_as_the_extractor(
    client: TestClient, db: Session, auth, users, lease
):
    document, _, _ = lease
    extraction = extract(db, document, users["admin"])
    values = values_by_key(db, extraction)

    cases = {
        "break_date": ("1st April 2027", "2027-04-01"),
        "break_notice_months": ("9", 9),
        "vat_elected": ("no", False),
        "repairing_obligation": ("Internal repairing", "internal_repairing"),
    }
    for field, (text, expected) in cases.items():
        response = client.post(
            f"{API}/extracted-values/{values[field].id}/review",
            headers=auth("admin"),
            json={"action": "correct", "corrected_value": text},
        )
        assert response.status_code == 200, (field, response.text)
        assert response.json()["effective_value"] == expected, field


def test_reject_and_re_review(client: TestClient, db: Session, auth, users, lease):
    document, _, _ = lease
    extraction = extract(db, document, users["admin"])
    value = values_by_key(db, extraction)["guarantor"]

    rejected = client.post(
        f"{API}/extracted-values/{value.id}/review",
        headers=auth("admin"),
        json={"action": "reject", "note": "That is the landlord's parent company."},
    ).json()
    assert rejected["review_status"] == "rejected"
    assert rejected["reviewed_by_name"] == "Owen Pryce-Reid"
    assert rejected["effective_value"] == rejected["value_json"], "the model's value is still shown"

    # A decision can be revisited; the latest one stands.
    confirmed = client.post(
        f"{API}/extracted-values/{value.id}/review",
        headers=auth("director"),
        json={"action": "confirm"},
    ).json()
    assert confirmed["review_status"] == "confirmed"
    assert confirmed["reviewed_by_name"] == "Priya Hallam"
    assert confirmed["review_note"] is None


def test_review_of_an_unknown_value_is_404(client: TestClient, auth, users):
    response = client.post(
        f"{API}/extracted-values/{uuid.uuid4()}/review",
        headers=auth("admin"),
        json={"action": "confirm"},
    )
    assert response.status_code == 404


# --- the queue -------------------------------------------------------------


def test_review_queue_counts_pending_values_per_document(
    client: TestClient, db: Session, auth, users, portfolios, monkeypatch
):
    # A lease with a break clause: the stub finds all seventeen values, so
    # nothing on it carries an issue.
    city, _ = ready_lease(
        db,
        portfolios["city"].id,
        _with_break(SPECS, True),
        title="City lease",
        uploaded_by=users["admin"].id,
    )
    north, _ = ready_lease(
        db, portfolios["north"].id, SPECS[1], title="North lease", uploaded_by=users["admin"].id
    )
    riverside, _ = ready_lease(
        db,
        portfolios["riverside"].id,
        SPECS[2],
        title="Riverside lease",
        uploaded_by=users["admin"].id,
    )
    extractions = {
        document.id: extract(db, document, users["admin"]) for document in (city, north, riverside)
    }
    # Confirm three of the city lease's values and leave the rest.
    city_values = values_by_key(db, extractions[city.id])
    for key in ("tenant_name", "unit", "term_end"):
        extraction_service.review(db, city_values[key], users["admin"], action="confirm")

    queue = client.get(f"{API}/review-queue", headers=auth("director")).json()
    by_title = {item["title"]: item for item in queue}
    assert set(by_title) == {"City lease", "North lease", "Riverside lease"}
    assert by_title["City lease"]["pending"] == 14
    assert by_title["North lease"]["pending"] == 17
    assert by_title["City lease"]["issues"] == 0
    assert by_title["City lease"]["portfolio_name"] == "City Centre"

    # A manager who is not a member of Riverside does not see it in the queue.
    queue = client.get(f"{API}/review-queue", headers=auth("manager")).json()
    assert {item["title"] for item in queue} == {"City lease", "North lease"}

    # Scoped to one portfolio; an invisible portfolio is a 404.
    scoped = client.get(
        f"{API}/review-queue?portfolio_id={portfolios['north'].id}", headers=auth("manager")
    ).json()
    assert [item["title"] for item in scoped] == ["North lease"]
    assert (
        client.get(
            f"{API}/review-queue?portfolio_id={portfolios['riverside'].id}", headers=auth("manager")
        ).status_code
        == 404
    )

    # Once everything on a document is reviewed it leaves the queue.
    for value in values_by_key(db, extractions[riverside.id]).values():
        extraction_service.review(db, value, users["admin"], action="confirm")
    queue = client.get(f"{API}/review-queue", headers=auth("director")).json()
    assert "Riverside lease" not in {item["title"] for item in queue}


def test_review_queue_counts_issues_first(
    client: TestClient, db: Session, auth, users, monkeypatch, lease
):
    document, _, _ = lease
    _scripted(
        monkeypatch,
        [
            ExtractedField(
                "tenant_name", "Acme Ltd", "not in the lease", str(uuid.uuid4()), "high"
            ),
            ExtractedField("term_years", "ten-ish", None, None, "low"),
        ],
    )
    extract(db, document, users["admin"])
    queue = client.get(f"{API}/review-queue", headers=auth("admin")).json()
    assert len(queue) == 1
    assert queue[0]["pending"] == 17
    # Two named fields with bad evidence plus fifteen with no evidence at all.
    assert queue[0]["issues"] == 17
