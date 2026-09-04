"""No em dash reaches a reader.

A standing rule from the founders, and one worth a guard rather than a habit:
an em dash is the single most reliable tell that a sentence was written by a
language model, and this product's whole pitch is that qualified humans are in
the loop. Copy that reads as machine-written undercuts the claim before anyone
gets to the argument.

The rule is about SHIPPED COPY, not about the codebase. This repo carries
thousands of em dashes inside comments and docstrings explaining why code is
the way it is, and rewriting those would delete the reasoning while changing
nothing a physician sees. So this test asks exactly the question the rule is
about: does a dash survive in a string a person reads?

The sweep that enforces it is ``backend/scripts/purge_em_dashes.py``, which
distinguishes copy from comment structurally rather than textually. That
distinction is load bearing: two earlier hand-rolled JavaScript lexers fell out
of phase on a regex literal and started rewriting comment prose as if it were
copy, which is why the current one works line by line and fails by MISSING a
dash rather than by corrupting a comment.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "backend" / "scripts" / "purge_em_dashes.py"


def _sweep():
    spec = importlib.util.spec_from_file_location("purge_em_dashes", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_no_em_dash_survives_in_copy():
    sweep = _sweep()
    offenders = []
    for path in sweep.targets():
        for line, text in sweep.hits_for(path):
            offenders.append(f"{path.relative_to(_ROOT)}:{line}: {' '.join(text.split())[:120]}")
    assert offenders == [], (
        "em dash in shipped copy. Run:\n"
        "    python3 backend/scripts/purge_em_dashes.py --fix --spaced\n"
        "then read the diff, because choosing between a colon and a comma is a\n"
        "judgment the script only approximates.\n\n" + "\n".join(offenders[:40])
    )


def test_the_sweep_can_tell_copy_from_comment():
    """The property that makes the sweep safe to run with --fix.

    Pinned because it has been wrong twice. If a future edit lets a comment's
    prose be seen as copy, this fails here rather than in a commit that quietly
    rewrites the reasoning in a file nobody re-reads.
    """
    sweep = _sweep()
    sample = (
        "  // Read off `kind` -- a backtick in a comment, and an apostrophe: don't.\n"
        "  const label = 'real copy with an em dash \u2014 here';\n"
        "  const re = /`([^`\\n]+)`/g;  // a regex literal carrying a backtick\n"
    )
    lines = list(sweep._js_copy_lines(sample))
    quoted = [line[a:b] for _, line, runs in lines for a, b in runs]
    assert any("real copy with an em dash" in q for q in quoted), quoted
    assert not any("backtick in a comment" in q for q in quoted), quoted
    assert not any("a regex literal carrying" in q for q in quoted), quoted


def test_the_repair_never_rewrites_a_pattern_literal():
    """A raw string is a regex, and rewriting one is a live bug.

    ``re.compile(r"\\s*[\u2013\u2014]+\\s*")`` became ``[--]`` in an early run of
    this sweep, which is the character RANGE hyphen-to-hyphen. The email dash
    scrubber would then have matched plain hyphens and mangled every hyphenated
    word we send.
    """
    sweep = _sweep()
    assert sweep._is_pattern_literal('r"\\s*[\u2013\u2014]+\\s*"')
    assert not sweep._is_pattern_literal('"a real sentence \u2014 with a dash"')
