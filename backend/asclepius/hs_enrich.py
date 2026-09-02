"""Light enrichment for a health-system introduction (HS-REF).

A physician names someone at a health system. Before we email that person on
the physician's behalf, we spend one model call establishing three things: the
person holds the role they were described as holding, the organization is the
kind of institution we think it is, and there is nothing about it that means we
should not be writing at all.

─── Why this is deliberately LIGHT ──────────────────────────────────────────
The temptation with a research step is to make it thorough: find the recent
press release, the earnings call, the conference talk, and open with it. That
is the wrong trade here for two reasons.

The first is that the personalization this email actually runs on is not a fact
we discovered, it is the referring physician's name and the sentence they
wrote about how they know the recipient. A colleague vouching for us outperforms
any hook research can produce, and it is already in hand before the call is made.

The second is that a hook is a liability when it is wrong. A confidently-cited
detail about the wrong James Okoye, or about a merger that closed two years ago,
reads worse than no detail at all, and it lands in the inbox of the person whose
introduction the physician staked their own relationship on. So this asks a small
number of checkable questions and is built to return NOTHING rather than
something shaky.

─── Nothing here may fail the send ──────────────────────────────────────────
Enrichment is an enhancement to an email that is already worth sending. Every
failure mode, no API key, a timeout, a refusal, a malformed answer, a model
that never calls the tool, resolves to ``state="skipped"`` and the caller sends
its clean body. The one outcome that stops a send is ``state="blocked"``, which
is a POSITIVE finding that we should not be writing to this person, not an
absence of findings. This function does not raise.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

log = logging.getLogger("asclepius.hs_enrich")

#: What the model is asked to fill in. Kept small on purpose, every field here
#: has to be worth a physician's introduction being delayed by the round trip.
RECORD_TOOL: Dict[str, Any] = {
    "name": "record_enrichment",
    "description": (
        "Record what you established about the contact and their health system. "
        "Call this exactly once, after searching. If you could not establish "
        "something, say so honestly in the fields rather than guessing."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "role_confirmed": {
                "type": "boolean",
                "description": "True only if a public source shows this person in this role at this organization.",
            },
            "org_confirmed": {
                "type": "boolean",
                "description": "True only if the organization exists and is a health system, hospital, or provider group.",
            },
            "org_type": {
                "type": "string",
                "description": "Short plain-language type, e.g. 'academic medical center', 'multi-state nonprofit system', 'physician group'. Empty string if unknown.",
            },
            "size_bucket": {
                "type": "string",
                "enum": ["single_site", "small_system", "regional_system", "large_system", "unknown"],
                "description": "Rough scale. Use 'unknown' rather than guessing.",
            },
            "one_public_fact": {
                "type": "string",
                "description": (
                    "At most one short, specific, currently-true fact about the ORGANIZATION "
                    "that a stranger could verify from the source URL today. Empty string if "
                    "you do not have one you would stake the introduction on. Never a fact "
                    "about the person's private life, and never inferred."
                ),
            },
            "source_url": {
                "type": "string",
                "description": "The URL the fact came from. Empty string if there is no fact.",
            },
            "seen_date": {
                "type": "string",
                "description": "The date on the source, YYYY-MM-DD, or empty string if undated.",
            },
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": "Your confidence that you found the right person at the right organization.",
            },
            "do_not_contact": {
                "type": "boolean",
                "description": (
                    "True if we should NOT email this person: the organization is a direct "
                    "competitor selling clinical AI evaluation data, the contact is a public "
                    "figure unrelated to the named system, or the organization is not a "
                    "healthcare provider at all."
                ),
            },
            "do_not_contact_reason": {
                "type": "string",
                "description": "One sentence, only when do_not_contact is true. Empty string otherwise.",
            },
        },
        "required": [
            "role_confirmed", "org_confirmed", "org_type", "size_bucket",
            "one_public_fact", "source_url", "seen_date", "confidence",
            "do_not_contact", "do_not_contact_reason",
        ],
    },
}

#: The dynamic-filtering search variant. ``max_uses`` is the cost ceiling AND
#: the latency ceiling: this call sits between a physician pressing a button and
#: an email going out, so a research agent that browses for a minute is the
#: wrong shape even when it would answer better.
_SEARCH_TOOL: Dict[str, Any] = {
    "type": "web_search_20260209",
    "name": "web_search",
    "max_uses": 4,
}

_SYSTEM = """You are verifying one business contact before a colleague-to-colleague introduction email is sent. You are not writing the email and you are not selling anything.

Search for the named person at the named organization. Establish only:
1. Does this person hold this role at this organization?
2. Is this organization a real healthcare provider, and roughly how large?
3. Is there any reason we should not write to them at all?

Then call record_enrichment exactly once.

