"""Hybrid retrieval: which lease, then which passage.

A question about a lease usually has two halves. "When does the term of the
Eccles Insurance Brokers lease at Lancastrian Business Park end?" names a
document with one half and asks about a clause with the other. Treating the
whole sentence as one query gets the first half right and the second wrong:
the cover page and the parties clause are dense with the names, so they
outrank the term clause on every measure, and the passage that answers the
question is not among the eight shown to the model. Measured on the demo's
golden set, that was the right document 93% of the time and the right page
33% of the time.

So retrieval runs in two stages. First the question's rarer terms are matched
against each visible document's identity -- its title, its metadata, and the
head of its first page -- and if two or more of them single out a document (or
a small tie), the search is confined to it. Then the *remaining* words, the
clause half, rank passages within that scope by two legs: cosine distance over
the embeddings and full-text rank over the stored tsvector, each contributing
its top candidates, fused by reciprocal rank rather than by score because a
distance and a text rank are not on the same scale. A question that names no
document runs both legs over everything the caller may see.

The portfolio filter is inside every query, not around them. A new endpoint
that calls ``search`` cannot forget it, and an empty visible set returns an
empty list without touching the database -- there is no unfiltered query to
run by mistake.
"""

from __future__ import annotations

import logging
import math
import re
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, String, Text, and_, any_, bindparam, cast, func, literal, select
from sqlalchemy.dialects.postgresql import ARRAY, REGCONFIG
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Session, joinedload

from app.core.config import get_settings
from app.models import Chunk, Document, DocumentPage
from app.models.enums import DocumentStatus
from app.providers import embeddings
from app.providers.llm import Source

logger = logging.getLogger(__name__)

#: The dictionary the ``chunks.tsv`` column was generated with. The query side
#: must use the same one or the stems will not line up.
TEXT_SEARCH_CONFIG = "english"

#: A question has to name a document with at least this many rare terms
#: before the search is confined to it. One term -- "Meridian" -- is a hint;
#: two -- "Meridian House" -- is a name.
MIN_IDENTIFYING_TERMS = 2

#: A term found in more than this share of the visible documents' identities
#: identifies nothing ("lease", "unit", "Manchester", the firm's own name).
COMMON_TERM_SHARE = 0.34

#: When several documents tie on the naming terms -- three leases in the same
#: building, asked about by building only -- the search covers all of them,
#: up to this many. More than that is not a name, it is a topic.
MAX_FOCUS_DOCUMENTS = 4

#: How much of the first page counts as identity. The cover of a lease names
#: the premises, the parties and the date; by six hundred characters it is
#: into the definitions.
FIRST_PAGE_HEAD = 600

_WORDS = re.compile(r"[A-Za-z0-9£][A-Za-z0-9£',.\-]*")
_CONFIG = cast(literal(TEXT_SEARCH_CONFIG), REGCONFIG)


@dataclass(frozen=True)
class Retrieved:
    """One chunk in the fused ranking, with where each leg placed it."""

    chunk: Chunk
    #: 0-based position in the list shown to the model; the model cites by it.
    source_index: int
    fused_score: float
    vector_rank: int | None
    lexical_rank: int | None


@dataclass(frozen=True)
class Fused:
    chunk_id: uuid.UUID
    score: float
    vector_rank: int | None
    lexical_rank: int | None


@dataclass(frozen=True)
class Focus:
    """What the question named, if anything.

    ``document_ids`` is empty when the question names no document, in which
    case ``passage_query`` is the question itself.
    """

    document_ids: list[uuid.UUID]
    matched_terms: list[str]
    passage_query: str

    @property
    def focused(self) -> bool:
        return bool(self.document_ids)


def config() -> dict[str, Any]:
    """The retrieval settings in force, recorded on every question.

    A change to ``k`` or to the embedding model is then visible on the
    questions it affected, and the evaluation harness can say what moved.
    """
    settings = get_settings()
    provider = embeddings.get_embedding_provider()
    return {
        "k": settings.retrieval_k,
        "candidates": settings.retrieval_candidates,
        "rrf_k": settings.retrieval_rrf_k,
        "embedding_provider": provider.name,
        "embedding_model": provider.model,
        "focus": {
            "min_terms": MIN_IDENTIFYING_TERMS,
            "common_share": COMMON_TERM_SHARE,
            "max_documents": MAX_FOCUS_DOCUMENTS,
        },
    }


