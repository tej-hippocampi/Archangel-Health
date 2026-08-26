"""The two-tier capability layer, after the advisor tier's retirement.

The whole technical risk of tier gating is one shape: ``tier == 'reviewer'``
written as a hard equality. Add or remove a value and somebody silently gains
or loses a surface; the check returns False, which is a legitimate answer for
a labeler, so nothing logs and nothing errors.

So these tests are mostly about that shape, not about tiers:

  * the capability table covers EVERY tier the column may hold (the grep test);
  * REFER is on both tiers now, because every verified physician can refer;
  * no gating literal survives anywhere in the backend;
  * a legacy ``tier='advisor'`` row is migrated to reviewer on boot.
"""

from __future__ import annotations

import pathlib
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import capabilities as caps  # noqa: E402
from asclepius import store as asc_store  # noqa: E402

BACKEND = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _isolated():
    A.fresh_store()
    yield


# ═══ The capability table ════════════════════════════════════════════════════
def test_capability_table_covers_every_tier_the_column_may_hold():
    """THE GREP TEST.

    If someone adds a tier, this fails loudly here instead of denying silently
    in production. The verification router's writable vocabulary and the
    capability table must be the same set, in both directions — and the
    retired advisor tier must be gone from both.
    """
    import routers.asclepius_verify as verify

    assert set(caps._BY_TIER) == set(caps.TIERS)
    assert set(verify._TIERS) == set(caps.TIERS)
    assert set(caps.TIERS) == {"labeler", "reviewer"}
    for tier, granted in caps._BY_TIER.items():
        assert granted <= set(caps.CAPABILITIES), f"{tier} grants an unknown capability"
    for tier in caps.TIERS:
        assert caps.tier_word(tier) and caps.tier_word(tier) != tier
    assert caps.tier_word(None) == "Unassigned"


def test_refer_is_on_every_tier():
    """Every verified physician can refer: the tier decides the KIND of
    casework, never whether a colleague's name is worth money to us."""
    for tier in caps.TIERS:
        assert caps.can({"tier": tier}, caps.REFER), tier


def test_review_stays_reviewer_only():
    assert caps.can({"tier": "reviewer"}, caps.REVIEW)
    assert not caps.can({"tier": "labeler"}, caps.REVIEW)


def test_capabilities_deny_by_default():
    assert caps.capabilities(None) == frozenset()
    assert caps.capabilities({}) == frozenset()
    assert caps.capabilities({"tier": None}) == frozenset()
    assert caps.capabilities({"tier": "wat"}) == frozenset()
    # The retired tier is an unknown value now, and unknown denies.
    assert caps.capabilities({"tier": "advisor"}) == frozenset()
    assert not caps.can({"tier": "labeler"}, caps.REVIEW)
    # An admin operates every surface, as today.
    assert caps.can({"role": "admin"}, caps.REVIEW)


def test_a_non_physician_role_gains_nothing_from_a_tier():
    """``can()`` was once role-blind below admin, so a ``data_partner`` row
    carrying a physician tier would have passed every capability check.
    ``get_current_user`` denies those roles the whole surface first, so this is
    defence in depth — but a capability check that ignores role is one refactor
    away from being the only check."""
    for role in ("data_partner", "buyer"):
        rogue = {"role": role, "tier": "reviewer"}
        assert caps.capabilities(rogue) == frozenset()
        assert not caps.can(rogue, caps.REVIEW)
    # Physician roles are unaffected, and a bare dict with no role still works
    # — many call sites pass only a tier.
    assert caps.can({"role": "evaluator", "tier": "reviewer"}, caps.REFER)
    assert caps.can({"tier": "reviewer"}, caps.REVIEW)


# ═══ The migration ═══════════════════════════════════════════════════════════
def test_a_legacy_advisor_row_is_migrated_to_reviewer_on_boot():
    """Advisor was a strict superset of reviewer, so migrating down to
    reviewer loses nothing the two-tier world still offers, and clearing
    equity_only makes the ex-advisor payable like everyone else."""
    store = asc_store.get_store()
    u = A.make_user(store, role="evaluator", specialty="nephrology")
    with store._conn() as conn:
        conn.execute(
            "UPDATE users SET tier = 'advisor', compensation_model = 'equity_only' "
            "WHERE id = ?", (u["id"],))
    # Re-run the boot migration the way a redeploy would.
    store._init_schema()
    row = store.get_user_by_id(u["id"])
    assert row["tier"] == "reviewer"
    assert row["compensation_model"] is None
    # And it is idempotent-quiet: a second boot changes nothing.
    store._init_schema()
    assert store.get_user_by_id(u["id"])["tier"] == "reviewer"


