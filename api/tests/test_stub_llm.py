"""The extractive stub: deterministic, cites what it repeats, refuses otherwise."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.providers.llm import CitationDelta, ExtractedField, Finished, Source, TextDelta
from app.providers.stub_llm import PIECE_CHARS, REFUSAL_TEXT, StubLLM, match_score, pieces


@dataclass(frozen=True)
class Field:
    """The shape the provider reads; the real registry has the same attributes."""

    key: str
    label: str
    type: str
    description: str
    enum_values: tuple[str, ...] = ()
    stub_pattern: str = ""


def source(index: int, blocks: list[str], page: int = 3) -> Source:
    return Source(
        index=index,
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_title=f"Lease {index}",
        page_number=page,
        blocks=blocks,
    )


LEASE = source(
    0,
    [
        "The Term shall commence on 1 April 2024 and shall expire on 31 March 2034.",
        "The Initial Rent is £42,500 (forty-two thousand five hundred pounds) per annum.",
        "The Tenant may determine this Lease on 31 March 2029 by giving the Landlord not "
        "less than 6 months' prior written notice.",
    ],
)
INSURANCE = source(
    1,
    [
        "The Landlord shall insure the Building against the Insured Risks.",
        "The Tenant shall pay the Insurance Rent within 14 days of demand.",
    ],
)


def split(events):
    events = list(events)
    texts = [e for e in events if isinstance(e, TextDelta)]
    cites = [e for e in events if isinstance(e, CitationDelta)]
    finished = [e for e in events if isinstance(e, Finished)]
    return texts, cites, finished


# ---------------------------------------------------------------- answer --


def test_answer_repeats_and_cites_the_best_sentence():
    events = list(StubLLM().answer("When does the term expire?", [LEASE, INSURANCE]))
    texts, cites, finished = split(events)

    assert "".join(t.text for t in texts) == LEASE.blocks[0] + " "
    assert cites == [CitationDelta(0, source_index=0, block_start=0, block_end=1,
                                   cited_text=LEASE.blocks[0])]
    # The citation follows the text it belongs to, and the stream ends once.
    assert events.index(cites[0]) > max(events.index(t) for t in texts)
    assert finished == [events[-1]]
    assert finished[0].stop_reason == "end_turn"
    assert finished[0].model == "extractive-v1"
    shown = sum(len(b) for s in (LEASE, INSURANCE) for b in s.blocks)
    assert finished[0].usage.input_tokens == shown // 4
    assert finished[0].usage.output_tokens == (len(LEASE.blocks[0]) + 1) // 4


def test_inflected_words_match_by_prefix():
    """"expiry" in the question, "expire" in the lease: a real question and a
    real clause, and they only meet on the first five letters."""
    assert match_score({"expiry"}, {"expire"}) == 1
    assert match_score({"expiry"}, {"expo"}) == 0
    assert match_score({"rent"}, {"rental"}) == 0, "short words need an exact match"

    _, cites, _ = split(StubLLM().answer("What is the expiry of the term?", [LEASE]))
    assert [c.block_start for c in cites] == [0]


def test_refuses_when_no_sentence_overlaps_enough():
    events = list(StubLLM().answer("What is the tenant's VAT number?", [LEASE, INSURANCE]))
    texts, cites, finished = split(events)

    assert "".join(t.text for t in texts) == REFUSAL_TEXT
    assert {t.block for t in texts} == {0}
    assert cites == [], "a refusal cites nothing"
    assert finished[0].stop_reason == "end_turn"
    assert finished[0].usage.output_tokens == len(REFUSAL_TEXT) // 4


def test_refuses_with_no_sources_at_all():
    texts, cites, finished = split(StubLLM().answer("Who is the tenant?", []))
    assert "".join(t.text for t in texts) == REFUSAL_TEXT
    assert cites == []
    assert finished[0].usage.input_tokens == 0


def test_a_short_question_needs_only_one_word_in_common():
    """"Who is the tenant?" has one content word; demanding two would refuse
    every short question."""
    _, cites, _ = split(StubLLM().answer("Who is the tenant?", [LEASE, INSURANCE]))
    assert [(c.block, c.source_index, c.block_start) for c in cites] == [(0, 0, 2), (1, 1, 1)]


def test_keeps_at_most_three_sources_highest_first():
    sources = [
        source(0, ["The rent is payable quarterly and the insurance premium annually."]),
        source(1, ["The rent review date falls every fifth year."]),
        source(2, ["Rent review date and insurance of the building are in Schedule 2."]),
        source(3, ["Insurance of the building is the Landlord's responsibility."]),
        source(4, ["The rent review is upwards only, the review date being fixed."]),
    ]
    _, cites, _ = split(
        StubLLM().answer("rent review date insurance building", sources)
    )
    assert [c.source_index for c in cites] == [2, 1, 4], "best first, ties in retrieval order"
    assert [c.block for c in cites] == [0, 1, 2], "answer blocks are numbered densely"


def test_text_streams_in_pieces_split_at_spaces():
    text = " ".join(["word"] * 30) + " "
    out = list(pieces(4, text))
    assert len(out) > 1
    assert "".join(p.text for p in out) == text, "pieces rebuild the text exactly"
    assert all(p.block == 4 for p in out)
    assert all(p.text.endswith(" ") for p in out), "no word is cut in half"
    assert all(len(p.text) >= PIECE_CHARS for p in out[:-1])


def test_answer_is_deterministic():
    stub = StubLLM()
    question = "How much notice does the tenant give to break?"
    assert list(stub.answer(question, [LEASE, INSURANCE])) == list(
        stub.answer(question, [LEASE, INSURANCE])
    )


# --------------------------------------------------------------- extract --

FIELDS = [
    Field("term_end", "Term end", "date", "When the term expires.",
          stub_pattern=r"shall expire on (?P<value>\d{1,2} \w+ \d{4})"),
    Field("annual_rent_gbp", "Annual rent", "money", "The initial rent.",
          stub_pattern=r"Initial Rent is £(?P<value>[\d,]+)"),
    Field("guarantor", "Guarantor", "text", "Who guarantees the lease.",
          stub_pattern=r"The Guarantor is (?P<value>[^.]+)"),
    Field("permitted_use", "Permitted use", "text", "No pattern yet."),
]


def test_extract_takes_the_first_match_and_quotes_its_sentence():
    out = StubLLM().extract("Lease 0", FIELDS, [INSURANCE, LEASE])

    assert out.fields[0] == ExtractedField(
        "term_end", "31 March 2034", LEASE.blocks[0], str(LEASE.chunk_id), "high"
    )
    assert out.fields[1] == ExtractedField(
        "annual_rent_gbp", "42,500", LEASE.blocks[1], str(LEASE.chunk_id), "high"
    )
    assert out.fields[2] == ExtractedField("guarantor", None, None, None, "low")
    assert out.fields[3] == ExtractedField("permitted_use", None, None, None, "low")
    assert out.model == "extractive-v1"
    assert out.usage.output_tokens == 200
    shown = sum(len(b) for s in (INSURANCE, LEASE) for b in s.blocks)
    assert out.usage.input_tokens == shown // 4


def test_extract_prefers_the_earlier_chunk():
    later = source(2, ["The Term shall expire on 30 June 2030."])
    spec = FIELDS[0]
    out = StubLLM().extract("Lease", [spec], [LEASE, later])
    assert out.fields[0].value_text == "31 March 2034"
    assert out.fields[0].chunk_id == str(LEASE.chunk_id)


def test_extract_without_a_named_group_uses_the_whole_match():
    spec = Field("rent_words", "Rent in words", "text", "", stub_pattern=r"\([^)]+ pounds\)")
    out = StubLLM().extract("Lease", [spec], [LEASE])
    assert out.fields[0].value_text == "(forty-two thousand five hundred pounds)"
    assert out.fields[0].quote == LEASE.blocks[1]


def test_extract_quotes_every_sentence_a_match_spans():
    spec = Field(
        "span", "Span", "text", "", stub_pattern=r"per annum\. The Tenant may determine"
    )
    out = StubLLM().extract("Lease", [spec], [LEASE])
    assert out.fields[0].quote == LEASE.blocks[1] + " " + LEASE.blocks[2]


def test_extract_with_no_fields_or_chunks():
    assert StubLLM().extract("Lease", [], []).fields == []
    out = StubLLM().extract("Lease", FIELDS, [])
    assert all(f.value_text is None and f.confidence == "low" for f in out.fields)
