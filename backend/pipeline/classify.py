"""
Routing + Classification Layer
Purpose: Determine whether the patient belongs in the Pre-Op or Post-Op pipeline
         by scoring clinical signals extracted from their EHR.

Decision rule:
  score > 0  →  post_op   (evidence of completed surgery or discharge)
  score ≤ 0  →  pre_op    (evidence of upcoming procedure, no discharge)
"""

from typing import Any, Dict, Literal


class ClassificationLayer:

    # Keywords that push toward Post-Op
    _POST_OP_KW = [
        "discharge", "post-op", "post op", "after surgery", "after procedure",
        "recovery instructions", "wound care", "follow up after", "operative note",
        "discharge order", "discharge summary", "post operative",
    ]

    # Keywords that push toward Pre-Op
    _PRE_OP_KW = [
        "pre-op", "pre op", "before surgery", "before procedure",
        "preparation", "scheduled for", "upcoming procedure",
        "fasting", "bowel prep", "pre operative", "pre-operative",
    ]

    def classify(self, data: Dict[str, Any]) -> Literal["pre_op", "post_op"]:
        """
        Returns 'pre_op' or 'post_op' based on weighted signal scoring.
        """
        score = self._score(data)
        return "post_op" if score > 0 else "pre_op"

    # ── Private ──────────────────────────────────────────────

    def _score(self, d: Dict[str, Any]) -> int:
        score = 0

        # ① Procedure status (strongest signal)
        status = (d.get("procedure_status") or "").lower()
        if status == "completed":
            score += 4
        elif status == "scheduled":
            score -= 4

        # ② Note type
        note_type = (d.get("note_type") or "").lower()
        if note_type in ("discharge_note",):
            score += 3
        elif note_type in ("post_op_visit",):
            score += 2
        elif note_type in ("pre_op_note",):
            score -= 3

        # ③ Instruction presence
        post_ins = (d.get("post_op_instructions") or "")
        pre_ins  = (d.get("pre_op_instructions") or "")
        if len(post_ins) > 40:
            score += 2
        if len(pre_ins) > 40:
            score -= 2

        # ④ New/changed medications (usually post-op)
        meds = d.get("medications") or []
        new_meds = [m for m in meds if (m.get("status") or "") in ("new", "changed")]
        if new_meds:
            score += 1

        # ⑤ Free-text keyword scan of all raw clinical sections
        raw = " ".join(str(v) for v in (d.get("_raw_clinical") or {}).values()).lower()
        for kw in self._POST_OP_KW:
            if kw in raw:
                score += 1
        for kw in self._PRE_OP_KW:
            if kw in raw:
                score -= 1

        return score