# ═══ No gating literal anywhere ═══════════════════════════════════════════════
def test_no_hard_tier_equality_survives_in_backend_gates():
    """No gate may compare a tier to a literal again.

    It scans the backend for ``tier == "reviewer"``-shaped comparisons; the
    capability layer is the only legitimate place a tier string is matched,
    and it does so through a dict lookup rather than an equality.
    """
    pattern = re.compile(
        r"""tier[^\n]{0,40}[=!]=\s*['"](labeler|reviewer|advisor)['"]"""
        r"""|['"](labeler|reviewer|advisor)['"]\s*[=!]=[^\n]{0,40}tier""")
    # A guard that cannot fail is decoration. Prove the pattern catches the
    # exact shapes this build removed before trusting an empty result set.
    for bad in ('    return (user or {}).get("tier") == "reviewer"',
                "if user['tier'] == 'advisor':",
                '        elif tier == "reviewer":',
                "if 'labeler' != row.tier:"):
        assert pattern.search(bad), f"the guard would not have caught: {bad}"
    for ok in ('return asc_caps.can(user, asc_caps.REVIEW)',
               'tier = (body.tier or "").strip().lower()',
               'if tier not in _TIERS:'):
        assert not pattern.search(ok), f"the guard false-positives on: {ok}"

    allowed = {
        # The capability layer itself and its compensation sibling are where
        # tier semantics are ALLOWED to live.
        "asclepius/capabilities.py",
        "asclepius/compensation.py",
        # Packaging resolves the related-party relationship for the provenance
        # flag; it is a disclosure lookup, not an access gate.
        "asclepius/packaging.py",
    }
    # A tier comparison written INSIDE SQL is invisible to the scan above,
    # because _code_lines blanks string literals. It must match a FILTER and
    # not an assignment: ``SET tier = 'reviewer'`` is a write, not a gate.
    sql_pattern = re.compile(
        r"""\b(?:WHERE|AND|OR)\s+[\w.]*\btier\s*(?:=|!=|<>)\s*['"](?:labeler|reviewer|advisor)['"]""",
        re.IGNORECASE)
    for bad_sql in ("WHERE tier = 'reviewer'", "AND u.tier != 'advisor'",
                    "... WHERE   u.tier='labeler'"):
        assert sql_pattern.search(bad_sql), f"the SQL guard would miss: {bad_sql}"
    for ok_sql in ("UPDATE users SET tier = 'reviewer', x = ?",
                   "SET tier='reviewer'"):
        assert not sql_pattern.search(ok_sql), (
            f"the SQL guard false-positives on a write: {ok_sql}")

    # The one legitimate SQL filter on a tier literal: the boot migration that
    # retires the advisor tier has to FIND advisor rows to migrate them.
    sql_allowed = {"asclepius/store.py": {"WHERE tier = 'advisor'"}}

    offenders = []
    for path in sorted(BACKEND.glob("**/*.py")):
        rel = path.relative_to(BACKEND).as_posix()
        if rel.startswith(("tests/", ".venv/")) or rel in allowed:
            continue
        for lineno, line in _code_lines(path):
            if pattern.search(line):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
        text = path.read_text(encoding="utf-8")
        for m in sql_pattern.finditer(text):
            if any(frag in m.group(0) for frag in sql_allowed.get(rel, ())):
                continue
            lineno = text.count("\n", 0, m.start()) + 1
            offenders.append(f"{rel}:{lineno}: (in SQL) {m.group(0)}")
    assert not offenders, (
        "Hard tier equality found — route it through asclepius.capabilities.can() "
        "instead; a missed literal denies SILENTLY:\n" + "\n".join(offenders))


def _code_lines(path):
    """(lineno, source) for lines with comments and string literals blanked out.

    Tokenized rather than pattern-matched on raw text: this file's own
    docstrings quote the old ``tier == "reviewer"`` code to explain why it was
    removed, and a naive line scan flags that prose as a violation."""
    import io
    import tokenize

    src = path.read_text(encoding="utf-8")
    lines = src.splitlines()
    blanked = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                for n in range(tok.start[0], tok.end[0] + 1):
                    blanked[n] = True
    except (tokenize.TokenError, IndentationError, SyntaxError):  # pragma: no cover
        return [(i, ln) for i, ln in enumerate(lines, 1)]
    return [(i, ln) for i, ln in enumerate(lines, 1) if not blanked.get(i)]
