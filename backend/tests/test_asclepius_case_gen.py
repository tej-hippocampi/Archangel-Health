"""Multimodal case generation + case judge + Stage 3c gate (PR-B).

The LLM is stubbed (no key needed). Verifies: generate_case / run_case_judge
parse + degrade; the multimodal generation branch produces multimodal tasks with
a structured case + provenance; each Stage 3c gate drops as specified; and a
skipped case judge never drops (same contract as the hardness judge).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests._asclepius as A  # noqa: E402

from asclepius import generation as gen  # noqa: E402


def _store():
    from asclepius.store import get_store
    return get_store()


def test_case_gen_prompt_ships_a_valid_reference_example():
    """The case-gen system prompt embeds a worked REFERENCE EXAMPLE so Opus learns
    the exact case shape + difficulty pattern (fewer schema-drift / low-necessity
    drops). Lock it: the example must be valid JSON AND survive the same
    ``assert_multimodal_content`` gate a generated case must pass — otherwise we'd be
    teaching the model a shape our own pipeline rejects."""
    import json
    from asclepius.prompts import ASCLEPIUS_CASE_GEN_SYSTEM
    from asclepius.cases import assert_multimodal_content

    marker = "REFERENCE EXAMPLE"
    assert marker in ASCLEPIUS_CASE_GEN_SYSTEM, "case-gen prompt lost its worked example"
    start = ASCLEPIUS_CASE_GEN_SYSTEM.index('{"question', ASCLEPIUS_CASE_GEN_SYSTEM.index(marker))
    obj = json.loads(ASCLEPIUS_CASE_GEN_SYSTEM[start:])  # raises if the example rots into invalid JSON
    case = obj["case"]
    assert_multimodal_content(case)  # the example passes the real content gate
    # And it demonstrates the difficulty pattern the gates reward: a trend across ≥2
    # panels at different offsets, plus a decisive flag and a red herring.
    offsets = {p["collected_offset_days"] for p in case["lab_panels"]}
    assert len(case["lab_panels"]) >= 2 and len(offsets) >= 2
    flags = {r.get("flag") for p in case["lab_panels"] for r in p["results"]}
    assert flags & {"L", "LL", "H", "HH"}  # at least one abnormal flag to interpret


# A content-complete note ≥200 chars (BUG-1 content assertion floor).
_LONG_NOTE = (
    "Nephrology consult. Patient euvolemic on exam with no edema or orthostasis, on a chronic "
    "thiazide for hypertension and reporting poor oral intake over the weekend. Urine studies were "
    "sent to distinguish a hypovolemic from an SIADH picture; the thiazide is the likely driver and "
    "must be held before any correction is attempted to avoid overcorrection."
)


def _content_complete_labs(na=112):
    """Two panels at DIFFERENT offsets, each ≥2 well-formed results (BUG-1 §2)."""
    return [
        {"panel": "BMP", "collected_offset_days": -2, "results": [
            {"analyte": "Sodium", "value": na + 4, "unit": "mmol/L", "ref_low": 135, "ref_high": 145, "flag": "L"},
            {"analyte": "Potassium", "value": 3.9, "unit": "mmol/L", "ref_low": 3.5, "ref_high": 5.0, "flag": ""}]},
        {"panel": "BMP", "collected_offset_days": 0, "results": [
            {"analyte": "Sodium", "value": na, "unit": "mmol/L", "ref_low": 135, "ref_high": 145, "flag": "LL"},
            {"analyte": "Urine osmolality", "value": 620, "unit": "mOsm/kg", "ref_low": 300, "ref_high": 900, "flag": "H"}]},
    ]


def _case(**over):
    base = dict(
        case_source="synthetic", specialty="nephrology",
        demographics={"age_band": "70-79", "sex": "M"},
        problem_list=[{"condition": "Hypertension", "since": "chronic"}],
        medications=[{"drug": "Hydrochlorothiazide", "dose": "25 mg", "route": "PO", "freq": "daily"}],
        lab_panels=_content_complete_labs(),
        notes=[{"note_type": "Consult", "author_role": "nephrology", "text": _LONG_NOTE}],
        ground_truth={"answer": "Thiazide-associated hyponatremia", "key_data": ["urine osm"]},
        hard_hook="urine studies decide", reasoning_divergence="SIADH shortcut ignores thiazide",
    )
    base.update(over)
    return base


# ─── Unit: generate_case + run_case_judge (mocked LLM) ────────────────────────
def test_generate_case_parses_and_stamps_synthetic(monkeypatch):
    import ai.llm_client as llm
    import json
    from asclepius import critic

    async def fake_call(**kw):
        return ({}, {"model": "case-model"})

    monkeypatch.setattr(llm, "call_llm", fake_call)
    monkeypatch.setattr(llm, "first_text", lambda resp: json.dumps({
        "question": "Classify the hyponatremia and set a safe correction rate.",
        "case": _case(),
    }))
    res = asyncio.run(critic.generate_case({"topic": "hyponatremia", "multimodal": {"panels": ["BMP"]}}, specialty="nephrology"))
    assert res["skipped"] is False
    assert res["question"].startswith("Classify")
    assert res["case"]["case_source"] == "synthetic"
    assert res["case"]["lab_panels"][0]["results"][0]["analyte"] == "Sodium"


def test_generate_case_degrades_offline():
    from asclepius import critic
    res = asyncio.run(critic.generate_case({"topic": "x", "multimodal": {}}, specialty="nephrology"))
    assert res["skipped"] is True


def test_run_case_judge_parses_scores(monkeypatch):
    import ai.llm_client as llm
    import json
    from asclepius import critic

    async def fake_call(**kw):
        return ({}, {"model": "judge"})

    monkeypatch.setattr(llm, "call_llm", fake_call)
    monkeypatch.setattr(llm, "first_text", lambda resp: json.dumps({
        "coherence": 0.9, "ground_truth_determinable": 0.85,
        "multimodal_necessity": 0.8, "reasoning_divergence_potential": 0.7, "explanation": "ok"}))
    res = asyncio.run(critic.run_case_judge(_case()))
    assert res["skipped"] is False
    assert res["coherence"] == 0.9 and res["multimodal_necessity"] == 0.8


# ─── Integration: the multimodal generation branch ────────────────────────────
def _install(monkeypatch, *, case_scores=None, hardness=0.85):
    """Stub the whole multimodal chain: one archetype, one case, passing judges."""
    counter = {"i": 0}

    # Distinct note narratives (each ≥200 chars, BUG-1 content floor) so cases
    # aren't collapsed by the exact/near-dup gates (real generation varies).
    _narratives = [
        "Euvolemic on chronic thiazide with no edema; poor oral intake reported over the weekend. Urine studies pending to separate hypovolemia from SIADH; the thiazide is the likely driver and should be held before any correction.",
        "Hypervolemic with 3+ peripheral edema; a heart failure exacerbation with diuretics recently held. Free water retention is worsening the hyponatremia and gentle decongestion with careful sodium tracking is the priority here.",
        "Postoperative day two with hypotonic maintenance fluids still running; nausea and a dull headache described. The iatrogenic free water load is the reversible contributor and the fluids should be changed before anything else.",
        "Marathon runner brought in after collapse at the finish; excessive free water intake during the race noted by companions. Exercise-associated hyponatremia is acute and hypertonic saline is warranted for the neurologic symptoms.",
        "Cirrhosis with tense ascites; a recent large-volume paracentesis with albumin given. The hypervolemic hyponatremia reflects impaired free water excretion and free water restriction rather than aggressive correction is indicated.",
    ]

    async def fake_generate_case(archetype, *, specialty="general"):
        counter["i"] += 1
        na = 106 + counter["i"]
        note = _narratives[counter["i"] % len(_narratives)]
        case = _case(
            lab_panels=_content_complete_labs(na),
            notes=[{"note_type": "Consult", "author_role": "nephrology", "text": note}])
        return {"case": case, "question": f"Classify this hyponatremia presentation ({counter['i']}) and set a safe correction rate.",
                "model": "cg", "skipped": False}

    async def fake_candidates(prompt, **k):
        return {"candidates": [{"id": "A", "text": "s"}, {"id": "B", "text": "f"}], "model": "cand", "intended_flawed_id": "B"}

    async def fake_prompt_judge(prompt, candidates):
        return {"skipped": False, "error_likelihood": 0.9, "revision_value": 0.9, "on_specialty": True, "safety_ok": True}

    async def fake_hardness(prompt, candidates=None, **k):
        return {"skipped": False, "hardness_score": hardness, "hardness_axes": ["multi_step"], "explanation": "h"}

    scores = case_scores if case_scores is not None else {
        "coherence": 0.9, "ground_truth_determinable": 0.85,
        "multimodal_necessity": 0.8, "reasoning_divergence_potential": 0.7}

    async def fake_case_judge(case):
        return {"skipped": False, **scores, "explanation": "", "model": "cj"}

    monkeypatch.setattr(gen, "_multimodal_archetypes", lambda specialty: [
        {"topic": "hyponatremia_beer_potomania_vs_siadh", "multimodal": {"panels": ["BMP"], "hard_hook": "urine studies"}}])
    monkeypatch.setattr(gen, "generate_case", fake_generate_case)
    monkeypatch.setattr(gen, "generate_candidates_ex", fake_candidates)
    monkeypatch.setattr(gen, "run_prompt_judge", fake_prompt_judge)
    monkeypatch.setattr(gen, "run_hardness_judge", fake_hardness)
    monkeypatch.setattr(gen, "run_case_judge", fake_case_judge)


def test_multimodal_generation_produces_case_tasks(monkeypatch):
    A.fresh_store()
    _install(monkeypatch)
    res = asyncio.run(gen.generate_tasks(_store(), specialty="nephrology", n=2, multimodal=True))
    assert res["accepted"] == 2, res["dropped"]
    tasks = _store().list_tasks(specialty="nephrology", limit=10)
    mm = [t for t in tasks if t["modality"] == "multimodal"]
    assert len(mm) == 2
    t = mm[0]
    assert t["difficulty"] == "hard"                     # multimodal is always hard
    assert t["capture_reasoning"] is True                # the value is the reasoning trace
    assert t["case"]["lab_panels"]                       # structured case stored
    assert "Sodium" in t["prompt"]                       # rendered case in the prompt
    g = t["generation"]
    assert g["modality"] == "multimodal" and g["case_source"] == "synthetic"
    assert g["case_judge"]["multimodal_necessity"] == 0.8
    assert g["seed_archetype_id"] == "hyponatremia_beer_potomania_vs_siadh"
    # server-side answer key present on the stored task, not leaked to prompt
    assert t["case"]["ground_truth"]["answer"] not in t["prompt"]


def test_case_judge_gates_drop_as_specified(monkeypatch):
    for field, reason in [
        ("coherence", "case_incoherent"),
        ("ground_truth_determinable", "ground_truth_indeterminate"),
        ("multimodal_necessity", "multimodal_not_necessary"),
        ("reasoning_divergence_potential", "low_reasoning_divergence"),
    ]:
        A.fresh_store()
        bad = {"coherence": 0.9, "ground_truth_determinable": 0.85,
               "multimodal_necessity": 0.8, "reasoning_divergence_potential": 0.7}
        bad[field] = 0.1  # below every floor
        _install(monkeypatch, case_scores=bad)
        res = asyncio.run(gen.generate_tasks(_store(), specialty="nephrology", n=1, multimodal=True))
        assert res["accepted"] == 0, (field, res)
        assert res["dropped"].get(reason, 0) >= 1, (field, reason, res["dropped"])


def test_multimodal_drops_when_case_judge_skipped(monkeypatch):
    """BUG-1 §4: the multimodal gates are NON-SKIPPABLE. A skipped case judge
    means the case is UNGATED, so it must be DROPPED (case_judge_unavailable),
    never passed — an ungated synthetic case must never enter the queue."""
    A.fresh_store()
    _install(monkeypatch)

    async def skipped_case_judge(case):
        return {"skipped": True, "error": "no key"}

    monkeypatch.setattr(gen, "run_case_judge", skipped_case_judge)
    res = asyncio.run(gen.generate_tasks(_store(), specialty="nephrology", n=1, multimodal=True))
    assert res["accepted"] == 0
    assert res["dropped"].get("case_judge_unavailable", 0) >= 1, res["dropped"]


def test_multimodal_drops_when_hardness_judge_skipped(monkeypatch):
    """BUG-1 §4: a skipped HARDNESS judge on a multimodal item is also a
    non-skippable gate — drop as hardness_unavailable, never pass ungated."""
    A.fresh_store()
    _install(monkeypatch)

    async def skipped_hardness(prompt, candidates=None, **k):
        return {"skipped": True, "error": "no key"}

    monkeypatch.setattr(gen, "run_hardness_judge", skipped_hardness)
    res = asyncio.run(gen.generate_tasks(_store(), specialty="nephrology", n=1, multimodal=True))
    assert res["accepted"] == 0
    assert res["dropped"].get("hardness_unavailable", 0) >= 1, res["dropped"]


def test_multimodal_survives_low_prompt_judge_score(monkeypatch):
    """The text prompt-judge (error_likelihood / revision_value, floors 0.5) is the
    WRONG gate for a structured case: a clean case with a decisive answer legitimately
    scores LOW, so this gate was silently dropping nearly every multimodal case as
    low_error_likelihood. A multimodal item must survive a low text-judge score (the
    case-judge is its quality gate); TEXT prompts still drop below the floor."""
    A.fresh_store()
    _install(monkeypatch)

    async def low_judge(prompt, candidates):
        return {"skipped": False, "error_likelihood": 0.05, "revision_value": 0.05,
                "on_specialty": True, "safety_ok": True}

    monkeypatch.setattr(gen, "run_prompt_judge", low_judge)
    res = asyncio.run(gen.generate_tasks(_store(), specialty="nephrology", n=1, multimodal=True))
    assert res["accepted"] == 1, res["dropped"]
    assert res["dropped"].get("low_error_likelihood", 0) == 0


def test_multimodal_still_dropped_when_unsafe(monkeypatch):
    """The one prompt-judge gate that STAYS active for multimodal is safety — an
    unsafe case is dropped even under the relaxation."""
    A.fresh_store()
    _install(monkeypatch)

    async def unsafe_judge(prompt, candidates):
        return {"skipped": False, "error_likelihood": 0.9, "revision_value": 0.9,
                "on_specialty": True, "safety_ok": False}

    monkeypatch.setattr(gen, "run_prompt_judge", unsafe_judge)
    res = asyncio.run(gen.generate_tasks(_store(), specialty="nephrology", n=1, multimodal=True))
    assert res["accepted"] == 0
    assert res["dropped"].get("unsafe", 0) >= 1, res["dropped"]


def test_multimodal_same_archetype_not_collapsed_by_near_dup(monkeypatch):
    """Cases from the same archetype share the note/panel scaffolding (very high token
    overlap) but carry different synthetic values — each is a distinct evaluation. The
    Jaccard near-dup gate (calibrated for text prompts) must NOT collapse them for
    multimodal, or V3 runs dry after a couple cases. Here: same note + question, only
    the sodium differs → distinct hash but Jaccard well above the near-dup threshold."""
    A.fresh_store()
    _install(monkeypatch)
    counter = {"i": 0}

    async def near_dup_case(archetype, *, specialty="general"):
        counter["i"] += 1
        case = _case(lab_panels=_content_complete_labs(100 + counter["i"]),  # only Na changes
                     notes=[{"note_type": "Consult", "author_role": "nephrology", "text": _LONG_NOTE}])
        return {"case": case, "question": "Classify this hyponatremia presentation.",
                "model": "cg", "skipped": False}

    monkeypatch.setattr(gen, "generate_case", near_dup_case)
    res = asyncio.run(gen.generate_tasks(_store(), specialty="nephrology", n=3, multimodal=True))
    assert res["accepted"] >= 2, res["dropped"]           # not collapsed to a single case
    assert res["dropped"].get("near_duplicate", 0) == 0


def test_multimodal_with_no_archetypes_creates_nothing(monkeypatch):
    A.fresh_store()
    _install(monkeypatch)
    monkeypatch.setattr(gen, "_multimodal_archetypes", lambda specialty: [])
    res = asyncio.run(gen.generate_tasks(_store(), specialty="nephrology", n=2, multimodal=True))
    assert res["accepted"] == 0


# ─── V3 bring-up relaxation (ASCLEPIUS_V3_RELAX_MM_GATES) ──────────────────────
def test_relaxed_gates_accept_low_score_multimodal_case(monkeypatch):
    """Bring-up: with the relaxation ON, a structurally-complete multimodal case is
    ACCEPTED even when every case-judge quality score is below floor — so V3 can
    show a case now. The scores are still RECORDED (for later re-tightening) and the
    task is flagged gates_relaxed."""
    monkeypatch.setenv("ASCLEPIUS_V3_RELAX_MM_GATES", "1")
    A.fresh_store()
    _install(monkeypatch, case_scores={
        "coherence": 0.1, "ground_truth_determinable": 0.1,
        "multimodal_necessity": 0.1, "reasoning_divergence_potential": 0.1},
        hardness=0.2)  # everything below floor
    res = asyncio.run(gen.generate_tasks(_store(), specialty="nephrology", n=1, multimodal=True))
    assert res["accepted"] == 1, res["dropped"]
    t = [x for x in _store().list_tasks(specialty="nephrology", limit=10)
         if x["modality"] == "multimodal"][0]
    assert t["difficulty"] == "hard"                       # multimodal is always hard
    assert t["case"]["lab_panels"] and t["case"]["notes"]  # the structured case is there
    g = t["generation"]
    assert g["gates_relaxed"] is True                      # audit trail
    assert g["case_judge"]["multimodal_necessity"] == 0.1  # score still recorded


def test_relaxed_gates_accept_when_judges_unavailable(monkeypatch):
    """Bring-up: with the relaxation ON, a skipped case/hardness judge no longer
    drops the case (it ships without those scores) — so a transient judge outage
    doesn't empty V3."""
    monkeypatch.setenv("ASCLEPIUS_V3_RELAX_MM_GATES", "1")
    A.fresh_store()
    _install(monkeypatch)

    async def skipped(*a, **k):
        return {"skipped": True, "error": "no key"}

    monkeypatch.setattr(gen, "run_case_judge", skipped)
    monkeypatch.setattr(gen, "run_hardness_judge", skipped)
    res = asyncio.run(gen.generate_tasks(_store(), specialty="nephrology", n=1, multimodal=True))
    assert res["accepted"] == 1, res["dropped"]


