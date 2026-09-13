"""The lease generator, and the round trip it is coordinated on.

Every generated lease is rendered, read back through the real PDF parser,
chunked with the real chunker and extracted with the stub provider. The
point is not that the generator works but that the three modules agree: the
canonical sentence the generator prints is the one the registry's pattern
finds, on the page the golden question expects, inside a chunk whose text
contains the quote. If any of them drifts, this file is where it shows.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.demo import leases
from app.demo.leases import (
    CLAUSE_TEMPLATES,
    DEMO_LEASE_COUNT,
    FIELD_TEMPLATE,
    PORTFOLIO_PLAN,
    UNANSWERABLE_QUESTIONS,
    Distractor,
    GoldenQ,
    LeaseSpec,
    assign_portfolios,
    canonical_sentence,
    fact_sentences,
    format_date,
    format_money,
    generate_specs,
    golden_questions,
    money_words,
    present_fields,
    render_lease_pdf,
    sample_specs,
    unanswerable_questions,
    write_samples,
)
from app.providers.llm import Source
from app.providers.stub_llm import StubLLM
from app.services.chunking import Chunk, chunk_document
from app.services.extraction_fields import FIELD_BY_KEY, FIELDS, parse_value
from app.services.pdf import extract_pages

SAMPLES_DIR = Path(__file__).resolve().parents[2] / "samples"
FIELD_KEYS = [spec.key for spec in FIELDS]


def truth(spec: LeaseSpec, key: str) -> Any:
    """The registry value in its JSON form, as the register will hold it."""
    value = getattr(spec, key)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


@pytest.fixture(scope="module")
def specs() -> list[LeaseSpec]:
    return generate_specs(DEMO_LEASE_COUNT)


@pytest.fixture(scope="module")
def rendered(specs: list[LeaseSpec]) -> list[tuple[LeaseSpec, bytes, list[str]]]:
    """Every demo lease rendered and read back once for the module: the
    render-and-parse is the slow part and nothing here mutates it."""
    out = []
    for spec in specs:
        data = render_lease_pdf(spec)
        out.append((spec, data, extract_pages(data)))
    return out


# --------------------------------------------------------------- specs --


def test_specs_are_deterministic_per_seed():
    assert generate_specs(6, seed=7) == generate_specs(6, seed=7)
    assert generate_specs(6, seed=7) != generate_specs(6, seed=8)
    assert generate_specs(6) == generate_specs(6, seed=leases.DEFAULT_SEED)
    # A prefix of a longer run is the shorter run: adding leases does not
    # reshuffle the ones already there.
    assert generate_specs(6, seed=7) == generate_specs(12, seed=7)[:6]


def test_forty_eight_specs_follow_the_portfolio_plan(specs: list[LeaseSpec]):
    assert PORTFOLIO_PLAN == [
        ("City Centre", 20, False),
        ("Northern Estates", 18, False),
        ("Riverside", 10, True),
    ]
    assigned = assign_portfolios(specs)
    assert [spec for _, spec in assigned] == specs
    counts = {name: 0 for name, _, _ in PORTFOLIO_PLAN}
    for portfolio, spec in assigned:
        assert portfolio == spec.portfolio
        counts[portfolio] += 1
    assert counts == {name: count for name, count, _ in PORTFOLIO_PLAN}
    # Small runs are balanced too, so a test that generates six gets a mix.
    assert {spec.portfolio for spec in generate_specs(6, seed=7)} == set(counts)


def test_specs_are_distinct_and_well_typed(specs: list[LeaseSpec]):
    assert len({spec.reference for spec in specs}) == len(specs)
    assert len({(spec.property_name, spec.unit) for spec in specs}) == len(specs)
    assert len({spec.tenant_name for spec in specs}) == len(specs)
    assert len({spec.title for spec in specs}) == len(specs)
    assert len({spec.filename for spec in specs}) == len(specs)

    for spec in specs:
        assert re.fullmatch(r"HP/(CC|NE|RS)/\d{4}", spec.reference)
        assert spec.property_name in spec.property_address
        assert isinstance(spec.term_start, date) and isinstance(spec.term_end, date)
        assert isinstance(spec.rent_review_date, date) and isinstance(spec.lease_date, date)
        assert spec.term_end == spec.term_start.replace(
            year=spec.term_start.year + spec.term_years
        ) - timedelta(days=1)
        assert spec.lease_date < spec.term_start < spec.rent_review_date < spec.term_end
        assert isinstance(spec.term_years, int) and spec.term_years in (5, 10, 15, 20)
        assert isinstance(spec.annual_rent_gbp, Decimal) and spec.annual_rent_gbp > 0
        assert isinstance(spec.deposit_gbp, Decimal) and 0 < spec.deposit_gbp < spec.annual_rent_gbp
        assert spec.rent_review_basis in FIELD_BY_KEY["rent_review_basis"].enum_values
        assert spec.repairing_obligation in FIELD_BY_KEY["repairing_obligation"].enum_values
        assert isinstance(spec.vat_elected, bool)
        assert (spec.break_date is None) == (spec.break_notice_months is None)
        if spec.break_date is not None:
            assert spec.term_start < spec.break_date < spec.term_end
            assert spec.break_notice_months in (3, 6, 9, 12)
        assert (spec.guarantor is None) == (spec.guarantor_description is None)
        assert 2 <= len(spec.distractors) <= 3
        assert all(isinstance(d, Distractor) and d.sentence.endswith(".") for d in spec.distractors)
        assert set(spec.metadata) == {"property", "unit", "tenant", "reference"}

    # The demo set has every shape the register cares about.
    assert any(s.break_date and s.guarantor for s in specs)
    assert any(s.break_date is None and s.guarantor is None for s in specs)
    assert any(s.break_date is None and s.guarantor for s in specs)
    assert {s.rent_review_basis for s in specs} >= {"open_market", "rpi", "cpi", "fixed_uplift"}
    assert {s.repairing_obligation for s in specs} == set(
        FIELD_BY_KEY["repairing_obligation"].enum_values
    )
    assert {s.vat_elected for s in specs} == {True, False}


def test_the_shared_fixture_of_the_extraction_tests_has_both_shapes():
    """tests/test_extraction.py and test_register.py generate six leases with
    seed 7 and expect the first with a break to state all seventeen facts and
    the first without one to lack exactly the two break fields. Guard that
    here so a change to the generator's draws fails loudly, not there."""
    six = generate_specs(6, seed=7)
    with_break = next(s for s in six if s.break_date)
    without = next(s for s in six if not s.break_date)
    assert present_fields(with_break) == FIELD_KEYS
    assert set(FIELD_KEYS) - set(present_fields(without)) == {"break_date", "break_notice_months"}


