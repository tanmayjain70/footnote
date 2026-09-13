"""Embedding providers: the hashed one must be boring and the real one must be real."""

from __future__ import annotations

import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.providers import embeddings
from app.providers.embeddings import (
    DIMENSIONS,
    FastEmbedEmbeddings,
    HashedEmbeddings,
    content_tokens,
    get_embedding_provider,
)

API_DIR = Path(__file__).resolve().parents[1]
FASTEMBED_CACHE = API_DIR / ".fastembed_cache"
CACHED_MODEL = FASTEMBED_CACHE / "models--qdrant--bge-small-en-v1.5-onnx-q"


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def norm(a: list[float]) -> float:
    return math.sqrt(sum(x * x for x in a))


# ---------------------------------------------------------------- hashed --


def test_hashed_vectors_have_384_unit_dimensions():
    vector = HashedEmbeddings().embed_query("The Term shall expire on 31 March 2034.")
    assert len(vector) == DIMENSIONS == 384
    assert norm(vector) == pytest.approx(1.0)
    assert all(math.isfinite(x) for x in vector)


def test_hashed_is_deterministic_across_instances():
    """The vectors live in the database; a different result on a different
    machine would make every stored embedding silently wrong."""
    text = "The Tenant may determine this Lease on 31 March 2029."
    assert HashedEmbeddings().embed_query(text) == HashedEmbeddings().embed_query(text)
    assert HashedEmbeddings().embed_documents([text, text]) == [
        HashedEmbeddings().embed_query(text)
    ] * 2


def test_distinct_texts_differ():
    provider = HashedEmbeddings()
    a = provider.embed_query("rent review every five years")
    b = provider.embed_query("insurance of the building")
    assert a != b
    assert cosine(a, b) < 0.5


def test_all_stopword_text_is_a_unit_vector_not_nan():
    """Nothing to hash must still be a valid vector: a NaN would poison the
    HNSW index and a zero vector has no distance to anything."""
    provider = HashedEmbeddings()
    for text in ("", "the and of shall be", "   "):
        vector = provider.embed_query(text)
        assert not any(math.isnan(x) for x in vector)
        assert vector[0] == 1.0
        assert norm(vector) == pytest.approx(1.0)


def test_related_texts_are_closer_than_unrelated():
    provider = HashedEmbeddings()
    query = provider.embed_query("when is the rent review date")
    related, unrelated = provider.embed_documents(
        [
            "The rent review date is the fifth anniversary of the Term Commencement Date.",
            "The Landlord shall insure the Building against the Insured Risks.",
        ]
    )
    assert cosine(query, related) > cosine(query, unrelated)


def test_query_and_document_use_the_same_function():
    provider = HashedEmbeddings()
    text = "break notice of not less than six months"
    assert provider.embed_query(text) == provider.embed_documents([text])[0]


def test_embedding_no_documents_is_no_vectors():
    assert HashedEmbeddings().embed_documents([]) == []


def test_content_tokens_drop_stopwords_and_contractions():
    assert content_tokens("What is the Tenant's VAT number?") == ["tenant", "vat", "number"]
    assert content_tokens("") == []


# --------------------------------------------------------------- factory --


@pytest.fixture
def fresh_factory():
    get_embedding_provider.cache_clear()
    yield
    get_embedding_provider.cache_clear()


def test_factory_reads_the_configured_provider(monkeypatch, fresh_factory):
    monkeypatch.setattr(
        embeddings,
        "get_settings",
        lambda: SimpleNamespace(
            embedding_provider="hashed", fastembed_cache_dir=".fastembed_cache", fastembed_threads=2
        ),
    )
    provider = get_embedding_provider()
    assert isinstance(provider, HashedEmbeddings)
    assert provider.dimensions == 384
    assert get_embedding_provider() is provider, "cached, not rebuilt per call"


def test_factory_builds_fastembed_lazily(monkeypatch, fresh_factory):
    """Choosing fastembed must not load the model: that happens on the first
    embed, so the app starts quickly and a missing model fails the first
    ingest, not the process."""
    monkeypatch.setattr(
        embeddings,
        "get_settings",
        lambda: SimpleNamespace(
            embedding_provider="fastembed", fastembed_cache_dir="/nowhere", fastembed_threads=3
        ),
    )
    provider = get_embedding_provider()
    assert isinstance(provider, FastEmbedEmbeddings)
    assert provider.model == "BAAI/bge-small-en-v1.5"
    assert provider.threads == 3


def test_factory_rejects_an_unknown_provider(monkeypatch, fresh_factory):
    monkeypatch.setattr(
        embeddings, "get_settings", lambda: SimpleNamespace(embedding_provider="openai")
    )
    with pytest.raises(ValueError, match="EMBEDDING_PROVIDER"):
        get_embedding_provider()


# ------------------------------------------------------------- fastembed --


@pytest.mark.skipif(
    not CACHED_MODEL.is_dir(),
    reason=(
        "no cached bge-small model under api/.fastembed_cache; run the app once with "
        "EMBEDDING_PROVIDER=fastembed to download it"
    ),
)
def test_fastembed_loads_the_cached_model_and_ranks_by_meaning(monkeypatch):
    # Offline on purpose: the test is about the cached model, and a network
    # fetch would hide a broken cache behind a slow pass.
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    provider = FastEmbedEmbeddings(cache_dir=str(FASTEMBED_CACHE), threads=2)

    query = provider.embed_query("rent review")
    on_topic, off_topic = provider.embed_documents(
        ["the rent shall be reviewed", "insurance of the building"]
    )

    assert len(query) == len(on_topic) == len(off_topic) == 384
    assert norm(query) == pytest.approx(1.0, abs=1e-3)
    assert cosine(query, on_topic) > cosine(query, off_topic)
    assert provider.embed_documents([]) == []
