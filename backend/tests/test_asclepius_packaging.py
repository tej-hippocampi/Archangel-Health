"""Packaging + canonical-format tests (opt §1.1, §4.12).

Golden-file byte-stable JSONL for a known submission across all three canonical
formats (hh-rlhf preference flat + chat, {prompt, completion} SFT, PRM800K
reasoning trace), plus the grounded premium-tier flag.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asclepius import profiles  # noqa: E402
from asclepius.packaging import package_submission  # noqa: E402


def _task(**kw):
    base = {
        "task_id": "t-neph-00231",
        "specialty": "nephrology",
        "difficulty": "hard",
        "capture_reasoning": False,
        "source": "lab_supplied",
        "buyer_request_id": None,
        "prompt": "72yo on HD, K+ 6.4 with peaked T-waves. Adjust dialysate and meds?",
        "candidate_answers": [
            {"id": "A", "text": "Lower dialysate K+ to 2.0, give calcium gluconate, then dialyze.", "generator_model": "model_x"},
            {"id": "B", "text": "Set dialysate K+ to 1.0 immediately and start insulin-dextrose.", "generator_model": "model_y"},
        ],
    }
    base.update(kw)
    return base


def _submission(payload, **kw):
    base = {
        "submission_id": "s-00231-7c2a",
        "task_id": "t-neph-00231",
        "verdict": payload.get("verdict"),
        "chosen_id": payload.get("chosen_id"),
        "rejected_id": payload.get("rejected_id"),
        "confidence": payload.get("confidence", "high"),
        "agreement_score": None,
        "created_at": "2026-06-26T12:00:00",
        "annotator": {
            "id_hashed": "a91f0000deadbeef",
            "credentials": "board_certified_nephrology",
            "specialty": "nephrology",
            "years_experience": 12,
        },
        "payload": payload,
    }
    base.update(kw)
    return base


def test_independent_answer_emits_blind_ideal_record():
    """Eval Flow Upgrade §3: in ``full`` independent mode the blind independent
    answer becomes an additional ``ideal_answer`` record tagged
    ``independent: true`` (premium uncontaminated SFT), alongside the preference
    — one submission, multiple records."""
    payload = {
        "verdict": "A_better", "chosen_id": "A", "rejected_id": "B", "confidence": "high",
        "prompt_review": {"reviewed": True, "verdict": "valid"},
        "independent_answer": {"text": "Give IV calcium to stabilize the membrane, shift potassium with insulin and dextrose, then dialyze.", "kind": "full"},
        "chosen_revision": {"edited": False, "why_better_notes": "safer"},
        "rejected_critique": {"error_tags": ["dosing_error"], "why_worse": "too aggressive"},
    }
    recs = package_submission(_task(independent_mode="full"), _submission(payload))
    assert any(r["type"] == "preference" for r in recs)
    blind = [r for r in recs if r["type"] == "ideal_answer" and r.get("independent")]
    assert len(blind) == 1
    b = blind[0]
    assert b["ideal_answer"].startswith("Give IV calcium")
    assert b["completion"] == b["ideal_answer"]
    assert b["messages"][0]["role"] == "user" and b["messages"][1]["role"] == "assistant"
    # Provenance upgrade rides every record, including the blind ideal.
    assert b["prompt_clinician_reviewed"] is True
    assert all(r.get("prompt_clinician_reviewed") is True for r in recs)


def test_reasoning_steps_carry_critique():
    """Eval Flow Upgrade §4: a graded step's one-line critique rides the
    reasoning_trace record alongside its label + reward."""
    payload = {
        "verdict": "both_inadequate", "confidence": "high",
        "independent_answer": {"text": "Give IV calcium, then insulin and dextrose, then dialyze."},
        "from_scratch": {
            "ideal_answer": "Give IV calcium, then insulin and dextrose, then dialyze.",
            "approach_notes": "",
            "reasoning_steps": [
                {"step": 1, "text": "Give IV calcium", "label": "good", "step_reward": 1, "critique": None},
                {"step": 2, "text": "Start oral resin only", "label": "bad", "step_reward": 0,
                 "critique": "too slow for K+ 6.4 with ECG changes"},
            ],
        },
    }
    recs = package_submission(_task(capture_reasoning=True), _submission(payload))
    trace = [r for r in recs if r["type"] == "reasoning_trace"]
    assert len(trace) == 1
    steps = trace[0]["steps"]
    assert steps[0]["critique"] is None
    assert steps[1]["critique"] == "too slow for K+ 6.4 with ECG changes"
    assert steps[1]["label"] == "bad" and steps[1]["step_reward"] == 0


def test_corrected_step_emits_original_gold_reason_and_derived_label():
    """Edit-to-Correct: a corrected step carries the AI's ``original_text`` (the
    negative), the expert ``text`` (gold), the ``correction_reason``, and the
    derived ``label`` (substantive reason → bad). An ``added`` step is the doctor's
    own reasoning: label=good, original_text=None. ``step_pairs`` exposes ready-made
    step-level preference pairs for every corrected step."""
    payload = {
        "verdict": "both_inadequate", "confidence": "high",
        "independent_answer": {"text": "Give IV calcium, then insulin and dextrose, then dialyze."},
        "from_scratch": {
            "ideal_answer": "Give IV calcium, then insulin and dextrose, then dialyze.",
            "approach_notes": "",
            "reasoning_steps": [
                # corrected (substantive) — original preserved, label derived to bad
                {"step": 1, "text": "Give IV calcium to stabilize the myocardium",
                 "original_text": "Give oral resin to lower potassium",
                 "corrected": True, "confirmed": False, "added": False,
                 "correction_reason": "factual_error", "label": "bad", "step_reward": 0},
                # confirmed as-is — label good
                {"step": 2, "text": "Shift K+ with insulin and dextrose",
                 "original_text": "Shift K+ with insulin and dextrose",
                 "corrected": False, "confirmed": True, "added": False,
                 "label": "good", "step_reward": 1},
                # authored (AI omitted) — added, no original
                {"step": 3, "text": "Then dialyze given the ESRD",
                 "original_text": None, "added": True, "label": "good", "step_reward": 1},
            ],
        },
    }
    recs = package_submission(_task(capture_reasoning=True), _submission(payload))
    trace = [r for r in recs if r["type"] == "reasoning_trace"][0]
    steps = trace["steps"]
    # corrected step: original (negative) + gold + reason + derived label
    assert steps[0]["original_text"] == "Give oral resin to lower potassium"
    assert steps[0]["text"] == "Give IV calcium to stabilize the myocardium"
    assert steps[0]["corrected"] is True
    assert steps[0]["correction_reason"] == "factual_error"
    assert steps[0]["label"] == "bad"
    # confirmed step
    assert steps[1]["confirmed"] is True and steps[1]["label"] == "good"
    # added step: no original, label good
    assert steps[2]["added"] is True
    assert steps[2]["original_text"] is None
    assert steps[2]["label"] == "good"
    # step_pairs: one ready-made preference pair for the single corrected step
    pairs = trace["step_pairs"]
    assert len(pairs) == 1
    assert pairs[0]["rejected"] == "Give oral resin to lower potassium"
    assert pairs[0]["chosen"] == "Give IV calcium to stabilize the myocardium"
    assert pairs[0]["reason"] == "factual_error"
    assert pairs[0]["prompt_context"] == trace["prompt"]


def test_minor_wording_edit_derives_neutral_label():
    """A non-substantive edit (minor_wording) is neutral, not a hard error."""
    payload = {
        "verdict": "both_inadequate", "confidence": "high",
        "independent_answer": {"text": "Give IV calcium, then dialyze."},
        "from_scratch": {
            "ideal_answer": "Give IV calcium, then dialyze.",
            "reasoning_steps": [
                {"step": 1, "text": "Administer IV calcium gluconate",
                 "original_text": "give calcium", "corrected": True,
                 "correction_reason": "minor_wording", "label": "neutral", "step_reward": 0},
            ],
        },
    }
    recs = package_submission(_task(capture_reasoning=True), _submission(payload))
    trace = [r for r in recs if r["type"] == "reasoning_trace"][0]
    assert trace["steps"][0]["label"] == "neutral"
    # still a corrected step, so it yields a step pair
    assert trace["step_pairs"][0]["reason"] == "minor_wording"


def test_corrected_label_is_derived_server_side_not_trusted_from_client():
    """Defense-in-depth: a corrected step's buyer-facing label is derived from the
    clinical reason on the server, overriding any (stale/wrong) client label."""
    payload = {
        "verdict": "both_inadequate", "confidence": "high",
        "independent_answer": {"text": "calcium then dialyze"},
        "from_scratch": {
            "ideal_answer": "calcium then dialyze",
            "reasoning_steps": [
                # client wrongly sent label=good on a step corrected for an UNSAFE reason
                {"step": 1, "text": "Give IV calcium", "original_text": "give nothing",
                 "corrected": True, "correction_reason": "unsafe", "label": "good", "step_reward": 1},
            ],
        },
    }
    recs = package_submission(_task(capture_reasoning=True), _submission(payload))
    trace = [r for r in recs if r["type"] == "reasoning_trace"][0]
    assert trace["steps"][0]["label"] == "bad"  # derived from reason, not the client


def test_no_independent_answer_emits_no_blind_record():
    payload = {
        "verdict": "A_better", "chosen_id": "A", "rejected_id": "B", "confidence": "high",
        "chosen_revision": {"edited": False, "why_better_notes": "safer"},
        "rejected_critique": {"error_tags": ["dosing_error"], "why_worse": "too aggressive"},
    }
    recs = package_submission(_task(), _submission(payload))
    assert not [r for r in recs if r["type"] == "ideal_answer" and r.get("independent")]
    # Unreviewed prompt -> the flag is present but False on every record.
    assert all(r.get("prompt_clinician_reviewed") is False for r in recs)


def test_preference_flat_and_chat_variants():
    payload = {
        "verdict": "A_better",
        "chosen_id": "A",
        "rejected_id": "B",
        "confidence": "high",
        "chosen_revision": {"edited": False, "revised_text": None, "why_better_tags": ["safer", "better_dosing"], "why_better_notes": "B over-lowers dialysate K+, arrhythmia risk"},
        "rejected_critique": {"error_tags": ["dosing_error", "unsafe_recommendation"], "severities": {"dosing_error": "high"}, "why_worse": "dialysate K+ 1.0 is too aggressive"},
    }
    recs = package_submission(_task(), _submission(payload))
    pref = [r for r in recs if r["type"] == "preference"]
    assert len(pref) == 1
    p = pref[0]
    # flat hh-rlhf
    assert p["prompt"].startswith("72yo on HD")
    assert "calcium gluconate" in p["chosen"]
    assert "dialysate K+ to 1.0" in p["rejected"]
    assert p["error_tags_on_rejected"] == ["dosing_error", "unsafe_recommendation"]
    assert p["rationale"]
    # chat hh-rlhf variant message arrays present
    assert p["chosen_messages"][0]["role"] == "user"
    assert p["chosen_messages"][1]["role"] == "assistant"
    # provenance + attestation
    assert p["annotator_credential"] == "board_certified_nephrology"
    assert p["license"] and p["ip_cleared"] is True and p["contains_phi"] is False
    assert p["source"] == "lab_supplied"

    # chat profile maps chosen/rejected to message arrays
    prof = dict(profiles.load_profile("default"))
    prof["preference_variant"] = "chat"
    mapped = profiles.map_record(prof, p)
    assert isinstance(mapped["chosen"], list) and mapped["chosen"][0]["role"] == "user"
    assert isinstance(mapped["rejected"], list)
    assert profiles.validate_against_schema(mapped, profiles.schema_for(prof, "preference")) == []


def test_revised_chosen_yields_sft_ideal_answer():
    payload = {
        "verdict": "B_better",
        "chosen_id": "B",
        "rejected_id": "A",
        "confidence": "medium",
        "chosen_revision": {"edited": True, "revised_text": "Confirm ECG, give calcium, then dialyze with K+ 2.0.", "why_better_tags": ["safer"], "why_better_notes": "corrected dialysate target"},
        "rejected_critique": {"error_tags": ["dosing_error"], "severities": {}, "why_worse": "too aggressive"},
    }
    recs = package_submission(_task(), _submission(payload))
    sft = [r for r in recs if r["type"] == "ideal_answer"]
    assert len(sft) == 1
    s = sft[0]
    # {prompt, completion} SFT alias (completion mirrors ideal_answer)
    assert s["completion"] == s["ideal_answer"] == "Confirm ECG, give calcium, then dialyze with K+ 2.0."
    assert s["messages"][1]["role"] == "assistant"


def test_both_inadequate_emits_ideal_and_prm800k_trace():
    payload = {
        "verdict": "both_inadequate",
        "confidence": "high",
        "from_scratch": {
            "ideal_answer": "Stabilize the membrane with calcium, shift K+, then dialyze.",
            "approach_notes": "confirm ECG first",
            "reasoning_steps": [
                {"step": 1, "text": "Assess ECG for instability", "label": "good", "step_reward": 1.0},
                {"step": 2, "text": "Give IV calcium for membrane stabilization", "label": "good"},
                {"step": 3, "text": "Then dialyze", "label": "neutral", "step_reward": 0.5},
            ],
        },
    }
    recs = package_submission(_task(), _submission(payload))
    types = sorted(r["type"] for r in recs)
    assert types == ["ideal_answer", "reasoning_trace"]
    trace = [r for r in recs if r["type"] == "reasoning_trace"][0]
    # PRM800K-style: ordered steps, each independently labeled good|neutral|bad
    assert [s["step"] for s in trace["steps"]] == [1, 2, 3]
    assert [s["label"] for s in trace["steps"]] == ["good", "good", "neutral"]
    assert trace["steps"][0]["step_reward"] == 1.0


def test_grounded_flag_set_when_anchor_valid():
    anchor = {"citation_text": "KDIGO 2024 hyperkalemia", "source_type": "guideline", "identifier": "KDIGO-2024-3.2"}
    payload = {
        "verdict": "A_better", "chosen_id": "A", "rejected_id": "B", "confidence": "high",
        "chosen_revision": {"edited": False, "why_better_notes": "safer dosing", "why_better_tags": ["safer"], "evidence_anchor": anchor},
        "rejected_critique": {"error_tags": ["dosing_error"], "severities": {}, "why_worse": "too aggressive"},
    }
    recs = package_submission(_task(), _submission(payload))
    p = [r for r in recs if r["type"] == "preference"][0]
    assert p["grounded"] is True
    assert p["evidence_anchor"]["identifier"] == "KDIGO-2024-3.2"

    # ungrounded when no anchor
    payload["chosen_revision"].pop("evidence_anchor")
    recs2 = package_submission(_task(), _submission(payload))
    assert [r for r in recs2 if r["type"] == "preference"][0]["grounded"] is False


def test_canonical_jsonl_is_byte_stable():
    """A known submission produces byte-identical canonical JSONL across runs
    (opt §4.12 golden-file guarantee)."""
    payload = {
        "verdict": "A_better", "chosen_id": "A", "rejected_id": "B", "confidence": "high",
        "chosen_revision": {"edited": False, "why_better_notes": "B over-lowers K+", "why_better_tags": ["safer"]},
        "rejected_critique": {"error_tags": ["dosing_error"], "severities": {}, "why_worse": "too aggressive"},
    }
    prof = profiles.load_profile("default")

    def serialize():
        recs = package_submission(_task(), _submission(payload))
        lines = []
        for r in recs:
            mapped = profiles.map_record(prof, r)
            if mapped is None:
                continue
            assert profiles.validate_against_schema(mapped, profiles.schema_for(prof, r["type"])) == []
            lines.append(json.dumps(mapped, ensure_ascii=False, sort_keys=True))
        return "\n".join(lines)

    first = serialize()
    second = serialize()
    assert first == second  # deterministic, byte-stable
    # sanity: the preference line round-trips and carries the premium provenance
    obj = json.loads(first.splitlines()[0])
    assert obj["type"] == "preference"
    assert obj["annotator_credential"] == "board_certified_nephrology"
    assert obj["taxonomy_version"] and obj["config_version"]


# ─── Speed Optimization §1/§2/§6: stance, assist provenance, tag reasons ──────
def test_stance_kind_ships_as_context_field_not_gold():
    """Feature 1: a stance-mode capture rides the preference record as ``stance``
    (anchoring guard); NO independent ideal record is emitted, and the gold
    ideal_answer stays the specialist-refined chosen answer."""
    payload = {
        "verdict": "A_better", "chosen_id": "A", "rejected_id": "B", "confidence": "high",
        "independent_answer": {"text": "keep calcium first; dialyze early", "kind": "stance"},
        "chosen_revision": {"edited": True, "revised_text": "Give calcium gluconate, then dialyze with K+ 2.0 and recheck.", "why_better_notes": "safer"},
        "rejected_critique": {"error_tags": ["dosing_error"], "why_worse": "too aggressive"},
    }
    recs = package_submission(_task(), _submission(payload))
    pref = [r for r in recs if r["type"] == "preference"][0]
    assert pref["stance"] == "keep calcium first; dialyze early"
    assert not any(r.get("independent") for r in recs)
    ideals = [r for r in recs if r["type"] == "ideal_answer"]
    assert len(ideals) == 1  # the refined-chosen gold, not the stance
    assert ideals[0]["ideal_answer"].startswith("Give calcium gluconate")
    assert ideals[0].get("independent") is None


def test_stance_default_resolves_from_task_mode():
    """A payload without ``kind`` falls back to the task's independent_mode
    (default stance) — direct API clients get the same semantics."""
    payload = {
        "verdict": "A_better", "chosen_id": "A", "rejected_id": "B", "confidence": "high",
        "independent_answer": {"text": "quick take"},
        "chosen_revision": {"edited": False, "why_better_notes": "safer"},
        "rejected_critique": {"error_tags": ["dosing_error"], "why_worse": "x"},
    }
    recs = package_submission(_task(), _submission(payload))  # no independent_mode on task
    assert [r for r in recs if r["type"] == "preference"][0]["stance"] == "quick take"
    assert not any(r.get("independent") for r in recs)


def test_assist_suggestions_and_tag_reasons_carried_next_to_finals():
    """Feature 2 + 6: the sanitized ``assist`` block (suggested_*) and the
    structured ``error_tag_reasons`` ship on the preference record alongside the
    human finals; per-step suggested_label ships next to the human label."""
    payload = {
        "verdict": "A_better", "chosen_id": "A", "rejected_id": "B", "confidence": "high",
        "independent_answer": {"text": "quick take", "kind": "stance"},
        "chosen_revision": {"edited": False, "why_better_notes": "safer"},
        "rejected_critique": {
            "error_tags": ["dosing_error"], "why_worse": "too aggressive",
            "error_tag_reasons": {"dosing_error": "dose_too_high", "hallucination": "unsafe"},
        },
        "assist": {
            "prelabeled": True, "suggested_verdict": "A_better",
            "suggested_error_tags": ["dosing_error"],
            "suggested_rationale": "K+ 1.0 dialysate is unsafe.",
            "suggested_step_labels": ["good", "bad"],
            "confidence": 0.85,
            "smuggled_field": "should never ship",
        },
        "reasoning_steps": [
            {"step": 1, "text": "Give IV calcium", "original_text": "Give IV calcium",
             "confirmed": True, "label": "good", "suggested_label": "good"},
            {"step": 2, "text": "Dialyze with K+ 2.0", "original_text": "Dialyze with K+ 1.0",
             "corrected": True, "correction_reason": "unsafe", "label": "bad",
             "suggested_label": "bad"},
        ],
    }
    recs = package_submission(_task(capture_reasoning=True), _submission(payload))
    pref = [r for r in recs if r["type"] == "preference"][0]
    # Only the reason for a SELECTED tag ships.
    assert pref["error_tag_reasons"] == {"dosing_error": "dose_too_high"}
    assert pref["assist"]["prelabeled"] is True
    assert pref["assist"]["suggested_verdict"] == "A_better"
    assert pref["assist"]["suggested_error_tags"] == ["dosing_error"]
    assert pref["assist"]["suggested_step_labels"] == ["good", "bad"]
    assert "smuggled_field" not in pref["assist"]
    # Per-step: suggestion + human label both present (override-rate monitoring).
    trace = [r for r in recs if r["type"] == "reasoning_trace"][0]
    assert trace["steps"][0]["suggested_label"] == "good" and trace["steps"][0]["label"] == "good"
    assert trace["steps"][1]["suggested_label"] == "bad" and trace["steps"][1]["label"] == "bad"


def test_no_assist_block_when_not_prelabeled():
    payload = {
        "verdict": "A_better", "chosen_id": "A", "rejected_id": "B", "confidence": "high",
        "independent_answer": {"text": "quick take"},
        "chosen_revision": {"edited": False, "why_better_notes": "safer"},
        "rejected_critique": {"error_tags": ["dosing_error"], "why_worse": "x"},
    }
    recs = package_submission(_task(), _submission(payload))
    assert [r for r in recs if r["type"] == "preference"][0]["assist"] is None


def test_client_kind_cannot_upgrade_stance_to_blind_gold():
    """Guardrail: the TASK's independent_mode is authoritative — a client-supplied
    kind='full' on a stance-mode task must never mint an ``independent: true``
    premium record from a (potentially post-reveal) answer."""
    payload = {
        "verdict": "A_better", "chosen_id": "A", "rejected_id": "B", "confidence": "high",
        "independent_answer": {"text": "Copied from revealed answer A.", "kind": "full"},
        "chosen_revision": {"edited": False, "why_better_notes": "safer"},
        "rejected_critique": {"error_tags": ["dosing_error"], "why_worse": "x"},
    }
    recs = package_submission(_task(independent_mode="stance"), _submission(payload))
    assert not any(r.get("independent") for r in recs)
    assert [r for r in recs if r["type"] == "preference"][0]["stance"] == "Copied from revealed answer A."


# ─── Asclepius V2: portal version drives capture kind + rides every record ────
def test_v1_portal_always_captures_full_blind_ideal_on_stance_task():
    """V1 (classic) captures a full blind ideal answer even on a stance-default
    task, and every record is stamped portal_version='v1'."""
    payload = {
        "verdict": "A_better", "chosen_id": "A", "rejected_id": "B", "confidence": "high",
        "portal_version": "v1",
        "independent_answer": {"text": "My full ideal answer written before reveal.", "kind": "full", "portal_version": "v1"},
        "chosen_revision": {"edited": False, "why_better_notes": "safer"},
        "rejected_critique": {"error_tags": ["dosing_error"], "why_worse": "x"},
    }
    recs = package_submission(_task(independent_mode="stance"), _submission(payload))
    blind = [r for r in recs if r["type"] == "ideal_answer" and r.get("independent")]
    assert len(blind) == 1  # classic full blind ideal, despite stance-default task
    assert all(r["portal_version"] == "v1" for r in recs)


def test_v2_portal_stance_task_no_blind_ideal_and_stamped_v2():
    payload = {
        "verdict": "A_better", "chosen_id": "A", "rejected_id": "B", "confidence": "high",
        "portal_version": "v2",
        "independent_answer": {"text": "quick take", "kind": "stance", "portal_version": "v2"},
        "chosen_revision": {"edited": True, "revised_text": "Refined chosen answer.", "why_better_notes": "safer"},
        "rejected_critique": {"error_tags": ["dosing_error"], "why_worse": "x"},
    }
    recs = package_submission(_task(independent_mode="stance"), _submission(payload))
    assert not any(r.get("independent") for r in recs)
    assert [r for r in recs if r["type"] == "preference"][0]["stance"] == "quick take"
    assert all(r["portal_version"] == "v2" for r in recs)


def test_submission_row_portal_version_overrides_payload():
    """The submission row's stamped version is authoritative over the payload."""
    payload = {
        "verdict": "A_better", "chosen_id": "A", "rejected_id": "B", "confidence": "high",
        "portal_version": "v2",  # stale/spoofed in payload
        "independent_answer": {"text": "full answer", "kind": "full"},
        "chosen_revision": {"edited": False, "why_better_notes": "safer"},
        "rejected_critique": {"error_tags": ["dosing_error"], "why_worse": "x"},
    }
    sub = _submission(payload, portal_version="v1")  # row says v1
    recs = package_submission(_task(independent_mode="full"), sub)
    assert all(r["portal_version"] == "v1" for r in recs)