def test_relaxed_gates_still_enforce_content_assertion(monkeypatch):
    """Bring-up relaxation loosens the QUALITY gates only — the STRUCTURAL content
    floor is NOT relaxed: a case that carries no labs AND no notes is still dropped
    (insufficient_case_content), never served as an empty 'multimodal' case."""
    monkeypatch.setenv("ASCLEPIUS_V3_RELAX_MM_GATES", "1")
    A.fresh_store()
    _install(monkeypatch)

    async def empty_case(archetype, *, specialty="general"):
        # A case with no labs and no notes — the exact empty-case failure BUG-1 fixed.
        return {"case": {"case_source": "synthetic", "specialty": specialty,
                         "problem_list": [{"condition": "CKD"}], "medications": [{"drug": "x"}]},
                "question": "q", "model": "cg", "skipped": False}

    monkeypatch.setattr(gen, "generate_case", empty_case)
    res = asyncio.run(gen.generate_tasks(_store(), specialty="nephrology", n=1, multimodal=True))
    assert res["accepted"] == 0
    assert res["dropped"].get("insufficient_case_content", 0) >= 1, res["dropped"]


# ─── Regression: a bad case must not be misreported as "no LLM" (review Finding 1)
def test_bad_case_counts_case_gen_failed_not_disabled(monkeypatch):
    """A returned-but-unparseable case (LLM working, this case bad) must count as
    case_gen_failed and let generation continue — NOT abort the whole batch as
    'no LLM configured'. Regression for the skipped=True conflation bug."""
    A.fresh_store()
    _install(monkeypatch)

    async def bad_first_then_good(archetype, *, specialty="general"):
        # First call: LLM answered but the case failed to parse (skipped=False,
        # case=None). Later calls succeed.
        bad_first_then_good.n += 1
        if bad_first_then_good.n == 1:
            return {"case": None, "question": None, "model": "cg", "skipped": False}
        na = 108 + bad_first_then_good.n
        note = (f"Case {na}: euvolemic patient on a chronic thiazide with poor oral intake; urine "
                "studies pending to separate hypovolemia from SIADH, and the thiazide must be held "
                "before any correction to avoid overcorrection of the sodium in this presentation.")
        case = _case(lab_panels=_content_complete_labs(na),
                     notes=[{"note_type": "Consult", "author_role": "nephrology", "text": note}])
        return {"case": case, "question": f"Classify hyponatremia {na}.", "model": "cg", "skipped": False}
    bad_first_then_good.n = 0

    monkeypatch.setattr(gen, "generate_case", bad_first_then_good)
    res = asyncio.run(gen.generate_tasks(_store(), specialty="nephrology", n=2, multimodal=True))
    # Did NOT raise GenerationDisabled; the bad first case is counted, later ones accepted.
    assert res["accepted"] >= 1, res
    assert res["dropped"].get("case_gen_failed", 0) >= 1, res["dropped"]


