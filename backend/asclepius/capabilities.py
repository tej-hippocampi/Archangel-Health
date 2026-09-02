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
REFERRAL = "referral"                # a referral link, invites, and what they earned
SURFACES = (TUTORIAL, BROWSE, COMMUNITY_READ, COMMUNITY_WRITE, REAL_WORK,
            EARNINGS, REFERRAL)

#: An applicant awaiting review reaches exactly two things: the practice case,
#: and a view-only dashboard that shows them where their application stands.
#:
#: This REVERSES an earlier widening, and the reason is worth keeping. The old
#: set also granted community read and write, earnings and referral, arguing
#: that an applicant who had cleared a mailbox OTP and signed the attestations
#: was trusted enough to post among colleagues, and that hiding the money
#: surfaces made the product look empty on the day we most wanted it to look
#: full. Both halves were decided before the product had a vetting decision to
#: make and before the practice case existed to occupy that wait.
#:
#: What changed: vetting is now the point of this state. An unvetted account
#: posting under a physician identity, in rooms whose whole value is that
#: everyone in them is a verified clinician, is precisely the exposure the
#: review queue exists to prevent, and it is not recoverable by rejecting the
#: application afterwards. The colleagues have already read the post.
#:
#: What replaces the "empty product" worry is the practice case: an applicant
#: now has something real to do, one piece of actual work that teaches what the
#: job is and that feeds the decision about them. That is a better answer to
#: the same problem than a ledger reading zero and a referral link.
#:
#: Nothing here is a hardship for a genuine applicant. Review is measured in a
#: day, and every surface opens on approval. The bounty for a colleague they
#: refer is unchanged either way: it has never paid until the person they
#: brought is verified and their first case is accepted.
_BY_ACCESS: Dict[str, FrozenSet[str]] = {
    FULL: frozenset(SURFACES),
    PROVISIONAL: frozenset({TUTORIAL, BROWSE}),
    NONE: frozenset(),
}


#: Account kinds that are not physicians. A physician is NULL here, which is
#: who everyone was before there was more than one door into this product.
ADVISOR = "advisor"       # a non-clinical supporter: sees the product, refers
REFERRER = "referrer"     # holds a referral link and nothing else

#: A referral-only account reaches exactly two things: the pages that explain
#: what this is, and their own referral surface. Not the case queue, not the
#: community, not another physician's work. The link is handed to someone who
#: knows doctors, not to a doctor.
_REFERRER_SURFACES: FrozenSet[str] = frozenset({BROWSE, REFERRAL})

#: An advisor sees the product and can refer. That is the whole account: they
#: are shown around so they can speak about us credibly, and the one thing they
#: DO is introduce people.
#:
#: Read the omissions rather than the list. No REAL_WORK, because an advisor is
#: not a clinician and a real case carries real patient data. No
#: COMMUNITY_WRITE, because the physicians in those channels are talking to
#: colleagues and a non-clinical voice among them changes what the room is;
#: reading is enough to understand it, and the confidentiality line they sign at
#: signup is what covers the reading. TUTORIAL is in, and it is the whole demo:
#: the practice case is virtual end to end, so an advisor clicking through it
#: touches no patient and writes no row.
_ADVISOR_SURFACES: FrozenSet[str] = frozenset(
    {BROWSE, TUTORIAL, COMMUNITY_READ, EARNINGS, REFERRAL}
)

#: Kind -> the ceiling that kind may ever reach. Intersected with whatever the
#: access level grants, so the cap holds INDEPENDENTLY of verification: an admin
#: clicking Approve on an advisor moves them to FULL and changes nothing about
#: what they can do. A physician is absent from this map and is capped by
#: nothing, which is the pre-existing behaviour for every account that predates
#: there being more than one door.
_BY_ACCOUNT_KIND: Dict[str, FrozenSet[str]] = {
    REFERRER: _REFERRER_SURFACES,
    ADVISOR: _ADVISOR_SURFACES,
}


def account_kind(user: Optional[Dict[str, Any]]) -> Optional[str]:
    return ((user or {}).get("account_kind") or "").strip().lower() or None


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
    granted_by_access = _BY_ACCESS.get(access_level(user), frozenset())
    # A non-physician account is capped no matter how its verification lands:
    # approving one does not turn the person who introduced us to a hospital
    # into someone who grades cases.
    cap = _BY_ACCOUNT_KIND.get(account_kind(user) or "")
    if cap is not None:
        return granted_by_access & cap
    return granted_by_access


