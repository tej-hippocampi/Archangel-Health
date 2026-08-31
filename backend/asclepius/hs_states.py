"""The onboarding state of a HEALTH SYSTEM, as opposed to of one account.

``hs_access.py`` answers "what may this login touch", from a row in
``hs_portal_users``. This module answers a different question about a different
row: how far through onboarding the ORGANIZATION is, from ``health_systems``.
They are deliberately separate, because a single organization holds several
logins and the agreement that opens the upload door is signed once, by one of
them, on behalf of all of them (PRD §0.1.2). Collapsing the two would mean
either every member re-signing the same contract or one member's approval
silently authorizing another member's session.

Both gates apply to an upload. Access says the account is allowed a surface;
state says the organization has finished the paperwork behind it. Neither
substitutes for the other.

    intake ──submit──▶ submitted ──approve──▶ approved_awaiting_dla ──sign──▶ active
                            │
                            └──decline──▶ declined

THE LEGACY COLLAPSE is the load-bearing decision, and it is the same one
``hs_access.access_level`` makes for a NULL ``approval_status``: an organization
whose ``onboarding_state`` is NULL predates this state machine, was provisioned
by an operator who had the conversation and the contract off-platform, and has
in several cases been uploading for months. A sweep that stamped ``intake`` on
those rows would lock every existing partner out of the door they already use,
on a deploy, with no way for them to open it themselves. So NULL reads as
``active`` and the migration backfills nothing.

That collapse costs one thing worth naming: an operator cannot tell a legacy
partner from a signed one by state alone. ``signed_agreement`` is what
distinguishes them, and the admin list shows the DLA chip separately for exactly
this reason -- "active, no agreement on file" is a real and visible condition,
not a hidden one.

Kept free of the words the provider-facing grep forbids, so
``routers/asclepius_provider.py`` may import it.
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, Optional, Tuple

# ─── States ──────────────────────────────────────────────────────────────────
#: Told us who they are is still ahead of them: the application form is open and
#: nothing else is.
INTAKE = "intake"
#: The application is in. Waiting on a person, and told so.
SUBMITTED = "submitted"
#: A person said yes. The agreement is rendered and waiting for a signature.
AWAITING_DLA = "approved_awaiting_dla"
#: Signed. The upload door is open and only here.
ACTIVE = "active"
#: A person said no. Not in the PRD's four chips, and unavoidable: `Decline` is
#: one of the two buttons on the admin card, so the outcome has to be
#: representable or the button would have nowhere to write its result.
DECLINED = "declined"

STATES: Tuple[str, ...] = (INTAKE, SUBMITTED, AWAITING_DLA, ACTIVE, DECLINED)

#: What the organization is told about itself, in partner words. Same rule the
#: upload status map and ``hs_access.account_state`` follow: our queue
#: vocabulary is ours. Every one of these names what happens next, because a
#: state that only says "wait" is the reason people phone.
LABELS: Dict[str, str] = {
    INTAKE: "Tell us about your organization",
    SUBMITTED: "With us for review",
    AWAITING_DLA: "Ready for signature",
    ACTIVE: "Active",
    DECLINED: "Closed",
}

#: The one sentence each state owes the reader.
NEXT_STEP: Dict[str, str] = {
    INTAKE: "Answer four questions and add anyone else who should have access. "
            "Nothing here commits you to anything.",
    SUBMITTED: "We are reading your answers. Expect to hear from us within one "
               "to two business days.",
    AWAITING_DLA: "Your data licensing agreement is ready. Read it and sign it, "
                  "and uploading opens the moment you do.",
    ACTIVE: "Uploading is open.",
    DECLINED: "We are not moving ahead right now. Reply to any email from us "
              "and a person will pick it up.",
}

#: state -> the states it may legally become. Nothing moves backwards: an
#: organization that has signed cannot be walked back into `intake` by a stray
#: call, and re-approving an active partner is not a transition, it is a bug.
_TRANSITIONS: Dict[str, FrozenSet[str]] = {
    # AWAITING_DLA from INTAKE is deliberate and is an OPERATOR-only edge: a
    # partner we already met on a call signs up, and the person who had that
    # call approves them without making them answer four questions they have
    # already answered out loud. The portal cannot take this edge -- it only
    # ever submits -- so the shortcut cannot be taken by the organization
    # itself.
    INTAKE: frozenset({SUBMITTED, AWAITING_DLA, DECLINED}),
    SUBMITTED: frozenset({AWAITING_DLA, DECLINED}),
    AWAITING_DLA: frozenset({ACTIVE, DECLINED}),
    ACTIVE: frozenset({DECLINED}),
    DECLINED: frozenset({SUBMITTED, AWAITING_DLA}),   # a decision reversed by hand
}


class TransitionRefused(ValueError):
    """Raised with a message written for an operator, not for a partner."""


def state_of(health_system: Optional[Dict[str, Any]]) -> str:
    """Map a ``health_systems`` row to its state. Reads the dict, never SQL.

    NULL and any unrecognised value collapse to ACTIVE. The unrecognised case
    matters as much as the NULL one: if a future release adds a state and is
    then rolled back, the older code reading the newer rows must not lock a
    partner out of a door they were already through.
    """
    row = health_system or {}
    raw = (row.get("onboarding_state") or "").strip().lower()
    if raw in STATES:
        return raw
    return ACTIVE


def can_upload(health_system: Optional[Dict[str, Any]]) -> bool:
    """The §6 gate, in one place. ACTIVE and nothing else."""
    return state_of(health_system) == ACTIVE


def can_sign(health_system: Optional[Dict[str, Any]]) -> bool:
    """Whether the agreement surface accepts a signature right now.

    Only AWAITING_DLA. Signing from ACTIVE would be a re-signature, which §5.3
    does allow -- as a NEW row for a NEW document version -- but that is a
    deliberate operator-initiated re-paper, not something the portal offers on
    its own, and letting the portal do it silently would produce duplicate rows
    for one agreement.
    """
    return state_of(health_system) == AWAITING_DLA


def check_transition(current: str, target: str) -> None:
    """Raise unless ``current -> target`` is a legal edge."""
    cur = (current or "").strip().lower() or ACTIVE
    tgt = (target or "").strip().lower()
    if tgt not in STATES:
        raise TransitionRefused(f"{target!r} is not an onboarding state.")
    if cur == tgt:
        raise TransitionRefused(f"Already in {tgt!r}.")
    if tgt not in _TRANSITIONS.get(cur, frozenset()):
        raise TransitionRefused(f"Cannot go from {cur!r} to {tgt!r}.")


def public_view(health_system: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """What the PORTAL is told about its own state. Three keys, all partner
    words, no raw token that would need a glossary."""
    state = state_of(health_system)
    return {
        "state": state,
        "state_label": LABELS.get(state, LABELS[ACTIVE]),
        "next_step": NEXT_STEP.get(state, NEXT_STEP[ACTIVE]),
    }
