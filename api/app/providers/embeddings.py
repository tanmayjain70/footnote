"""Embedding providers: one that needs nothing, one that needs a model.

``HashedEmbeddings`` is feature hashing over words and word pairs. It knows
nothing about meaning, but two passages that share vocabulary land near each
other, it is identical on every machine, and it costs no download and no
model load. That is what the tests, CI and a fresh checkout want.

``FastEmbedEmbeddings`` is a real sentence model (bge-small, 384 dimensions)
run locally through ONNX. The demo uses it because "rent review" should find
"the rent shall be reviewed" even when the words differ.

Both produce 384 floats, so the ``chunks.embedding`` column and the HNSW
index are the same whichever is configured.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
from functools import lru_cache
from typing import TYPE_CHECKING, Protocol

from app.core.config import get_settings

if TYPE_CHECKING:
    from fastembed import TextEmbedding

logger = logging.getLogger(__name__)

DIMENSIONS = 384

#: Words that carry no signal about which passage is meant. Shared with the
#: stub answerer so that "content token" means the same thing everywhere.
#: ``shall``, ``may`` and ``must`` are here because a lease says them in
#: every clause; the one- and two-letter entries are what "tenant's" and
#: "don't" leave behind once the apostrophe is gone.
STOPWORDS = frozenset(
    """
    a about above after again all also am an and any are as at be because been
    before being below between both but by can could d did do does doing down
    during each few for from further had has have having he her here hers him
    his how i if in into is it its itself just ll m may me more most must my no
    nor not of off on once only or other our ours out over own re s same shall
    she should so some such t than that the their theirs them then there these
    they this those through to too under until up ve very was we were what when
    where which while who whom why will with would you your yours
    """.split()  # noqa: SIM905 - a word list reads as prose, not as 130 quoted strings
)

_TOKEN = re.compile(r"[a-z0-9]+")


def content_tokens(text: str) -> list[str]:
    """Lower-case word and number tokens with the stopwords removed."""
    return [t for t in _TOKEN.findall(text.lower()) if t not in STOPWORDS]


class EmbeddingProvider(Protocol):
    name: str
    model: str
    dimensions: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class HashedEmbeddings:
    """Deterministic feature hashing. No model, no network, no NaN."""

    name = "hashed"
    model = "hashed-384"
    dimensions = DIMENSIONS

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        # The same function for both sides: a query is just a short document,
        # and any asymmetry here would show up as a retrieval bug nobody can
        # reproduce by hand.
        return self._embed(text)

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        tokens = content_tokens(text)
        # Bigrams at half weight: "break notice" should pull towards passages
        # about break notices, without swamping the individual words.
        features = [(token, 1.0) for token in tokens]
        features += [(f"{a} {b}", 0.5) for a, b in zip(tokens, tokens[1:], strict=False)]

        for feature, weight in features:
            h = int(hashlib.blake2b(feature.encode(), digest_size=8).hexdigest(), 16)
            sign = 1.0 if (h >> 63) & 1 else -1.0
            vector[h % self.dimensions] += sign * weight

        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0.0:
            # Nothing but stopwords (or nothing at all). A zero vector has no
            # cosine distance to anything; a fixed unit vector is merely far
            # from everything, which is the honest answer.
            vector[0] = 1.0
            return vector
        return [v / norm for v in vector]


_fastembed_model: TextEmbedding | None = None
_fastembed_lock = threading.Lock()


class FastEmbedEmbeddings:
    """bge-small-en-v1.5 through fastembed, loaded once per process."""

    name = "fastembed"
    model = "BAAI/bge-small-en-v1.5"
    dimensions = DIMENSIONS

    #: bge is trained with this instruction on the query side only. Without it
    #: short queries drift away from the passages they should match.
    QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
    BATCH_SIZE = 32

    def __init__(self, cache_dir: str, threads: int = 2):
        self.cache_dir = cache_dir
        self.threads = threads

    def _model(self) -> TextEmbedding:
        # Loading takes about a second and a good deal of memory, so it happens
        # on the first embed rather than at import, and once per process rather
        # than once per provider instance.
        global _fastembed_model
        if _fastembed_model is None:
            with _fastembed_lock:
                if _fastembed_model is None:
                    from fastembed import TextEmbedding

                    logger.info("loading embedding model %s from %s", self.model, self.cache_dir)
                    _fastembed_model = TextEmbedding(
                        model_name=self.model, cache_dir=self.cache_dir, threads=self.threads
                    )
        return _fastembed_model

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = self._model().embed(texts, batch_size=self.BATCH_SIZE)
        return [[float(x) for x in vector] for vector in vectors]

    def embed_query(self, text: str) -> list[float]:
        vector = next(iter(self._model().embed([self.QUERY_PREFIX + text])))
        return [float(x) for x in vector]


@lru_cache
def get_embedding_provider() -> EmbeddingProvider:
    settings = get_settings()
    if settings.embedding_provider == "fastembed":
        return FastEmbedEmbeddings(
            cache_dir=settings.fastembed_cache_dir, threads=settings.fastembed_threads
        )
    if settings.embedding_provider == "hashed":
        return HashedEmbeddings()
    raise ValueError(
        f"EMBEDDING_PROVIDER is {settings.embedding_provider!r}; expected 'hashed' or 'fastembed'."
    )