def search(
    db: Session,
    *,
    query: str,
    visible_portfolio_ids: list[uuid.UUID],
    portfolio_id: uuid.UUID | None = None,
    k: int | None = None,
    candidates: int | None = None,
) -> list[Retrieved]:
    """The top ``k`` chunks for ``query`` among the documents the caller may see.

    ``visible_portfolio_ids`` is the caller's whole visible set;
    ``portfolio_id`` narrows it further. Both are applied inside the SQL.
    """
    settings = get_settings()
    k = k or settings.retrieval_k
    candidates = candidates or settings.retrieval_candidates

    visible = list(visible_portfolio_ids)
    query = query.strip()
    if not visible or not query:
        return []

    focus_ = focus(db, query, visible, portfolio_id)
    scope = focus_.document_ids or None
    if focus_.focused:
        logger.info(
            "question names %d document(s) by %s; passages ranked by %r",
            len(focus_.document_ids),
            focus_.matched_terms,
            focus_.passage_query,
        )

    vector_ids = _vector_leg(db, focus_.passage_query, visible, portfolio_id, candidates, scope)
    lexical_ids = _lexical_leg(db, focus_.passage_query, visible, portfolio_id, candidates, scope)
    fused = fuse(vector_ids, lexical_ids, rrf_k=settings.retrieval_rrf_k, k=k)
    if not fused:
        return []

    # One query for the rows, with the document for its title. The fused order
    # is reapplied in Python because IN (...) returns rows in any order.
    rows = db.execute(
        select(Chunk)
        .options(joinedload(Chunk.document))
        .where(Chunk.id.in_([f.chunk_id for f in fused]))
    ).scalars()
    by_id = {chunk.id: chunk for chunk in rows}

    results: list[Retrieved] = []
    for entry in fused:
        chunk = by_id.get(entry.chunk_id)
        if chunk is None:
            # Deleted between the ranking and the load. Rare, harmless: the
            # source indexes stay dense because they are assigned here.
            continue
        results.append(
            Retrieved(
                chunk=chunk,
                source_index=len(results),
                fused_score=entry.score,
                vector_rank=entry.vector_rank,
                lexical_rank=entry.lexical_rank,
            )
        )
    return results


def fuse(
    vector_ids: list[uuid.UUID], lexical_ids: list[uuid.UUID], *, rrf_k: int, k: int
) -> list[Fused]:
    """Reciprocal rank fusion of two rankings: ``score = sum 1 / (rrf_k + rank)``.

    A chunk that is second on both legs beats one that is first on only one
    of them, which is the property wanted: agreement between two different
    kinds of evidence is worth more than either alone. Ties go to the better
    vector rank, then to the chunk id so the order is stable across runs.
    """
    vector_rank = {chunk_id: rank for rank, chunk_id in enumerate(vector_ids, start=1)}
    lexical_rank = {chunk_id: rank for rank, chunk_id in enumerate(lexical_ids, start=1)}

    scores: dict[uuid.UUID, float] = defaultdict(float)
    for chunk_id, rank in vector_rank.items():
        scores[chunk_id] += 1.0 / (rrf_k + rank)
    for chunk_id, rank in lexical_rank.items():
        scores[chunk_id] += 1.0 / (rrf_k + rank)

    ordered = sorted(scores, key=lambda cid: (-scores[cid], vector_rank.get(cid, math.inf), cid))
    return [
        Fused(cid, scores[cid], vector_rank.get(cid), lexical_rank.get(cid)) for cid in ordered[:k]
    ]


