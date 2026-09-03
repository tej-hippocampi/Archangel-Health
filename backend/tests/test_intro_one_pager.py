"""The physician one-pager: a rendered artifact, not a hand-built one.

The intro-call follow-up sends a link and a document. If the document is code,
then changing the pitch is a deploy and it stops being changed. These tests hold
the property that makes it a content change: the bytes come from a versioned
file under docs/, and pointing at a founder-supplied PDF replaces them without
touching Python.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import one_pager  # noqa: E402

client = TestClient(A.app)


@pytest.fixture(autouse=True)
def _forget_the_source():
    """The loader is cached on version, so a test that writes a temporary
    document would otherwise read the previous test's bytes."""
    one_pager.clear_cache()
    yield
    one_pager.clear_cache()


def test_the_shipped_version_renders_to_a_real_pdf():
    """A follow-up that attaches a broken document is worse than one that
    attaches nothing, because nobody finds out."""
    pdf, source = one_pager.pdf_bytes()
    assert source == one_pager.SOURCE_RENDERED
    assert pdf.startswith(b"%PDF-")
    assert pdf.rstrip().endswith(b"%%EOF")
    assert len(pdf) > 1500


def test_the_text_comes_from_the_file_under_docs(tmp_path, monkeypatch):
    """The whole point of the versioned source: editing the markdown changes the
    document, and no Python has to be touched to do it."""
    monkeypatch.setenv("ARCHANGEL_ASCLEPIUS_DOCS_DIR", str(tmp_path))
    (tmp_path / "PHYSICIAN_ONE_PAGER_v1.md").write_text(
        "# A HEADING\n\nA sentence only this test would write.\n", encoding="utf-8")
    one_pager.clear_cache()
    assert "A sentence only this test would write." in one_pager.render()
    # Markers are stripped for the reader, exactly as the agreements do it.
    assert "#" not in one_pager.render()


def test_comments_in_the_source_never_reach_a_reader(tmp_path, monkeypatch):
    """The source carries editing instructions for whoever rewrites the pitch.
    A physician must never see them."""
    monkeypatch.setenv("ARCHANGEL_ASCLEPIUS_DOCS_DIR", str(tmp_path))
    (tmp_path / "PHYSICIAN_ONE_PAGER_v1.md").write_text(
        "<!-- do not ship this line -->\nVisible.\n", encoding="utf-8")
    one_pager.clear_cache()
    text = one_pager.render()
    assert "do not ship this line" not in text
    assert "Visible." in text


def test_a_founder_supplied_pdf_replaces_the_rendered_one(tmp_path, monkeypatch):
    """A designed PDF should ship without waiting for an engineer. The config
    points at a file, and that file is what goes out."""
    supplied = tmp_path / "designed.pdf"
    supplied.write_bytes(b"%PDF-1.4\nfounder supplied\n%%EOF\n")
    monkeypatch.setenv("ASCLEPIUS_ONE_PAGER_PDF", str(supplied))
    pdf, source = one_pager.pdf_bytes()
    assert source == one_pager.SOURCE_FILE
    assert b"founder supplied" in pdf


def test_a_misconfigured_override_degrades_to_the_default(tmp_path, monkeypatch):
    """A typo in an env var must not silence the follow-up. The rendered
    document is the default, not a fallback of last resort."""
    monkeypatch.setenv("ASCLEPIUS_ONE_PAGER_PDF", str(tmp_path / "does-not-exist.pdf"))
    pdf, source = one_pager.pdf_bytes()
    assert source == one_pager.SOURCE_RENDERED
    assert pdf.startswith(b"%PDF-")


def test_a_file_that_is_not_a_pdf_is_refused(tmp_path, monkeypatch):
    """Pointing the override at a .docx or a half-written file must not mail a
    physician something their reader cannot open."""
    bad = tmp_path / "notes.txt"
    bad.write_text("this is not a pdf", encoding="utf-8")
    monkeypatch.setenv("ASCLEPIUS_ONE_PAGER_PDF", str(bad))
    _pdf, source = one_pager.pdf_bytes()
    assert source == one_pager.SOURCE_RENDERED


def test_the_public_route_serves_it_without_a_token():
    """It is linked from an email to somebody with no account. A token on it
    would make the one artifact worth forwarding the one that cannot be."""
    r = client.get("/api/onboarding/asclepius/one-pager.pdf")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF-")
    assert r.headers["x-asclepius-one-pager-source"] == one_pager.SOURCE_RENDERED
    assert "inline" in r.headers["content-disposition"]


def test_the_shipped_document_quotes_no_rate():
    """asclepius/compensation.py has the accrual seam and no rate. A one-pager
    that names a number nobody has decided is how a physician arrives at their
    first payout expecting something else."""
    text = one_pager.render()
    assert "$" not in text
    assert "per hour" not in text.lower()
    assert "per case" not in text.lower()
