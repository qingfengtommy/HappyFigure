"""Proposal file loading with support for multiple formats (Markdown, PDF, LaTeX).

Agent-first mode: the agent reads files directly — this module just gathers paths.
Python-stages / LangGraph: ``extract_text()`` provides inline-able text for prompts.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Extensions recognised as proposal content (text-extractable).
TEXT_EXTENSIONS = {".md", ".tex", ".bib", ".txt", ".rst", ".latex"}
PDF_EXTENSIONS = {".pdf"}
PROPOSAL_EXTENSIONS = TEXT_EXTENSIONS | PDF_EXTENSIONS


def gather_proposal_files(path: Path) -> list[Path]:
    """Return proposal-relevant files from *path* (file or directory).

    For a single file, returns ``[path]``.  For a directory, globs the
    top level for recognised extensions; falls back to one level deep.
    Only returns text-extractable files (no images).
    """
    if path.is_file():
        return [path]

    if not path.is_dir():
        return []

    files = sorted(f for f in path.glob("*") if f.is_file() and f.suffix.lower() in PROPOSAL_EXTENSIONS)
    if not files:
        files = sorted(f for f in path.glob("*/*") if f.is_file() and f.suffix.lower() in PROPOSAL_EXTENSIONS)
    return files


def extract_text(path: Path) -> str:
    """Extract inline-able text content from *path*.

    - ``.md``, ``.tex``, ``.bib``, ``.txt``, ``.rst`` → read as UTF-8
    - ``.pdf`` → extract via ``pymupdf`` (optional dependency)
    - Unrecognised extensions → empty string with warning
    """
    if not path.is_file():
        return ""

    suffix = path.suffix.lower()

    if suffix in TEXT_EXTENSIONS:
        return path.read_text(encoding="utf-8")

    if suffix in PDF_EXTENSIONS:
        return _extract_pdf(path)

    logger.warning("Unknown proposal file type %s — skipping", path.name)
    return ""


def _extract_pdf(path: Path) -> str:
    """Extract text from a PDF using pymupdf (fitz)."""
    try:
        import fitz  # pymupdf
    except ImportError:
        logger.warning(
            "pymupdf not installed — cannot extract text from %s. Install with: pip install pymupdf",
            path.name,
        )
        return f"[PDF: {path.name} — install pymupdf for text extraction]"

    doc = None
    try:
        doc = fitz.open(str(path))
        parts: list[str] = []
        for page in doc:
            text = page.get_text()
            if text.strip():
                parts.append(text)
        return "\n\n".join(parts)
    except Exception as exc:
        logger.warning("Failed to extract text from %s: %s", path.name, exc)
        return f"[PDF: {path.name} — extraction failed: {exc}]"
    finally:
        if doc is not None:
            doc.close()
