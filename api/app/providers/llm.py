"""The language model contract: what the application sends, what it gets back.

The shapes here are deliberately narrow. A ``Source`` is one retrieved chunk
split into sentence blocks; the model cites by source index and block range,
never by quoting prose, so the answering service can check every citation
against what the model was actually shown. A provider streams ``TextDelta``
and ``CitationDelta`` events and ends with exactly one ``Finished``.

Services call ``llm.get_llm_provider()`` through the module, not by importing
the function, so a test can swap the provider with one monkeypatch.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Protocol

from app.core.config import get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Source:
    """One retrieved chunk as shown to the model."""

    index: int  # 0-based position in the list; the model cites by this
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    page_number: int
    blocks: list[str]  # sentence blocks, from chunking.split_sentences


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class TextDelta:
    block: int
    text: str


@dataclass(frozen=True)
class CitationDelta:
    block: int
    source_index: int
    block_start: int
    block_end: int  # exclusive
    cited_text: str


@dataclass(frozen=True)
class Finished:
    stop_reason: str  # end_turn | max_tokens | refusal | error
    usage: Usage
    model: str


AnswerEvent = TextDelta | CitationDelta | Finished


@dataclass(frozen=True)
class ExtractedField:
    key: str
    value_text: str | None
    quote: str | None
    chunk_id: str | None
    confidence: str  # high | medium | low


@dataclass(frozen=True)
class ExtractionOutput:
    fields: list[ExtractedField] = field(default_factory=list)
    usage: Usage = Usage()
    model: str = ""


class FieldLike(Protocol):
    """What a provider needs to know about a registry field.

    The registry itself lives in ``services.extraction_fields``; the providers
    only read these attributes, so they depend on the shape rather than the
    module.
    """

    key: str
    label: str
    type: str
    description: str
    enum_values: tuple[str, ...]
    stub_pattern: str


class LLMProvider(Protocol):
    name: str  # "stub" | "anthropic"
    model: str

    def answer(self, question: str, sources: list[Source]) -> Iterator[AnswerEvent]: ...

    def extract(
        self, document_title: str, fields: Sequence[FieldLike], chunks: list[Source]
    ) -> ExtractionOutput: ...


class ProviderError(RuntimeError):
    """The model could not be asked, or did not answer in a usable shape.

    Raised from ``extract`` so the extraction job records ``failed`` with the
    reason. ``answer`` never raises; it ends the stream with
    ``Finished("error")`` instead, because half an answer has already been
    sent to the browser by then.
    """


@lru_cache
def get_llm_provider() -> LLMProvider:
    settings = get_settings()
    # Imported here rather than at the top: both modules import this one for
    # the event types.
    from app.providers.stub_llm import StubLLM

    if settings.llm_provider == "anthropic":
        if not settings.anthropic_api_key:
            # The app must come up without a key -- a fresh checkout, CI, a
            # reviewer's laptop -- so this is a warning and a fallback, not a
            # startup failure. /health reports which provider is really live.
            logger.warning(
                "LLM_PROVIDER is anthropic but ANTHROPIC_API_KEY is empty; "
                "answering with the extractive stub instead"
            )
            return StubLLM()
        from app.providers.anthropic_llm import AnthropicLLM

        return AnthropicLLM.from_settings(settings)
    if settings.llm_provider == "stub":
        return StubLLM()
    raise ValueError(f"LLM_PROVIDER is {settings.llm_provider!r}; expected 'stub' or 'anthropic'.")


def reset_provider_cache() -> None:
    """Forget the cached providers so the next call re-reads settings.

    For tests that change ``LLM_PROVIDER`` or ``EMBEDDING_PROVIDER`` between
    cases.
    """
    from app.providers.embeddings import get_embedding_provider

    get_llm_provider.cache_clear()
    get_embedding_provider.cache_clear()