def test_no_llm_still_disables_generation(monkeypatch):
    """The genuine 'no LLM' path (skipped=True) must still disable generation."""
    A.fresh_store()
    _install(monkeypatch)

    async def no_llm(archetype, *, specialty="general"):
        return {"case": None, "question": None, "model": None, "skipped": True}

    monkeypatch.setattr(gen, "generate_case", no_llm)
    try:
        asyncio.run(gen.generate_tasks(_store(), specialty="nephrology", n=2, multimodal=True))
        assert False, "expected GenerationDisabled"
    except gen.GenerationDisabled:
        pass


# ─── Regression: multimodal ignores a non-hard difficulty_mix (review Finding 2)
def test_multimodal_ignores_difficulty_mix(monkeypatch):
    """Multimodal cases are definitionally hard; a difficulty_mix without a 'hard'
    bucket must NOT drop every case as difficulty_mix_skew."""
    A.fresh_store()
    _install(monkeypatch)
    res = asyncio.run(gen.generate_tasks(
        _store(), specialty="nephrology", n=2, multimodal=True,
        difficulty_mix={"easy": 1.0}))
    assert res["accepted"] == 2, res["dropped"]
    assert res["dropped"].get("difficulty_mix_skew", 0) == 0


# ─── Generation robustness: alias drift is RECOVERED; empty content still drops ─
def test_schema_drift_labs_key_is_recovered(monkeypatch):
    """A generated response using ``labs`` instead of ``lab_panels`` must NOT be
    thrown away (that dropped real cases and left V3 on text). The generation-path
    sanitizer maps the alias back to ``lab_panels`` so the case is recovered and
    served — while REAL-EHR ingestion stays strict (extra='forbid') elsewhere."""
    import ai.llm_client as llm
    import json as _json
    from asclepius import critic

    async def fake_call(**kw):
        return ({}, {"model": "cg"})

    drifted = _case()
    drifted["labs"] = drifted.pop("lab_panels")  # wrong key name from the LLM
    drifted["teaching_point"] = "a benign extra key the LLM added"  # must not nuke the case
    monkeypatch.setattr(llm, "call_llm", fake_call)
    monkeypatch.setattr(llm, "first_text", lambda resp: _json.dumps(
        {"question": "Classify.", "case": drifted}))
    res = asyncio.run(critic.generate_case({"topic": "x", "multimodal": {}}, specialty="nephrology"))
    assert res["skipped"] is False
    assert res["case"] is not None                       # recovered, not dropped
    assert len(res["case"]["lab_panels"]) == 2           # labs alias mapped back
    assert "teaching_point" not in res["case"]           # benign extra stripped


