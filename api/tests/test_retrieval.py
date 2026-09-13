"""Hybrid retrieval: both legs, the fusion, and the access filter inside it.

The documents here are inserted directly as the ingestion pipeline would
leave them -- ready, paged, chunked, embedded -- because these tests are
about what retrieval does with chunks, not about how chunks are made.
``ready_document`` is shared with the answering and usage tests.
"""

from __future__ import annotations

import hashlib
import math
import uuid

from sqlalchemy.orm import Session

from app.deps import visible_portfolio_ids
from app.models import Chunk, Document, DocumentPage, Portfolio, User
from app.models.enums import DocumentStatus
from app.providers import embeddings
from app.services import retrieval

BREAK_CLAUSE = (
    "The Tenant may determine this Lease on 31 March 2029 by giving the Landlord "
    "not less than 6 months' prior written notice."
)
TERM_CLAUSE = "The Term shall commence on 1 April 2024 and shall expire on 31 March 2034."
RENT_CLAUSE = "The Initial Rent is £42,500 (forty-two thousand five hundred pounds) per annum."
INSURANCE_CLAUSE = (
    "The Landlord shall insure the Building against the Insured Risks for its full "
    "reinstatement value."
)
REPAIR_CLAUSE = (
    "The Tenant shall keep the Premises in good and substantial repair and condition "
    "throughout the Term."
)


def ready_document(
    db: Session,
    portfolio: Portfolio,
    title: str,
    pages: list[list[str]],
    *,
    status: str = DocumentStatus.READY,
    embed: bool = True,
) -> Document:
    """Insert a document as ingestion leaves it. ``pages`` is one list of
    chunk texts per page; the page text is the chunks joined by newlines."""
    provider = embeddings.get_embedding_provider()
    document = Document(
        portfolio_id=portfolio.id,
        title=title,
        filename=f"{title.lower().replace(' ', '-')}.pdf",
        content_sha256=hashlib.sha256(f"{portfolio.id}:{title}".encode()).hexdigest(),
        byte_size=4096,
        status=status,
        page_count=len(pages),
        chunk_count=sum(len(page) for page in pages),
    )
    db.add(document)
    db.flush()

    ordinal = 0
    for page_number, chunk_texts in enumerate(pages, start=1):
        page_text = "\n".join(chunk_texts)
        db.add(DocumentPage(document_id=document.id, page_number=page_number, text=page_text))
        vectors = provider.embed_documents(chunk_texts) if embed else [None] * len(chunk_texts)
        offset = 0
        for text, vector in zip(chunk_texts, vectors, strict=True):
            start = page_text.index(text, offset)
            db.add(
                Chunk(
                    document_id=document.id,
                    page_number=page_number,
                    ordinal=ordinal,
                    text=text,
                    char_start=start,
                    char_end=start + len(text),
                    token_estimate=len(text) // 4,
                    embedding=vector,
                )
            )
            offset = start + len(text)
            ordinal += 1
    db.commit()
    db.refresh(document)
    return document


def _visible(db: Session, users: dict[str, User], key: str) -> list[uuid.UUID]:
    return visible_portfolio_ids(db, users[key])


def _texts(results: list[retrieval.Retrieved]) -> list[str]:
    return [r.chunk.text for r in results]


# --- the two legs -------------------------------------------------------


def test_lexical_leg_finds_the_exact_phrase(db: Session, users, portfolios):
    """A date is exactly the kind of token the lexical leg exists for."""
    ready_document(
        db, portfolios["city"], "Meridian House", [[TERM_CLAUSE, INSURANCE_CLAUSE], [BREAK_CLAUSE]]
    )

    results = retrieval.search(
        db, query="31 March 2029", visible_portfolio_ids=_visible(db, users, "manager")
    )

    assert results, "the phrase is in a chunk"
    assert results[0].chunk.text == BREAK_CLAUSE
    assert results[0].lexical_rank == 1
    assert results[0].chunk.page_number == 2
    assert results[0].source_index == 0


