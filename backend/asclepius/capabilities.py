"""Who can do what, derived from tier — in one place (Advisor PRD §2.2).

Before this module, ``tier == "reviewer"`` was written inline at every gate.
Adding a third tier to that world means finding every literal, and the ones you
miss fail SILENTLY: "this user is not a reviewer" is a legitimate answer for a
labeler, so nothing logs and nothing 500s. The advisor just cannot click the
button, and nobody finds out until they say so.

That is the same failure shape the last audit found four times — a check that
squeezes three states into two — so the fix is structural rather than another
``or tier == "advisor"`` at each call site: every gate routes through ``can()``,
and ``TIERS`` is the single enumeration of what the ``users.tier`` column may
hold. ``tests/test_advisor_tier.py`` asserts ``_BY_TIER`` covers ``TIERS``
exactly, so a fourth tier added without a capability row fails loudly here
instead of denying quietly in production.
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, Optional

# ─── Capabilities ─────────────────────────────────────────────────────────────
LABEL = "label"                    # draw and complete a task from scratch
REVIEW = "review"                  # grade another physician's submission
REFER = "refer"                    # mint referral invites
SIGNOFF_TASKS = "signoff_tasks"    # preview + approve new task batches
SIGNOFF_EXPORT = "signoff_export"  # inspect outbound buyer bundles
SIGNOFF_INTAKE = "signoff_intake"  # inspect inbound hospital metadata
SIGNOFF_SPEC = "signoff_spec"      # comment on a product spec

CAPABILITIES = (
    LABEL, REVIEW, REFER, SIGNOFF_TASKS, SIGNOFF_EXPORT, SIGNOFF_INTAKE, SIGNOFF_SPEC,
)

# ─── Tiers ────────────────────────────────────────────────────────────────────
# The ONLY values ``users.tier`` may hold, besides NULL ("not yet assigned").
# Imported by routers/asclepius_verify.py (_TIERS), the admin roster chips and
# the appointment endpoint so no second list of tier strings can drift from it.
LABELER = "labeler"
REVIEWER = "reviewer"
ADVISOR = "advisor"
TIERS = (LABELER, REVIEWER, ADVISOR)

# Display words, so a raw token never reaches a human (PRD C §3 vocabulary
# rule). The frontend keeps its own map for offline rendering; this one is for
# server-rendered text, error messages and admin payloads.
TIER_WORDS = {
    LABELER: "Labeler",
    REVIEWER: "Reviewer",
    ADVISOR: "Medical Advisor",
}

# An advisor is NOT "a reviewer with more buttons" — it is a third tier that
# happens to be a superset of the second. The superset is a fact about today's
# product, not a promise: keep the sets literal so narrowing one later is a
# one-line edit here rather than an archaeology exercise across six routers.
_BY_TIER: Dict[str, FrozenSet[str]] = {
    LABELER: frozenset({LABEL}),
    REVIEWER: frozenset({LABEL, REVIEW}),
    ADVISOR: frozenset({LABEL, REVIEW, REFER,
                        SIGNOFF_TASKS, SIGNOFF_EXPORT, SIGNOFF_INTAKE, SIGNOFF_SPEC}),
}


# Roles a TIER can grant capabilities to. A tier is a physician-supply concept;
# a ``data_partner`` or ``buyer`` row carrying one is meaningless at best and a
# privilege escalation at worst. ``auth.get_current_user`` already denies both
# roles the entire evaluator surface, so this is defence in depth (audit L4) —
# but a capability check that ignores role is one refactor away from being the
# only check, and it costs nothing to be correct here.
_CAPABLE_ROLES = frozenset({"evaluator", "admin", "qa_reviewer"})


def capabilities(user: Optional[Dict[str, Any]]) -> FrozenSet[str]:
    """What this user's TIER alone permits. NULL tier -> empty set.

    'Not yet assigned' and 'no' both deny for access control, but the admin
    queue still needs to tell them apart (PRD B §4), so that distinction lives
    in the tier column, not here. Reads the tier off the user dict only — never
    via SQL — so this is safe to call before any migration has run.
    """
    u = user or {}
    # A role is only checked when one is present: many call sites pass a bare
    # ``{"tier": ...}`` and must keep working.
    role = u.get("role")
    if role is not None and role not in _CAPABLE_ROLES:
        return frozenset()
    return _BY_TIER.get(u.get("tier") or "", frozenset())


def can(user: Optional[Dict[str, Any]], capability: str) -> bool:
    """The one gate. An admin can operate every surface, as today."""
    if (user or {}).get("role") == "admin":
        return True
    return capability in capabilities(user)


def granted(user: Optional[Dict[str, Any]]) -> FrozenSet[str]:
    """Everything this user can do INCLUDING the admin override — the shape the
    portal needs to decide which sections to render. ``capabilities()`` stays
    tier-only on purpose: it is the policy table, this is the effective answer.
    """
    if (user or {}).get("role") == "admin":
        return frozenset(CAPABILITIES)
    return capabilities(user)


def tier_word(tier: Optional[str]) -> str:
    """Never render a raw token to a human."""
    return TIER_WORDS.get(tier or "", "Unassigned")