def test_empty_case_still_dropped_after_sanitize(monkeypatch):
    """The sanitizer must not become a hole in the empty-case guard: a case whose
    labs really are absent (here an unmappable key) still fails the content
    assertion and is dropped — never stored as an empty 'multimodal' case."""
    import ai.llm_client as llm
    import json as _json
    from asclepius import critic

    async def fake_call(**kw):
        return ({}, {"model": "cg"})

    empty = _case()
    empty["lab_panels"] = []  # genuinely no labs
    monkeypatch.setattr(llm, "call_llm", fake_call)
    monkeypatch.setattr(llm, "first_text", lambda resp: _json.dumps(
        {"question": "Classify.", "case": empty}))
    res = asyncio.run(critic.generate_case({"topic": "x", "multimodal": {}}, specialty="nephrology"))
    assert res["case"] is None
    assert res["error"].startswith("insufficient_content")


def test_empty_labs_case_fails_content_assertion(monkeypatch):
    """A structurally-valid case with NO labs is rejected by the content
    assertion (a multimodal case with no labs is not a multimodal case)."""
    import ai.llm_client as llm
    import json as _json
    from asclepius import critic

    async def fake_call(**kw):
        return ({}, {"model": "cg"})

    empty_labs = _case(lab_panels=[])
    monkeypatch.setattr(llm, "call_llm", fake_call)
    monkeypatch.setattr(llm, "first_text", lambda resp: _json.dumps(
        {"question": "Classify.", "case": empty_labs}))
    res = asyncio.run(critic.generate_case({"topic": "x", "multimodal": {}}, specialty="nephrology"))
    assert res["case"] is None
    assert res["error"].startswith("insufficient_content")


def test_generate_case_retries_once_then_succeeds(monkeypatch):
    """BUG-1 §5: a first response missing content triggers ONE corrective retry;
    if the retry is content-complete, the case is returned."""
    import ai.llm_client as llm
    import json as _json
    from asclepius import critic

    calls = {"n": 0}

    async def fake_call(**kw):
        calls["n"] += 1
        return ({}, {"model": "cg"})

    def fake_first_text(resp):
        # First attempt: no meds/problems (fails content). Retry: complete.
        if calls["n"] <= 1:
            bad = _case(problem_list=[], medications=[])
            return _json.dumps({"question": "Classify.", "case": bad})
        return _json.dumps({"question": "Classify.", "case": _case()})

    monkeypatch.setattr(llm, "call_llm", fake_call)
    monkeypatch.setattr(llm, "first_text", fake_first_text)
    res = asyncio.run(critic.generate_case({"topic": "x", "multimodal": {}}, specialty="nephrology"))
    assert calls["n"] == 2               # retried exactly once
    assert res["case"] is not None and res["skipped"] is False