def test_too_many_leases_is_a_clear_error():
    with pytest.raises(ValueError, match="no units left"):
        generate_specs(500)


# ------------------------------------------------------------ formatting --


def test_dates_and_money_print_the_way_the_patterns_expect():
    assert format_date(date(2024, 4, 1)) == "1 April 2024"
    assert format_date(date(2034, 3, 31)) == "31 March 2034"
    assert format_money(Decimal(42500)) == "£42,500"
    assert format_money(8000) == "£8,000"
    assert money_words(42500) == "forty-two thousand five hundred"
    assert money_words(112750) == "one hundred and twelve thousand seven hundred and fifty"
    assert money_words(10050) == "ten thousand and fifty"
    assert money_words(8000) == "eight thousand"
    assert money_words(1_250_000) == "one million two hundred and fifty thousand"


def test_canonical_sentences_match_the_examples_in_the_brief(specs: list[LeaseSpec]):
    """The three sentences the specification quotes, with the brief's values."""
    spec = specs[0]
    values = dict(
        vars(spec),
        term_start=date(2024, 4, 1),
        term_end=date(2034, 3, 31),
        annual_rent_gbp=Decimal(42500),
        break_date=date(2029, 3, 31),
        break_notice_months=6,
    )
    example = LeaseSpec(**values)
    sentences = fact_sentences(example)
    assert (
        sentences["term"]
        == "The Term shall commence on 1 April 2024 and shall expire on 31 March 2034."
    )
    assert (
        sentences["annual_rent_gbp"]
        == "The Initial Rent is £42,500 (forty-two thousand five hundred pounds) per annum."
    )
    assert sentences["break"] == (
        "The Tenant may determine this Lease on 31 March 2029 by giving the Landlord not "
        "less than 6 months' prior written notice."
    )
    assert set(CLAUSE_TEMPLATES) == set(FIELD_TEMPLATE.values())


