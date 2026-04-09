"""Tests for pipeline.proposal_loader — multi-format proposal ingestion."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pipeline.proposal_loader import (
    PDF_EXTENSIONS,
    PROPOSAL_EXTENSIONS,
    TEXT_EXTENSIONS,
    extract_text,
    gather_proposal_files,
)


# ===================================================================
# gather_proposal_files
# ===================================================================


class TestGatherSingleFile:
    """gather_proposal_files with a single file path."""

    @pytest.mark.parametrize("ext", [".md", ".pdf", ".tex", ".bib", ".txt", ".rst", ".latex"])
    def test_recognised_extension(self, tmp_path: Path, ext: str):
        f = tmp_path / f"paper{ext}"
        f.write_text("content") if ext != ".pdf" else f.write_bytes(b"%PDF")
        assert gather_proposal_files(f) == [f]

    def test_unrecognised_extension_still_returned(self, tmp_path: Path):
        """A single file is always returned — caller asked for it explicitly."""
        f = tmp_path / "data.csv"
        f.write_text("a,b,c")
        assert gather_proposal_files(f) == [f]

    def test_nonexistent_file_returns_empty(self, tmp_path: Path):
        assert gather_proposal_files(tmp_path / "nope.md") == []


class TestGatherDirectory:
    """gather_proposal_files with a directory path."""

    def test_mixed_formats(self, tmp_path: Path):
        (tmp_path / "intro.md").write_text("# Intro")
        (tmp_path / "main.tex").write_text(r"\begin{document}")
        (tmp_path / "refs.bib").write_text("@article{foo,}")
        files = gather_proposal_files(tmp_path)
        names = {f.name for f in files}
        assert names == {"intro.md", "main.tex", "refs.bib"}

    def test_images_excluded(self, tmp_path: Path):
        """Images should NOT appear in gather results (they can't be text-extracted)."""
        (tmp_path / "paper.md").write_text("# Paper")
        (tmp_path / "fig1.png").write_bytes(b"\x89PNG")
        (tmp_path / "photo.jpg").write_bytes(b"\xff\xd8")
        (tmp_path / "diagram.svg").write_text("<svg/>")
        files = gather_proposal_files(tmp_path)
        names = {f.name for f in files}
        assert names == {"paper.md"}

    def test_unrecognised_files_excluded(self, tmp_path: Path):
        (tmp_path / "data.csv").write_text("a,b")
        (tmp_path / "config.yaml").write_text("key: val")
        (tmp_path / "paper.md").write_text("# Paper")
        files = gather_proposal_files(tmp_path)
        assert [f.name for f in files] == ["paper.md"]

    def test_falls_back_to_subdirs(self, tmp_path: Path):
        sub = tmp_path / "sections"
        sub.mkdir()
        (sub / "methods.md").write_text("# Methods")
        (sub / "results.tex").write_text(r"\section{Results}")
        files = gather_proposal_files(tmp_path)
        names = {f.name for f in files}
        assert names == {"methods.md", "results.tex"}

    def test_top_level_takes_priority_over_subdirs(self, tmp_path: Path):
        """If top-level has matches, subdirs are not searched."""
        (tmp_path / "paper.md").write_text("# Top")
        sub = tmp_path / "old"
        sub.mkdir()
        (sub / "draft.md").write_text("# Old draft")
        files = gather_proposal_files(tmp_path)
        assert [f.name for f in files] == ["paper.md"]

    def test_empty_dir(self, tmp_path: Path):
        assert gather_proposal_files(tmp_path) == []

    def test_sorted_deterministically(self, tmp_path: Path):
        for name in ["c.md", "a.md", "b.tex"]:
            (tmp_path / name).write_text("x")
        files = gather_proposal_files(tmp_path)
        assert [f.name for f in files] == ["a.md", "b.tex", "c.md"]

    def test_case_insensitive_extensions(self, tmp_path: Path):
        (tmp_path / "PAPER.MD").write_text("# Paper")
        (tmp_path / "draft.PDF").write_bytes(b"%PDF")
        (tmp_path / "main.TEX").write_text(r"\doc")
        files = gather_proposal_files(tmp_path)
        assert len(files) == 3

    def test_skips_directories_matching_extension(self, tmp_path: Path):
        """A directory named 'section.md/' should not be returned."""
        (tmp_path / "section.md").mkdir()
        (tmp_path / "real.md").write_text("content")
        files = gather_proposal_files(tmp_path)
        assert [f.name for f in files] == ["real.md"]

    def test_latex_extension(self, tmp_path: Path):
        (tmp_path / "paper.latex").write_text(r"\documentclass{article}")
        files = gather_proposal_files(tmp_path)
        assert [f.name for f in files] == ["paper.latex"]

    def test_nonexistent_dir(self, tmp_path: Path):
        assert gather_proposal_files(tmp_path / "nope") == []


# ===================================================================
# extract_text
# ===================================================================


class TestExtractTextFormats:
    """extract_text for each supported text-based format."""

    @pytest.mark.parametrize("ext", [".md", ".tex", ".bib", ".txt", ".rst", ".latex"])
    def test_text_formats_read_as_utf8(self, tmp_path: Path, ext: str):
        f = tmp_path / f"file{ext}"
        f.write_text("Hello world — unicode: é ñ 中文", encoding="utf-8")
        result = extract_text(f)
        assert "Hello world" in result
        assert "中文" in result

    def test_preserves_latex_commands(self, tmp_path: Path):
        f = tmp_path / "paper.tex"
        content = r"""
\documentclass{article}
\usepackage{amsmath}
\begin{document}
\section{Introduction}
We prove that $E = mc^2$ using \eqref{eq:main}.
\end{document}
"""
        f.write_text(content)
        result = extract_text(f)
        assert r"\section{Introduction}" in result
        assert r"$E = mc^2$" in result

    def test_preserves_bibtex_entries(self, tmp_path: Path):
        f = tmp_path / "refs.bib"
        content = """@inproceedings{vaswani2017attention,
  title={Attention is all you need},
  author={Vaswani, Ashish},
  year={2017}
}"""
        f.write_text(content)
        result = extract_text(f)
        assert "vaswani2017attention" in result
        assert "Attention is all you need" in result

    def test_empty_file(self, tmp_path: Path):
        f = tmp_path / "empty.md"
        f.write_text("")
        assert extract_text(f) == ""


class TestExtractTextPDF:
    """extract_text for PDF files."""

    def test_graceful_without_pymupdf(self, tmp_path: Path):
        f = tmp_path / "paper.pdf"
        f.write_bytes(b"%PDF-1.4 fake")
        with patch.dict("sys.modules", {"fitz": None}):
            result = extract_text(f)
        assert "install pymupdf" in result.lower()
        assert "paper.pdf" in result

    @pytest.mark.skipif(
        not importlib.util.find_spec("fitz"),
        reason="pymupdf not installed",
    )
    def test_extracts_text_from_valid_pdf(self, tmp_path: Path):
        import fitz

        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Hello from PDF")
        pdf_path = tmp_path / "real.pdf"
        doc.save(str(pdf_path))
        doc.close()

        result = extract_text(pdf_path)
        assert "Hello from PDF" in result

    @pytest.mark.skipif(
        not importlib.util.find_spec("fitz"),
        reason="pymupdf not installed",
    )
    def test_multipage_pdf(self, tmp_path: Path):
        import fitz

        doc = fitz.open()
        for i in range(3):
            page = doc.new_page()
            page.insert_text((72, 72), f"Page {i + 1} content")
        pdf_path = tmp_path / "multi.pdf"
        doc.save(str(pdf_path))
        doc.close()

        result = extract_text(pdf_path)
        assert "Page 1 content" in result
        assert "Page 3 content" in result

    @pytest.mark.skipif(
        not importlib.util.find_spec("fitz"),
        reason="pymupdf not installed",
    )
    def test_empty_pdf(self, tmp_path: Path):
        """A PDF with no text content should return empty string."""
        import fitz

        doc = fitz.open()
        doc.new_page()  # blank page
        pdf_path = tmp_path / "empty.pdf"
        doc.save(str(pdf_path))
        doc.close()

        result = extract_text(pdf_path)
        assert result == ""

    def test_corrupt_pdf_with_pymupdf(self, tmp_path: Path):
        """Corrupt PDF should return an error placeholder, not raise."""
        fitz_mod = importlib.util.find_spec("fitz")
        if fitz_mod is None:
            pytest.skip("pymupdf not installed")
        f = tmp_path / "corrupt.pdf"
        f.write_bytes(b"this is not a pdf at all")
        result = extract_text(f)
        assert "extraction failed" in result.lower() or "PDF" in result


class TestExtractTextUnknown:
    """extract_text for unrecognised file types."""

    def test_returns_empty(self, tmp_path: Path):
        f = tmp_path / "data.csv"
        f.write_text("a,b,c")
        assert extract_text(f) == ""

    def test_logs_warning(self, tmp_path: Path, caplog):
        f = tmp_path / "data.xyz"
        f.write_text("stuff")
        with caplog.at_level("WARNING"):
            extract_text(f)
        assert "data.xyz" in caplog.text


# ===================================================================
# Extension set consistency
# ===================================================================


class TestExtensionSets:

    def test_no_overlap_text_and_pdf(self):
        assert TEXT_EXTENSIONS & PDF_EXTENSIONS == set()

    def test_proposal_is_union(self):
        assert PROPOSAL_EXTENSIONS == TEXT_EXTENSIONS | PDF_EXTENSIONS

    def test_all_extensions_start_with_dot(self):
        for ext in PROPOSAL_EXTENSIONS:
            assert ext.startswith("."), f"{ext} missing leading dot"


# ===================================================================
# Integration: gather + extract round-trip
# ===================================================================


class TestGatherExtractIntegration:
    """Verify that gather → extract works end-to-end."""

    def test_directory_with_md_and_tex(self, tmp_path: Path):
        (tmp_path / "intro.md").write_text("# Introduction\n\nThis paper presents…")
        (tmp_path / "methods.tex").write_text(r"\section{Methods}" "\n" r"We use $\alpha = 0.05$.")
        (tmp_path / "refs.bib").write_text("@article{doe2024, title={Test}}")

        files = gather_proposal_files(tmp_path)
        texts = [extract_text(f) for f in files]

        combined = "\n".join(texts)
        assert "Introduction" in combined
        assert r"\section{Methods}" in combined
        assert "doe2024" in combined

    def test_single_file_round_trip(self, tmp_path: Path):
        f = tmp_path / "proposal.tex"
        f.write_text(r"\documentclass{article}" "\n" r"\begin{document}" "\nHello\n" r"\end{document}")

        files = gather_proposal_files(f)
        assert len(files) == 1
        text = extract_text(files[0])
        assert "Hello" in text
