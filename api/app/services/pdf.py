"""Text out of a PDF, one string per page.

Only the text layer is read. A scanned lease is a PDF of pictures with no text
layer, and pretending to have read it would be worse than refusing: the first
question about it would be answered "not in the documents" for a reason nobody
could see. So a PDF with no extractable text is rejected with a message that
says why, and OCR is a separate, later piece of work (docs/brief.md, milestone 2).
"""

from __future__ import annotations

import io
import re

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.core.errors import UnprocessableUpload

_SPACES = re.compile(r"[ \t\f\v ]+")

NO_TEXT_MESSAGE = (
    "This PDF has no extractable text. Scanned documents need OCR, which is "
    "not part of this milestone."
)


def normalise(text: str) -> str:
    """Whitespace as the rest of the pipeline expects it.

    A PDF text layer arrives one visual line at a time. Lines are joined into
    paragraphs with single spaces; a blank line is kept as a paragraph break
    (one newline). The sentence splitter treats a newline as a hard boundary,
    so joining wrapped lines here is what keeps a sentence that wraps across
    lines in one piece -- and therefore in one citable block.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _SPACES.sub(" ", text)
    paragraphs: list[str] = []
    current: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        current.append(line)
    if current:
        paragraphs.append(" ".join(current))
    return "\n".join(paragraphs)


def extract_pages(data: bytes) -> list[str]:
    """The text of every page, normalised. Raises UnprocessableUpload when the
    bytes are not a readable PDF or no page has any text."""
    try:
        reader = PdfReader(io.BytesIO(data))
        # An owner password that only restricts printing still lets the text
        # be read with an empty user password; try that before giving up.
        if reader.is_encrypted and not reader.decrypt(""):
            raise UnprocessableUpload(
                "This PDF is password-protected and cannot be read.",
                detail={"reason": "encrypted"},
            )
        pages = [normalise(page.extract_text() or "") for page in reader.pages]
    except UnprocessableUpload:
        raise
    except (PdfReadError, ValueError, TypeError, KeyError, IndexError, OSError) as exc:
        raise UnprocessableUpload(
            "This file could not be read as a PDF.",
            detail={"reason": str(exc)[:200] or exc.__class__.__name__},
        ) from exc
    except Exception as exc:  # pypdf raises a long tail of its own types
        raise UnprocessableUpload(
            "This file could not be read as a PDF.",
            detail={"reason": str(exc)[:200] or exc.__class__.__name__},
        ) from exc

    if not pages or not any(page.strip() for page in pages):
        raise UnprocessableUpload(NO_TEXT_MESSAGE, detail={"reason": "no_text_layer"})
    return pages
