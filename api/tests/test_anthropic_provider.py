"""The Anthropic provider, exercised through the real SDK against a fake API.

What matters here is the request that leaves the process and the events that
come back: citations enabled on every document, the cache marker on the
system prompt, nothing deprecated, and a stream that ends with exactly one
``Finished`` whatever the API did.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.providers import llm, pricing
from app.providers.anthropic_llm import SYSTEM_PROMPT, AnthropicLLM
from app.providers.llm import (
    CitationDelta,
    ExtractedField,
    Finished,
    ProviderError,
    Source,
    TextDelta,
    Usage,
)
from app.providers.stub_llm import StubLLM
from tests.fake_anthropic import FakeAnthropic

QUESTION = "When can the tenant break the lease at Whitworth Court?"


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    type: str
    description: str
    enum_values: tuple[str, ...] = ()
    stub_pattern: str = ""


FIELDS = [
    Field("term_end", "Term end", "date", "The date the term expires."),
    Field("break_date", "Break date", "date", "The date the tenant may determine the lease."),
    Field(
        "repairing_obligation",
        "Repairing obligation",
        "enum",
        "Who repairs what.",
        enum_values=("full_repairing", "internal_repairing", "landlord_repairing"),
    ),
]


def make_sources() -> list[Source]:
    return [
        Source(
            index=0,
            chunk_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            document_title="Lease of Unit 4, Meridian House",
            page_number=3,
            blocks=[
                "The Term shall commence on 1 April 2024 and shall expire on 31 March 2034.",
                "The Initial Rent is £42,500 per annum.",
            ],
        ),
        Source(
            index=1,
            chunk_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            document_title="Lease of Unit 2B, Whitworth Court",
            page_number=7,
            blocks=[
                "The Tenant may determine this Lease on 31 March 2029 by giving the Landlord "
                "not less than 6 months' prior written notice.",
                "Time is of the essence.",
            ],
        ),
    ]


@pytest.fixture(scope="module")
def fake():
    with FakeAnthropic() as server:
        yield server


@pytest.fixture
def provider(fake: FakeAnthropic) -> AnthropicLLM:
    fake.reset()
    # No retries: a scripted 500 must surface as one failed request, not a
    # test that sleeps through the SDK's backoff.
    return AnthropicLLM(
        model="claude-opus-5",
        max_tokens=2048,
        effort="medium",
        api_key="test-key",
        base_url=fake.base_url,
        max_retries=0,
    )


# ---------------------------------------------------------------- answer --


def test_answer_sends_the_request_the_api_expects(provider, fake):
    sources = make_sources()
    list(provider.answer(QUESTION, sources))

    assert len(fake.requests) == 1
    request = fake.requests[0]
    assert request.path == "/v1/messages"
    assert request.headers["x-api-key"] == "test-key"
    assert "anthropic-beta" not in request.headers, "stays on the stable surface"

    body = request.body
    assert body["stream"] is True
    assert body["model"] == "claude-opus-5"
    assert body["max_tokens"] == 2048
    assert body["system"] == [
        {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
    ]
    assert body["output_config"] == {"effort": "medium"}
    assert "temperature" not in body
    assert "thinking" not in body
    assert "fallbacks" not in body

    (message,) = body["messages"]
    assert message["role"] == "user"
    *documents, question = message["content"]
    assert question == {"type": "text", "text": QUESTION}
    assert len(documents) == len(sources)
    for document, source in zip(documents, sources, strict=True):
        assert document["type"] == "document"
        assert document["source"]["type"] == "content"
        assert [b["text"] for b in document["source"]["content"]] == source.blocks
        assert all(b["type"] == "text" for b in document["source"]["content"])
        assert document["citations"] == {"enabled": True}
        assert document["title"] == f"{source.document_title} — page {source.page_number}"
        assert json.loads(document["context"]) == {
            "source_index": source.index,
            "chunk_id": str(source.chunk_id),
            "page": source.page_number,
        }


def test_answer_yields_text_citations_and_one_finished(provider, fake):
    events = list(provider.answer(QUESTION, make_sources()))

    texts = [e for e in events if isinstance(e, TextDelta)]
    cites = [e for e in events if isinstance(e, CitationDelta)]
    finished = [e for e in events if isinstance(e, Finished)]

    # The fake streams a thinking block first; answer blocks are still 0 and 1.
    assert sorted({t.block for t in texts}) == [0, 1]
    assert "".join(t.text for t in texts if t.block == 0) == fake.blocks[0].text
    assert "".join(t.text for t in texts if t.block == 1) == fake.blocks[1].text
    assert len(texts) > 2, "text arrives in pieces, not one lump"

    assert cites == [
        CitationDelta(
            block=0,
            source_index=1,
            block_start=0,
            block_end=1,
            cited_text=fake.blocks[0].citation["cited_text"],
        ),
        CitationDelta(
            block=1,
            source_index=1,
            block_start=0,
            block_end=2,
            cited_text=fake.blocks[1].citation["cited_text"],
        ),
    ]
    # Each citation arrives after the text of its own block.
    last_text_of_block_0 = max(events.index(t) for t in texts if t.block == 0)
    assert events.index(cites[0]) > last_text_of_block_0

    assert finished == [events[-1]]
    assert finished[0] == Finished(
        stop_reason="end_turn",
        usage=Usage(
            input_tokens=1200, output_tokens=42, cache_read_tokens=300, cache_write_tokens=900
        ),
        model="claude-opus-5",
    )


def test_a_refusal_becomes_finished_refusal(provider, fake):
    fake.next_refusal()
    events = list(provider.answer(QUESTION, make_sources()))

    assert not any(isinstance(e, TextDelta | CitationDelta) for e in events)
    assert events == [
        Finished(
            stop_reason="refusal",
            usage=Usage(
                input_tokens=1200, output_tokens=42, cache_read_tokens=300, cache_write_tokens=900
            ),
            model="claude-opus-5",
        )
    ]


def test_a_server_error_becomes_finished_error_not_an_exception(provider, fake, caplog):
    fake.next_error(500)
    with caplog.at_level(logging.WARNING, logger="app.providers.anthropic_llm"):
        events = list(provider.answer(QUESTION, make_sources()))

    assert events == [Finished("error", Usage(), "claude-opus-5")]
    assert len(fake.requests) == 1, "max_retries=0 means the SDK asked once"
    assert "anthropic answer failed" in caplog.text


def test_max_tokens_is_reported_as_such(provider, fake):
    from app.providers.anthropic_llm import _stop_reason

    assert _stop_reason("max_tokens") == "max_tokens"
    assert _stop_reason("model_context_window_exceeded") == "max_tokens"
    assert _stop_reason("end_turn") == "end_turn"
    assert _stop_reason("stop_sequence") == "end_turn"
    assert _stop_reason(None) == "end_turn"
    assert _stop_reason("refusal") == "refusal"


def test_the_client_is_built_lazily_from_settings():
    settings = SimpleNamespace(
        llm_model="claude-sonnet-5",
        llm_max_tokens=1024,
        llm_effort="high",
        anthropic_api_key="",
        anthropic_base_url=None,
    )
    provider = AnthropicLLM.from_settings(settings)
    assert provider.model == "claude-sonnet-5"
    assert provider.max_tokens == 1024
    assert provider.effort == "high"
    assert provider._max_retries == 2, "settings without the field get the SDK default"
    assert provider._client is None, "no SDK client until the first call"


# --------------------------------------------------------------- extract --


def test_extract_parses_the_structured_output(provider, fake):
    chunks = make_sources()
    fake.extraction = {
        "fields": [
            {
                "key": "term_end",
                "value": "31 March 2034",
                "quote": chunks[0].blocks[0],
                "chunk_id": str(chunks[0].chunk_id),
                "confidence": "high",
            },
            {
                "key": "break_date",
                "value": "31 March 2029",
                "quote": chunks[1].blocks[0],
                "chunk_id": str(chunks[1].chunk_id),
                "confidence": "medium",
            },
            {
                "key": "repairing_obligation",
                "value": None,
                "quote": None,
                "chunk_id": None,
                "confidence": "low",
            },
        ]
    }

    out = provider.extract("Lease of Unit 4, Meridian House", FIELDS, chunks)

    assert out.fields == [
        ExtractedField(
            "term_end", "31 March 2034", chunks[0].blocks[0], str(chunks[0].chunk_id), "high"
        ),
        ExtractedField(
            "break_date", "31 March 2029", chunks[1].blocks[0], str(chunks[1].chunk_id), "medium"
        ),
        ExtractedField("repairing_obligation", None, None, None, "low"),
    ]
    assert out.usage == Usage(
        input_tokens=1200, output_tokens=42, cache_read_tokens=300, cache_write_tokens=900
    )
    assert out.model == "claude-opus-5"

    body = fake.requests[0].body
    assert not body.get("stream")
    assert body["max_tokens"] == 8000
    assert "temperature" not in body
    assert "thinking" not in body
    schema = body["output_config"]["format"]
    assert schema["type"] == "json_schema"
    assert "fields" in schema["schema"]["properties"]

    system = body["system"]
    for spec in FIELDS:
        assert spec.key in system
        assert spec.label in system
        assert spec.description in system
    assert "full_repairing, internal_repairing, landlord_repairing" in system
    assert "verbatim" in system and "null" in system

    content = body["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "Document: Lease of Unit 4, Meridian House"}
    assert all(block["type"] == "text" for block in content), "no document blocks here"
    for block, chunk in zip(content[1:], chunks, strict=True):
        label, text = block["text"].split("\n", 1)
        assert label == f"[chunk {chunk.chunk_id}] (page {chunk.page_number})"
        assert text == " ".join(chunk.blocks)


def test_extract_raises_provider_error_on_api_failure(provider, fake):
    fake.next_error(500)
    with pytest.raises(ProviderError, match="extraction failed"):
        provider.extract("Lease", FIELDS, make_sources())
    assert len(fake.requests) == 1


def test_extract_raises_provider_error_when_nothing_parseable_comes_back(provider, fake):
    fake.next_refusal()
    with pytest.raises(ProviderError, match="refusal"):
        provider.extract("Lease", FIELDS, make_sources())


def test_extract_raises_provider_error_on_off_schema_json(provider, fake):
    fake.extraction = {"fields": [{"key": "term_end", "confidence": "certain"}]}
    with pytest.raises(ProviderError, match="unusable"):
        provider.extract("Lease", FIELDS, make_sources())


# --------------------------------------------------------------- pricing --


def test_cost_of_a_call_uses_all_four_token_prices():
    usage = Usage(
        input_tokens=1200, output_tokens=42, cache_read_tokens=300, cache_write_tokens=900
    )
    # (1200 * 5 + 42 * 25 + 300 * 0.5 + 900 * 6.25) / 1,000,000
    assert pricing.cost_usd("claude-opus-5", usage) == Decimal("0.012825")
    assert pricing.cost_usd("claude-haiku-4-5", Usage(output_tokens=1)) == Decimal("0.000005")


def test_cost_is_quantised_to_six_places_and_the_stub_is_free():
    cost = pricing.cost_usd("claude-sonnet-5", Usage(input_tokens=1, cache_read_tokens=3))
    assert cost.as_tuple().exponent == -6
    assert pricing.cost_usd("extractive-v1", Usage(input_tokens=10_000, output_tokens=200)) == 0


def test_an_unpriced_model_costs_nothing_and_warns_once(caplog):
    """Zero is wrong, but it is visibly wrong on the usage page; a guess would
    look like data. One warning, not one per call."""
    pricing._unpriced_warned.discard("claude-mystery-9")
    with caplog.at_level(logging.WARNING, logger="app.providers.pricing"):
        assert pricing.cost_usd("claude-mystery-9", Usage(input_tokens=500)) == Decimal(0)
        assert pricing.cost_usd("claude-mystery-9", Usage(input_tokens=500)) == Decimal(0)
    assert caplog.text.count("no price on record") == 1


# --------------------------------------------------------------- factory --


@pytest.fixture
def fresh_factory():
    llm.reset_provider_cache()
    yield
    llm.reset_provider_cache()


def settings_for(provider_name: str, api_key: str) -> SimpleNamespace:
    return SimpleNamespace(
        llm_provider=provider_name,
        llm_model="claude-opus-5",
        llm_effort="medium",
        llm_max_tokens=2048,
        anthropic_api_key=api_key,
        anthropic_base_url=None,
        anthropic_max_retries=1,
    )


def test_factory_returns_the_stub_when_configured(monkeypatch, fresh_factory):
    monkeypatch.setattr(llm, "get_settings", lambda: settings_for("stub", "sk-ant-unused"))
    provider = llm.get_llm_provider()
    assert isinstance(provider, StubLLM)
    assert llm.get_llm_provider() is provider, "cached"


def test_factory_returns_anthropic_with_a_key(monkeypatch, fresh_factory):
    monkeypatch.setattr(llm, "get_settings", lambda: settings_for("anthropic", "sk-ant-test"))
    provider = llm.get_llm_provider()
    assert isinstance(provider, AnthropicLLM)
    assert provider.name == "anthropic"
    assert provider.model == "claude-opus-5"
    assert provider._max_retries == 1


def test_factory_falls_back_to_the_stub_without_a_key(monkeypatch, fresh_factory, caplog):
    """A missing key must never stop the app starting; it must be visible."""
    monkeypatch.setattr(llm, "get_settings", lambda: settings_for("anthropic", ""))
    with caplog.at_level(logging.WARNING, logger="app.providers.llm"):
        provider = llm.get_llm_provider()
    assert isinstance(provider, StubLLM)
    assert "ANTHROPIC_API_KEY is empty" in caplog.text


def test_factory_rejects_an_unknown_provider(monkeypatch, fresh_factory):
    monkeypatch.setattr(llm, "get_settings", lambda: settings_for("openai", ""))
    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        llm.get_llm_provider()


def test_reset_forgets_the_cached_provider(monkeypatch, fresh_factory):
    monkeypatch.setattr(llm, "get_settings", lambda: settings_for("stub", ""))
    first = llm.get_llm_provider()
    llm.reset_provider_cache()
    assert llm.get_llm_provider() is not first