Rules that matter more than completeness:
- An honest "I could not confirm this" is worth more than a plausible guess. Set confidence to "low" and leave fields empty rather than filling them in from inference.
- one_public_fact must be about the ORGANIZATION, checkable at source_url today, and something you would be comfortable seeing quoted back. If you do not have one that clears that bar, return an empty string. Most of the time you will not have one, and that is a correct outcome.
- Never record anything about the person's personal life, health, family, or politics.
- If search results are about a different person with the same name, that is a "low" confidence answer, not a fact."""


def _find_tool_use(resp: Any, name: str) -> Optional[Dict[str, Any]]:
    """The ``record_enrichment`` arguments, or None.

    Scans the WHOLE content list rather than reading ``content[0]``. With a
    server-side search tool in play the response interleaves ``server_tool_use``
    and ``web_search_tool_result`` blocks, and often a line of assistant text , 
    before anything we care about, so position is not a safe assumption.
    """
    for block in list(getattr(resp, "content", []) or []):
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == name:
            raw = getattr(block, "input", None)
            return dict(raw) if isinstance(raw, dict) else None
    return None


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _normalize(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce the model's answer into the exact shape callers may rely on.

    Built by WHITELIST: a field the model invented is dropped rather than
    carried into an email template. Same reasoning as ``public_referral``, a
    whitelist cannot leak the next key somebody adds upstream.
    """
    fact = _clean(raw.get("one_public_fact"))
    source = _clean(raw.get("source_url"))
    # A fact without a source is an assertion, and an assertion is what this
    # whole module exists to avoid putting in front of a stranger. Drop both.
    if not source or not source.lower().startswith(("http://", "https://")):
        fact, source = "", ""
    if not fact:
        source = ""
    size = _clean(raw.get("size_bucket")) or "unknown"
    if size not in ("single_site", "small_system", "regional_system", "large_system", "unknown"):
        size = "unknown"
    confidence = _clean(raw.get("confidence")).lower()
    if confidence not in ("high", "medium", "low"):
        confidence = "low"
    return {
        "role_confirmed": bool(raw.get("role_confirmed")),
        "org_confirmed": bool(raw.get("org_confirmed")),
        "org_type": _clean(raw.get("org_type"))[:120],
        "size_bucket": size,
        "one_public_fact": fact[:400],
        "source_url": source[:500],
        "seen_date": _clean(raw.get("seen_date"))[:20],
        "confidence": confidence,
        "do_not_contact": bool(raw.get("do_not_contact")),
        "do_not_contact_reason": _clean(raw.get("do_not_contact_reason"))[:300],
    }


def may_personalize(enrichment: Optional[Dict[str, Any]]) -> bool:
    """Whether the email may cite the enriched fact.

    THE GATE. Every condition has to hold: we have a fact, we have a source for
    it, we are confident we found the right person, and the organization checked
    out. A single missing piece sends the clean body instead, an email with one
    less sentence, rather than an email with one wrong sentence in it.
    """
    if not enrichment:
        return False
    if enrichment.get("do_not_contact"):
        return False
    if enrichment.get("confidence") not in ("high", "medium"):
        return False
    if not enrichment.get("org_confirmed"):
        return False
    return bool(enrichment.get("one_public_fact") and enrichment.get("source_url"))


async def enrich_health_system(
    *,
    contact_name: str,
    contact_role: str,
    hs_name: str,
) -> Dict[str, Any]:
    """Verify one contact. Returns ``{"state": ..., "data": ...}``; never raises.

    ``state`` is one of:
      ``ok``: we have an answer; ``may_personalize`` decides if it is usable
      ``skipped``: no answer (no key, timeout, refusal, no tool call, bad JSON)
      ``blocked``: a positive finding that we must not email this person
    """
    from ai.model_config import is_anthropic_configured

    if not is_anthropic_configured():
        # Not an error: a deployment without a key still sends introductions,
        # it just sends the clean ones.
        return {"state": "skipped", "data": None, "reason": "no_api_key"}

    who = _clean(contact_name)
    org = _clean(hs_name)
    if not who or not org:
        return {"state": "skipped", "data": None, "reason": "insufficient_input"}

    role = _clean(contact_role)
    user = (
        f"Person: {who}\n"
        f"Stated role: {role or '(not given)'}\n"
        f"Organization: {org}\n\n"
        "Verify and call record_enrichment."
    )

    try:
        from ai.llm_client import call_llm

        resp, _rec = await call_llm(
            role="asclepius_hs_enrich",
            purpose="hs_referral_enrichment",
            system=_SYSTEM,
            messages=[{"role": "user", "content": user}],
            tools=[_SEARCH_TOOL, RECORD_TOOL],
            # NO forced tool_choice. Pinning ``record_enrichment`` would make the
            # model call it on turn one, before it has searched anything, and the
            # whole point of the call is what the search turns up.
        )
    except Exception as exc:  # noqa: BLE001, an enhancement must never fail the send
        log.warning("hs_enrich: call failed for %r at %r: %r", who, org, exc)
        return {"state": "skipped", "data": None, "reason": "call_failed"}

    # A turn that paused on the server-side search budget has no final answer in
    # it. Resuming would cost another round trip on a call that is already the
    # slow part of a button press; the clean email is the better trade.
    if getattr(resp, "stop_reason", None) == "pause_turn":
        return {"state": "skipped", "data": None, "reason": "paused"}

    raw = _find_tool_use(resp, "record_enrichment")
    if raw is None:
        return {"state": "skipped", "data": None, "reason": "no_tool_use"}

    data = _normalize(raw)
    if data["do_not_contact"]:
        return {"state": "blocked", "data": data,
                "reason": data.get("do_not_contact_reason") or "flagged"}
    return {"state": "ok", "data": data, "reason": None}


def to_json(data: Optional[Dict[str, Any]]) -> Optional[str]:
    """Serialize for the ``enrich_json`` column, with sorted keys so the stored
    text is stable and diffable across runs."""
    if not data:
        return None
    try:
        return json.dumps(data, sort_keys=True)
    except (TypeError, ValueError):
        return None