def test_vector_leg_finds_a_paraphrase(db: Session, users, portfolios):
    """The question paraphrases the break clause: the vector leg must rank it
    first on its own, whatever the lexical leg makes of the shared words."""
    ready_document(
        db, portfolios["city"], "Meridian House", [[RENT_CLAUSE, INSURANCE_CLAUSE, BREAK_CLAUSE]]
    )

    results = retrieval.search(
        db,
        query="What notice must the tenant give to use the break?",
        visible_portfolio_ids=_visible(db, users, "manager"),
    )

    by_text = {r.chunk.text: r for r in results}
    assert BREAK_CLAUSE in by_text
    assert by_text[BREAK_CLAUSE].vector_rank == 1
    assert results[0].chunk.text == BREAK_CLAUSE


def test_a_query_of_stopwords_skips_the_lexical_leg(db: Session, users, portfolios):
    ready_document(db, portfolios["city"], "Meridian House", [[TERM_CLAUSE, RENT_CLAUSE]])

    results = retrieval.search(
        db, query="the of and", visible_portfolio_ids=_visible(db, users, "manager")
    )

    assert results, "the vector leg still returns the nearest chunks"
    assert all(r.lexical_rank is None for r in results)
    assert all(r.vector_rank is not None for r in results)


def test_chunks_without_an_embedding_come_only_from_the_lexical_leg(db: Session, users, portfolios):
    """A half-ingested document is found by its words or not at all -- never
    by a vector it does not have."""
    ready_document(db, portfolios["city"], "Meridian House", [[BREAK_CLAUSE]], embed=False)

    results = retrieval.search(
        db, query="31 March 2029", visible_portfolio_ids=_visible(db, users, "manager")
    )

    assert _texts(results) == [BREAK_CLAUSE]
    assert results[0].vector_rank is None
    assert results[0].lexical_rank == 1


# --- fusion -------------------------------------------------------------


def test_fusion_puts_a_chunk_found_by_both_legs_first(db: Session, users, portfolios):
    ready_document(
        db,
        portfolios["city"],
        "Meridian House",
        [[TERM_CLAUSE, INSURANCE_CLAUSE], [BREAK_CLAUSE, REPAIR_CLAUSE]],
    )

    results = retrieval.search(
        db, query="31 March 2029 notice", visible_portfolio_ids=_visible(db, users, "manager")
    )

    first = results[0]
    assert first.chunk.text == BREAK_CLAUSE
    assert first.vector_rank == 1 and first.lexical_rank == 1
    others = [r for r in results[1:]]
    assert others, "the term clause shares '31 March' and is found by the vector leg"
    # Only the break clause has every word, so nothing else can outrank it on
    # the lexical leg, whatever else that leg also matched.
    assert all((r.lexical_rank or math.inf) > 1 for r in others)
    assert all(first.fused_score > r.fused_score for r in others)


def test_second_on_both_legs_beats_first_on_one():
    """The property reciprocal rank fusion is chosen for, checked directly."""
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    fused = retrieval.fuse([a, c], [b, c], rrf_k=60, k=8)

    assert [f.chunk_id for f in fused] == [c, a, b]
    assert math.isclose(fused[0].score, 2 / 62)
    assert fused[0].vector_rank == 2 and fused[0].lexical_rank == 2
    assert fused[1].lexical_rank is None and fused[2].vector_rank is None


def test_fusion_ties_go_to_the_better_vector_rank():
    a, b = uuid.uuid4(), uuid.uuid4()

    fused = retrieval.fuse([a, b], [b, a], rrf_k=60, k=8)

    assert [f.chunk_id for f in fused] == [a, b]


# --- the access filter --------------------------------------------------


def test_another_portfolio_is_invisible_to_a_non_member(db: Session, users, portfolios):
    """The confidential portfolio: found by its one manager, not by the other.
    The filter is in the SQL, so this holds for every caller of search."""
    ready_document(db, portfolios["riverside"], "Riverside Wharf", [[BREAK_CLAUSE]])

    as_manager = retrieval.search(
        db, query="31 March 2029", visible_portfolio_ids=_visible(db, users, "manager")
    )
    as_riverside = retrieval.search(
        db, query="31 March 2029", visible_portfolio_ids=_visible(db, users, "riverside_manager")
    )
    as_director = retrieval.search(
        db, query="31 March 2029", visible_portfolio_ids=_visible(db, users, "director")
    )

    assert as_manager == []
    assert _texts(as_riverside) == [BREAK_CLAUSE]
    assert _texts(as_director) == [BREAK_CLAUSE]


