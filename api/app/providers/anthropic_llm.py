"""Claude, through the Anthropic SDK, with citations the API checks for us.

Each retrieved chunk goes to the model as a *document* whose content is the
chunk's sentence blocks. With citations enabled the API returns, alongside
the answer text, which document and which block range each sentence came
from -- as structured data, not as prose the model was asked to format. The
answering service then verifies every one of those references against the
list it sent. A citation the model invents cannot survive that.

The request stays on the stable ``client.messages`` surface with no beta
headers, so it validates on every current model. ``thinking`` is left out
(adaptive by default) and ``temperature`` is never sent.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterator, Sequence
from typing import Any, Literal

import anthropic
from pydantic import BaseModel

from app.providers.llm import (
    AnswerEvent,
    CitationDelta,
    ExtractedField,
    ExtractionOutput,
    FieldLike,
    Finished,
    ProviderError,
    Source,
    TextDelta,
    Usage,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_MAX_RETRIES = 2
EXTRACTION_MAX_TOKENS = 8000

#: Keep this byte-for-byte stable between requests: it is the cached prefix.
SYSTEM_PROMPT = """\
You answer questions for the property managers of a commercial property firm, \
strictly from the lease documents supplied with each question.

Rules:
- Use only what the supplied documents say. Cite every factual sentence.
- If the documents do not contain the answer, say exactly that in one sentence \
and cite nothing. Do not guess, and do not answer from general knowledge.
- Never infer a value that is not written. If a date, amount or period is not \
stated, say it is not stated.
- Quote dates and sums of money exactly as they are written in the document.
- Keep answers short: a sentence or two is usually enough.
- Write in British English."""

EXTRACTION_PROMPT = """\
You extract the key terms of a commercial lease into a fixed set of fields.

The document is supplied as numbered chunks. Each chunk begins with a bracketed \
label of the form [chunk <id>] (page <n>).

For every field listed below return one entry with:
- key: the field key, exactly as listed.
- value: the value exactly as it is written in the document, or null when the \
document does not state it. Do not normalise, convert or infer.
- quote: the sentence containing the value, copied verbatim from ONE chunk, or \
null when value is null.
- chunk_id: the id copied from that chunk's bracketed label, or null.
- confidence: high when the sentence states the value plainly, medium when it \
must be read from context, low when it is unclear.

Return every field, including the ones that are absent. Never invent a value.

