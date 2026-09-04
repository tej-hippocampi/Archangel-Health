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
    return {"specialty": FALLBACK_SPECIALTY, "is_own": False, "applied_with": applied}


def exam_task_for(store: Any, user: Dict[str, Any], attempt: int) -> Optional[Dict[str, Any]]:
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
