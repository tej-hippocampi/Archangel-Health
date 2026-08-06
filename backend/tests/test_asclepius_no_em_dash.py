"""Guard: no em dash (U+2014) in user-facing Asclepius copy.

Em dashes read as machine-written, which erodes trust for a portal shown to
thousands of physicians. This test fails if one creeps back into either the
evaluator SPA (every string in it is user-facing) or the taxonomy vocab and
disclaimers the SPA renders as chips / banners.

Note: this intentionally does NOT scan backend docstrings/comments — only copy
that actually reaches a physician. If you add a new user-facing constant, add it
to TAXONOMY_NAMES below.
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
]

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
    text = io.open(os.path.join(REPO, rel), encoding="utf-8").read()
    bad = [f"  line {i}: {ln.strip()}"
           for i, ln in enumerate(text.splitlines(), 1) if EM_DASH in ln]
    assert not bad, (
        f"{rel} contains {len(bad)} em dash(es). Replace with a period, colon, "
        f"comma, or parentheses (never a bare ' - ' in prose):\n" + "\n".join(bad[:40])
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
