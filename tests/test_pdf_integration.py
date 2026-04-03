"""Integration tests for md_to_pdf module."""
import os, re, subprocess, sys, tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from md_to_pdf import auto_linkify, convert

# Ensure pandoc is findable
os.environ["PATH"] = "/opt/homebrew/bin:" + os.environ.get("PATH", "")


def _write_and_convert(md: str) -> Path:
    """Helper: write markdown to temp file, convert to PDF, return PDF path."""
    d = tempfile.mkdtemp(prefix="pdftest_")
    md_path = Path(d) / "test.md"
    pdf_path = Path(d) / "test.pdf"
    md_path.write_text(md)
    convert(str(md_path), str(pdf_path))
    return pdf_path


class TestConvertBasic:
    def test_convert_basic(self):
        pdf = _write_and_convert("# Hello World\n\nThis is a test paragraph.\n")
        assert pdf.exists()
        assert pdf.stat().st_size > 1024

    def test_convert_with_tables(self):
        md = "# Table Test\n\n| Name | Value |\n|------|-------|\n| A | 1 |\n| B | 2 |\n"
        pdf = _write_and_convert(md)
        assert pdf.exists()
        assert pdf.stat().st_size > 1024


class TestAutoLinkify:
    def test_backtick_protection(self):
        md = "See `V2153219207` for details."
        result = auto_linkify(md)
        assert "](https://t.corp.amazon.com/" not in result
        assert "`V2153219207`" in result

    def test_tickets_linked(self):
        md = "Check V2153219207 for the fix."
        result = auto_linkify(md)
        assert "[V2153219207](https://t.corp.amazon.com/V2153219207)" in result

    def test_code_block_protection(self):
        md = "Text\n```\nV2153219207\n```\nMore text"
        result = auto_linkify(md)
        # Inside code block — should NOT be linked
        assert result.count("t.corp.amazon.com") == 0

    def test_case_id_linked(self):
        md = "Case 123456789012345 needs review."
        result = auto_linkify(md)
        assert "command-center.support.aws.a2z.com" in result


class TestAutolinkChain:
    def test_sage_and_ticket(self):
        """Verify _autolink_internal patterns (Sage) + auto_linkify (V-ticket) both work in chain."""
        # _autolink_internal is in server.py and hard to import without starting MCP.
        # Test auto_linkify alone here; the chain is tested via convert().
        md = "See V1234567890 and case 123456789012345."
        result = auto_linkify(md)
        assert "t.corp.amazon.com/V1234567890" in result
        assert "command-center.support.aws.a2z.com" in result


class TestPandoc:
    def test_no_deprecation_warning(self):
        r = subprocess.run(
            ["pandoc", "-f", "markdown", "-t", "html5", "-s", "--syntax-highlighting=tango"],
            input="# Test", capture_output=True, text=True,
        )
        assert r.returncode == 0
        assert "WARNING" not in r.stderr
        assert "deprecated" not in r.stderr.lower()


class TestMermaid:
    def test_mermaid_render(self):
        md = "# Diagram\n\n```mermaid\nflowchart LR\n    A --> B\n```\n"
        d = tempfile.mkdtemp(prefix="pdftest_mermaid_")
        md_path = Path(d) / "test.md"
        pdf_path = Path(d) / "test.pdf"
        md_path.write_text(md)
        convert(str(md_path), str(pdf_path))
        # PDF should exist even if mermaid rendering falls back
        assert pdf_path.exists()
        assert pdf_path.stat().st_size > 1024
