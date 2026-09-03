"""Community welcome on verification (Community v2).

The moment a physician becomes a verified colleague — the admin approval in
the verify queue, or the Contributors-vault flag — they are ALREADY a
community member (membership is derived, never stored). What was missing is
the human moment: this module posts the one-time public welcome into
#introductions, mentioning the new member, so the community's continuous
trickle of introductions never depends on anyone remembering to say hi.

Contract: idempotent (``users.slack_joined`` set FIRST — the safe failure is
a missed welcome, never a double-post) and best-effort (any failure logs and
returns; the verification decision must stand regardless, mirroring the
welcome-email guard in ``asclepius_verify``).
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from asclepius.store import get_store as get_asclepius_store

log = logging.getLogger("community.onboard")

WELCOME_CHANNEL = "introductions"


def _welcome_body(member: Dict[str, Any]) -> str:
    name = member.get("display_name") or "a new colleague"
    bits = []
    if member.get("specialty"):
        bits.append(str(member["specialty"]))
    years = member.get("years_in_practice")
    if years:
        bits.append(f"{years} yrs in practice")
    suffix = f" — {', '.join(bits)}" if bits else ""
    return (
        f"Please welcome **{name}**{suffix} to the community. "
        f"Say hello, and drop your own intro below."
    )


async def welcome_new_member(user: Dict[str, Any]) -> bool:
    """Post the one-time welcome for ``user`` (an asclepius users row).
    Returns True when a welcome was posted this call."""
    try:
        user_id = user.get("id")
        if not user_id or user.get("role") != "evaluator":
            return False  # staff and non-evaluator roles get no public intro
        if user.get("slack_joined"):
            return False  # already welcomed

        astore = get_asclepius_store()

        # Late imports: keep this module import-light so the approval hook can
        # import it without dragging the router in at module load.
        from community.router import member_map  # noqa: PLC0415
        from community.system_posts import post_system_message  # noqa: PLC0415

        # Resolve the member BEFORE claiming the welcome, which is the opposite
        # of the original order and the ordering that matters. Claiming first
        # burns the one-time flag on somebody who is not in the community yet,
        # and the flag is what every later caller checks: an applicant welcomed
        # at signup would never be welcomed at approval, which is the one moment
        # a physician actually walks into the room. A read cannot go wrong the
        # way that write can.
        #
        # Idempotency is unchanged, because it never rested on the order. It
        # rests on ``mark_community_welcomed`` being a guarded UPDATE that tells
        # exactly one concurrent caller it won; two callers that both see a
        # visible member still produce exactly one post.
        member = member_map().get(user_id)
        if not member:
            # Not gate-visible: banned, inactive, or not yet a verified
            # colleague. Introducing somebody who cannot open the door is noise
            # in #introductions and an invitation the product then refuses.
            log.info("[onboard] user %s not community-visible; no welcome", user_id)
            return False

        if not astore.mark_community_welcomed(user_id):
            return False  # lost the race: the other caller posts the welcome

        posted = await post_system_message(
            channel_slug=WELCOME_CHANNEL,
            body=_welcome_body(member),
            kind="system_welcome",
            mention_user_ids=[user_id],
            notify=True,
        )
        return posted is not None
    except Exception:
        log.exception("[onboard] community welcome failed (decision stands)")
        return False