def to_sources(retrieved: list[Retrieved]) -> list[Source]:
    """What the model is shown: each chunk split into the sentence blocks a
    citation will index into. The same splitter ran at ingest, so a block
    range in a citation maps back to text on the page."""
    # Imported where used, as ingestion does: a broken splitter should fail
    # the question it is answering, not the import of the router every
    # request in the API is mounted behind.
    from app.services.chunking import split_sentences

    return [
        Source(
            index=r.source_index,
            chunk_id=r.chunk.id,
            document_id=r.chunk.document_id,
            document_title=r.chunk.document.title,
            page_number=r.chunk.page_number,
            blocks=split_sentences(r.chunk.text),
        )
        for r in retrieved
    ]


# ----------------------------------------------------------- which lease --


def focus(
    db: Session,
    query: str,
    visible: list[uuid.UUID],
    portfolio_id: uuid.UUID | None = None,
) -> Focus:
    """Decide which document(s) the question names, and what is left to ask.

    Rarity is judged against the documents the caller can see, not a fixed
    stopword list: "Stamford" identifies a building in one portfolio and is
    noise in a portfolio where every lease is in Stamford Quarter.
    """
    words = _WORDS.findall(query)
    if not words:
        return Focus([], [], query)

    lexemes_of = _lexemes_by_word(db, words)
    query_lexemes: list[str] = []
    for word in words:
        for lexeme in lexemes_of.get(word, ()):
            if lexeme not in query_lexemes:
                query_lexemes.append(lexeme)
    if len(query_lexemes) < MIN_IDENTIFYING_TERMS:
        return Focus([], [], query)

    identities = _identities(db, visible, portfolio_id)
    if not identities:
        return Focus([], [], query)

    # A lexeme's document frequency, over the named part of each identity
    # and the first-page part alike.
    frequency: Counter[str] = Counter()
    for named, page in identities.values():
        frequency.update(named | page)
    ceiling = max(1, math.floor(len(identities) * COMMON_TERM_SHARE))
    identifying = [
        lexeme for lexeme in query_lexemes if 0 < frequency.get(lexeme, 0) <= ceiling
    ]
    if len(identifying) < MIN_IDENTIFYING_TERMS:
        return Focus([], [], query)

    # The title and metadata are the document's own name for itself; a hit
    # there is worth two hits in the running text of the cover page, where a
    # unit number can be a date and a street can be another lease's.
    scores: dict[uuid.UUID, int] = {}
    for document_id, (named, page) in identities.items():
        score = sum(2 for lexeme in identifying if lexeme in named)
        score += sum(1 for lexeme in identifying if lexeme in page and lexeme not in named)
        scores[document_id] = score
    best = max(scores.values())
    if best < MIN_IDENTIFYING_TERMS:
        return Focus([], [], query)

    chosen = sorted(document_id for document_id, score in scores.items() if score == best)
    # A focus that does not narrow anything is not a focus: with one or two
    # documents in view every word is "rare", and stripping them would only
    # damage the question.
    if len(chosen) > MAX_FOCUS_DOCUMENTS or len(chosen) >= len(identities):
        return Focus([], [], query)

    # Only the words that matched a chosen document's own name come out of
    # the passage query. A first-page match helped pick the document, but the
    # cover of a lease is running text -- "notice", "rent", "tenant" -- and
    # those are exactly the words the clause half of the question needs.
    matched = [lexeme for lexeme in identifying if any(lexeme in identities[d][0] for d in chosen)]
    matched_set = set(matched)
    remaining = [
        word
        for word in words
        if not lexemes_of.get(word) or not (set(lexemes_of[word]) <= matched_set)
    ]
    passage_query = " ".join(remaining).strip() or query
    return Focus(chosen, matched, passage_query)


def _lexemes_by_word(db: Session, words: list[str]) -> dict[str, tuple[str, ...]]:
    """Each word's lexemes under the text-search dictionary, in one round trip.

    Done in the database rather than with a Python stemmer so the query side
    and the ``chunks.tsv`` column can never disagree about a stem.
    """
    unique = list(dict.fromkeys(words))
    word = func.unnest(bindparam("words", unique, type_=ARRAY(String))).column_valued("word")
    stmt = select(word, func.tsvector_to_array(func.to_tsvector(_CONFIG, word)))
    return {w: tuple(lexemes or ()) for w, lexemes in db.execute(stmt).all()}


