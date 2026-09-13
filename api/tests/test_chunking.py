"""Sentences and chunks: the properties a citation's block index depends on.

A citation is (chunk, block range). It maps back to text only if the same
splitter produces the same blocks at ingest and at answer time, no chunk cuts
a sentence in half, and a chunk's text gives back exactly its sentences. The
tests use both hand-written text and a real generated lease, because the
lease is what the chunker will actually see.
"""

from __future__ import annotations

import pytest

from app.demo.leases import generate_specs, render_lease_pdf
from app.services.chunking import (
    DEFAULT_TARGET_CHARS,
    Chunk,
    chunk_document,
    chunk_page,
    split_sentences,
)
from app.services.pdf import extract_pages


@pytest.fixture(scope="module")
def lease_pages() -> list[str]:
    spec = generate_specs(1, seed=3)[0]
    return extract_pages(render_lease_pdf(spec))


# ------------------------------------------------------------- sentences --


def test_splits_at_terminal_punctuation_before_a_capital_or_digit():
    text = (
        "The Term shall commence on 1 April 2024. The Initial Rent is £42,500 per annum; "
        "It is payable quarterly: 25 March is the first Quarter Day. Is that clear? Yes! "
        '(The Plan is annexed). "Quoted" too.'
    )
    assert split_sentences(text) == [
        "The Term shall commence on 1 April 2024.",
        "The Initial Rent is £42,500 per annum;",
        "It is payable quarterly:",
        "25 March is the first Quarter Day.",
        "Is that clear?",
        "Yes!",
        "(The Plan is annexed).",
        '"Quoted" too.',
    ]
    # A closing bracket after the full stop is not a boundary; only the
    # punctuation itself is.
    assert split_sentences("(The Plan is annexed.) Next.") == ["(The Plan is annexed.) Next."]


def test_does_not_split_before_a_lowercase_word_or_inside_a_number():
    text = (
        "The rent is 4.5 per cent, i.e. above base rate. the tenant pays it; quarterly. "
        "Clause 19(1A) applies."
    )
    assert split_sentences(text) == [
        "The rent is 4.5 per cent, i.e. above base rate. the tenant pays it; quarterly.",
        "Clause 19(1A) applies.",
    ]


def test_newlines_are_hard_boundaries_and_blank_lines_vanish():
    assert split_sentences("first line\n\n  second line  \nthird") == [
        "first line",
        "second line",
        "third",
    ]


def test_a_bare_clause_number_is_folded_into_what_follows():
    """ "4." on its own is what a numbered heading leaves behind; nobody cites it."""
    assert split_sentences("4. Rent. 4.1 The Rent is payable quarterly. (a) in advance.") == [
        "4. Rent.",
        "4.1 The Rent is payable quarterly.",
        "(a) in advance.",
    ]
    assert split_sentences("3.") == ["3."], "a trailing label is kept rather than lost"


def test_split_is_idempotent_on_its_own_output():
    text = "One sentence. Another one; and a third: Fourth here. 4. Heading. 4.1 Body text."
    blocks = split_sentences(text)
    assert split_sentences("\n".join(blocks)) == blocks
    assert split_sentences("") == []
    assert split_sentences("   \n  ") == []


def test_runs_of_spaces_collapse():
    assert split_sentences("Two  words.   Three   more   words.") == [
        "Two words.",
        "Three more words.",
    ]


# ---------------------------------------------------------------- chunks --


def _contiguous_slices(page_text: str, chunks: list[Chunk]) -> None:
    """Every chunk is a contiguous run of the page's sentences, in order."""
    sentences = split_sentences(page_text)
    for chunk in chunks:
        start = sentences.index(chunk.sentences[0])
        assert tuple(sentences[start : start + len(chunk.sentences)]) == chunk.sentences


