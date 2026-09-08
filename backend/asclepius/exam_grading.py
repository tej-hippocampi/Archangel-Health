"""What the examination shows, for the person deciding about the applicant.

THIS MODULE COMPUTES NO VERDICT, and the distinction it rests on is the whole
reason it can exist beside ``_examination_block``'s promise not to make one.

A verdict is "this physician is good enough". A FACT is "they rejected candidate
B, and candidate B is the one this case was authored to be wrong". The first is
the reading admin's call and putting a number beside somebody's answers would be
this code making it first. The second is just reading the case's own answer key
next to what they wrote, which an admin would otherwise do by hand, in their
head, differently every time and worse late on a Friday.

So what comes out of here is a small set of observations with the answer key
quoted alongside them. No score, no band, no pass, no fail, no recommendation.

WHY IT RUNS AT READ TIME. ``/exam/submit`` documents, in its own docstring, that
it computes nothing a client could read a verdict out of, and that promise is
worth more than the milliseconds this costs. Grading here also means the answer
key can be corrected without a backfill: the key lives in ``gold_cases.py``,
which is edited far more often than anyone would want to re-run a migration for.

Deterministic and LLM-free, in the idiom ``tutorial_case._grade_finding``
already uses: a phrase match over the physician's own prose. Phrase matching is
weak, and it is supposed to be. It can only ever say "they wrote the words the
key looked for", which is why the matched phrases are handed back for a person
to read rather than totalled into anything.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

#: Punctuation and case are noise; a physician who writes "urine osm 120" and a
#: key that says "urine osmolality 120 (LOW)" are saying the same thing, and no
#: string comparison is going to bridge that on its own. See ``_phrase_hit``.
_WS = re.compile(r"[^a-z0-9]+")


def _norm(text: Any) -> str:
    return _WS.sub(" ", str(text or "").lower()).strip()


def _tokens(text: str) -> List[str]:
    return [t for t in _norm(text).split() if t]

#: Words that carry no clinical signal and would let a phrase match on nothing.
_STOP = frozenset({
    "a", "an", "and", "at", "for", "in", "is", "of", "on", "or", "the", "to",
    "with", "low", "high", "given", "already", "recent",
})


def _phrase_hit(phrase: str, blob: str) -> bool:
    """Did the physician's prose reach for this piece of the key?

    A whole-phrase match is far too strict for free text: a key entry reading
    "urine osmolality 120 (LOW)" would miss "urine osm was 120", which is the
    same observation written the way a nephrologist actually writes it. So the
    test is that every CONTENTFUL token of the phrase appears somewhere in what
    they wrote.

    That is generous on purpose. This output is a reading aid; a false negative
    tells an admin a physician missed something they did not, which is the
    expensive direction, and a false positive costs nothing because the matched
    phrase is shown for a person to judge.
    """
    wanted = [t for t in _tokens(phrase) if t not in _STOP]
    if not wanted:
        return False
    written = set(_tokens(blob))
    return all(_token_hit(t, written) for t in wanted)


def _token_hit(token: str, written: set) -> bool:
    """One key token, allowing for the way clinicians actually write.

    Physicians abbreviate relentlessly: "urine osm" for urine osmolality, "cr"
    for creatinine, "bicarb" for bicarbonate. An exact token match calls every
    one of those a miss, and a miss here tells an admin a physician overlooked
    something they in fact named, which is the expensive direction to be wrong
    in.

    So a token also counts when one side is a prefix of the other, with a floor
    of three characters on the written side. The floor is what stops "na"
    matching "nausea" and turning an abbreviation rule into a coincidence
    generator.
    """
    if token in written:
        return True
    return any(
        len(w) >= 3 and (token.startswith(w) or w.startswith(token))
        for w in written
    )


def _answer_key(task: Dict[str, Any]) -> Dict[str, Any]:
    """The held-out key, wherever this case carries it.

    Gold cases nest it under ``case.ground_truth``; a generated task may carry
    it at the top level. Reading both keeps this working across the two rather
    than being correct for the ten cases somebody happened to test it on.
    """
    if not isinstance(task, dict):
        return {}
    case = task.get("case")
    if isinstance(case, dict) and isinstance(case.get("ground_truth"), dict):
        return case["ground_truth"]
    gt = task.get("ground_truth")
    return gt if isinstance(gt, dict) else {}


def _intended_flawed_id(task: Dict[str, Any]) -> str:
    """Which candidate the case was authored to be wrong.

    In ``gold_cases.py`` this is a top-level key on the case entry. By the time
    the case is a TASK ROW it has moved into ``generation`` alongside the rest
    of the provenance, and reading only the top level finds nothing: the whole
    candidate observation silently disappeared from the decision screen while
    the key-data half of the same card kept working, which is exactly the shape
    of bug that survives a green test suite. Both places are read.
    """
    direct = str(task.get("intended_flawed_id") or "").strip().upper()
    if direct:
        return direct
    gen = task.get("generation")
    if isinstance(gen, dict):
        return str(gen.get("intended_flawed_id") or "").strip().upper()
    return ""


def observations(task: Optional[Dict[str, Any]],
                 payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Facts about one filed examination, or an empty reading when it cannot be.

    ``graded`` is False when the case is gone or carries no key, and the caller
    renders "we cannot line this up against an answer key" rather than an empty
    checklist that reads as a physician having missed everything.
    """
    task = task or {}
    payload = payload or {}
    key = _answer_key(task)
    intended_flawed = _intended_flawed_id(task)
    key_data = [str(k) for k in (key.get("key_data") or []) if str(k).strip()]

    if not intended_flawed and not key_data:
        return {"graded": False, "reason": "no_answer_key"}

    rejected = str(payload.get("rejected_id") or "").strip().upper()
    chosen = str(payload.get("chosen_id") or "").strip().upper()

    # Every place the physician wrote prose. The rubric and the reasoning steps
    # count: a doctor who names the deciding lab in a rubric row rather than in
    # the free text has still named it.
    parts: List[str] = [
        str((payload.get("independent_answer") or {}).get("text") or ""),
        str(payload.get("verdict") or ""),
    ]
    for step in (payload.get("reasoning_steps") or []):
        if isinstance(step, dict):
            parts.append(str(step.get("text") or step.get("note") or ""))
        else:
            parts.append(str(step))
    for row in (payload.get("rubric") or []):
        if isinstance(row, dict):
            parts.append(str(row.get("text") or ""))
    critique = payload.get("rejected_critique")
    if isinstance(critique, dict):
        parts.append(str(critique.get("note") or ""))
    blob = _norm(" ".join(parts))

    matched = [k for k in key_data if _phrase_hit(k, blob)]
    missed = [k for k in key_data if k not in matched]

    return {
        "graded": True,
        # Did they reject the candidate this case was authored to be wrong?
        # None when the case declares no flawed candidate, which is a property
        # of the case and must not read as the physician failing to do a thing
        # they were never asked to do.
        "rejected_the_flawed_candidate": (rejected == intended_flawed) if intended_flawed else None,
        "intended_flawed_id": intended_flawed or None,
        "rejected_id": rejected or None,
        "chosen_id": chosen or None,
        # The key's own phrases, split into the ones their prose reached for and
        # the ones it did not. Handed back in full so the admin reads the words
        # rather than a fraction.
        "key_data_matched": matched,
        "key_data_missed": missed,
        "key_data_total": len(key_data),
        # Context, deliberately unscored. A fast examination is not a worse one
        # and a slow one is not better; this is here because "four minutes" and
        # "fifty minutes" are different things to read and the admin should see
        # which they are looking at.
        "time_spent_sec": int(payload.get("time_spent_sec") or 0),
        "answer": str(key.get("answer") or "") or None,
        "rationale": str(key.get("rationale") or "") or None,
    }


def own_specialty(exam_row: Dict[str, Any]) -> Optional[bool]:
    """Was this case in the applicant's own specialty?

    None means we do not know, which is the honest answer for every row filed
    before the column existed. It matters because a nephrology case answered by
    a hepatologist measures something other than what it measures for a
    nephrologist, and a signal that quietly means two things is worse than one
    that says it does not know.
    """
    raw = (exam_row or {}).get("is_own_specialty")
    if raw is None:
        return None
    return bool(raw)
