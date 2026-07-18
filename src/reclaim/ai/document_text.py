from __future__ import annotations

from pathlib import Path

from reclaim.ai._optional import require

# Local text extraction for Feature 1b (spec §2: "Text extracted locally (docx/pdf/txt/md) —
# extraction is privacy-sensitive, stays on device, never logged"). This module contains NO
# logging calls anywhere, on purpose — not even at debug level, not even a filename-only log
# line — because the easiest way to guarantee document content never leaves the device is to
# never hand it to a logger in the first place. Callers receive plain strings and are
# responsible for the same discipline (never printing/logging extracted text).

_SUPPORTED_SUFFIXES = frozenset({".txt", ".md", ".docx", ".pdf"})


def is_supported_document(path: Path) -> bool:
    return path.suffix.lower() in _SUPPORTED_SUFFIXES


def extract_text(path: Path) -> str | None:
    """Returns the extracted plain text, or `None` (not an error) for an unsupported
    extension or a file that fails to parse — corrupt/password-protected/non-text-despite-
    extension files are common and expected, never worth raising for (same posture as
    `phash.compute_image_hashes` and `keep_best.score_image_quality`)."""
    suffix = path.suffix.lower()
    if suffix in (".txt", ".md"):
        return _extract_plain_text(path)
    if suffix == ".docx":
        return _extract_docx(path)
    if suffix == ".pdf":
        return _extract_pdf(path)
    return None


def _extract_plain_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _extract_docx(path: Path) -> str | None:
    docx = require("docx", feature="local .docx text extraction")
    try:
        document = docx.Document(str(path))
        return "\n".join(paragraph.text for paragraph in document.paragraphs)
    except Exception:
        # python-docx raises a variety of exception types (PackageNotFoundError, KeyError,
        # BadZipFile, ...) for a corrupt/non-docx file — all mean "not extractable," none
        # worth aborting the whole scan for.
        return None


def _extract_pdf(path: Path) -> str | None:
    pypdf = require("pypdf", feature="local .pdf text extraction")
    try:
        reader = pypdf.PdfReader(str(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        return None
