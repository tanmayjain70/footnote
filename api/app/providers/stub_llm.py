"""An extractive answerer that needs no model and no network.

It does what the product promises, in the smallest way that is still real: it
finds the sentence in each passage that shares the most words with the
question, repeats those sentences, and cites each one by source and block.
When nothing overlaps it says so and cites nothing.

That makes it deterministic, free, and honest about its limits -- which is
exactly what the tests, the seed and the evaluation harness need, and why it
is a provider rather than a mock.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator, Sequence

from app.providers.embeddings import content_tokens
from app.providers.llm import (
    AnswerEvent,
    CitationDelta,
    ExtractedField,
    ExtractionOutput,
    FieldLike,
    Finished,
    Source,
    TextDelta,
    Usage,
)

REFUSAL_TEXT = (
    "I can't answer that from the documents I can see. "
    "The closest passages are shown on the right."
)

#: Two words in common is a passage about the question; one is a coincidence,
#: unless the question itself only has one or two words to offer.
MIN_SCORE = 2
SHORT_QUESTION_TOKENS = 3
MAX_SOURCES = 3

#: Streaming is visible in the browser at roughly this many characters per
#: event. Splits happen at spaces so no word is ever cut in half.
PIECE_CHARS = 40
#: "expiry" and "expires" share this much; so do "review" and "reviewed".
PREFIX_CHARS = 5

_WORD_AND_SPACE = re.compile(r"\S+\s*")

#: A question asks in plain English about a clause written in lease English.
#: These are the handful of substitutions that let "when does it end" find
#: "shall expire on"; everything else relies on the prefix rule.
SYNONYMS = {
    "end": "expire",
    "ends": "expire",
    "ending": "expire",
    "finish": "expire",
    "finishes": "expire",
    "start": "commence",
    "starts": "commence",
    "begin": "commence",
    "begins": "commence",
    "annual": "annum",
    "yearly": "annum",
    "guarantee": "guarantor",
    "guaranteed": "guarantor",
    "break": "determine",
    "terminate": "determine",
    "reviewed": "review",
    "repairs": "repair",
    "repairing": "repair",
}

#: Words that are in every clause of every lease. They never decide whether a
#: sentence answers the question, unless they are all the question has.
GENERIC = frozenset({"tenant", "landlord", "lease", "premises", "property", "building", "unit"})

#: Of the question's remaining content words, this share has to appear in a
#: sentence before the sentence counts as an answer. Half is a compromise the
#: evaluation harness measured: at 0.6 the stub refused a natural question
#: about break notice; at 0.4 it answered "VAT registration number" with the
#: VAT clause. It still does the latter sometimes -- an extractive matcher
#: cannot tell a registration number from a registered company -- and the
#: harness reports exactly how often.
MATCH_SHARE = 0.5


def question_terms(question: str, sources: list[Source]) -> list[str]:
    """The words of a question that are about the *clause*, not the lease.

    A question usually names its lease -- "the Fallowfield Creative Studios
    lease at Spring Gardens" -- and those words are in every clause's
    neighbourhood on the cover and in the parties clause, so they would match
    anything. Capitalised words after the first, and any word in a retrieved
    document's title, are treated as the name and set aside; what is left is
    what the answer has to contain. If nothing is left, the whole question
    stands.
    """
    names: set[str] = set()
    for position, word in enumerate(question.split()):
        # All-caps words are acronyms -- VAT, RPI -- not names.
        if position and word[:1].isupper() and not word.isupper():
            names.update(content_tokens(word))
    for source in sources:
        names.update(content_tokens(source.document_title))

    tokens = [SYNONYMS.get(token, token) for token in content_tokens(question)]
    remaining = [token for token in dict.fromkeys(tokens) if token not in names]
    specific = [token for token in remaining if token not in GENERIC]
    return specific or remaining or list(dict.fromkeys(tokens))


def match_score(question_tokens: set[str], block_tokens: set[str]) -> int:
    """How many of the question's content words the block contains.

    A word counts when it appears exactly or when the block has a word with
    the same first five letters, so an inflected form still matches.
    """
    prefixes = {t[:PREFIX_CHARS] for t in block_tokens if len(t) >= PREFIX_CHARS}
    score = 0
    for token in question_tokens:
        exact = token in block_tokens
        inflected = len(token) >= PREFIX_CHARS and token[:PREFIX_CHARS] in prefixes
        if exact or inflected:
            score += 1
    return score


def pieces(block: int, text: str) -> Iterator[TextDelta]:
    """Yield ``text`` as TextDeltas of about PIECE_CHARS, split at spaces.

    The pieces concatenate back to ``text`` exactly, whitespace included.
    """
    buffer = ""
    for match in _WORD_AND_SPACE.finditer(text):
        buffer += match.group(0)
        if len(buffer) >= PIECE_CHARS:
            yield TextDelta(block, buffer)
            buffer = ""
    if buffer:
        yield TextDelta(block, buffer)


class StubLLM:
    name = "stub"
    model = "extractive-v1"

    def answer(self, question: str, sources: list[Source]) -> Iterator[AnswerEvent]:
        question_tokens = set(question_terms(question, sources))
        if len(question_tokens) < SHORT_QUESTION_TOKENS:
            threshold = 1
        else:
            threshold = max(MIN_SCORE, math.ceil(len(question_tokens) * MATCH_SHARE))
        input_chars = sum(len(block) for source in sources for block in source.blocks)

        # (score, source index, block index, block text) for the best block of
        # every source that clears the bar.
        candidates: list[tuple[int, int, int, str]] = []
        for source in sources:
            best_score, best_block, best_text = 0, -1, ""
            for b, block in enumerate(source.blocks):
                score = match_score(question_tokens, set(content_tokens(block)))
                if score > best_score:
                    best_score, best_block, best_text = score, b, block
            if best_score >= threshold:
                candidates.append((best_score, source.index, best_block, best_text))

        # Highest score first; ties keep retrieval order, which is already the
        # order of fused relevance.
        candidates.sort(key=lambda c: (-c[0], c[1]))
        kept = candidates[:MAX_SOURCES]

        if not kept:
            yield from pieces(0, REFUSAL_TEXT)
            yield Finished(
                "end_turn",
                Usage(input_tokens=input_chars // 4, output_tokens=len(REFUSAL_TEXT) // 4),
                self.model,
            )
            return

        output_chars = 0
        for block, (_score, source_index, b, text) in enumerate(kept):
            answer_text = f"{text} "
            output_chars += len(answer_text)
            yield from pieces(block, answer_text)
            yield CitationDelta(block, source_index, b, b + 1, text)

        yield Finished(
            "end_turn",
            Usage(input_tokens=input_chars // 4, output_tokens=output_chars // 4),
            self.model,
        )

    def extract(
        self, document_title: str, fields: Sequence[FieldLike], chunks: list[Source]
    ) -> ExtractionOutput:
        input_chars = sum(len(block) for chunk in chunks for block in chunk.blocks)
        results = [self._extract_field(spec, chunks) for spec in fields]
        return ExtractionOutput(
            fields=results,
            usage=Usage(input_tokens=input_chars // 4, output_tokens=200),
            model=self.model,
        )

    @staticmethod
    def _extract_field(spec: FieldLike, chunks: list[Source]) -> ExtractedField:
        missing = ExtractedField(spec.key, None, None, None, "low")
        if not spec.stub_pattern:
            return missing
        pattern = re.compile(spec.stub_pattern)

        for chunk in chunks:
            # Search the chunk as one string so a pattern may cross a sentence
            # boundary, then quote the sentence(s) the match fell in: the
            # reviewer sees a whole sentence, and the verifier can find it in
            # the chunk text.
            text = " ".join(chunk.blocks)
            match = pattern.search(text)
            if match is None:
                continue
            value = match.group("value") if "value" in pattern.groupindex else match.group(0)
            quote = _blocks_covering(chunk.blocks, match.start(), match.end())
            return ExtractedField(spec.key, value.strip(), quote, str(chunk.chunk_id), "high")
        return missing


def _blocks_covering(blocks: list[str], start: int, end: int) -> str:
    """The sentence block(s) of ``" ".join(blocks)`` that span ``[start, end)``."""
    covered: list[str] = []
    offset = 0
    for block in blocks:
        block_end = offset + len(block)
        if block_end > start and offset < end:
            covered.append(block)
        offset = block_end + 1  # the joining space
        if offset >= end:
            break
    return " ".join(covered)
