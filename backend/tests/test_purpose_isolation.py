"""Static isolation — PRD-I §3.3.

The load-bearing control for the whole confidentiality requirement, and the
cheapest: if the concept never appears in provider-reachable code, no amount of
middleware, padding or timing normalization has to save it, and no future
refactor can accidentally serialize it.

This is the test that catches the next person adding a debug field.

ON COMMENTS
-----------
Comments and docstrings are stripped before matching. A literal zero-hit grep
would force provider-facing code to stay SILENT about the one invariant it most
needs to explain, and the next maintainer would delete a rule they were never
told about. What must not exist is executable code that reads, branches on, or
serializes the value; a comment saying "never serialize this" cannot reach a
partner. String literals are NOT stripped — ``{"purpose": ...}`` in a response
dict is exactly what this must catch.
"""

from __future__ import annotations

import io
import re
import sys
import token as _token
import tokenize
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
ROOT = BACKEND.parent

sys.path.insert(0, str(BACKEND))

# Every word that would name the distinction, in any form a leak could take.
FORBIDDEN = ("purpose", "brokering", "broker", "task_creation")

# Provider-REACHABLE source. Not "everything that mentions ingestion" — the admin
# router and the promotion gate must say these words, that is their job.
PROVIDER_PY = [
    BACKEND / "routers" / "asclepius_provider.py",
    BACKEND / "asclepius" / "uploads.py",
]
PROVIDER_WEB = sorted((ROOT / "frontend" / "provider").glob("*"))


def _strip_comments_and_docstrings(src: str) -> str:
    """Executable Python only. Drops COMMENT tokens and STRING tokens that stand
    alone as a statement (docstrings), keeping every other string literal."""
    out: list = []
    prev_end = (1, 0)
    prev_toktype = _token.INDENT
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except tokenize.TokenError:  # pragma: no cover - source must parse
        return src
    for toktype, ttext, (slin, scol), (elin, ecol), _line in tokens:
        if slin > prev_end[0]:
            prev_end = (slin, 0)
        if scol > prev_end[1]:
            out.append(" " * (scol - prev_end[1]))
        if toktype == tokenize.COMMENT:
            pass
        elif toktype == tokenize.STRING and prev_toktype in (
                tokenize.INDENT, tokenize.DEDENT, tokenize.NEWLINE, tokenize.NL,
                _token.INDENT):
            pass  # docstring / bare string statement
        else:
            out.append(ttext)
        prev_end = (elin, ecol)
        if toktype not in (tokenize.NL, tokenize.COMMENT):
            prev_toktype = toktype
    return "".join(out)


def _strip_js_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", src)


def _strip_html_comments(src: str) -> str:
    return re.sub(r"<!--.*?-->", "", src, flags=re.S)


def _hits(text: str) -> list:
    found = []
    for n, line in enumerate(text.splitlines(), start=1):
        low = line.lower()
        for word in FORBIDDEN:
            if word in low:
                found.append((n, word, line.strip()[:120]))
    return found


@pytest.mark.parametrize("path", PROVIDER_PY, ids=lambda p: p.name)
def test_provider_python_never_names_the_distinction(path):
    hits = _hits(_strip_comments_and_docstrings(path.read_text(encoding="utf-8")))
    assert not hits, (
        f"{path.name} contains provider-reachable code naming the upload's purpose: "
        f"{hits}. The purpose is a column read by admin surfaces only; a provider "
        "door must be given no way to name it, because a door that can name it is "
        "a door that can leak it.")


@pytest.mark.parametrize("path", PROVIDER_WEB, ids=lambda p: p.name)
def test_provider_frontend_never_names_the_distinction(path):
    if path.is_dir():
        pytest.skip("directory")
    src = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix == ".js":
        src = _strip_js_comments(src)
    elif path.suffix in (".html", ".htm"):
        src = _strip_html_comments(src)
    hits = _hits(src)
    assert not hits, f"{path.name} names the upload's purpose to a provider: {hits}"


def test_the_provider_serializers_are_explicit_allowlists():
    """A ``**row`` or a dict comprehension over a row would ship whatever column
    the table grows next. Both provider-facing serializers name their fields."""
    from asclepius import uploads as asc_uploads

    src = _strip_comments_and_docstrings(
        (BACKEND / "asclepius" / "uploads.py").read_text(encoding="utf-8"))
    body = src[src.index("def public_session"):]
    assert "**session" not in body and "session.items()" not in body

    prov = _strip_comments_and_docstrings(
        (BACKEND / "routers" / "asclepius_provider.py").read_text(encoding="utf-8"))
    for fn in ("_hs_upload_view", "_provider_file_view"):
        seg = prov[prov.index(f"def {fn}"):]
        seg = seg[:seg.index("\ndef ", 1)] if "\ndef " in seg[1:] else seg
        assert "**up" not in seg and ".items()" not in seg, (
            f"{fn} splats a database row into a provider response")


def test_the_admin_side_does_name_it():
    """The inverse assertion, and it matters as much: a suite that only checks for
    absence passes just as happily when the feature was never built."""
    admin = (BACKEND / "routers" / "asclepius_admin.py").read_text(encoding="utf-8")
    promote = (BACKEND / "routers" / "asclepius.py").read_text(encoding="utf-8")
    assert "brokering" in admin and "purpose" in admin
    assert "is_brokering" in promote


def test_no_provider_response_model_can_carry_it():
    """Walk the LIVE route table rather than the source: a route registered from
    somewhere unexpected is exactly the case a file-scoped grep misses."""
    from main import app

    for route in app.routes:
        path = getattr(route, "path", "") or ""
        if not ("/hs/" in path or "/provider/" in path):
            continue
        assert not any(w in path.lower() for w in FORBIDDEN), (
            f"provider route {path} encodes the purpose in its URL")
        endpoint = getattr(route, "endpoint", None)
        src_file = getattr(getattr(endpoint, "__code__", None), "co_filename", "")
        assert "asclepius_provider" in src_file or "static" in src_file.lower(), (
            f"provider route {path} is served from {src_file}, outside the file the "
            "static isolation check covers")
