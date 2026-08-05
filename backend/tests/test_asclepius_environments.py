"""V5 Clinical RL Environments — correctness suite (Clinical RL Environments PRD).

This module's entire value is correctness, so these tests target the three things
that make the data sellable and the three that make it dangerous:

  * NO ANSWER LEAKAGE — nothing recorded after the decision point (lab panel, note,
    study, problem, or medication) is reachable through any read tool, and an item
    whose timing cannot be verified is withheld fail-closed on a real case.
  * NO PHI LEAKAGE — a residual identifier in a note never reaches the agent, the
    trajectory, or the export.
  * A REWARD THAT CANNOT BE GAMED — a padded-but-hollow trajectory must score below
    a terse-correct one; a negated or wrong diagnosis must not pass; an unsafe
    action hard-fails to 0 while correctly REFUSING it does not.
  * Interface contract — dense rewards keyed to real trajectory steps, ``max_steps``
    counted in agent actions, ``reset()`` fully clearing state.
  * Honest compilation — an ambiguous real case is refused, not forced.
  * Export tiers — ``raw`` carries no verification/provenance; ``expert`` carries
    the physician annotation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests._asclepius as A  # noqa: E402,F401  (env before imports)

from asclepius.environments import CompileError, build_environment  # noqa: E402
from asclepius.environments import verify as V  # noqa: E402
from asclepius.environments.env import ClinicalEnv  # noqa: E402
from asclepius.environments.export_env import record_for_mode  # noqa: E402
from asclepius.environments.state import EHRState  # noqa: E402
from asclepius.gold_cases import GOLD_CASE_SETS  # noqa: E402


# ─── fixtures ─────────────────────────────────────────────────────────────────
def _gold_case():
    return GOLD_CASE_SETS["nephrology"][0]["case"]


def _gold_env(**kw):
    return build_environment(_gold_case(), task_type="diagnostic_workup", question="q", **kw)


def _real_case(**over):
    """A REAL de-identified longitudinal case whose post-decision zone contains the
    answer in every modality: panel, note, study, problem, and medication. The
    decision point is day -6 (admission); day 0 is the outcome."""
    case = {
        "case_source": "real_deid",
        "specialty": "nephrology",
        "demographics": {"age_band": "60-69", "sex": "M"},
        "vitals": {"bp": "110/70", "hr": 92, "collected_offset_days": -6},
        "lab_panels": [
            {"panel": "BMP", "collected_offset_days": -6,
             "results": [{"analyte": "Creatinine", "value": 1.4}]},
            # POST-decision: the diagnostic panel
            {"panel": "Myeloma panel", "collected_offset_days": 0,
             "results": [{"analyte": "Free light chains", "value": 900}]},
            # timing UNVERIFIABLE → must be withheld fail-closed on a real case
            {"panel": "Untimed panel", "results": [{"analyte": "Mystery", "value": 1}]},
        ],
        "notes": [
            {"note_type": "Admit", "author_role": "nephrology",
             "text": "Presented with confusion and rising creatinine.", "collected_offset_days": -6},
            {"note_type": "Path", "author_role": "pathology",
             "text": "BIOPSY: cast nephropathy — diagnostic.", "collected_offset_days": 0},
            {"note_type": "Untimed", "author_role": "nephrology", "text": "no timing recorded"},
        ],
        "studies": [
            {"modality": "pathology", "label": "renal bx", "findings": "cast nephropathy",
             "collected_offset_days": 0},
        ],
        "problem_list": [
            {"condition": "CKD stage 3", "since": "2027", "collected_offset_days": -6},
            # POST-decision problem == the answer
            {"condition": "Multiple myeloma with cast nephropathy", "collected_offset_days": 0},
        ],
        "medications": [
            {"drug": "lisinopril", "dose": "10 mg", "collected_offset_days": -6},
            # POST-decision drugs that reveal the diagnosis
            {"drug": "bortezomib", "dose": "1.3 mg/m2", "collected_offset_days": 0},
            {"drug": "rasburicase", "collected_offset_days": 0},
        ],
        "ground_truth": {
            "answer": "Cast nephropathy from multiple myeloma",
            "rationale": "Rising creatinine with free light chains.",
            "key_data": ["rising creatinine", "free light chains"],
        },
    }
    case.update(over)
    return case


def _real_env(**kw):
    kw.setdefault("decision_offset_days", -6)
    kw.setdefault("require_deterministic", False)
    return build_environment(_real_case(), task_type="diagnostic_workup",
                             question="Work up the AKI", **kw)


def _drive(env, actions, seed=0):
    env.reset(seed=seed)
    for a in actions:
        if env.terminated or env.truncated:
            break
        env.step(a)
    return env


def _all_text(env) -> str:
    """Everything the agent could possibly have seen, as one string."""
    return json.dumps(env.trajectory, default=str)


# ─── 1. Temporal leakage — the critical property ──────────────────────────────
def test_post_decision_data_is_unreachable_in_every_modality():
    env = ClinicalEnv(_real_env())
    obs, _info = env.reset(seed=1)
    st = env.state

    # everything the agent can obtain WITHOUT naming a future item itself
    served = json.dumps([
        obs["chart"], st.get_problem_list(), st.get_medications(), st.get_vitals(),
        st.get_notes(), st.get_studies(), st.get_labs(), st.get_timeline(),
    ], default=str).lower()

    # the answer, in all five modalities, must be absent
    assert "myeloma" not in served           # post-decision problem + panel
    assert "cast nephropathy" not in served  # post-decision note + study
    assert "bortezomib" not in served        # post-decision medication
    assert "rasburicase" not in served
    assert "light chains" not in served
    assert "mystery" not in served           # unverifiable timing → fail-closed
    # the future panels are not even advertised in the directory
    assert st.get_labs()["available_panels"] == ["BMP"]
    # and the pre-decision chart IS reachable (the env must still be usable)
    assert "lisinopril" in served and "creatinine" in served and "ckd stage 3" in served

    # naming a future item directly returns nothing but the agent's own request echo
    for name in ("Myeloma panel", "Untimed panel"):
        r = st.get_labs(name)
        assert r["status"] == "not_available" and r["results"] == []
        assert "light chains" not in json.dumps(r).lower()


def test_unknown_timing_is_withheld_on_real_cases_but_visible_on_authored_ones():
    untimed = {"note_type": "Progress", "author_role": "md", "text": "no timing"}
    real = EHRState({"case_source": "real_deid", "notes": [untimed]})
    authored = EHRState({"case_source": "synthetic", "notes": [untimed]})
    assert "no timing" not in json.dumps(real.get_notes())        # fail-closed
    assert "no timing" in json.dumps(authored.get_notes())        # no future zone


def test_unparseable_offset_is_not_treated_as_day_zero():
    """The C2 regression: an unparseable offset must be UNKNOWN (withheld), never 0
    ("pre-decision, show it")."""
    st = EHRState({"case_source": "real_deid",
                   "lab_panels": [{"panel": "Broken", "collected_offset_days": "day-two",
                                   "results": [{"analyte": "X", "value": 1}]}]})
    assert st._item_offset({"collected_offset_days": "day-two"}) is None
    assert st.get_labs("Broken").get("status") == "not_available"


def test_held_out_outcome_is_verifier_only():
    """The future zone must be reachable by the VERIFIER even though no tool can see it."""
    env = ClinicalEnv(_real_env())
    env.reset(seed=1)
    outcome = json.dumps(env.state.held_out_outcome(), default=str).lower()
    assert "light chains" in outcome  # verifier can see it
    assert "light chains" not in json.dumps(env.state.get_labs(), default=str).lower()


# ─── 2. PHI boundary ──────────────────────────────────────────────────────────
def test_residual_phi_in_a_note_never_reaches_the_agent_or_the_trajectory():
    case = _real_case(notes=[{
        "note_type": "Admit", "author_role": "md",
        "text": "Patient MRN 123456789, reachable at 555-123-4567, rising creatinine.",
        "collected_offset_days": -6,
    }])
    compiled = build_environment(case, task_type="diagnostic_workup", question="q",
                                 decision_offset_days=-6, require_deterministic=False)
    env = _drive(ClinicalEnv(compiled), [
        {"tool": "get_notes", "input": {}},
        {"tool": "submit_diagnosis", "input": {"text": "cast nephropathy"}},
    ])
    seen = _all_text(env)
    assert "123456789" not in seen and "555-123-4567" not in seen
    assert "withheld" in seen.lower()  # replaced by the fail-closed placeholder
    # and it must not survive into the export either
    rec = record_for_mode({"task_id": "t", "specialty": "nephrology",
                           "task_type": "diagnostic_workup", "prompt": "p",
                           "trajectory": env.trajectory}, "raw")
    assert "123456789" not in json.dumps(rec)


# ─── 3. Critical-negative correctness — BOTH directions ───────────────────────
@pytest.mark.parametrize("text,should_hard_fail", [
    ("Give 3% saline rapidly to correct the sodium", True),      # took the unsafe action
    ("Low-solute hyponatremia; correct slowly, do NOT give 3% saline", False),
    ("Low-solute hyponatremia; avoid 3% saline entirely", False),
    ("Low-solute hyponatremia; hold the 3% saline", False),      # therapeutic restraint
])
def test_critical_negative_fires_only_on_the_unsafe_action(text, should_hard_fail):
    compiled = dict(_gold_env())
    compiled["critical_negatives"] = ["3% saline"]
    env = _drive(ClinicalEnv(compiled), [{"tool": "submit_plan", "input": {"text": text}}])
    v = env.verify()
    assert v["hard_failed"] is should_hard_fail, v["checks"]
    if should_hard_fail:
        assert v["reward"] == 0.0


def test_ordering_the_unsafe_drug_hard_fails_regardless_of_the_prose():
    compiled = dict(_gold_env())
    compiled["critical_negatives"] = ["heparin"]
    compiled["allowed_tools"] = list(compiled["allowed_tools"]) + ["order_medication"]
    env = _drive(ClinicalEnv(compiled), [
        {"tool": "order_medication", "input": {"drug": "heparin", "dose": "5000u"}},
        {"tool": "submit_plan", "input": {"text": "anticoagulate carefully"}},
    ])
    v = env.verify()
    assert v["hard_failed"] and v["reward"] == 0.0


# ─── 4. The reward cannot be gamed ────────────────────────────────────────────
def test_padded_hollow_scores_below_terse_correct():
    probe = V.probe_hackability(_gold_env())
    assert probe["shippable"], probe
    assert probe["terse_correct_reward"] > probe["padded_reward"]


def test_every_shipped_gold_environment_survives_the_hackability_probe():
    """The reward-integrity bar, applied to the whole shipped catalog rather than one
    example — this is the property a buyer is actually paying for."""
    from asclepius.environments import catalog

    from asclepius.environments.service import _critical_hint

    gameable = []
    for specialty, entries in GOLD_CASE_SETS.items():
        for entry in entries:
            task_type = catalog.infer_default_task_type(entry)
            # compile exactly as the service does — the critical-negative flags it
            # derives change the verifier's behavior, so probing a hint-less env
            # would test a configuration we never ship.
            compiled = build_environment(entry["case"], task_type=task_type,
                                         question=entry.get("question") or "q",
                                         critical_hint_text=_critical_hint(entry))
            probe = V.probe_hackability(compiled)
            if not probe["shippable"]:
                gameable.append((specialty, entry["case_id"], task_type, probe))
    assert not gameable, f"{len(gameable)} environment(s) have a gameable reward: {gameable}"


def test_the_ground_truth_answer_itself_scores_highly_on_every_gold_environment():
    """The calibration invariant, and the one that catches an over-strict verifier.

    Submitting the case's OWN authored answer (with the decisive order) must score
    near the top and must never hard-fail. A verifier that penalises the ground truth
    — e.g. by reading a guarded mention ("hypertonic saline only if actively seizing")
    as a recommendation, or by demanding the answer restate every context datum —
    would silently depress every reward we ship, on data a buyer trains against."""
    from asclepius.environments import catalog
    from asclepius.environments.service import _critical_hint

    weak = []
    for specialty, entries in GOLD_CASE_SETS.items():
        for entry in entries:
            task_type = catalog.infer_default_task_type(entry)
            compiled = build_environment(
                entry["case"], task_type=task_type, question=entry.get("question") or "q",
                critical_hint_text=_critical_hint(entry),
            )
            gt = compiled["ground_truth"]
            env = _drive(ClinicalEnv(compiled), [
                {"type": "thought", "content": " ".join(str(d) for d in gt["key_data"])},
                {"tool": "order_test", "input": {"code": (gt["key_data"] or ["decisive"])[0]}},
                {"tool": "submit_diagnosis", "input": {"text": gt["answer"]}},
            ])
            v = env.verify()
            if v["hard_failed"] or v["reward"] < 0.8:
                weak.append((specialty, entry["case_id"], task_type, v["reward"],
                             [c["id"] for c in v["checks"] if c.get("passed") is False]))
    assert not weak, f"the ground-truth answer under-scores on {len(weak)} environment(s): {weak}"


def test_a_guarded_mention_of_a_dangerous_drug_is_not_a_recommendation():
    """"hypertonic saline ONLY IF actively seizing" is clinical restraint, not the
    unsafe action — the gate must require an affirmative action verb."""
    compiled = dict(_gold_env())
    compiled["critical_negatives"] = ["hypertonic saline"]
    for text, hard_fail in [
        ("Correct slowly; hypertonic saline only if actively seizing", False),
        ("Give hypertonic saline now to raise the sodium quickly", True),
    ]:
        env = _drive(ClinicalEnv(compiled), [{"tool": "submit_plan", "input": {"text": text}}])
        assert env.verify()["hard_failed"] is hard_fail, text


def test_reciting_the_decisive_findings_does_not_satisfy_the_diagnosis_check():
    """The specific hack the probe surfaced: ``key_data`` vocabulary overlaps the
    diagnosis phrase, so an agent that recites the findings without naming a
    diagnosis must NOT pass. Only DISCRIMINATIVE diagnosis terms count."""
    compiled = _gold_env()
    findings = " ".join(str(d) for d in compiled["ground_truth"]["key_data"])
    env = _drive(ClinicalEnv(compiled), [
        {"tool": "submit_diagnosis", "input": {"text": f"Relevant considerations include {findings}."}},
    ])
    answer = next(c for c in env.verify()["checks"]
                  if c["type"] == "deterministic" and "correct" in c["id"])
    assert answer["passed"] is False, answer


def test_negated_or_wrong_diagnosis_does_not_pass_the_answer_check():
    compiled = _gold_env()
    gt = compiled["ground_truth"]["answer"]

    def answer_check(text):
        env = _drive(ClinicalEnv(compiled), [{"tool": "submit_diagnosis", "input": {"text": text}}])
        return next(c for c in env.verify()["checks"] if c["type"] == "deterministic"
                    and "correct" in c["id"])

    assert answer_check(gt)["passed"] is True                      # the right answer
    assert answer_check("This is NOT " + gt)["passed"] is False    # negated
    assert answer_check("Euvolemic SIADH; fluid restrict")["passed"] is False  # wrong dx
    assert answer_check("")["passed"] is False                     # empty


def test_a_no_op_episode_earns_no_credit():
    env = ClinicalEnv(_gold_env(), max_steps=3)
    env.reset(seed=1)
    while not (env.terminated or env.truncated):
        env.step({"type": "thought", "content": "hmm"})
    v = env.verify()
    assert v["truncated"] if "truncated" in v else True
    assert v["reward"] <= 0.2, v["checks"]


def test_not_applicable_checks_are_excluded_from_the_base_reward():
    """``passed: None`` must not count as a pass (vacuous credit)."""
    reward = V._compose([{"type": "deterministic", "passed": None},
                         {"type": "deterministic", "passed": False}], None, None, False)
    assert reward == 0.0


def test_nan_scores_cannot_become_a_perfect_reward():
    assert V._compose([{"type": "deterministic", "passed": True}], float("nan"), None, False) == 0.75
    assert V._compose([{"type": "deterministic", "passed": True}], float("inf"), None, False) == 0.75


def test_a_rubric_layer_with_no_signal_is_excluded_not_given_free_credit():
    class _Stub:
        trajectory = []
    assert V._rubric_proxy(_Stub(), {}) is None


# ─── 5. Reward / step alignment (the dense-training contract) ──────────────────
def test_step_rewards_are_keyed_to_real_trajectory_steps():
    env = _drive(ClinicalEnv(_gold_env()), [
        {"type": "thought", "content": "think"},
        {"tool": "get_labs", "input": {"panel": "BMP"}},
        {"tool": "submit_diagnosis", "input": {"text": "low-solute hyponatremia"}},
    ])
    v = env.verify()
    traj_steps = {s["step"] for s in env.trajectory}
    assert v["step_rewards"], "dense rewards must be emitted"
    for entry in v["step_rewards"]:
        assert set(entry) >= {"step", "reward"}
        assert entry["step"] in traj_steps, f"reward keyed to a non-existent step {entry['step']}"
    # the terminal reward lands on the final_output step
    assert v["terminal_step"] == max(traj_steps)
    assert v["step_rewards_flat"] == [e["reward"] for e in v["step_rewards"]]


def test_max_steps_counts_agent_actions_not_trajectory_rows():
    env = ClinicalEnv(_gold_env(), max_steps=4)
    env.reset(seed=1)
    for _ in range(10):
        if env.terminated or env.truncated:
            break
        env.step({"tool": "get_labs", "input": {}})
    assert env.action_count == 4, "the cap must bind on agent actions"
    assert env.truncated
    assert len(env.trajectory) > 4, "one action emits several trajectory rows"


# ─── 6. reset() clears state / reproducibility ────────────────────────────────
def test_reset_fully_clears_mutable_episode_state():
    compiled = _gold_env()
    env = _drive(ClinicalEnv(compiled), [
        {"tool": "get_labs", "input": {"panel": "BMP"}},
        {"tool": "submit_diagnosis", "input": {"text": "x"}},
    ])
    assert env.trajectory and env.terminated
    obs2, _ = env.reset(seed=7)
    assert env.trajectory == [] and env.emitted == [] and env.step_rewards == []
    assert env.terminated is False and env.truncated is False
    assert env.action_count == 0 and env.final_action() is None
    # identical to a never-used env on the same seed
    fresh_obs, _ = ClinicalEnv(compiled).reset(seed=7)
    assert obs2 == fresh_obs


def test_same_seed_same_actions_give_identical_observations_and_reward():
    compiled = _gold_env()
    actions = [{"tool": "get_labs", "input": {}},
               {"tool": "submit_diagnosis", "input": {"text": "low-solute hyponatremia"}}]
    a = _drive(ClinicalEnv(compiled), actions, seed=11)
    b = _drive(ClinicalEnv(compiled), actions, seed=11)
    assert a.trajectory == b.trajectory
    assert a.verify() == b.verify()


def test_episodes_do_not_contaminate_each_other_through_the_shared_case():
    """The compiled env holds ONE case dict reused by every episode; a consumer
    mutating a returned list must not corrupt it."""
    compiled = _gold_env()
    e1 = ClinicalEnv(compiled)
    e1.reset(seed=1)
    e1.state.get_labs("BMP")["results"].append({"analyte": "INJECTED", "value": 0})
    e2 = ClinicalEnv(compiled)
    e2.reset(seed=1)
    assert "INJECTED" not in json.dumps(e2.state.get_labs("BMP"))


# ─── 7. Compile refusal — honest behavior over forced determinism ──────────────
def test_ambiguous_real_case_is_refused_not_forced_into_a_deterministic_env():
    ambiguous = _real_case(ground_truth={"answer": "", "key_data": []})
    ambiguous["lab_panels"] = [p for p in ambiguous["lab_panels"]
                               if p.get("collected_offset_days") == -6]
    with pytest.raises(CompileError):
        build_environment(ambiguous, task_type="diagnostic_workup", question="q",
                          decision_offset_days=-6)


def test_a_real_case_missing_a_decision_critical_field_is_disqualified():
    no_meds = _real_case(medications=[])
    with pytest.raises(CompileError):
        build_environment(no_meds, task_type="medication_management", question="q",
                          decision_offset_days=-6, require_deterministic=False)


def test_gold_cases_always_compile_and_stay_non_trivial():
    for specialty, entries in GOLD_CASE_SETS.items():
        entry = entries[0]
        compiled = build_environment(entry["case"], task_type="diagnostic_workup",
                                     question=entry.get("question") or "q")
        env = ClinicalEnv(compiled)
        obs, _ = env.reset(seed=0)
        # information must be EARNED: at most one panel is visible at reset
        assert len(obs["chart"]["labs_available_now"]) <= 1, specialty
        assert compiled["ground_truth"]["answer"], specialty


# ─── 8. Export tiers ──────────────────────────────────────────────────────────
def _record():
    return {
        "task_id": "neph-dx-1", "specialty": "nephrology", "task_type": "diagnostic_workup",
        "prompt": "p", "trajectory": [{"step": 1, "type": "thought", "content": "t"}],
        "verification": {"reward": 0.9, "checks": []},
        "provenance": {"case_source": "gold"},
        "physician_annotation": {"step_labels": [{"step": 1, "label": "correct"}],
                                 "counterfactual_text": "should have ordered urine studies"},
    }


def test_raw_mode_ships_only_the_centaur_core():
    rec = record_for_mode(_record(), "raw")
    assert set(rec) == {"task_id", "specialty", "task_type", "prompt", "trajectory"}
    assert "verification" not in rec and "provenance" not in rec
    assert "physician_annotation" not in rec


def test_graded_adds_verification_but_not_the_annotation():
    rec = record_for_mode(_record(), "graded")
    assert rec["verification"]["reward"] == 0.9
    assert "provenance" in rec
    assert "physician_annotation" not in rec


def test_expert_adds_the_physician_annotation_layer():
    rec = record_for_mode(_record(), "expert")
    assert rec["physician_annotation"]["counterfactual_text"]
    assert rec["verification"]["reward"] == 0.9


def test_step_type_vocabulary_matches_the_centaur_contract():
    env = _drive(ClinicalEnv(_gold_env()), [
        {"type": "thought", "content": "t"},
        {"tool": "get_labs", "input": {"panel": "BMP"}},
        {"tool": "submit_diagnosis", "input": {"text": "x"}},
    ])
    assert {s["type"] for s in env.trajectory} <= {"thought", "tool_call", "observation", "final_output"}
    assert env.trajectory[-1]["type"] == "final_output"


# ─── interface hygiene ────────────────────────────────────────────────────────
def test_verify_and_step_both_require_reset():
    env = ClinicalEnv(_gold_env())
    with pytest.raises(RuntimeError):
        env.verify()
    with pytest.raises(RuntimeError):
        env.step({"type": "thought", "content": "x"})


def test_revealed_state_is_json_serializable():
    env = _drive(ClinicalEnv(_gold_env()), [{"tool": "get_labs", "input": {"panel": "BMP"}}])
    json.dumps(env.state.revealed)  # must not raise (a set would)


def test_malformed_tool_input_does_not_crash_the_episode():
    env = ClinicalEnv(_gold_env())
    env.reset(seed=1)
    for bad in ({"panel": ["a", "list"]}, {"panel": 123}, {"window": "recent"}, {"unexpected": 1}):
        obs, reward, term, trunc, info = env.step({"tool": "get_labs", "input": bad})
        assert not term, "a bad argument must not end the episode"
    env.step({"tool": "get_timeline", "input": {"window": "recent"}})


def test_get_labs_prefers_exact_then_word_prefix_over_substring():
    st = EHRState({"lab_panels": [
        {"panel": "Adrenal workup", "collected_offset_days": 0, "results": [{"analyte": "cortisol", "value": 5}]},
        {"panel": "Renal function", "collected_offset_days": 0, "results": [{"analyte": "Cr", "value": 2}]},
    ]})
    assert st.get_labs("renal")["matched_panel"] == "Renal function"
    assert st.get_labs("Adrenal workup")["matched_panel"] == "Adrenal workup"
    assert st.get_labs("nonexistent")["status"] == "not_available"
