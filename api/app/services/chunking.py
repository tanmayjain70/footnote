"""Sentences and chunks.

A citation points at a *block* -- a sentence -- inside a chunk, by index. That
only works if the sentences the model was shown are the sentences the page
viewer highlights later, so one function splits text into sentences and it is
used everywhere: when a page is chunked at ingest, when a stored chunk is
turned into the blocks the model may cite, and when a chunk is shown with its
blocks. A second implementation, however similar, would eventually disagree
with the first by one index, and a citation off by one sentence is exactly the
kind of quiet wrongness this product exists to prevent.

Two properties are relied on elsewhere and tested:

- A chunk never splits a sentence.
- A chunk's text is its sentences joined with a newline, so
  ``split_sentences(chunk.text)`` gives back exactly the sentences it was
  built from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: A sentence ends at terminal punctuation followed by whitespace and something
#: that looks like the start of another. Semicolons and colons count: lease
#: clauses are long lists joined by them, and a citation to one item in the
#: list is more useful than a citation to the whole list.
_BOUNDARY = re.compile(r"(?<=[.!?;:])\s+(?=[A-Z0-9(£\"'“‘])")
_SPACES = re.compile(r"[ \t\f\v ]+")

#: A clause number on its own -- "3.", "4.1", "(a)", "(ii)" -- is what the
#: boundary above leaves behind when a heading is numbered. It is not a
#: sentence anyone would cite, so it is folded into whatever follows it.
_LABEL_ONLY = re.compile(r"^(?:\(?\d+(?:\.\d+)*[.)]?|\([a-z]{1,4}\)|[A-Za-z][.)])$")

#: The overlap between consecutive chunks, in sentences. One sentence is enough
#: for a fact that straddles a boundary to appear whole in one of the two.
DEFAULT_OVERLAP = 1
DEFAULT_TARGET_CHARS = 1100


def split_sentences(text: str) -> list[str]:
    """Split text into sentence blocks. Deterministic, and idempotent on its
    own output joined with newlines."""
    blocks: list[str] = []
    for paragraph in text.split("\n"):
        paragraph = _SPACES.sub(" ", paragraph).strip()
        if not paragraph:
            continue
        pending_label: str | None = None
        for piece in _BOUNDARY.split(paragraph):
            piece = piece.strip()
            if not piece:
                continue
            if pending_label is not None:
                piece = f"{pending_label} {piece}"
                pending_label = None
            if _LABEL_ONLY.match(piece):
                pending_label = piece
                continue
            blocks.append(piece)
        if pending_label is not None:
            blocks.append(pending_label)
    return blocks


@dataclass(frozen=True)
class Chunk:
    page_number: int
    ordinal_in_page: int
    sentences: tuple[str, ...]
    #: Offsets into the page text the chunk was cut from, so a viewer can
    #: scroll to it. Approximate only when the page text was not already
    #: whitespace-normalised, which the PDF parser guarantees it is.
    char_start: int
    char_end: int
    #: Position within the whole document; assigned by ``chunk_document``.
    ordinal: int = 0

    @property
    def text(self) -> str:
        return "\n".join(self.sentences)

    @property
    def token_estimate(self) -> int:
        # Four characters per token is the usual rule of thumb for English
        # prose; this is a budget figure, not a count.
        return max(1, len(self.text) // 4)


def _positions(page_text: str, sentences: list[str]) -> list[tuple[int, int]]:
    """Where each sentence sits in the page text, found in order so a sentence
    that appears twice maps to its own occurrence."""
    positions: list[tuple[int, int]] = []
    cursor = 0
    for sentence in sentences:
        found = page_text.find(sentence, cursor)
        if found < 0:
            found = cursor
        end = found + len(sentence)
        positions.append((found, end))
        cursor = end
    return positions


def chunk_page(
    page_text: str,
    page_number: int,
    *,
    target_chars: int = DEFAULT_TARGET_CHARS,
    overlap_sentences: int = DEFAULT_OVERLAP,
) -> list[Chunk]:
    """Cut one page into chunks of whole sentences.

    Sentences are accumulated until adding the next would exceed the target;
    a single sentence longer than the target becomes a chunk on its own rather
    than being cut. Each chunk after the first starts with the last
    ``overlap_sentences`` sentence(s) of the one before, and every chunk
    contributes at least one sentence that no earlier chunk had, so the loop
    always advances.
    """
    sentences = split_sentences(page_text)
    if not sentences:
        return []
    positions = _positions(page_text, sentences)
    target = max(1, target_chars)
    overlap = max(0, overlap_sentences)

    chunks: list[Chunk] = []
    start = 0
    count = len(sentences)
    while start < count:
        end = start
        length = len(sentences[start])
        while end + 1 < count and length + 1 + len(sentences[end + 1]) <= target:
            end += 1
            length += 1 + len(sentences[end])
        chunks.append(
            Chunk(
                page_number=page_number,
                ordinal_in_page=len(chunks),
                sentences=tuple(sentences[start : end + 1]),
                char_start=positions[start][0],
                char_end=positions[end][1],
            )
        )
        if end + 1 >= count:
            break
        start = max(end + 1 - overlap, start + 1)
    return chunks


def chunk_document(
    pages: list[str],
    *,
    target_chars: int | None = None,
    overlap_sentences: int = DEFAULT_OVERLAP,
) -> list[Chunk]:
    """Chunk every page, numbering chunks across the whole document."""
    if target_chars is None:
        # Read lazily so this module stays importable without a configured
        # environment -- the generator's tests use it with no database at all.
        from app.core.config import get_settings

        target_chars = get_settings().chunk_target_chars

    chunks: list[Chunk] = []
    for page_number, page_text in enumerate(pages, start=1):
        for chunk in chunk_page(
            page_text,
            page_number,
            target_chars=target_chars,
            overlap_sentences=overlap_sentences,
        ):
            chunks.append(
                Chunk(
                    page_number=chunk.page_number,
                    ordinal_in_page=chunk.ordinal_in_page,
                    sentences=chunk.sentences,
                    char_start=chunk.char_start,
                    char_end=chunk.char_end,
                    ordinal=len(chunks),
                )
            )
    return chunks
