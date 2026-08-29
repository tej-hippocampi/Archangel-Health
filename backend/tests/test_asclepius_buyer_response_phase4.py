"""Buyer Response PRD Phase 4 — credentials block, citations, agreement, ceiling.

Covers §12 tests 27-34 plus the §8/§9 record annotations (signal_ceiling,
supervision_type, decisive action).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asclepius import agreement as AG  # noqa: E402
from asclepius import credentials as CR  # noqa: E402
from asclepius import validation as VAL  # noqa: E402
from asclepius.packaging import package_submission  # noqa: E402
from asclepius.supervision import DecisiveAction, decisive_action_satisfied  # noqa: E402


# ─── §6 E2 structured credential block ────────────────────────────────────────
def test_board_cert_visible_identity_masked():
    """board_certified/certifying_board/status present; npi, name, institution,
    exact years absent at every tier."""
    ship = {
        "board_certifications": ["ABIM Nephrology"], "certifying_board": "ABIM",
        "primary_specialty": "Nephrology", "subspecialties": ["Transplant Nephrology"],
        "years_in_active_practice": 12, "credentials_verified": True,
        "practice_setting": "academic_medical_center",
        # identifying fields that must NOT appear:
        "npi": "1234567890", "name": "Dr Eleanor Price", "institution": "UC Davis",
    }
    verify = {"npi": "1234567890", "license_state": "CA",
              "medical_license_number": "A-99887", "name": "Eleanor Price"}
    block = CR.structured_credential_block(
        id_hashed="a3f2", credential="board_certified_nephrology", ship=ship, verify=verify)
    assert block["board_certified"] is True
    assert block["certifying_board"] == "ABIM"
    assert block["certification_status"] == "active"
    assert block["verification_authority"]
    assert block["state_licensed"] is True and block["npi_verified"] is True
    blob = str(block)
    for forbidden in ("1234567890", "Eleanor", "Price", "UC Davis", "A-99887"):
        assert forbidden not in blob, forbidden
    # exact years never present, only the band
    assert "years_experience" not in block
    assert block["years_experience_band"] == "10-14"


def test_years_experience_banded():
    """Exact integer never ships."""
    assert CR.years_experience_band(12) == "10-14"
    assert CR.years_experience_band(1) == "0-1"
    assert CR.years_experience_band(27) == "25+"
    assert CR.years_experience_band(None) is None


def test_shipped_record_bands_years_not_exact():
    """The packaged record ships a years BAND, never the exact integer (§6 E2)."""
    annot = {"credential": "board_certified_nephrology", "specialty": "nephrology",
             "id_hashed": "h1", "years_experience": 12}
    recs = package_submission(_task(), _submission([], annot))
    pref = [r for r in recs if r["type"] == "preference"][0]
    assert pref["annotator_years_experience"] == "10-14"
    assert pref["annotator_years_experience_band"] == "10-14"
    assert pref["annotator_years_experience"] != 12


# ─── §5 citations ─────────────────────────────────────────────────────────────
def _task():
    return {"task_id": "t1", "specialty": "nephrology", "prompt": "manage hyperkalemia",
            "candidate_answers": [{"id": "A", "text": "calcium then dialyze"},
                                  {"id": "B", "text": "insulin only"}]}


def _submission(anchors, annotator, **kw):
    base = {"submission_id": "s1", "task_id": "t1", "verdict": "A_better",
            "chosen_id": "A", "rejected_id": "B", "confidence": "high",
            "created_at": "2026-06-26T12:00:00", "annotator": annotator,
            "payload": {"verdict": "A_better", "chosen_id": "A", "rejected_id": "B",
                        "chosen_revision": {"why_better_notes": "correct sequencing",
                                            "evidence_anchors": anchors},
                        "rejected_critique": {"why_worse": "unsafe", "error_tags": []}}}
    base.update(kw)
    return base


_ANNOT = {"credential": "board_certified_nephrology", "specialty": "nephrology",
          "id_hashed": "h1"}


def test_hand_typed_citation_reaches_export():
    """Both a hand-typed (DOI) and a library-confirmed citation appear with correct
    source_type, citation_confirmed, and grounding_tier."""
    library = {"citation_text": "KDIGO 2021 Glomerular §3.2", "source_type": "guideline",
               "identifier": "KDIGO 2021 Glomerular", "url": "https://kdigo.org/guidelines/gd",
               "citation_confirmed": True}
    hand = {"citation_text": "Pronase IF unmasks deposits", "source_type": "primary_literature",
            "identifier": "10.1111/his.13478", "citation_confirmed": False}
    recs = package_submission(_task(), _submission([library, hand], _ANNOT))
    pref = [r for r in recs if r["type"] == "preference"][0]
    anchors = pref["evidence_anchors"]
    by_id = {a["identifier"]: a for a in anchors}
    assert by_id["KDIGO 2021 Glomerular"]["grounding_tier"] == "guideline"
    assert by_id["KDIGO 2021 Glomerular"]["citation_confirmed"] is True
    assert by_id["10.1111/his.13478"]["grounding_tier"] == "primary"
    assert by_id["10.1111/his.13478"]["citation_confirmed"] is False


def test_unverifiable_citation_not_grounded():
    """A citation with no resolvable identifier does not count as grounded."""
    vague = {"citation_text": "as I recall from residency", "source_type": "other",
             "identifier": "my memory"}
    assert VAL.grounding_tier(vague) == "unverified"
    assert VAL.anchor_is_grounded(vague) is False
    # A resolvable DOI on an 'other' anchor does count.
    ok = {"citation_text": "x", "source_type": "other", "identifier": "10.1016/j.kint.2021.05.021"}
    assert VAL.grounding_tier(ok) == "primary"


# ─── §7 agreement ─────────────────────────────────────────────────────────────
def test_double_label_quota():
    """Frontier-hard and V4 cases always double-labeled; random top-up to the rate."""
    assert AG.should_double_label({"declared_difficulty": "frontier-hard"}, current_rate=0.9)
    assert AG.should_double_label({"case_source": "real_deid"}, current_rate=0.9)
    assert AG.should_double_label({"first_annotator_confidence": "low"}, current_rate=0.9)
    assert AG.should_double_label({"specialty": "new"}, current_rate=0.9, specialty_n=5)
    # A routine task below the target rate is routed; at or above it is not.
    # Expressed RELATIVE to the target rather than against a pinned 0.15: PRD R
    # §1.1 moved the default to 1.0 (two labels is the normal path, not a
    # sample), and the property this test exists to protect — the top-up stops at
    # the target — is the same at any target.
    target = AG.double_label_rate()
    assert AG.should_double_label({"specialty": "neph"}, current_rate=target / 3) is True
    assert AG.should_double_label({"specialty": "neph"}, current_rate=target) is False
    assert AG.should_double_label({"specialty": "neph"}, current_rate=target + 0.1) is False


def test_unblinded_observation_excluded():
    """blinded: False cannot enter the κ computation."""
    obs = [{"verdict_a": "A_better", "verdict_b": "A_better", "blinded": True,
            "specialty": "neph"}] * 40
    obs += [{"verdict_a": "A_better", "verdict_b": "B_better", "blinded": False,
             "specialty": "neph"}] * 40
    agg = AG.aggregate_kappa(obs)
    assert agg["n"] == 40  # only the blinded ones
    assert agg["excluded_unblinded"] == 40


def test_blinded_column_round_trip_excludes_unblinded(tmp_path, monkeypatch):
    """The blinded flag persists to the agreement table and an int-0 (unblinded) row
    is excluded from κ (audit fix: the filter must handle SQLite 0/1, not just bools)."""
    import os
    monkeypatch.setenv("ASCLEPIUS_DB_PATH", str(tmp_path / "agg.db"))
    from asclepius import store as asc_store
    st = asc_store.reset_store_for_tests(db_path=str(tmp_path / "agg.db"))
    st.upsert_agreement(task_id="t-blind", specialty="neph", sub_a="a", sub_b="b",
                        verdict_a="A_better", verdict_b="B_better", tags_a=[], tags_b=[],
                        jaccard_tags=0.0, verdict_agree=False, n_labels=1, flagged=False, blinded=False)
    st.upsert_agreement(task_id="t-open", specialty="neph", sub_a="a", sub_b="b",
                        verdict_a="A_better", verdict_b="A_better", tags_a=[], tags_b=[],
                        jaccard_tags=1.0, verdict_agree=True, n_labels=1, flagged=False, blinded=True)
    obs = st.list_agreement_observations()
    assert {o["blinded"] for o in obs} == {0, 1}
    agg = AG.aggregate_kappa(obs)
    assert agg["n"] == 1  # only the blinded row counts
    assert agg["excluded_unblinded"] == 1


def test_kappa_min_n_gate():
    """n < 30 → null WITH a reason string, not a bare null."""
    obs = [{"verdict_a": "A_better", "verdict_b": "A_better", "blinded": True}] * 12
    agg = AG.aggregate_kappa(obs)
    assert agg["overall"] is None
    assert agg["reason"] and "30" in agg["reason"]
    assert agg["n"] == 12
    # At/above the floor it reports a number with a CI.
    obs30 = [{"verdict_a": "A_better", "verdict_b": "B_better", "blinded": True}
             for _ in range(15)] + [{"verdict_a": "A_better", "verdict_b": "A_better",
                                     "blinded": True} for _ in range(15)]
    agg2 = AG.aggregate_kappa(obs30)
    assert agg2["overall"] is not None
    assert agg2["ci"] is not None


def test_external_adjudication_agreement():
    """Partner sealed answer vs physician answer produces a separate agreement stat."""
    # Below the gate → null with reason.
    small = AG.external_adjudication_agreement([("MGRS", "MGRS")] * 10)
    assert small["agreement"] is None and "30" in small["reason"]
    # 27 of 30 agree.
    pairs = [("MGRS", "MGRS")] * 27 + [("MGRS", "PIGN")] * 3
    stat = AG.external_adjudication_agreement(pairs)
    assert stat["n"] == 30 and stat["n_agree"] == 27
    assert abs(stat["agreement"] - round(27 / 30, 4)) < 1e-9


# ─── §8/§9 record annotations ─────────────────────────────────────────────────
def test_signal_ceiling_and_supervision_on_records():
    """Every record names its signal ceiling and ladder position (§8, §9.3)."""
    recs = package_submission(_task(), _submission([], _ANNOT))
    pref = [r for r in recs if r["type"] == "preference"][0]
    sc = pref["signal_ceiling"]
    assert sc["mode"] == "model_pair"
    assert sc["bounded_by"] == "best_of_two_frontier_models"
    assert sc["physician_authored_content"] is False
    assert pref["supervision_type"] == "pairwise_preference"
    assert pref["has_verifiable_outcome"] is False and pref["n_turns"] == 1


def test_decisive_action_verifier():
    """The physician-authored decisive action is a boolean, human-free verifier (§9.2)."""
    da = DecisiveAction(action="order pronase-digested paraffin IF",
                        tool_name="order_pronase_if", rationale="unmasks monotypic deposits")
    # Correct: ordered before the final answer.
    assert decisive_action_satisfied(
        ["order_urine_micro", "order_pronase_if", "final_answer"], da) is True
    # Guessed: answered without ordering it.
    assert decisive_action_satisfied(["order_urine_micro", "final_answer"], da) is False


# ─── §9.2 the verifier actually RUNS (wiring the orphaned supervision module) ──
#
# ``decisive_action_satisfied`` was executable and tested, but nothing called it:
# ``packaging._supervision_type`` promoted a record to ``environment_verifiable``
# because ``task["decisive_action"]`` merely EXISTED. A named decisive action is a
# claim; running the verifier is a measurement. These tests hold the line that the
# packaged record distinguishes the two.

_DECISIVE = {"action": "order pronase-digested paraffin IF",
             "tool_name": "order_pronase_if",
             "rationale": "unmasks monotypic deposits"}


def _pref(task, submission):
    return [r for r in package_submission(task, submission) if r["type"] == "preference"][0]


def test_satisfied_trajectory_is_verified_true():
    """The decisive action ordered before the final answer: measured, and it passed."""
    task = {**_task(), "decisive_action": _DECISIVE}
    sub = _submission([], _ANNOT)
    sub["payload"]["tool_calls"] = ["order_urine_micro", "order_pronase_if", "final_answer"]

    rec = _pref(task, sub)
    assert rec["supervision_type"] == "environment_verifiable"
    assert rec["decisive_action_satisfied"] is True
    assert rec["verifiable_outcome"]["verified"] is True
    assert rec["verifiable_outcome"]["decisive_action_satisfied"] is True


def test_action_ordered_after_the_final_answer_is_not_satisfied():
    """Answer first, decisive test afterwards — the agent guessed. The record still
    sits on the environment-verifiable rung (it IS checkable) but ships the
    measurement that says the trajectory failed the check."""
    task = {**_task(), "decisive_action": _DECISIVE}
    sub = _submission([], _ANNOT)
    sub["payload"]["tool_calls"] = ["order_urine_micro", "final_answer", "order_pronase_if"]

    rec = _pref(task, sub)
    assert rec["supervision_type"] == "environment_verifiable"
    assert rec["decisive_action_satisfied"] is False
    assert rec["verifiable_outcome"]["verified"] is True
    assert rec["verifiable_outcome"]["decisive_action_satisfied"] is False


def test_a_decisive_action_with_no_trajectory_is_a_claim_not_a_measurement():
    """The pre-existing behaviour, now labelled: the ladder position is unchanged
    and ``verified`` is false, so a buyer can see the claim was never checked."""
    task = {**_task(), "decisive_action": _DECISIVE}
    rec = _pref(task, _submission([], _ANNOT))

    assert rec["supervision_type"] == "environment_verifiable"
    assert rec["verifiable_outcome"]["verified"] is False
    assert "decisive_action_satisfied" not in rec["verifiable_outcome"]
    assert "decisive_action_satisfied" not in rec


def test_no_decisive_action_leaves_behaviour_unchanged():
    """The regression guard for every record that has no decisive action at all."""
    rec = _pref(_task(), _submission([], _ANNOT))
    assert rec["supervision_type"] == "pairwise_preference"
    assert "verifiable_outcome" not in rec
    assert "decisive_action_satisfied" not in rec


def test_tool_calls_as_objects_are_verified_the_same_as_bare_names():
    """Real payloads carry call objects, not only name strings."""
    task = {**_task(), "decisive_action": _DECISIVE}
    sub = _submission([], _ANNOT)
    sub["payload"]["tool_calls"] = [
        {"tool_name": "order_urine_micro", "args": {}},
        {"name": "order_pronase_if"},
        {"tool": "final_answer"},
    ]
    rec = _pref(task, sub)
    assert rec["decisive_action_satisfied"] is True


def test_a_malformed_decisive_action_falls_back_to_unverified():
    """A spec we cannot parse is a claim we cannot check — not a failed check.
    Asserting the trajectory MISSED an action whose spec was unreadable would be a
    false negative shipped as a measurement."""
    task = {**_task(), "decisive_action": {"rationale": "no tool_name, no action"}}
    sub = _submission([], _ANNOT)
    sub["payload"]["tool_calls"] = ["order_pronase_if", "final_answer"]

    rec = _pref(task, sub)
    assert rec["verifiable_outcome"]["verified"] is False
    assert "decisive_action_satisfied" not in rec


def test_tool_calls_alone_still_reach_the_environment_rung():
    """A trajectory with no decisive action to check is environment-verifiable on
    the strength of the tool calls, exactly as before, and claims no measurement."""
    sub = _submission([], _ANNOT)
    sub["payload"]["tool_calls"] = ["order_urine_micro", "final_answer"]
    rec = _pref(_task(), sub)
    assert rec["supervision_type"] == "environment_verifiable"
    assert "decisive_action_satisfied" not in rec


def test_a_malformed_tool_calls_payload_keeps_its_original_label():
    """Byte-identical-behaviour guard for the wiring change.

    The ladder label tested ``payload["tool_calls"]`` for plain truthiness, so a
    malformed shape (a dict, a string) reached the environment rung. Adding a
    verifier must not quietly tighten that: the label is unchanged, and only the
    measurement — which cannot read names out of a shape it does not recognize —
    declines to claim a result.
    """
    for malformed in ({"first": "order_pronase_if"}, "order_pronase_if", 7):
        sub = _submission([], _ANNOT)
        sub["payload"]["tool_calls"] = malformed
        rec = _pref(_task(), sub)
        assert rec["supervision_type"] == "environment_verifiable", malformed
        assert "decisive_action_satisfied" not in rec, malformed