# ------------------------------------------------------------- rendering --


def test_every_lease_renders_to_a_pdf_of_eight_to_fourteen_pages(rendered):
    for spec, data, pages in rendered:
        assert data.startswith(b"%PDF"), spec.reference
        assert 8 <= len(pages) <= 14, (spec.reference, len(pages))
        assert all(page.strip() for page in pages), "no blank pages"
        assert "’" not in "".join(pages) and "ﬁ" not in "".join(pages)


def test_rendering_is_byte_for_byte_reproducible(specs: list[LeaseSpec]):
    assert render_lease_pdf(specs[0]) == render_lease_pdf(specs[0])


def test_every_canonical_sentence_is_on_exactly_one_page(rendered):
    for spec, _, pages in rendered:
        for key, sentence in fact_sentences(spec).items():
            hits = [n for n, page in enumerate(pages, start=1) if sentence in page]
            assert len(hits) == 1, (spec.reference, key, hits)
        absent = {"break", "guarantor"} - set(fact_sentences(spec))
        for key in absent:
            assert canonical_sentence(spec, "break_date" if key == "break" else key) is None
        for distractor in spec.distractors:
            assert any(distractor.sentence in page for page in pages), distractor.key


def test_fact_pages_differ_between_leases(rendered):
    """The padding is doing its job: the same fact is not always on the same
    page, so a retrieval test cannot pass by learning a page number."""
    for key in ("break", "deposit_gbp", "vat_elected", "permitted_use"):
        pages_hit = set()
        for spec, _, pages in rendered:
            sentence = fact_sentences(spec).get(key)
            if sentence is not None:
                pages_hit.add(next(n for n, page in enumerate(pages, start=1) if sentence in page))
        assert len(pages_hit) >= 3, (key, pages_hit)


# ----------------------------------------------------- stub round trip --


def test_stub_patterns_round_trip_every_present_fact_and_nothing_else(rendered):
    for spec, _, pages in rendered:
        text = "\n".join(pages)
        for field in FIELDS:
            matches = [m.group("value") for m in re.finditer(field.stub_pattern, text)]
            if canonical_sentence(spec, field.key) is None:
                assert matches == [], (spec.reference, field.key, matches)
                continue
            assert len(matches) == 1, (spec.reference, field.key, matches)
            value, issue = parse_value(field, matches[0])
            assert issue is None, (spec.reference, field.key, matches[0])
            assert value == truth(spec, field.key), (spec.reference, field.key)


def _sources(spec: LeaseSpec, chunks: list[Chunk]) -> tuple[list[Source], dict[str, Chunk]]:
    document_id = uuid.uuid4()
    sources, by_id = [], {}
    for chunk in chunks:
        chunk_id = uuid.uuid4()
        by_id[str(chunk_id)] = chunk
        sources.append(
            Source(
                index=chunk.ordinal,
                chunk_id=chunk_id,
                document_id=document_id,
                document_title=spec.title,
                page_number=chunk.page_number,
                blocks=list(chunk.sentences),
            )
        )
    return sources, by_id


def test_the_stub_extracts_the_truth_with_a_quote_from_the_chunk_it_names(rendered):
    provider = StubLLM()
    for spec, _, pages in rendered:
        chunks = chunk_document(pages, target_chars=1100)
        sources, by_id = _sources(spec, chunks)
        output = provider.extract(spec.title, FIELDS, sources)
        assert [f.key for f in output.fields] == FIELD_KEYS
        for extracted in output.fields:
            field = FIELD_BY_KEY[extracted.key]
            if canonical_sentence(spec, field.key) is None:
                assert extracted.value_text is None and extracted.chunk_id is None
                assert extracted.confidence == "low"
                continue
            assert extracted.value_text is not None, (spec.reference, field.key)
            value, issue = parse_value(field, extracted.value_text)
            assert issue is None and value == truth(spec, field.key), (spec.reference, field.key)
            chunk = by_id[extracted.chunk_id]
            assert extracted.quote and extracted.quote in " ".join(chunk.sentences)
            assert canonical_sentence(spec, field.key) in extracted.quote
            assert canonical_sentence(spec, field.key) in pages[chunk.page_number - 1]


