"""Guard: no em dash (U+2014) in user-facing Asclepius copy.

Em dashes read as machine-written, which erodes trust for a portal shown to
thousands of physicians. This test fails if one creeps back into the copy the
SPA renders, or into the taxonomy vocab and disclaimers it shows as chips /
banners.

SCOPE: copy that actually reaches a physician, and nothing else. That is the
rule the first version of this guard stated and did not implement: it scanned
every LINE of four frontend files, so it failed on code comments no physician
will ever see, while ignoring manual-content.js and earnings.js, which are
almost entirely physician-facing prose. It policed the invisible and let the
instruction manual through.

So comments are stripped before scanning, and the file list covers the modules
where physician copy actually lives. An author arguing with this guard about a
comment was arguing with a bug; an author arguing with it about a sentence a
doctor reads is arguing with the product decision.

If you add a new user-facing constant, add it to TAXONOMY_NAMES below.
"""
import io
import os
import sys

import pytest

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)  # make `asclepius` importable regardless of CWD
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
EM_DASH = "—"  # —

FRONTEND_FILES = [
    "frontend/asclepius/asclepius.js",
    "frontend/asclepius/asclepius.css",
    "frontend/asclepius/_tokens.css",
    "frontend/asclepius/_base.css",
    # The modules that are almost entirely physician-facing prose, and were the
    # ones this guard was missing: the instruction manual every physician is
    # pointed at, the page where they read what they have earned, and the review
    # console, which is a whole second page of copy a reviewer works inside.
    "frontend/asclepius/manual-content.js",
    "frontend/asclepius/earnings.js",
    "frontend/asclepius/review.js",
]


def _strip_comments(text, css):
    """Blank out // and /* */ comments, preserving line numbering.

    A comment is not copy. Scanning them is what made this guard fire on an
    author's reasoning and stay silent on the manual."""
    out, i, n, line = [], 0, len(text), []
    while i < n:
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            chunk = text[i:(n if j == -1 else j + 2)]
            out.append("\n" * chunk.count("\n"))          # keep the line count
            i = n if j == -1 else j + 2
        elif not css and text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j == -1 else j
        else:
            out.append(text[i])
            i += 1
    return "".join(out)

# The user-facing vocab surfaced by GET /taxonomy + the rendered disclaimers.
TAXONOMY_NAMES = [
    "CONFIDENCE_LEVELS", "WHY_BETTER_TAGS", "ERROR_TAXONOMY", "ERROR_SEVERITIES",
    "EVIDENCE_SOURCE_TYPES", "REASONING_STEP_LABELS", "STEP_CORRECTION_REASONS",
    "ERROR_TAG_REASONS", "RUBRIC_AXES", "RUBRIC_TIERS", "VERDICTS",
    "PROMPT_REVIEW_VERDICTS", "FAILURE_MODES", "GROUNDED_PREMIUM_DISCLAIMER",
    "CREDENTIAL_SUMMARY_WATERMARK",
]


@pytest.mark.parametrize("rel", FRONTEND_FILES)
def test_evaluator_spa_has_no_em_dash(rel):
    raw = io.open(os.path.join(REPO, rel), encoding="utf-8").read()
    text = _strip_comments(raw, css=rel.endswith(".css"))
    # A lone em dash in quotes is a GLYPH, not prose: the empty-value placeholder
    # in a table cell, and the `num` of a section that sits outside the numbered
    # sequence. The rule here is about punctuation that reads as machine-written
    # inside a sentence; a dash standing alone in a cell has no sentence to read
    # badly in.
    text = text.replace("'" + EM_DASH + "'", "''").replace('"' + EM_DASH + '"', '""')
    bad = [f"  line {i}: {ln.strip()}"
           for i, ln in enumerate(text.splitlines(), 1) if EM_DASH in ln]
    assert not bad, (
        f"{rel} contains {len(bad)} em dash(es) in copy a physician reads. "
        f"Replace with a period, colon, comma, or parentheses (never a bare "
        f"' - ' in prose). Comments are not scanned:\n" + "\n".join(bad[:40])
    )


def _walk_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk_strings(k)
            yield from _walk_strings(v)
    elif isinstance(obj, (list, tuple, set)):
        for item in obj:
            yield from _walk_strings(item)


def test_taxonomy_vocab_has_no_em_dash():
    from asclepius import constants as C
    offenders = []
    for name in TAXONOMY_NAMES:
        value = getattr(C, name, None)
        if value is None:
            continue
        for s in _walk_strings(value):
            if EM_DASH in s:
                offenders.append(f"  {name}: {s!r}")
    assert not offenders, (
        "Em dash in user-facing taxonomy copy:\n" + "\n".join(offenders)
    )