def _identities(
    db: Session, visible: list[uuid.UUID], portfolio_id: uuid.UUID | None
) -> dict[uuid.UUID, tuple[set[str], set[str]]]:
    """Per visible document: the lexemes of its name (title and metadata
    values) and of the head of its first page."""
    named_text = func.concat_ws(" ", Document.title, cast(Document.metadata_, Text))
    page_text = func.coalesce(func.left(DocumentPage.text, FIRST_PAGE_HEAD), "")
    stmt = (
        select(
            Document.id,
            func.tsvector_to_array(func.to_tsvector(_CONFIG, named_text)),
            func.tsvector_to_array(func.to_tsvector(_CONFIG, page_text)),
        )
        .outerjoin(
            DocumentPage,
            and_(DocumentPage.document_id == Document.id, DocumentPage.page_number == 1),
        )
        .where(
            Document.status == DocumentStatus.READY,
            Document.portfolio_id
            == any_(bindparam("visible", visible, type_=ARRAY(PgUUID(as_uuid=True)))),
        )
    )
    if portfolio_id is not None:
        stmt = stmt.where(Document.portfolio_id == portfolio_id)
    return {
        document_id: (set(named or ()), set(page or ()))
        for document_id, named, page in db.execute(stmt).all()
    }


# ------------------------------------------------------------- the legs --


def _scoped(
    stmt: Select[Any],
    visible: list[uuid.UUID],
    portfolio_id: uuid.UUID | None,
    scope: list[uuid.UUID] | None,
) -> Select[Any]:
    """Join the document and apply the access filter. Every leg goes through
    here; there is no path to ``chunks`` that skips it."""
    stmt = stmt.join(Document, Document.id == Chunk.document_id).where(
        Document.status == DocumentStatus.READY,
        Document.portfolio_id
        == any_(bindparam("visible", visible, type_=ARRAY(PgUUID(as_uuid=True)))),
    )
    if portfolio_id is not None:
        stmt = stmt.where(Document.portfolio_id == portfolio_id)
    if scope:
        stmt = stmt.where(Chunk.document_id.in_(scope))
    return stmt


def _vector_leg(
    db: Session,
    query: str,
    visible: list[uuid.UUID],
    portfolio_id: uuid.UUID | None,
    n: int,
    scope: list[uuid.UUID] | None,
) -> list[uuid.UUID]:
    qvec = embeddings.get_embedding_provider().embed_query(query)
    distance = Chunk.embedding.cosine_distance(qvec).label("distance")
    # ORDER BY the distance expression alone: that is the shape the HNSW index
    # serves. A secondary sort key would turn it into a full scan.
    stmt = (
        _scoped(select(Chunk.id, distance), visible, portfolio_id, scope)
        .where(Chunk.embedding.is_not(None))
        .order_by(distance)
        .limit(n)
    )
    return list(db.execute(stmt).scalars())


def _lexical_leg(
    db: Session,
    query: str,
    visible: list[uuid.UUID],
    portfolio_id: uuid.UUID | None,
    n: int,
    scope: list[uuid.UUID] | None,
) -> list[uuid.UUID]:
    # Any term, ranked by how many and how close, rather than every term: a
    # question is not a search string, and one word the passage does not
    # contain must not empty the whole leg.
    words = _WORDS.findall(query)
    if not words:
        return []
    tsquery = func.websearch_to_tsquery(_CONFIG, " OR ".join(dict.fromkeys(words)))

    # A question made only of stopwords ("what is the") parses to an empty
    # query, which matches nothing and ranks everything at zero. Skip the leg
    # rather than pay for a scan that says so.
    if db.execute(select(func.numnode(tsquery))).scalar_one() == 0:
        return []

    rank = func.ts_rank_cd(Chunk.tsv, tsquery).label("rank")
    stmt = (
        _scoped(select(Chunk.id, rank), visible, portfolio_id, scope)
        .where(Chunk.tsv.op("@@")(tsquery))
        .order_by(rank.desc(), Chunk.id)
        .limit(n)
    )
    return list(db.execute(stmt).scalars())