# ------------------------------------------------------------- questions --


def test_golden_questions_point_at_the_page_with_the_sentence(rendered):
    seen: set[str] = set()
    total = 0
    for spec, _, pages in rendered:
        questions = golden_questions(spec, pages)
        assert 3 <= len(questions) <= 4, spec.reference
        assert questions == golden_questions(spec, pages), "deterministic"
        for q in questions:
            assert isinstance(q, GoldenQ) and q.answerable and q.field_key in FIELD_KEYS
            assert q.question not in seen, q.question
            seen.add(q.question)
            assert q.question.endswith("?") and "{" not in q.question
            assert spec.property_name in q.question
            assert q.expected_pages and isinstance(q.expected_pages, list)
            sentence = canonical_sentence(spec, q.field_key)
            for page_number in q.expected_pages:
                assert sentence in pages[page_number - 1], (spec.reference, q.field_key)
        # One question per sentence: term start and end share one.
        templates = [FIELD_TEMPLATE[q.field_key] for q in questions]
        assert len(templates) == len(set(templates))
        total += len(questions)
    assert total >= 3 * len(rendered)


def test_golden_questions_vary_their_phrasing(rendered):
    questions = [q for spec, _, pages in rendered for q in golden_questions(spec, pages)]
    fields_asked = {q.field_key for q in questions}
    assert {"term_end", "annual_rent_gbp", "break_date", "break_notice_months"} <= fields_asked
    # Synonyms, not one template: expiry is asked both ways.
    ends = [q.question for q in questions if q.field_key == "term_end"]
    assert any("expire" in q for q in ends) and any(" end" in q for q in ends)


def test_twelve_unanswerable_questions(specs: list[LeaseSpec]):
    assert len(UNANSWERABLE_QUESTIONS) == 12
    questions = unanswerable_questions(specs)
    assert len(questions) == 12
    assert len({q.question for q in questions}) == 12
    for q in questions:
        assert q.answerable is False and q.expected_pages == [] and q.field_key is None
        assert "{" not in q.question and q.question.endswith("?")
    assert unanswerable_questions([]) == []
    # The things they ask about are never printed.
    text = "\n".join("".join(extract_pages(render_lease_pdf(s))) for s in specs[:2]).lower()
    for phrase in ("vat registration", "employees", "previous tenant", "bank account", "epc"):
        assert phrase not in text, phrase


# --------------------------------------------------------------- samples --


def test_write_samples_writes_three_distinct_leases_and_a_readme(tmp_path: Path):
    written = write_samples(tmp_path)
    assert len(written) == 3 and len(set(written)) == 3
    for path in written:
        assert path.suffix == ".pdf" and path.read_bytes().startswith(b"%PDF")
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    for path in written:
        assert path.name in readme
    assert "generated" in readme.lower() and "not answerable" in readme

    profiles = sample_specs()
    assert [tmp_path / spec.filename for _, spec in profiles] == written
    (_, with_both), (_, neither), (_, rpi) = profiles
    assert with_both.break_date and with_both.guarantor
    assert neither.break_date is None and neither.guarantor is None
    assert rpi.rent_review_basis == "rpi"


def test_the_committed_samples_are_what_the_generator_produces():
    """Regenerating must be a no-op; otherwise the committed PDFs and the
    README drift from the code that claims to have produced them."""
    for _, spec in sample_specs():
        committed = SAMPLES_DIR / spec.filename
        assert committed.exists(), f"run python -m app.demo.leases to write {committed.name}"
        assert committed.read_bytes() == render_lease_pdf(spec), committed.name