def can_surface(user: Optional[Dict[str, Any]], surface: str) -> bool:
    return surface in surfaces(user)


# ─── The practice-case gate (third axis) ──────────────────────────────────────
# Tier says what KIND of work; access level says whether real patient data is
# reachable at all; this says whether this physician has been shown the standard
# before their first real case. Three questions, three predicates, none implying
# the others, for the same reason ACCESS_LEVELS was split out of TIERS.
#
# It is deliberately NOT folded into surfaces(). Dropping REAL_WORK would render
# the "this is where the work happens" card instead of "finish the practice
# case", and would gate every require_surface(REAL_WORK) endpoint with a message
# about credential verification, which is unintelligible to someone whose
# credentials are fine.
#
# Mirrors the verification_status / access_level split above: tutorial_json
# ["status"] stays REPORTING truth (what the physician did) and
# tutorial_json["gate"] is the ACCESS answer. Overloading one field with both is
# exactly how "skipped" would come to mean "allowed".
GATE_LOCKED = "locked"
GATE_PASSED = "passed"
GATE_GRANDFATHERED = "grandfathered"
GATE_STATES = (GATE_LOCKED, GATE_PASSED, GATE_GRANDFATHERED)

#: Gate states that open real work. Deny by default: anything else, including
#: an absent gate and an unrecognised string, is locked.
_GATE_OPEN = frozenset({GATE_PASSED, GATE_GRANDFATHERED})


def _tutorial_blob(user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The parsed tutorial_json off a user row, however it arrived.

    Store getters hand back a dict; a raw sqlite row hands back TEXT. Accepting
    both keeps this readable from either, and a blob we cannot parse reads as
    absent, which denies.
    """
    raw = (user or {}).get("tutorial_json")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        import json  # noqa: PLC0415 - keeps this module import-light at boot
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def practice_gate(user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """This physician's practice-case gate sub-object, or {} when never decided."""
    gate = _tutorial_blob(user).get("gate")
    return gate if isinstance(gate, dict) else {}


def practice_gate_state(user: Optional[Dict[str, Any]]) -> str:
    state = str(practice_gate(user).get("state") or "").strip().lower()
    return state if state in GATE_STATES else GATE_LOCKED


def practice_first_pass(user: Optional[Dict[str, Any]]) -> bool:
    """True when this physician passed the practice case on their first attempt.

    Reads the stamp written at the moment of the first pass rather than
    comparing attempt counts now, because attempts keep climbing on replays.

    A grandfathered account returns False, and that is correct rather than
    unkind: those accounts predate the practice case, so there is no first
    attempt to have passed. False here means "no positive signal", which is
    also what it means for someone who has not sat the case yet."""
    return practice_gate(user).get("first_attempt_pass") is True


def practice_gate_reason(user: Optional[Dict[str, Any]], *,
                         required_version: int) -> Optional[str]:
    """None when real work is open. Otherwise WHY it is not, as a short token.

    ``required_version`` is passed in rather than imported so this module keeps
    its zero-dependency posture: importing tutorial_case would pull the whole
    case-rendering stack into the policy table.

    Exemptions, both narrow. An admin is not a contributor and does not draw
    from the queue. The mock contributor is the demo account, provisioned on
    every boot, and gating it would break the walkthrough the sales motion runs
    on. is_mock is written only by the mock provisioning path, never by a user.
    """
    u = user or {}
    if u.get("role") == "admin" or u.get("is_mock"):
        return None

    gate = practice_gate(u)
    state = practice_gate_state(u)

    if state == GATE_GRANDFATHERED:
        # Never version-checked. These physicians were never asked to take the
        # practice case, so re-gating them on a version bump would silently
        # undo the migration that let them keep working.
        return None

    if state == GATE_PASSED:
        try:
            passed_version = int(gate.get("passed_version") or 0)
        except (TypeError, ValueError):
            passed_version = 0
        if passed_version < int(required_version):
            return "stale_version"
        return None

    # Locked. Say which flavour, so the client can pick a screen: "you have not
    # started" and "you tried and it did not pass" are different conversations.
    try:
        attempts = int(gate.get("attempts") or 0)
    except (TypeError, ValueError):
        attempts = 0
    if attempts:
        return "failed"
    status = str(_tutorial_blob(u).get("status") or "").strip().lower()
    return "in_progress" if status == "in_progress" else "not_started"


def practice_gate_open(user: Optional[Dict[str, Any]], *,
                       required_version: int) -> bool:
    return practice_gate_reason(user, required_version=required_version) is None