def test_portfolio_scope_narrows_the_visible_set(db: Session, users, portfolios):
    ready_document(db, portfolios["city"], "Meridian House", [[BREAK_CLAUSE]])
    ready_document(db, portfolios["north"], "Whitworth Court", [[BREAK_CLAUSE]])
    visible = _visible(db, users, "manager")

    everything = retrieval.search(db, query="31 March 2029", visible_portfolio_ids=visible)
    north_only = retrieval.search(
        db,
        query="31 March 2029",
        visible_portfolio_ids=visible,
        portfolio_id=portfolios["north"].id,
    )

    assert {r.chunk.document.title for r in everything} == {"Meridian House", "Whitworth Court"}
    assert [r.chunk.document.title for r in north_only] == ["Whitworth Court"]


def test_a_scope_outside_the_visible_set_finds_nothing(db: Session, users, portfolios):
    """Both filters apply; a scope the caller cannot see is not a way round."""
    ready_document(db, portfolios["riverside"], "Riverside Wharf", [[BREAK_CLAUSE]])

    results = retrieval.search(
        db,
        query="31 March 2029",
        visible_portfolio_ids=_visible(db, users, "manager"),
        portfolio_id=portfolios["riverside"].id,
    )

    assert results == []


def test_an_empty_visible_set_returns_nothing(db: Session, users, portfolios):
    """Nobody has an unfiltered query. A person with no portfolios gets an
    empty list, even though the chunk exists."""
    ready_document(db, portfolios["city"], "Meridian House", [[BREAK_CLAUSE]])

    assert retrieval.search(db, query="31 March 2029", visible_portfolio_ids=[]) == []


def test_documents_that_are_not_ready_are_skipped(db: Session, users, portfolios):
    ready_document(
        db,
        portfolios["city"],
        "Meridian House",
        [[BREAK_CLAUSE]],
        status=DocumentStatus.PROCESSING,
    )

    assert (
        retrieval.search(
            db, query="31 March 2029", visible_portfolio_ids=_visible(db, users, "manager")
        )
        == []
    )


# --- limits -------------------------------------------------------------


def test_k_and_candidates_are_honoured(db: Session, users, portfolios):
    clauses = [
        f"The service charge for Unit {n} is payable quarterly in advance on the usual "
        f"quarter days together with any VAT."
        for n in range(1, 7)
    ]
    ready_document(db, portfolios["city"], "Trafford Park", [clauses])
    visible = _visible(db, users, "manager")

    everything = retrieval.search(db, query="service charge", visible_portfolio_ids=visible)
    two = retrieval.search(db, query="service charge", visible_portfolio_ids=visible, k=2)
    narrow = retrieval.search(
        db, query="service charge", visible_portfolio_ids=visible, candidates=1
    )

    assert len(everything) == 6
    assert [r.source_index for r in everything] == list(range(6))
    assert len(two) == 2
    # One candidate per leg: at most two chunks can reach the fusion at all.
    assert 1 <= len(narrow) <= 2


def test_to_sources_splits_each_chunk_into_citable_blocks(db: Session, users, portfolios):
    """The blocks a citation indexes into are the same split used at ingest;
    ``Source.index`` is the position the model cites by."""
    from app.services.chunking import split_sentences

    two_sentences = f"{TERM_CLAUSE} {RENT_CLAUSE}"
    ready_document(db, portfolios["city"], "Meridian House", [[two_sentences, BREAK_CLAUSE]])

    results = retrieval.search(
        db, query="31 March 2034 rent", visible_portfolio_ids=_visible(db, users, "manager")
    )
    sources = retrieval.to_sources(results)

    assert [s.index for s in sources] == list(range(len(results)))
    by_chunk = {s.chunk_id: s for s in sources}
    first = by_chunk[next(r.chunk.id for r in results if r.chunk.text == two_sentences)]
    assert first.blocks == split_sentences(two_sentences)
    assert len(first.blocks) == 2
    assert first.document_title == "Meridian House"
    assert first.page_number == 1


def test_config_records_what_the_ranking_depended_on():
    config = retrieval.config()

    assert {"k", "candidates", "rrf_k", "embedding_provider", "embedding_model"} <= set(config)
    assert config["embedding_provider"] == "hashed"