def test_chunks_are_whole_sentences_joined_by_newlines():
    page = " ".join(f"Sentence number {i} of the page is here." for i in range(40))
    chunks = chunk_page(page, 1, target_chars=200)
    assert len(chunks) > 1
    _contiguous_slices(page, chunks)
    for chunk in chunks:
        assert chunk.text == "\n".join(chunk.sentences)
        assert split_sentences(chunk.text) == list(chunk.sentences)
        assert len(chunk.text) <= 200 or len(chunk.sentences) == 1
        assert chunk.token_estimate == max(1, len(chunk.text) // 4)


def test_consecutive_chunks_overlap_by_one_sentence():
    page = " ".join(f"Sentence number {i} of the page is here." for i in range(40))
    chunks = chunk_page(page, 1, target_chars=200, overlap_sentences=1)
    for previous, following in zip(chunks, chunks[1:], strict=False):
        assert following.sentences[0] == previous.sentences[-1]
        assert len(following.sentences) > 1, "a chunk must add a sentence, not only repeat one"
    # No overlap when none is asked for.
    flat = chunk_page(page, 1, target_chars=200, overlap_sentences=0)
    joined = [s for chunk in flat for s in chunk.sentences]
    assert joined == split_sentences(page)


def test_char_offsets_index_the_page_text():
    page = "First sentence here. Second one follows.\nA new paragraph starts. And ends here."
    for chunk in chunk_page(page, 2, target_chars=45):
        assert page[chunk.char_start : chunk.char_end].startswith(chunk.sentences[0])
        assert page[chunk.char_start : chunk.char_end].endswith(chunk.sentences[-1])
        assert chunk.page_number == 2


def test_a_sentence_longer_than_the_target_becomes_its_own_chunk():
    long = "This single sentence is " + "very " * 60 + "long indeed."
    page = f"Short one. {long} Another short one."
    chunks = chunk_page(page, 1, target_chars=100, overlap_sentences=0)
    assert [len(c.sentences) for c in chunks] == [1, 1, 1]
    assert chunks[1].sentences == (long,)
    assert len(chunks[1].text) > 100


def test_empty_pages_produce_no_chunks():
    assert chunk_page("", 1) == []
    assert chunk_page("   \n ", 1) == []
    assert chunk_document(["", "One sentence.", ""], target_chars=100) == [
        Chunk(
            page_number=2,
            ordinal_in_page=0,
            sentences=("One sentence.",),
            char_start=0,
            char_end=13,
        )
    ]


def test_document_ordinals_are_contiguous_across_pages():
    pages = [" ".join(f"Page {p} sentence {i} is here." for i in range(12)) for p in range(1, 4)]
    chunks = chunk_document(pages, target_chars=120)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    assert [c.page_number for c in chunks] == sorted(c.page_number for c in chunks)
    for page_number in (1, 2, 3):
        in_page = [c for c in chunks if c.page_number == page_number]
        assert [c.ordinal_in_page for c in in_page] == list(range(len(in_page)))
    assert {c.page_number for c in chunks} == {1, 2, 3}


def test_the_default_target_comes_from_settings():
    page = " ".join(f"Sentence number {i} of the page is here." for i in range(200))
    by_default = chunk_document([page])
    explicit = chunk_document([page], target_chars=DEFAULT_TARGET_CHARS)
    assert by_default == explicit
    assert all(len(c.text) <= DEFAULT_TARGET_CHARS for c in by_default)


# ------------------------------------------------------- a real lease --


def test_a_generated_lease_chunks_cleanly(lease_pages: list[str]):
    chunks = chunk_document(lease_pages, target_chars=1100)
    assert len(chunks) >= len(lease_pages), "every page with text yields at least one chunk"
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    for page_number, page in enumerate(lease_pages, start=1):
        in_page = [c for c in chunks if c.page_number == page_number]
        _contiguous_slices(page, in_page)
        for chunk in in_page:
            assert split_sentences(chunk.text) == list(chunk.sentences)
            assert page[chunk.char_start : chunk.char_end].startswith(chunk.sentences[0])
            assert page[chunk.char_start : chunk.char_end].endswith(chunk.sentences[-1])
            assert len(chunk.text) <= 1100 or len(chunk.sentences) == 1
        for previous, following in zip(in_page, in_page[1:], strict=False):
            assert following.sentences[0] == previous.sentences[-1]
    # The union of chunks covers every sentence of every page.
    for page in lease_pages:
        covered = {s for c in chunks for s in c.sentences}
        assert set(split_sentences(page)) <= covered
