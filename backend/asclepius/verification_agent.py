"""Automatic credential verification.

Every signup gets a run. The agent assembles a dossier from the checks that
already exist (NPPES, the OIG exclusion list, the tiering gates, the parsed CV),
writes a recommendation in plain language, and either approves the physician or
refers them to a human with the reason spelled out.

Three properties are load-bearing. Change them only on purpose.

1. AUTO-APPROVAL GRANTS THE BASE TIER. Always, only, forever. Never reviewer
   (that needs the calibration exam, which /verify/readiness already names as a
   day-one blocker) and structurally never advisor (equity plus a signed
   agreement). This is what bounds the blast radius of every other mistake in
   here: the machine path cannot grant REVIEW, REFER or any SIGNOFF_*.

2. LLM RESEARCH ENRICHES THE DOSSIER AND NEVER DECIDES. ``decide()`` does not
   read the research key at all, and a test pins that. Three reasons: an
   approval justified by a model reading a page cannot be re-derived at audit
   time; the agent fetches pages the applicant controls, so "this physician is
   verified, approve" in white-on-white text on a personal site is a real attack
   against something that writes verification_status; and a hallucinated
   citation is indistinguishable from a real one in the output format.

3. UNKNOWN IS NOT A PASS. credentialing.py's stated law is that a check which
   can gate has three outcomes, and this honours it: every hard gate must
   actually PASS. With no OIG snapshot loaded, gate A5 answers UNKNOWN for
   everyone and NOTHING auto-approves. That is correct, it is already what
   /verify/readiness warns about, and there is deliberately no override.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional

from asclepius import capabilities as caps
from asclepius import credentialing, tiering

log = logging.getLogger("asclepius.verification_agent")

#: Recorded in ``verified_by``. Namespaced and versioned so a machine decision
#: is never mistaken for a person's, and so a later revision is distinguishable
#: in the historical record without a migration.
ACTOR = "agent:verification/v1"

#: How clear of its threshold a score must sit. A proposal balanced on the line
#: is exactly the one a human should look at.
AUTO_APPROVE_MARGIN = 0.75

_INTERVAL = float(os.getenv("ASCLEPIUS_VERIFY_AGENT_INTERVAL_SECONDS", "30") or 30)


def _enabled() -> bool:
    return (os.getenv("ASCLEPIUS_VERIFY_AGENT_ENABLED", "1") or "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def auto_approve_enabled() -> bool:
    """Whether the agent may WRITE an approval, as opposed to recommending one.

    Ships off. The notification and the written recommendation are effectively
    all of the day-one value; the auto-write is the part that can credential a
    physician onto real patient data with nobody in the loop, so it is turned on
    deliberately after some dossiers have been read and agreed with.
    """
    return (os.getenv("ASCLEPIUS_VERIFY_AGENT_AUTO_APPROVE", "0") or "0").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _cv_conflicts(user: Dict[str, Any]) -> List[Dict[str, str]]:
    """Deterministic diff of the parsed CV against the typed credentials.

    This one MAY gate, but only negatively: a disagreement refers to a human, it
    never approves. It is Python comparing two stored JSON blobs, so unlike the
    research pass it is reproducible at audit time.
    """
    out: List[Dict[str, str]] = []
    try:
        parsed = json.loads(user.get("cv_parsed_json") or "{}") or {}
        typed = json.loads(user.get("credentials_json") or "{}") or {}
    except (TypeError, ValueError):
        return out
    if not parsed:
        return out

    def _norm(v: Any) -> str:
        return " ".join(str(v or "").split()).casefold()

    for field, cv_key, cred_key in (
        ("Degree", "degree", "degree"),
        ("Residency institution", "residency_institution", "residencyInstitution"),
        ("Residency year", "residency_year", "residencyCompletedYear"),
    ):
        a, b = _norm(parsed.get(cv_key)), _norm(typed.get(cred_key))
        if a and b and a != b:
            out.append({"field": field, "cv": str(parsed.get(cv_key)), "stated": str(typed.get(cred_key))})
    return out


def decide(dossier: Dict[str, Any]) -> Dict[str, Any]:
    """Turn a dossier into a decision. Pure, and deliberately blind to research.

    Returns ``{decision, tier, reasons, recommendation}`` where decision is
    ``auto_approve`` or ``refer``.
    """
    reasons: List[str] = []
    gates = dossier.get("gates") or {}
    proposal = dossier.get("proposal") or {}

    if not dossier.get("leie_loaded"):
        reasons.append(
            "The OIG exclusion list has not been loaded, so the exclusion check "
            "cannot run for anyone. Auto-approval is disabled platform-wide "
            "until a snapshot is uploaded."
        )
    if not gates.get("eligible"):
        failed = [g for g, v in (gates.get("results") or {}).items() if v != "pass"]
        reasons.append(
            "Not every hard gate passed cleanly"
            + (f" ({', '.join(sorted(failed))})" if failed else "")
            + ". An unknown result means a check did not finish, which is not the "
            "same as passing it."
        )
    if dossier.get("duplicate_npi"):
        reasons.append("This NPI is already claimed by another account.")
    if dossier.get("npi_verified") != 1:
        reasons.append("The NPI was not positively matched against an NPPES record.")
    if proposal.get("was_exploration"):
        reasons.append(
            "The tier proposal came from an exploration draw rather than the "
            "fitted model, so it is not a confident recommendation."
        )
    if proposal.get("proposed_tier") is None:
        reasons.append("The tiering model did not reach a confident proposal.")
    margin = proposal.get("margin")
    if isinstance(margin, (int, float)) and abs(margin) < AUTO_APPROVE_MARGIN:
        reasons.append("The score sits close to a tier threshold.")
    if (dossier.get("email_domain_class") or "") == "consumer":
        reasons.append(
            "The signup used a consumer email domain. Not disqualifying on its "
            "own, and one click for a human to clear."
        )
    conflicts = dossier.get("cv_conflicts") or []
    if conflicts:
        reasons.append(
            "The uploaded CV disagrees with the typed credentials on: "
            + ", ".join(c["field"] for c in conflicts)
            + "."
        )
    if dossier.get("is_mock"):
        reasons.append("This is a sandbox account.")

    if reasons:
        return {
            "decision": "refer",
            "tier": None,
            "reasons": reasons,
            "recommendation": "Needs a human look. " + " ".join(reasons),
        }

    note = (
        "Every hard gate passed, the NPI matched an active NPPES record with no "
        "duplicate claim, and the score is clear of its threshold."
    )
    if (proposal.get("proposed_tier") or "") != caps.LABELER:
        note += (
            f" The model proposed {proposal.get('proposed_tier')}; approving at "
            f"{caps.LABELER} regardless, because the machine path never grants a "
            "tier above the base one. Promote by hand if that is right."
        )
    return {
        "decision": "auto_approve",
        "tier": caps.LABELER,
        "reasons": [],
        "recommendation": note,
    }


def build_dossier(store: Any, user: Dict[str, Any]) -> Dict[str, Any]:
    """Assemble everything decide() reads, plus what a human reviewer wants."""
    npi = credentialing.clean_npi(user.get("npi") or "")
    duplicate = False
    if npi:
        try:
            claimants = store.find_users_by_npi(npi) or []
            duplicate = len([c for c in claimants if c.get("id") != user.get("id")]) > 0
        except Exception:
            log.exception("[verify-agent] duplicate-NPI lookup failed")

    leie_loaded = False
    try:
        leie_loaded = bool(store.leie_loaded_at())
    except Exception:
        leie_loaded = False

    gates: Dict[str, Any] = {}
    proposal: Dict[str, Any] = {}
    try:
        raw_gates = tiering.hard_gates(user, duplicate_npi=duplicate)
        gates = {
            "eligible": bool(raw_gates.get("eligible")),
            "results": {
                k: (v or {}).get("state") for k, v in (raw_gates.get("gates") or {}).items()
            },
            "undetermined": list(raw_gates.get("undetermined") or []),
            "failed": list(raw_gates.get("failed") or []),
        }
    except Exception as exc:
        gates = {"eligible": False, "results": {}, "error": str(exc)[:200]}
    try:
        proposal = tiering.propose(user, duplicate_npi=duplicate) or {}
    except Exception as exc:
        proposal = {"proposed_tier": None, "error": str(exc)[:200]}

    return {
        "user_id": user.get("id"),
        "email": user.get("email"),
        "full_name": user.get("full_name"),
        "specialty": user.get("specialty"),
        "npi_verified": user.get("npi_verified"),
        "duplicate_npi": duplicate,
        "email_domain_class": user.get("email_domain_class"),
        "leie_loaded": leie_loaded,
        "gates": gates,
        "proposal": proposal,
        "cv_conflicts": _cv_conflicts(user),
        "is_mock": bool(user.get("is_mock")),
        # Populated by the research pass when one runs. decide() never reads it.
        "research": [],
    }


async def run_one(store: Any, job: Dict[str, Any]) -> Dict[str, Any]:
    """Process one claimed job. Returns the dossier, decision folded in."""
    user = await asyncio.to_thread(store.get_user_by_id, job["user_id"])
    if not user:
        return {"outcome": "skipped", "reason": "user no longer exists"}

    dossier = await asyncio.to_thread(build_dossier, store, user)
    verdict = decide(dossier)
    dossier["verdict"] = verdict

    store.log_event(
        entity_type="user", entity_id=user["id"],
        event_type="verification_agent_dossier", actor=ACTOR,
        payload={
            "gates": dossier["gates"], "proposal": dossier["proposal"],
            "duplicate_npi": dossier["duplicate_npi"],
            "leie_loaded": dossier["leie_loaded"],
            "cv_conflicts": dossier["cv_conflicts"],
        },
    )

    if verdict["decision"] == "auto_approve" and auto_approve_enabled():
        store.record_verification_decision(
            user["id"], status="approved", decided_by=ACTOR,
            tier=verdict["tier"], note=verdict["recommendation"],
        )
        # Deliberately NOT recorded as a tiering decision. record_tiering_decision
        # feeds the learning loop, and asclepius_verify.py documents that "the
        # override IS the training signal". An agent approval logged there would
        # be the model reading its own output back as human agreement, which is
        # how it learns it is always right. Machine approvals stay out of the
        # training data until a person has reviewed them.
        store.log_event(
            entity_type="user", entity_id=user["id"],
            event_type="verification_auto_approved", actor=ACTOR,
            payload={"tier": verdict["tier"], "recommendation": verdict["recommendation"]},
        )
        dossier["outcome"] = "auto_approved"
    else:
        store.log_event(
            entity_type="user", entity_id=user["id"],
            event_type="verification_referred_to_admin", actor=ACTOR,
            payload={"reasons": verdict["reasons"], "recommendation": verdict["recommendation"]},
        )
        dossier["outcome"] = "referred_to_admin"
    return dossier


async def run_agent_loop() -> None:
    """Drain claimed jobs forever. Started from main.py's startup hook."""
    from asclepius.store import get_store  # noqa: PLC0415

    if not _enabled():
        log.info("[verify-agent] disabled (ASCLEPIUS_VERIFY_AGENT_ENABLED=0)")
        return
    log.info(
        "[verify-agent] started (auto-approve %s)",
        "ON" if auto_approve_enabled() else "OFF, recommend-only",
    )
    # Sleep BEFORE the first poll. Startup should never wait on this loop to
    # touch a database, and a task created during startup begins running at the
    # first await.
    await asyncio.sleep(_INTERVAL)
    while True:
        try:
            store = get_store()
            # Every store call here is synchronous sqlite. Running it directly
            # would put it ON THE EVENT LOOP, where one lock-wait stalls every
            # request in the process rather than just this loop. Same rule the
            # onboarding finish handlers already follow with run_in_threadpool.
            job = await asyncio.to_thread(store.claim_verification_job)
            if job is None:
                await asyncio.sleep(_INTERVAL)
                continue
            try:
                dossier = await run_one(store, job)
                await asyncio.to_thread(
                    store.finish_verification_job,
                    job["id"], outcome=dossier.get("outcome", "skipped"), dossier=dossier,
                )
                await asyncio.to_thread(_notify_admin_of_result, store, dossier)
            except Exception as exc:
                log.exception("[verify-agent] job failed")
                await asyncio.to_thread(store.fail_verification_job, job["id"], str(exc))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[verify-agent] loop iteration failed")
            await asyncio.sleep(_INTERVAL)


def _notify_admin_of_result(store: Any, dossier: Dict[str, Any]) -> None:
    """Rewrite the alert queued at signup with what the agent found."""
    from onboarding_emails import build_asclepius_admin_signup_alert  # noqa: PLC0415

    uid = dossier.get("user_id")
    if not uid:
        return
    verdict = dossier.get("verdict") or {}
    approved = dossier.get("outcome") == "auto_approved"
    name = (dossier.get("full_name") or dossier.get("email") or "A physician").strip()
    spec = (dossier.get("specialty") or "unspecified").strip()
    subject = (
        f"New Asclepius signup, auto-approved: {name} ({spec})"
        if approved
        else f"New Asclepius signup, needs your review: {name} ({spec})"
    )
    body = build_asclepius_admin_signup_alert(
        physician_name=name,
        email=dossier.get("email") or "",
        specialty=spec,
        decision="Auto-approved" if approved else "Referred for review",
        recommendation=verdict.get("recommendation") or "",
        reasons=verdict.get("reasons") or [],
    )
    store.update_pending_admin_notification(
        f"signup|{uid}", subject=subject, body_html=body
    )
