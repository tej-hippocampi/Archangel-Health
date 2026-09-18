"""Specialty-matched examinations with assessment answers kept out of paid data.

The authored gold sets remain available for nephrology, cardiology and oncology.
Other specialties use the separately stored, evidence-reviewed synthetic
onboarding bank. An absent case never becomes an unrelated specialty's case.
Examination responses and their answer-key snapshots live in credentialing_exams;
new onboarding cases never enter tasks, submissions or export inventory.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

log = logging.getLogger("asclepius.exam_case")

def available_specialties() -> List[str]:
    from asclepius.gold_cases import GOLD_CASE_SETS

    return sorted(GOLD_CASE_SETS)


def exam_specialty(user: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve the confirmed specialty, with no unrelated fallback."""
    from asclepius.onboarding_specialties import resolve
    picked = resolve(user or {})
    return {**picked, "is_own": bool(picked["specialty"])}


def exam_task_for(store: Any, user: Dict[str, Any], attempt: int,
                  *, seed: bool = True) -> Optional[Dict[str, Any]]:
    """The ready own-specialty case this attempt draws, or None.

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
        from asclepius import onboarding_cases
        return (onboarding_cases.get_task(store, onboarding_cases.task_id(specialty, "examination", attempt))
                if specialty else None)

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


def task_for_id(store: Any, task_id: str) -> Optional[Dict[str, Any]]:
    """Resolve onboarding material explicitly; never add it to paid inventory."""
    from asclepius import onboarding_cases
    if str(task_id or "").startswith(onboarding_cases.PREFIX):
        return onboarding_cases.get_task(store, task_id)
    return store.get_task(task_id)


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
