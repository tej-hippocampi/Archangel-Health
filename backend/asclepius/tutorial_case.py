"""Calibration Case 1 — the first-run tutorial's practice case + answer key.

A single hand-authored nephrology case a brand-new evaluator labels on their
first login, guided by the frontend tour. It is a REAL exercise of the real
labeling flow (same blinding, same stages, same payload), but the task is fully
virtual: it never exists in the ``tasks`` table, its submission never reaches
the pipeline, and nothing it produces can appear in the queue, records, exports,
stats, agreement, or value metrics. The only durable trace is ``events`` rows.

The case is scored against ``REFERENCE_FINDINGS`` — deterministic, no-LLM
graders over the exact fields the client's ``buildSubmissionPayload()`` sends —
and the result screen explains each match/miss in one clause ("calibration",
not "tutorial": the doctor is being compared to a reference panel, not taught
to click buttons).

Clinical content: the cardiorenal "permissive creatinine rise" trap — a heart
failure patient whose creatinine rises on a loop diuretic. The loud signal
(rising creatinine) invites stopping diuresis and giving fluids; the quiet
decisive signal (still congested: JVP, orthopnea, edema, hemoconcentration) says
to intensify decongestion. Distinct content from every gold case (those are real
queue inventory and must never be burned as practice material).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from asclepius.cases import assert_multimodal_content, render_case_prompt

#: Bump whenever the case content or the answer key changes.
TUTORIAL_VERSION = 1

#: The oldest pass that still opens the gate.
#:
#: Two constants, deliberately. The practice case is a hard gate on all real
#: work, so re-gating everyone who already passed is a supply outage. Bumping
#: TUTORIAL_VERSION for a typo, a clearer reason_miss, or a widened keyword set
#: must NOT do that -- otherwise improving the copy becomes an act of sabotage
#: against the supply side, and the copy stops being improved. Re-gating is a
#: separate, deliberate second edit to THIS constant, and it is a supply
#: decision rather than a content one.
TUTORIAL_PASS_MIN_VERSION = 1

TUTORIAL_TASK_ID = "tutorial-calibration-1"

#: Steps whose output the answer key actually reads. The tour's "Skip this
#: step" autofill fires the app's own handlers with a clinically reasonable
#: placeholder so nobody is stranded by a target that never renders, and that
#: affordance stays. But clinically reasonable is exactly what the key checks:
#: before this set existed, fourteen clicks of Skip matched all four findings,
#: which under a hard gate would have unlocked every paid case.
#:
#: Honest about what this is: the ledger is client-declared, so it is a
#: PEDAGOGY gate and not an authz boundary. A determined person could omit it.
#: That is acceptable because the only other route to a pass is doing the case
#: properly, which is the outcome we wanted from them anyway.
GRADED_STEP_IDS = frozenset({"ch3-verdict", "ch4-why", "ch4-critique", "ch5-rubric"})

#: What it takes to pass. Choosing the answer the panel rejected is
#: disqualifying on its own: a physician who anchors on the creatinine trend
#: has failed this case, and no amount of rubric hygiene should paper over it.
#: Three of four leaves exactly one degree of freedom, which is what makes the
#: planted finding survivable.
#:
#: DO NOT raise this to 4. The planted finding is graded by substring match
#: over free text, and at 4-of-4 a regex false negative is the thing standing
#: between a competent physician and paid work. A supply outage caused by a
#: keyword miss is a far worse failure than a lenient pass.
PASS_REQUIRED_FINDING = "sound-answer"
PASS_MIN_MATCHED = 3

# Criticality bands mirror the client's tierForPoints: critical = |points| 8-10.
_CRITICAL_NEGATIVE_FLOOR = -8

TUTORIAL_CASE_ENTRY: Dict[str, Any] = {
    "case_id": "tutorial-calibration-1",
    "title": "Calibration Case 1 — creatinine rise on decongestion",
    "ai_failure_mode": "anchoring (rising creatinine → stop diuresis / give fluids) despite persistent congestion",
    "question": (
        "A patient admitted with acute decompensated heart failure has a creatinine "
        "rising from 1.4 to 1.9 on IV furosemide. How do you interpret the creatinine "
        "rise and what do you do with the diuresis?"
    ),
    "intended_flawed_id": "A",
    "candidate_answers": [
        {"id": "A", "text": (
            "The rising creatinine indicates diuretic-induced acute kidney injury from "
            "overdiuresis and intravascular volume depletion. Hold the furosemide and give "
            "a gentle crystalloid bolus (250-500 mL) to restore renal perfusion, then "
            "recheck the creatinine in 12-24 hours before resuming any diuretic. Renal "
            "recovery takes priority; decongestion can resume once the creatinine returns "
            "toward baseline.")},
        {"id": "B", "text": (
            "This patient is still congested — JVP 12 cm, orthopnea, 2+ edema, and the "
            "weight is down only 1.5 kg from admission — so the creatinine rise on "
            "diuresis is expected and permissive, not diuretic-induced AKI. Hemoconcentration "
            "has not yet occurred (stable hematocrit/albumin), which argues against "
            "intravascular depletion. The move is to intensify decongestion (higher loop "
            "dose or continuous infusion, consider adding a thiazide), follow daily weights, "
            "urine output, and electrolytes, and tolerate a modest creatinine rise; stopping "
            "diuresis and giving fluids would re-congest the patient and worsen outcomes.")},
    ],
    "case": {
        "case_source": "synthetic", "specialty": "nephrology",
        "demographics": {"age_band": "60-69", "sex": "male"},
        "problem_list": [
            {"condition": "Heart failure with reduced ejection fraction (EF 30%)", "since": "2023"},
            {"condition": "Chronic kidney disease stage 2", "since": "2022"},
            {"condition": "Type 2 diabetes mellitus", "since": "2015"},
        ],
        "medications": [
            {"drug": "furosemide", "dose": "80 mg IV", "route": "IV", "freq": "BID"},
            {"drug": "lisinopril", "dose": "10 mg", "route": "PO", "freq": "daily"},
            {"drug": "metoprolol succinate", "dose": "50 mg", "route": "PO", "freq": "daily"},
            {"drug": "empagliflozin", "dose": "10 mg", "route": "PO", "freq": "daily"},
        ],
        "vitals": {"bp": "104/68", "hr": 84, "rr": 20, "weight_kg": 92.5,
                   "volume": "JVP 12 cm, 2+ pitting edema to knees, orthopnea; weight down 1.5 kg from 94 kg at admission"},
        "lab_panels": [
            {"panel": "BMP (admission)", "collected_offset_days": -2, "results": [
                {"analyte": "Sodium", "value": 134, "unit": "mmol/L", "ref_low": 135, "ref_high": 145, "flag": "L"},
                {"analyte": "Potassium", "value": 4.2, "unit": "mmol/L", "ref_low": 3.5, "ref_high": 5.0, "flag": ""},
                {"analyte": "Creatinine", "value": 1.4, "unit": "mg/dL", "ref_low": 0.7, "ref_high": 1.3, "flag": "H"},
                {"analyte": "BUN", "value": 28, "unit": "mg/dL", "ref_low": 7, "ref_high": 20, "flag": "H"},
            ]},
            {"panel": "BMP (hospital day 2)", "collected_offset_days": 0, "results": [
                {"analyte": "Sodium", "value": 133, "unit": "mmol/L", "ref_low": 135, "ref_high": 145, "flag": "L"},
                {"analyte": "Potassium", "value": 3.8, "unit": "mmol/L", "ref_low": 3.5, "ref_high": 5.0, "flag": ""},
                {"analyte": "Creatinine", "value": 1.9, "unit": "mg/dL", "ref_low": 0.7, "ref_high": 1.3, "flag": "H"},
                {"analyte": "BUN", "value": 39, "unit": "mg/dL", "ref_low": 7, "ref_high": 20, "flag": "H"},
            ]},
            {"panel": "CBC + markers (hospital day 2)", "collected_offset_days": 0, "results": [
                {"analyte": "Hematocrit", "value": 38, "unit": "%", "ref_low": 40, "ref_high": 52, "flag": "L"},
                {"analyte": "Albumin", "value": 3.4, "unit": "g/dL", "ref_low": 3.5, "ref_high": 5.0, "flag": "L"},
                {"analyte": "NT-proBNP", "value": 9800, "unit": "pg/mL", "ref_low": None, "ref_high": 900, "flag": "H"},
            ]},
        ],
        "notes": [
            {"note_type": "Progress", "author_role": "cardiology", "text": (
                "Hospital day 2 of ADHF admission. On IV furosemide 80 BID; net negative "
                "~1.5 L since admission, weight 94 -> 92.5 kg. Still orthopneic on 2 pillows, "
                "JVP 12 cm at 45 degrees, 2+ pitting edema to the knees, bibasilar crackles. "
                "BP 104/68 with warm extremities. Creatinine up 1.4 -> 1.9; team debating "
                "whether to hold diuresis for 'AKI'. Hematocrit and albumin have not risen "
                "from admission (no hemoconcentration). Making urine, ~1.8 L/day.")},
        ],
        "ground_truth": {
            "answer": (
                "The creatinine rise is permissive, not diuretic-induced AKI: the patient "
                "remains clearly congested (JVP 12, orthopnea, 2+ edema, weight barely down, "
                "NT-proBNP 9800) and there is no hemoconcentration to suggest intravascular "
                "depletion. Intensify decongestion (increase the loop dose or convert to a "
                "continuous infusion; consider adding a thiazide), monitor daily weight, "
                "urine output, and electrolytes, and tolerate a modest creatinine rise. "
                "Holding diuresis and giving fluids would re-congest the patient."),
            "rationale": (
                "In ADHF, worsening renal function during effective decongestion does not "
                "predict worse outcomes when the patient is still volume-overloaded; "
                "persistent congestion does. The volume exam and absent hemoconcentration "
                "are the decisive data, not the creatinine trend."),
            "key_data": [
                "JVP 12 cm + orthopnea + 2+ edema (still congested)",
                "weight down only 1.5 kg from admission",
                "hematocrit/albumin flat (no hemoconcentration)",
                "NT-proBNP 9800",
                "making urine ~1.8 L/day",
            ],
        },
        "hard_hook": (
            "The loud signal (creatinine 1.4 -> 1.9 on furosemide) screams 'stop the "
            "diuretic, give fluids.' The quiet decisive signal (JVP 12, orthopnea, edema, "
            "no hemoconcentration) says the patient is still congested and the rise is "
            "permissive."),
        "reasoning_divergence": (
            "Sound path reads the volume exam and hemoconcentration markers, classifies "
            "the creatinine rise as permissive, and intensifies decongestion. Shortcut "
            "anchors on the creatinine trend, labels it diuretic-induced AKI, holds "
            "diuresis and gives fluids — re-congesting the patient."),
    },
}

# ─── The answer key ──────────────────────────────────────────────────────────
# Four reference findings, each with a deterministic grader over the submitted
# payload. ``planted`` marks the "most physicians miss this" finding, called out
# separately on the reveal. Copy is peer-register: evidence, not judgment.
REFERENCE_FINDINGS: List[Dict[str, Any]] = [
    {
        "id": "sound-answer",
        "label": "Picked the answer that continues decongestion",
        "grader": {"kind": "chosen_id", "expect": "B"},
        "reason_match": "Answer B reads the volume exam instead of anchoring on the creatinine trend.",
        "reason_miss": (
            "The reference panel chose B: with JVP 12, orthopnea, and 2+ edema the patient "
            "is still congested, so stopping diuresis re-congests them."),
        "planted": False,
    },
    {
        "id": "trap-error-tagged",
        "label": "Tagged the rejected answer's decisive error",
        "grader": {"kind": "error_tag",
                   "any_of": ["unsafe_recommendation", "misreads_labs", "wrong_diagnosis"]},
        "reason_match": "You flagged the fluid-bolus-on-a-congested-patient move as the core error.",
        "reason_miss": (
            "The panel tagged the rejected answer 'unsafe recommendation' (or 'misreads "
            "labs'): a fluid bolus in a still-congested patient is the dangerous step, not "
            "a stylistic one."),
        "planted": False,
    },
    {
        "id": "critical-negative",
        "label": "Wrote a critical negative against stopping diuresis / giving fluids",
        "grader": {"kind": "rubric_critical_negative",
                   "any_of": ["diure", "fluid", "bolus", "decongest"]},
        "reason_match": "Your scoring guide hard-fails an answer that halts decongestion or gives fluids.",
        "reason_miss": (
            "The panel's rubric carries one critical negative: any answer that stops "
            "diuresis or gives IV fluids here must hard-fail, whatever else it gets right."),
        "planted": False,
    },
    {
        "id": "congestion-evidence",
        "label": "Cited the persistent-congestion evidence explicitly",
        "grader": {"kind": "keyword",
                   "fields": ["independent_answer.text",
                              "chosen_revision.why_better_notes",
                              "chosen_revision.revised_text",
                              "rejected_critique.why_worse"],
                   "any_of": ["congest", "jvp", "jugular", "orthopnea", "edema",
                              "volume overload", "hemoconcentr", "permissive",
                              "still wet", "decongest"]},
        "reason_match": "You named the congestion evidence (JVP/orthopnea/edema/no hemoconcentration) in your reasoning.",
        "reason_miss": (
            "Most reviewers describe WHAT to do without naming WHY: the JVP of 12, the "
            "orthopnea, and the flat hematocrit are the data that make the creatinine rise "
            "permissive. Buyers pay for reasoning that cites them."),
        "planted": True,
    },
]


# ─── The case registry ───────────────────────────────────────────────────────
# One case today, and a seam for the second. The practice case is now a hard
# gate on all real work, so a physician who fails it retries -- against a case
# whose answer key they have just read. That is acceptable for what this
# actually certifies (the format and the standard, not the diagnosis: the
# clinical instrument downstream is contributor_score and the review pipeline),
# but only while there is one case. When Calibration Case 2 is authored and
# clinically reviewed, appending it here is the whole change: case_for_attempt
# starts rotating and no endpoint moves.
#
# TUTORIAL_CASE_ENTRY and REFERENCE_FINDINGS stay as module-level aliases to
# case 1 rather than being replaced, because they are imported by name in
# tests and reading them as "the case" is still correct while there is one.
TUTORIAL_CASES: List[Dict[str, Any]] = [
    {
        "case_key": "calibration-1",
        # The literal id, not a derived one: it is the localStorage draft key
        # (asclepius_draft_tutorial-calibration-1), it is excluded by name in
        # findResumableDraft, and a test pins it. Renaming it would orphan a
        # draft on every physician's machine to no purpose.
        "task_id": TUTORIAL_TASK_ID,
        "entry": TUTORIAL_CASE_ENTRY,
        "reference_findings": REFERENCE_FINDINGS,
    },
]


def _case(case_key: Optional[str] = None) -> Dict[str, Any]:
    """The registry row for a case key. Unknown keys fall back to case 1 rather
    than raising: a stale key from an older client must not 500 a physician out
    of the one thing standing between them and paid work."""
    if case_key:
        for row in TUTORIAL_CASES:
            if row["case_key"] == case_key:
                return row
    return TUTORIAL_CASES[0]


def case_for_attempt(attempt: int) -> str:
    """Which case a given attempt number draws. 1-indexed.

    A no-op rotation today (one case, so every attempt is case 1). It exists so
    the day a second case lands, the rotation is already wired and the only
    edit is to TUTORIAL_CASES.
    """
    idx = max(0, int(attempt or 1) - 1) % len(TUTORIAL_CASES)
    return TUTORIAL_CASES[idx]["case_key"]


def tutorial_task_id(case_key: Optional[str] = None) -> str:
    return _case(case_key)["task_id"]


def tutorial_raw_task(case_key: Optional[str] = None) -> Dict[str, Any]:
    """The tutorial task in the exact dict shape ``_blind_task`` / ``_task_answers``
    consume for a stored task — assembled in memory, never inserted anywhere."""
    row = _case(case_key)
    entry = _validated_entry(row["case_key"])
    case = entry["case"]
    return {
        "task_id": row["task_id"],
        "prompt": render_case_prompt(case, entry["question"]),
        "specialty": case.get("specialty", "nephrology"),
        "difficulty": "hard",
        "capture_reasoning": True,
        "grounding_mode": "optional",
        "independent_mode": "stance",
        "source": "tutorial",
        "modality": "multimodal",
        "case_source": case.get("case_source", "synthetic"),
        "candidate_answers": [dict(c) for c in entry["candidate_answers"]],
        "case": case,
        "empirical_difficulty": None,
        "difficulty_measured": False,
    }


_validated_cache: Dict[str, Dict[str, Any]] = {}


def _validated_entry(case_key: Optional[str] = None) -> Dict[str, Any]:
    """Fail fast if the tutorial case would not clear the real content gate — a
    broken practice case must never ship silently (same posture as
    ``gold_cases._validated``). Runs per case, so a second case cannot be added
    without clearing the same bar case 1 does."""
    row = _case(case_key)
    key = row["case_key"]
    if key in _validated_cache:
        return _validated_cache[key]
    entry = row["entry"]
    findings = row["reference_findings"]
    assert_multimodal_content(entry["case"])
    cands = entry.get("candidate_answers") or []
    if len(cands) != 2 or entry.get("intended_flawed_id") not in ("A", "B"):
        raise ValueError("tutorial case missing a valid A/B pair")
    known = {"chosen_id", "error_tag", "rubric_critical_negative", "keyword"}
    planted = [f for f in findings if f.get("planted")]
    if len(planted) != 1:
        raise ValueError("tutorial answer key needs exactly one planted finding")
    ids = {f.get("id") for f in findings}
    if PASS_REQUIRED_FINDING not in ids:
        raise ValueError(
            f"tutorial answer key must carry the required finding {PASS_REQUIRED_FINDING!r}")
    for f in findings:
        if (f.get("grader") or {}).get("kind") not in known:
            raise ValueError(f"tutorial finding {f.get('id')} has an unknown grader kind")
    _validated_cache[key] = entry
    return entry


# ─── Grading ─────────────────────────────────────────────────────────────────
def _dig(payload: Dict[str, Any], dotted: str) -> str:
    cur: Any = payload
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return ""
        cur = cur.get(part)
    return cur if isinstance(cur, str) else ""


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower())


def _grade_finding(finding: Dict[str, Any], payload: Dict[str, Any]) -> bool:
    g = finding["grader"]
    kind = g["kind"]
    if kind == "chosen_id":
        return (payload.get("chosen_id") or "") == g["expect"]
    if kind == "error_tag":
        tags = ((payload.get("rejected_critique") or {}).get("error_tags")) or []
        return any(t in g["any_of"] for t in tags)
    if kind == "rubric_critical_negative":
        needles = [n.lower() for n in g["any_of"]]
        for c in (payload.get("rubric") or []):
            points = c.get("points") or 0
            if points > _CRITICAL_NEGATIVE_FLOOR:
                continue
            text = _norm(str(c.get("text") or ""))
            if any(n in text for n in needles):
                return True
        return False
    if kind == "keyword":
        blob = " ".join(_norm(_dig(payload, f)) for f in g["fields"])
        return any(n.lower() in blob for n in g["any_of"])
    return False


def _your_answer(finding: Dict[str, Any], payload: Dict[str, Any]) -> str:
    """What THIS physician put where the key looked, in their own words.

    The single highest-value thing on the reveal. "You missed the congestion
    evidence" teaches nothing next to "you wrote: the creatinine is rising so
    I would hold the loop", which is the sentence they can actually learn from.
    One branch per grader kind, so the quoted field is always the field that
    was graded rather than a plausible-looking neighbour.
    """
    g = finding.get("grader") or {}
    kind = g.get("kind")
    if kind == "chosen_id":
        chosen = (payload.get("chosen_id") or "").strip()
        if chosen in ("A", "B"):
            return f"You chose {chosen}."
        return "You did not pick either answer."
    if kind == "error_tag":
        tags = ((payload.get("rejected_critique") or {}).get("error_tags")) or []
        if not tags:
            return "You tagged no error on the rejected answer."
        return "You tagged: " + ", ".join(str(t).replace("_", " ") for t in tags) + "."
    if kind == "rubric_critical_negative":
        worst = None
        for c in (payload.get("rubric") or []):
            pts = c.get("points") or 0
            if worst is None or pts < (worst.get("points") or 0):
                worst = c
        if not worst or (worst.get("points") or 0) > _CRITICAL_NEGATIVE_FLOOR:
            return "Your scoring guide had no critical negative."
        return f"Your hardest criterion was: \"{_clip(str(worst.get('text') or ''))}\""
    if kind == "keyword":
        for field in g.get("fields") or []:
            text = _dig(payload, field).strip()
            if text:
                return f"You wrote: \"{_clip(text)}\""
        return "You left the reasoning fields empty."
    return ""


def _clip(text: str, limit: int = 220) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _headline(*, passed: bool, matched: int, sound: bool, planted_hit: bool,
              assisted: bool) -> str:
    """A verdict, in words, with no digits in it.

    Deliberately carries no score. A physician never sees their contributor
    score anywhere in this product, and "2 of 4" on the practice case is the
    same number wearing a different hat: it invites them to read a grade off a
    four-item keyword rubric that was built to teach, not to rank.
    """
    if assisted:
        return ("You used Skip this step on the parts we score. That is fine for learning "
                "the interface, and it is not a pass. The case is still here.")
    if not sound:
        return ("You went with the answer the reference panel rejected. Here is what it "
                "read that you did not.")
    if passed and planted_hit and matched == 4:
        return "You read this case the way the reference panel did."
    if passed and not planted_hit:
        return ("You got the call right. You did not say why, and the why is what a buyer "
                "is paying for.")
    if passed:
        return "You got the call right, and enough of it written down to be worth shipping."
    return ("Right call. Not enough of it written down yet to be worth anything to a "
            "buyer. Here is what the panel put in writing.")


def teaching_block(case_key: Optional[str] = None) -> Dict[str, Any]:
    """Why the trap works, released only AFTER a submission.

    Every field here is stripped from the task by ``_blind_task`` and has never
    been shown to anybody. That blinding is not relaxed to expose it: parity
    with a real task is the whole isolation story for the practice case, and a
    test asserts the split stays deliberate. This is a separate door that opens
    once the physician has committed an answer and can no longer be anchored.
    """
    entry = _validated_entry(case_key)
    case = entry.get("case") or {}
    gt = case.get("ground_truth") or {}
    return {
        "ai_failure_mode": entry.get("ai_failure_mode"),
        "hard_hook": case.get("hard_hook"),
        "reasoning_divergence": case.get("reasoning_divergence"),
        "key_data": list(gt.get("key_data") or []),
        "reference_answer": gt.get("answer"),
        "reference_rationale": gt.get("rationale"),
    }


def grade_tutorial_submission(payload: Dict[str, Any],
                              case_key: Optional[str] = None) -> Dict[str, Any]:
    """Deterministically score a tutorial submission against the answer key.

    Returns a PASS DECISION now, not just feedback. The practice case gates all
    real work, so "you completed it" had to stop meaning "you clicked through
    it": before this, a 0-of-4 stamped ``completed`` and that was the only
    thing standing between a new physician and a paid case.

    ``matched``/``total`` are still returned for the admin and for events. They
    are not for the physician; ``headline`` is what they read.
    """
    payload = payload or {}
    row = _case(case_key)
    _validated_entry(row["case_key"])
    reference = row["reference_findings"]

    # Which graded steps the client says it filled in on the physician's behalf.
    assisted_steps = sorted(
        {str(s) for s in (payload.get("assisted") or []) if str(s) in GRADED_STEP_IDS})

    findings = []
    matched = 0
    planted_out = None
    sound = False
    for f in reference:
        hit = _grade_finding(f, payload)
        if hit:
            matched += 1
            if f["id"] == PASS_REQUIRED_FINDING:
                sound = True
        row_out = {
            "id": f["id"],
            "label": f["label"],
            "matched": hit,
            "reason": f["reason_match"] if hit else f["reason_miss"],
            "your_answer": _your_answer(f, payload),
            "planted": bool(f.get("planted")),
        }
        findings.append(row_out)
        if f.get("planted"):
            planted_out = row_out

    total = len(reference)
    passed = bool(sound and matched >= PASS_MIN_MATCHED and not assisted_steps)
    planted_hit = bool(planted_out and planted_out["matched"])

    return {
        "passed": passed,
        "matched": matched,
        "total": total,
        "findings": findings,
        "planted_finding": planted_out,
        "assisted_steps": assisted_steps,
        # A pass that missed the planted finding still has something the
        # physician has to read before they leave. The client keeps "Start real
        # cases" disabled until they open it: one click, not a quiz.
        "must_acknowledge": [f["id"] for f in findings if not f["matched"]],
        "teaching": teaching_block(row["case_key"]),
        "headline": _headline(passed=passed, matched=matched, sound=sound,
                              planted_hit=planted_hit, assisted=bool(assisted_steps)),
    }