Fields:
{fields}"""


class FieldOut(BaseModel):
    key: str
    value: str | None
    quote: str | None
    chunk_id: str | None
    confidence: Literal["high", "medium", "low"]


class ExtractionSchema(BaseModel):
    fields: list[FieldOut]


def document_block(source: Source) -> dict[str, Any]:
    """One retrieved chunk as a citable document."""
    # A source with no sentences would be rejected by the API and, if skipped,
    # would shift every later document index. A placeholder keeps the indexes
    # honest; nothing sensible cites it.
    blocks = source.blocks or ["(no text)"]
    return {
        "type": "document",
        "source": {
            "type": "content",
            "content": [{"type": "text", "text": block} for block in blocks],
        },
        "title": f"{source.document_title} — page {source.page_number}",
        "context": json.dumps(
            {
                "source_index": source.index,
                "chunk_id": str(source.chunk_id),
                "page": source.page_number,
            }
        ),
        "citations": {"enabled": True},
    }


def extraction_system_prompt(fields: Sequence[FieldLike]) -> str:
    lines = []
    for spec in fields:
        line = f"- {spec.key} ({spec.type}): {spec.label}. {spec.description}"
        if spec.enum_values:
            line += f" One of: {', '.join(spec.enum_values)}."
        lines.append(line)
    return EXTRACTION_PROMPT.format(fields="\n".join(lines))


def _usage(usage: anthropic.types.Usage) -> Usage:
    return Usage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_input_tokens or 0,
        cache_write_tokens=usage.cache_creation_input_tokens or 0,
    )


def _merge_usage(seen: Usage, reported: Any) -> Usage:
    """Fold a mid-stream usage report into what is known so far.

    ``message_start`` carries the input tokens and ``message_delta`` the
    output count as it grows, each field appearing only when the API has
    something to say about it. Taking the larger of the two never invents
    tokens and never forgets any.
    """
    latest = _usage(reported)
    return Usage(
        input_tokens=max(seen.input_tokens, latest.input_tokens),
        output_tokens=max(seen.output_tokens, latest.output_tokens),
        cache_read_tokens=max(seen.cache_read_tokens, latest.cache_read_tokens),
        cache_write_tokens=max(seen.cache_write_tokens, latest.cache_write_tokens),
    )


def _stop_reason(reason: str | None) -> str:
    # The application knows four outcomes. Anything else the API might say
    # (stop_sequence, pause_turn) still produced an answer.
    if reason == "refusal":
        return "refusal"
    if reason in {"max_tokens", "model_context_window_exceeded"}:
        return "max_tokens"
    return "end_turn"


class AnthropicLLM:
    name = "anthropic"

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        max_tokens: int = 2048,
        effort: str = "medium",
        api_key: str | None = None,
        base_url: str | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ):
        self.model = model
        self.max_tokens = max_tokens
        self.effort = effort
        self._api_key = api_key or None
        self._base_url = base_url or None
        self._max_retries = max_retries
        self._client: anthropic.Anthropic | None = None
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: Any) -> AnthropicLLM:
        return cls(
            model=settings.llm_model,
            max_tokens=settings.llm_max_tokens,
            effort=settings.llm_effort,
            api_key=settings.anthropic_api_key,
            base_url=settings.anthropic_base_url,
            # Not every deployment's settings know this one yet; the SDK's own
            # default is the sensible fallback.
            max_retries=getattr(settings, "anthropic_max_retries", DEFAULT_MAX_RETRIES),
        )

    @property
    def client(self) -> anthropic.Anthropic:
        # Built on first use so that constructing the provider -- which happens
        # at startup -- cannot fail on credentials; the first answer can, and
        # reports it as an event rather than a crash.
        if self._client is None:
            with self._lock:
                if self._client is None:
                    self._client = anthropic.Anthropic(
                        api_key=self._api_key,
                        base_url=self._base_url,
                        max_retries=self._max_retries,
                    )
        return self._client

    # ---------------------------------------------------------- answering --

    def answer(self, question: str, sources: list[Source]) -> Iterator[AnswerEvent]:
        content: list[dict[str, Any]] = [document_block(source) for source in sources]
        content.append({"type": "text", "text": question})

        # Tokens as they are reported, so a stream that breaks part-way still
        # says what it spent. Declared before the request, because the request
        # is what can fail first.
        spent = Usage()

        try:
            with self.client.messages.stream(
                model=self.model,
                max_tokens=self.max_tokens,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                output_config={"effort": self.effort},
                messages=[{"role": "user", "content": content}],
            ) as stream:
                # API content index -> dense text block number. Thinking and
                # other non-text blocks take API indexes but are not part of
                # the answer, and the browser numbers what it shows.
                text_blocks: dict[int, int] = {}
                for event in stream:
                    if event.type == "message_start":
                        spent = _usage(event.message.usage)
                        continue
                    if event.type == "message_delta":
                        spent = _merge_usage(spent, event.usage)
                        continue
                    if event.type == "content_block_start":
                        if event.content_block.type == "text":
                            text_blocks[event.index] = len(text_blocks)
                        continue
                    if event.type != "content_block_delta":
                        continue
                    block = text_blocks.get(event.index)
                    if block is None:
                        continue
                    delta = event.delta
                    if delta.type == "text_delta":
                        yield TextDelta(block, delta.text)
                    elif delta.type == "citations_delta":
                        citation = delta.citation
                        if citation.type == "content_block_location":
                            yield CitationDelta(
                                block=block,
                                source_index=citation.document_index,
                                block_start=citation.start_block_index,
                                block_end=citation.end_block_index,
                                cited_text=citation.cited_text,
                            )
                final = stream.get_final_message()
        except anthropic.APIError as exc:
            logger.warning("anthropic answer failed: %s", exc)
            yield Finished("error", spent, self.model)
            return
        except Exception:
            # By this point text may already be on its way to the browser. An
            # exception here would tear down the SSE stream with no explanation;
            # a Finished("error") lets the service record what happened.
            logger.exception("unexpected failure while streaming an answer")
            yield Finished("error", spent, self.model)
            return

        if final.stop_reason == "refusal":
            logger.info(
                "anthropic refused: %s",
                final.stop_details.category if final.stop_details else "no category",
            )
        yield Finished(_stop_reason(final.stop_reason), _usage(final.usage), final.model)

    # --------------------------------------------------------- extraction --

    def extract(
        self, document_title: str, fields: Sequence[FieldLike], chunks: list[Source]
    ) -> ExtractionOutput:
        content: list[dict[str, Any]] = [{"type": "text", "text": f"Document: {document_title}"}]
        for chunk in chunks:
            text = " ".join(chunk.blocks)
            content.append(
                {
                    "type": "text",
                    "text": f"[chunk {chunk.chunk_id}] (page {chunk.page_number})\n{text}",
                }
            )

        try:
            message = self.client.messages.parse(
                model=self.model,
                max_tokens=EXTRACTION_MAX_TOKENS,
                system=extraction_system_prompt(fields),
                messages=[{"role": "user", "content": content}],
                output_format=ExtractionSchema,
            )
        except anthropic.APIError as exc:
            raise ProviderError(f"anthropic extraction failed: {exc}") from exc
        except ValueError as exc:
            # The SDK validates the JSON against the schema on the way in;
            # pydantic's ValidationError is a ValueError.
            raise ProviderError(f"anthropic returned an unusable extraction: {exc}") from exc

        parsed = message.parsed_output
        if parsed is None:
            raise ProviderError(
                f"anthropic returned no extraction (stop_reason={message.stop_reason})"
            )
        return ExtractionOutput(
            fields=[
                ExtractedField(f.key, f.value, f.quote, f.chunk_id, f.confidence)
                for f in parsed.fields
            ],
            usage=_usage(message.usage),
            model=message.model,
        )
