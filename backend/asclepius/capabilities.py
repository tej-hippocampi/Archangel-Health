"""Who can do what, derived from tier, in one place.

Before this module, ``tier == "reviewer"`` was written inline at every gate,
and a tier added to that world means finding every literal; the ones you miss
fail SILENTLY: "this user is not a reviewer" is a legitimate answer for a
labeler, so nothing logs and nothing 500s. So every gate routes through
``can()``, and ``TIERS`` is the single enumeration of what the ``users.tier``
column may hold. ``tests/test_tier_capabilities.py`` asserts ``_BY_TIER``
covers ``TIERS`` exactly, so a new tier added without a capability row fails
loudly here instead of denying quietly in production.

The advisor tier that used to live here is retired: advisors are ordinary
users now, and referral minting (``refer``) belongs to every verified
physician. Rows that still say ``tier='advisor'`` are migrated to reviewer on
boot (store.py).
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, Optional

# ─── Capabilities ─────────────────────────────────────────────────────────────
LABEL = "label"    # draw and complete a task from scratch
REVIEW = "review"  # grade another physician's submission
REFER = "refer"    # mint referral invites and hold a referral link

CAPABILITIES = (LABEL, REVIEW, REFER)

# ─── Tiers ────────────────────────────────────────────────────────────────────
# The ONLY values ``users.tier`` may hold, besides NULL ("not yet assigned").
# Imported by routers/asclepius_verify.py (_TIERS) and the admin roster chips
# so no second list of tier strings can drift from it.
LABELER = "labeler"
REVIEWER = "reviewer"
TIERS = (LABELER, REVIEWER)

# Display words, so a raw token never reaches a human (PRD C §3 vocabulary
# rule). The frontend keeps its own map for offline rendering; this one is for
# server-rendered text, error messages and admin payloads.
TIER_WORDS = {
    LABELER: "Labeler",
    REVIEWER: "Reviewer",
}

# Every verified physician can refer: the tier decides the KIND of casework,
# not whether a colleague's name is worth money to us. Keep the sets literal so
# narrowing one later is a one-line edit here rather than an archaeology
# exercise across six routers.
_BY_TIER: Dict[str, FrozenSet[str]] = {
    LABELER: frozenset({LABEL, REFER}),
    REVIEWER: frozenset({LABEL, REVIEW, REFER}),
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


# ─── Access levels ────────────────────────────────────────────────────────────
# The SECOND axis, orthogonal to tier. A tier says what KIND of work you may do;
# an access level says whether you may touch real patient data, real money, or
# another physician's work at all. Both are required on the real-work paths.
#
# Derived from ``users.verification_status`` on every call, never stored: a
# second copy of a state that already exists is a second thing to keep in sync.
#
# NOTE the deliberate collapse: FULL covers both 'approved' AND the NULL of a
# pre-verification-era account. That is correct for ACCESS and wrong for
# REPORTING, so ``verification_status`` stays on the wire verbatim and is never
# re-derived from an access level. The admin queue needs those four states
# distinguishable; the gate does not.
FULL = "full"                 # approved, or the pre-verification-era NULL
PROVISIONAL = "provisional"   # signed up, credentials still under review
NONE = "none"                 # refused, or deactivated
ACCESS_LEVELS = (FULL, PROVISIONAL, NONE)

# ─── Surfaces ─────────────────────────────────────────────────────────────────
# Gated on ACCESS LEVEL, in contrast to CAPABILITIES above which are gated on
# tier. Endpoints declare the surface they serve, so adding one means naming its
# surface rather than appending to an allowlist that has to be found first.
TUTORIAL = "tutorial"                # the virtual practice case
BROWSE = "browse"                    # taxonomy, specialties, own profile, guide
COMMUNITY_READ = "community_read"
COMMUNITY_WRITE = "community_write"  # channel posts. NOT DMs, NOT attachments
REAL_WORK = "real_work"              # draw, open or submit a REAL case, and the
                                     # LLM-spend endpoints that serve one
EARNINGS = "earnings"                # the money ledger and billable sessions
SURFACES = (TUTORIAL, BROWSE, COMMUNITY_READ, COMMUNITY_WRITE, REAL_WORK, EARNINGS)

#: A physician awaiting verification gets the product, minus real patient data
#: and minus money. They have already cleared an OTP on their institutional
#: mailbox, submitted an NPI, and signed the confidentiality and
#: independent-judgment attestations, which is why they are trusted to post
#: among colleagues. DMs and attachments still wait: those are the
#: unsolicited-contact and PHI vectors, and they are worth waiting a day for.
_BY_ACCESS: Dict[str, FrozenSet[str]] = {
    FULL: frozenset(SURFACES),
    PROVISIONAL: frozenset({TUTORIAL, BROWSE, COMMUNITY_READ, COMMUNITY_WRITE}),
    NONE: frozenset(),
}


def access_level(user: Optional[Dict[str, Any]]) -> str:
    """Map a user row to its access level. Reads the dict only, never SQL."""
    u = user or {}
    if not u:
        return NONE
    # 'active' is only consulted when present: many call sites pass a partial row.
    if u.get("active") is not None and not u.get("active"):
        return NONE
    status = u.get("verification_status")
    if status == "rejected":
        return NONE
    if status == "pending":
        return PROVISIONAL
    # 'approved' and NULL both land here. See the note above.
    return FULL


def surfaces(user: Optional[Dict[str, Any]]) -> FrozenSet[str]:
    """Which product surfaces this user may reach. An admin reaches all."""
    if (user or {}).get("role") == "admin":
        return frozenset(SURFACES)
    return _BY_ACCESS.get(access_level(user), frozenset())


def can_surface(user: Optional[Dict[str, Any]], surface: str) -> bool:
    return surface in surfaces(user)
