"""The one case a physician is judged on, and where it comes from.

An applicant used to be assessed on the PRACTICE case, which is a guided tour
with a "Skip this step" button on every screen. That is a poor thing to decide
about somebody with, and it made an exercise meant to teach behave like a test.

So the practice case teaches, and this is the examination: one case, in the
applicant's own specialty, in the same workspace and the same interface a paid
case uses, with the same validation. The founders' instruction was "the same
format and the same way we do tasks currently", and the closest honest reading
of that is not a special exam screen but the real one.

WHERE THE CASE COMES FROM. ``gold_cases`` already holds ratified, pre-authored
V3 cases for nephrology, cardiology and oncology: ``case_source: "synthetic"``,
each with its own authored A/B pair, no LLM needed to serve one. A nephrologist
sits a synthetic nephrology case. Nothing new had to be written.

WHY NOT THE LIVE QUEUE. Drawing from ``/tasks/next`` would have been the most
literal reading, and it is worse in three ways: every applicant would sit a
different case, so nobody could be compared with anybody; an unverified account
would read live buyer data; and a real case would be consumed per applicant.

WHY THE ANSWERS LIVE IN THEIR OWN TABLE. A gold task is also served to paid
physicians, so writing an applicant's answers into ``submissions`` against a
live ``task_id`` would put them within reach of the pay and export paths, and
``AGENTS.md`` documents an "exactly three code sites may write export_ready"
invariant that nobody should be testing on a hunch. A separate table makes it
structurally impossible: there is no join from here into records.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

log = logging.getLogger("asclepius.exam_case")

#: What an applicant sits when their own specialty has no authored gold set.
#: Nephrology is the specialty this product was built around and the one with
#: the deepest case set, so a physician outside the three we author for gets a
#: real, ratified case rather than nothing. The screen SAYS SO rather than
#: quietly handing a cardiologist a kidney case: see `exam_specialty`'s second
#: return value, which the client renders.
FALLBACK_SPECIALTY = "nephrology"


def available_specialties() -> List[str]:
    from asclepius.gold_cases import GOLD_CASE_SETS

    return sorted(GOLD_CASE_SETS)


def exam_specialty(user: Dict[str, Any]) -> Dict[str, Any]:
    """Which specialty's cases this applicant sits, and whether it is theirs.

    Returns ``{specialty, is_own, applied_with}``. ``is_own`` is False when we
    had to fall back, and the client says so out loud: serving a cardiologist a
    nephrology case without a word would read as a broken product, and they
    would reasonably answer it as though we had made a mistake.
    """
    applied = (user or {}).get("specialty") or ""
    applied = applied.strip().lower()
    sets = available_specialties()
    if applied in sets:
        return {"specialty": applied, "is_own": True, "applied_with": applied}
    # THE EXACT-MATCH TEST ABOVE IS NOT ENOUGH, and the gap was silent.
    #
    # Physicians do not type registry names. They type "interventional
    # cardiology", "pediatric cardiology", "cardiologist", "nephrology
    # transplant" - and every one of those fell straight past `in sets` to the
    # nephrology fallback. A cardiologist was handed a nephrology case and told,
    # correctly and uselessly, that we had no set for their specialty.
    #
    # specialties.match_specialty already solves exactly this: registry hit,
    # then alias, then leading token, then the practitioner-noun stem, and it
    # returns None rather than guessing, because a WRONG specialty is worse than
    # a missing one. Its answer is only usable here if we also hold a case set
    # for what it names, so hepatology (enabled for generation, no gold set)
    # still falls back honestly rather than 404ing on a draw.
    matched = _match_specialty(applied)
    if matched and matched in sets:
        return {"specialty": matched, "is_own": True, "applied_with": applied}
    return {"specialty": FALLBACK_SPECIALTY, "is_own": False, "applied_with": applied}


def _match_specialty(applied: str) -> Optional[str]:
    """``specialties.match_specialty``, and never an exception.

    Imported lazily and wrapped because this sits on the path that draws an
    applicant's examination: a registry that cannot be read should cost them
    the specialty match, not the case.
    """
    if not applied:
        return None
    try:
        from asclepius.specialties import match_specialty  # noqa: PLC0415

        return match_specialty(applied)
    except Exception:  # pragma: no cover - defensive
        return None


def exam_task_for(store: Any, user: Dict[str, Any], attempt: int,
                  *, seed: bool = True) -> Optional[Dict[str, Any]]:
    """The gold task this attempt draws, or None when none can be loaded.

    Selected BY ID rather than through ``_query_next``, deliberately. The queue
    path takes a lease, counts against the case's ``max_labels`` and reorders
    what a paid physician sees next; an examination must do none of those. This
    reads a row and hands it back.

    The attempt number rotates the choice, so a retake is a different case. The
    rotation is deterministic rather than random: two applicants in the same
    specialty on their first attempt sit the same case, which is what makes
    them comparable to the person reading both.
    """
    from asclepius.gold_cases import GOLD_CASE_SETS, load_gold_cases

    picked = exam_specialty(user)
    specialty = picked["specialty"]
    entries = GOLD_CASE_SETS.get(specialty) or []
    if not entries:
        return None

    # Idempotent and LLM-free: already-present cases are skipped. Cheap enough
    # to call on the draw, which is what keeps a fresh deployment from having
    # an examination that 404s until somebody remembers to seed it.
    #
    # ``seed=False`` for the callers that are only ASKING which case this is —
    # `is_users_exam_task` runs on every task fetch from a legacy-blob account,
    # including the ones that end in 403, and an authorization predicate has no
    # business writing rows. A deployment with no gold cases yet answers "not
    # your task", which is the safe direction; the draw seeds them.
    if seed:
        try:
            load_gold_cases(store, specialty=specialty)
        except Exception:
            log.exception("[exam] could not ensure gold cases for %s", specialty)

    idx = max(0, int(attempt or 1) - 1) % len(entries)
    task_id = "gold-" + entries[idx]["case_id"]
    task = store.get_task(task_id)
    if not task:
        # The rotation landed on a case this deployment does not have. Any
        # loaded case is better than refusing an applicant their examination.
        for entry in entries:
            task = store.get_task("gold-" + entry["case_id"])
            if task:
                break
    return task


def is_users_exam_task(store: Any, user: Dict[str, Any], task_id: str) -> bool:
    """True when ``task_id`` is the case this user's OWN examination is sitting on.

    The identity check behind the one provisional carve-out (Onboarding Master
    PRD A §2.1). An applicant is not granted "task access"; they are granted
    access to *this* row, the one ``/exam/task`` already served them, and the
    answer comes from their own ``tutorial_json`` rather than from anything the
    client sends.

    Deliberately conservative in three ways:

    * An examination that was never drawn opens nothing. ``state`` must be
      ``in_progress`` or ``submitted`` — a bare ``{"attempt": 1}`` blob is not a
      claim on a task.
    * The stamp is the primary answer. ``/exam/task`` records ``task_id`` on the
      draw, so the common path is a string comparison against a value only the
      server has ever written.
    * The fallback is a RECOMPUTE, not a guess. Applicants who were already
      mid-examination when this shipped have a stamp with no ``task_id`` in it,
      so for those we re-derive the case the same deterministic rotation would
      serve for their attempt and compare against that. It is the same function
      that served them, so it cannot admit a case they were not given, and it
      is additive: no stored blob is rewritten to make this work.
    """
    task_id = (task_id or "").strip()
    if not task_id:
        return False
    try:
        blob = store.get_tutorial_state(user["id"]) or {}
    except Exception:
        log.exception("[exam] could not read tutorial state for %s", user.get("id"))
        return False
    exam = blob.get("exam") if isinstance(blob.get("exam"), dict) else None
    if not exam:
        return False
    if exam.get("state") not in ("in_progress", "submitted"):
        return False

    stamped = str(exam.get("task_id") or "").strip()
    if stamped:
        return stamped == task_id

    # Legacy blob, drawn before the stamp existed. Recompute rather than trust.
    attempt = int(exam.get("attempt") or 0) or 1
    try:
        # seed=False: this is an authorization question, and answering it must
        # not write rows. See exam_task_for.
        task = exam_task_for(store, user, attempt, seed=False)
    except Exception:
        log.exception("[exam] could not re-derive exam task for %s", user.get("id"))
        return False
    return bool(task) and str(task.get("task_id") or "") == task_id


def exam_state(store: Any, user: Dict[str, Any]) -> str:
    """What stage of the examination this account is at: the one word the login
    gate branches on (Onboarding Master PRD §3.2 step 1).

    Returns ``"not_started"``, ``"in_progress"`` or ``"submitted"``. Anything
    unreadable — no blob, a corrupt blob, a store that raises — answers
    ``"not_started"``, which is the safe direction: the caller uses this to
    decide whether to TELL an applicant they still owe us an examination, and
    saying so to somebody who has already sat one is a smaller harm than
    silently withholding the only door back into their account.
    """
    try:
        blob = store.get_tutorial_state(user["id"]) or {}
    except Exception:
        log.exception("[exam] could not read tutorial state for %s", user.get("id"))
        return "not_started"
    exam = blob.get("exam") if isinstance(blob.get("exam"), dict) else None
    if not exam:
        return "not_started"
    state = str(exam.get("state") or "").strip()
    return state if state in ("in_progress", "submitted") else "not_started"
