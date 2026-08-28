"""Longitudinal cases — the sealed future and the outcome that verifies it.

Six things are tested here, and the first is the one that must never regress:

  1. **THE SEAL (§9.1).** The labeler queue sorts on label count FIRST, so the
     moment a second physician touches a chart walk, a later point outranks the
     earlier ones for everybody. Test ``test_prd_9_1_priority_sort_scenario``
     reproduces the exact four-step scenario from the PRD and asserts the gate
     holds. The direct-open path is tested separately, because a queue-only fix
     is not a fix — the physician has the task id in the URL.
  2. **TRUNCATION IS A SERVER RESPONSIBILITY (§4.1).** The served payload is
     asserted to contain no offset greater than the decision point. Not "hidden",
     not "collapsed" — absent.
  3. **THE DENSITY GATE IS THE PRODUCT (§2).** ≥ 2 distinct dates, ≥ 8 events,
     ≥ 2 resource types, and the verifiable set is the qualifying set minus one.
  4. **PER-TRUNCATION MODALITIES (§4.2.1).** Inheriting the chart's declaration
     quarantines every early decision point with a clinical-sounding rejection
     for correct behaviour. The round-trip invariant is asserted directly.
  5. **THE κ POOL (§4.2.4).** Blinding does not make sequential labels
     independent. A blinded trajectory observation must not enter κ, and must not
     stamp ``independent_second_label`` on a shipped record.
  6. **PRICING (§9.6).** A trajectory point is single-labelled by default, on
     BOTH paths that lift capacity — the queue and the background sweep.

LLM stubbed throughout; nothing here needs a key.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import agreement as asc_agreement  # noqa: E402
from asclepius import ingestion as asc_ingestion  # noqa: E402
from asclepius import packaging as asc_packaging  # noqa: E402
from asclepius import pipeline as asc_pipeline  # noqa: E402
from asclepius import real_cases  # noqa: E402
from asclepius import routing as asc_routing  # noqa: E402
from asclepius import trajectory as asc_trajectory  # noqa: E402

client = TestClient(A.app)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    A.fresh_store()

    async def _ok_critic(task, submission):
        return {"consistent": True, "issues": [], "skipped": True}

    async def _ok_grounding(task, submission):
        return {"grounding_ok": True, "issues": [], "skipped": True, "checked_anchors": 0}

    monkeypatch.setattr(asc_pipeline, "run_critic", _ok_critic)
    monkeypatch.setattr(asc_pipeline, "run_grounding_check", _ok_grounding)
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


def _approved_user(store, role: str = "evaluator"):
    """A physician cleared for real de-identified data — the V4 wall is upstream of
    everything here, so a fixture that is not approved would 403 before any
    trajectory rule ran, and every assertion below would pass for the wrong reason."""
    user = A.make_user(store, role=role)
    store.set_real_data_approved(user["id"], True)
    return store.get_user_by_id(user["id"]) or user


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures — a chart with a real longitudinal shape
# ═══════════════════════════════════════════════════════════════════════════════
def _chart():
    """Four encounters at −120, −60, −30 and −5, each dense enough to qualify.

    Shaped after patient-1's ERCP admission: a cholestatic picture that gets
    drained, enzymes fall, then GGT climbs again — the §3.3 worked example, where
    the physician's own stated falsifier fires and the chart proves it.
    """
    panels, notes, studies = [], [], []
    for base, ggt in ((-120, 1361), (-60, 123), (-30, 983), (-5, 210)):
        for day in (base, base + 1, base + 2, base + 3):
            panels.append({
                "panel": "LFT", "collected_offset_days": day,
                "results": [
                    {"analyte": "GGT", "value": ggt, "unit": "U/L",
                     "ref_low": 5, "ref_high": 40, "flag": "H"},
                    {"analyte": "Bilirubin", "value": 15.04, "unit": "mg/dL",
                     "ref_low": 0.2, "ref_high": 1.2, "flag": "H"},
                ],
            })
            notes.append({"note_type": "Progress", "author_role": "gastroenterology",
                          "collected_offset_days": day,
                          "text": "Cholestatic picture; obstruction suspected."})
        studies.append({"modality": "ultrasound", "label": "Abdominal ultrasound",
                        "collected_offset_days": base + 1,
                        "findings": "Dilated intrahepatic ducts."})
    return {
        "case_source": "real_deid", "specialty": "gastroenterology",
        "demographics": {"age_band": "60-69", "sex": "M"},
        "lab_panels": panels, "notes": notes, "studies": studies,
        "medications": [{"drug": "ursodeoxycholic acid", "collected_offset_days": -119}],
        "problem_list": [{"condition": "Cholestasis", "collected_offset_days": -121}],
    }


def _traj_case(**over):
    base = {
        "case_source": "real_deid", "specialty": "gastroenterology",
        "demographics": {"age_band": "60-69", "sex": "M"},
        "lab_panels": [{"panel": "LFT", "collected_offset_days": -2, "results": [
            {"analyte": "GGT", "value": 1361, "unit": "U/L",
             "ref_low": 5, "ref_high": 40, "flag": "H"}]}],
        "notes": [{"note_type": "Progress", "author_role": "gastroenterology",
                   "collected_offset_days": -1, "text": "Cholestatic picture."}],
    }
    base.update(over)
    return base


def _walk(store, n=4, *, specialty="gastroenterology", max_labels=1, tid=None):
    """A generated chart walk: ``n`` ordered points sharing one trajectory_id."""
    tid = tid or asc_trajectory.new_trajectory_id()
    return tid, [
        store.insert_task(
            prompt=f"Decision point {i}: what now?", specialty=specialty,
            case=_traj_case(), max_labels=max_labels,
            generation={"index_event_offset": -120 + (i * 30)},
            trajectory_id=tid, sequence_index=i,
        )
        for i in range(n)
    ]


def _submit(store, task, evaluator_id, *, sub_id=None, verdict="A_better"):
    return store.insert_submission(
        submission_id=sub_id or f"s-{evaluator_id}-{task['task_id'][-6:]}",
        task_id=task["task_id"], evaluator_id=evaluator_id, verdict=verdict,
        chosen_id="a", rejected_id="b", confidence="high", time_spent_sec=300,
        payload={}, annotator={}, dedupe_hash=None,
    )


def _queue(store, evaluator_id, *, specialty="gastroenterology"):
    """Task ids the labeler queue would offer, in priority order."""
    sql, params = store.labeler_queue_sql(
        evaluator_id=evaluator_id, specialty=specialty, real_only=True)
    with store._conn() as conn:
        return [dict(r)["task_id"] for r in conn.execute(sql, params).fetchall()]


# ═══════════════════════════════════════════════════════════════════════════════
# §2 — the density gate is the product
# ═══════════════════════════════════════════════════════════════════════════════
def test_density_gate_thresholds_are_all_three_and_all_required():
    chart = _chart()
    encounters = real_cases.segment_longitudinal_record(chart)
    assert len(encounters) == 4
    for enc in encounters:
        q = real_cases.qualify_encounter(chart, enc)
        assert q["qualifies"], q
        assert q["n_distinct_dates"] >= real_cases.ENCOUNTER_MIN_DISTINCT_DATES
        assert q["n_events"] >= real_cases.ENCOUNTER_MIN_EVENTS
        assert q["n_resource_types"] >= real_cases.ENCOUNTER_MIN_RESOURCE_TYPES


def test_a_repeat_lab_draw_is_not_a_decision_point():
    """§2.1 — 34 of 59 real encounters fail this gate, and that is the point."""
    chart = {"lab_panels": [{"panel": "BMP", "collected_offset_days": -10,
                             "results": [{"analyte": "Creatinine", "value": 1.1}]}]}
    enc = real_cases.segment_longitudinal_record(chart)[0]
    q = real_cases.qualify_encounter(chart, enc)
    assert q["qualifies"] is False
    # The measurements travel with the verdict so an admin can argue with it.
    assert q["n_distinct_dates"] == 1 and q["n_events"] == 1
    assert len(q["reasons"]) == 3


def test_qualify_reports_which_threshold_was_missed():
    """Only the threshold that was actually missed is named — a gate that reports
    three reasons for one failure is a gate nobody can act on."""
    chart = _chart()
    enc = dict(real_cases.segment_longitudinal_record(chart)[0])
    enc["n_events"] = 4          # dense enough on dates and resource types, not events
    q = real_cases.qualify_encounter(chart, enc)
    assert q["qualifies"] is False
    assert q["reasons"] == ["4 recorded event(s); the gate is 8"]


def test_undated_chart_never_qualifies():
    enc = real_cases.segment_longitudinal_record({})[0]
    assert enc["undated"] is True
    assert real_cases.qualify_encounter({}, enc)["qualifies"] is False


def test_verifiable_set_is_the_qualifying_set_minus_one():
    """§2 — patient-1: 14 pass the gate, 13 are verifiable. Always n−1."""
    chart = _chart()
    encounters = real_cases.segment_longitudinal_record(chart)
    pairs = real_cases.pair_decision_points(chart, encounters)
    n_qualifying = sum(1 for e in encounters
                       if real_cases.qualify_encounter(chart, e)["qualifies"])
    assert n_qualifying == 4
    assert len(pairs) == n_qualifying - 1
    # Consecutive, oldest first, and the LAST qualifying encounter is never a
    # decision point: there is nothing later to check it against.
    assert [(p["decision_index"], p["outcome_index"]) for p in pairs] == [(0, 1), (1, 2), (2, 3)]


def test_outcome_is_the_next_QUALIFYING_encounter_not_merely_the_next_one():
    """A decision checked against a stray lab draw is checked against noise."""
    chart = _chart()
    # Splice a single-event contact between encounters 0 and 1.
    chart["lab_panels"].append({"panel": "BMP", "collected_offset_days": -95,
                                "results": [{"analyte": "Sodium", "value": 139}]})
    encounters = real_cases.segment_longitudinal_record(chart)
    stray = [e for e in encounters if e["start_offset"] == -95]
    assert stray and not real_cases.qualify_encounter(chart, stray[0])["qualifies"]
    pairs = real_cases.pair_decision_points(chart, encounters)
    # The stray contact is neither a decision point nor an outcome.
    assert stray[0]["index"] not in {p["decision_index"] for p in pairs}
    assert stray[0]["index"] not in {p["outcome_index"] for p in pairs}


# ═══════════════════════════════════════════════════════════════════════════════
# §4.2.1 — per-truncation modalities, the trap that quarantines everything
# ═══════════════════════════════════════════════════════════════════════════════
def test_modality_declaration_round_trips_through_the_completeness_check():
    """THE invariant. ``completeness_check`` quarantines on a RECOGNISED-and-absent
    token, so a declaration this function emits must always come back all-present.
    If this fails, every truncated case quarantines."""
    for case in (
        {},
        {"lab_panels": [{"panel": "LFT", "results": [{"analyte": "GGT"}]}]},
        {"notes": [{"note_type": "Progress", "text": "x"}]},
        {"medications": [{"drug": "ursodeoxycholic acid"}]},
        {"studies": [{"modality": "pathology",
                      "label": "ERCP procedure report with stent placement"}]},
        _chart(),
    ):
        declared = asc_ingestion.modalities_present_in(case)
        comp = asc_ingestion.completeness_check(declared, case)
        assert comp["missing"] == [], (declared, comp)
        assert comp["unresolved"] == [], (declared, comp)


def test_truncated_case_declares_only_what_the_window_carries():
    """Patient-1's ERCP report exists at day −1242; a case truncated at day −1810
    must not contain it and must not claim to. Inheriting the parent chart's
    declaration is what produces ``missing = ['ERCP procedure report']`` → a
    clinical-sounding quarantine for correct behaviour."""
    chart = _chart()
    chart["required_modalities"] = ["longitudinal labs", "clinical notes",
                                    "ERCP procedure report"]
    chart["studies"].append({"modality": "pathology",
                             "label": "ERCP procedure report",
                             "collected_offset_days": -5,
                             "findings": "Stent placed."})
    encounters = real_cases.segment_longitudinal_record(chart)
    early = encounters[0]
    visible, _held, _stats = real_cases.build_encounter_case(
        chart, early, early["offsets"][-1])
    declared = visible["required_modalities"]
    # NOT inherited: the ERCP report is not in this window.
    assert not any("ercp" in d.lower() for d in declared)
    assert asc_ingestion.completeness_check(declared, visible)["missing"] == []
    # And the late window, which does carry it, declares it.
    late = encounters[-1]
    late_visible, _h, _s = real_cases.build_encounter_case(
        chart, late, late["offsets"][-1])
    assert any("ercp" in d.lower() for d in late_visible["required_modalities"])


# ═══════════════════════════════════════════════════════════════════════════════
# §4.1 — truncation is a server responsibility
# ═══════════════════════════════════════════════════════════════════════════════
def test_no_item_after_the_decision_point_survives_into_the_visible_case():
    chart = _chart()
    encounters = real_cases.segment_longitudinal_record(chart)
    for enc in encounters:
        visible, _held, _stats = real_cases.build_encounter_case(
            chart, enc, enc["offsets"][-1])
        # The module's own assertion, plus an independent walk of the payload so a
        # bug in the assertion cannot hide a leak.
        real_cases.assert_temporal_split(visible)
        for key in ("lab_panels", "notes", "studies", "medications", "problem_list"):
            for item in visible.get(key) or []:
                assert item["collected_offset_days"] <= 0, (key, item)


def test_served_payload_carries_no_offset_past_the_decision_point():
    """§4.1 stated as a wire assertion: the CLIENT never receives what it is meant
    not to show. Absent from the payload — not hidden, not styled away."""
    store = _store()
    chart = _chart()
    enc = real_cases.segment_longitudinal_record(chart)[1]
    visible, _h, _s = real_cases.build_encounter_case(chart, enc, enc["offsets"][-1])
    task = store.insert_task(prompt="what now?", specialty="gastroenterology",
                             case=visible, max_labels=1)
    user = _approved_user(store)
    r = client.get(f"/api/asclepius/tasks/{task['task_id']}", headers=A.headers_for(user))
    assert r.status_code == 200, r.text

    offsets = []

    def _walk_payload(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "collected_offset_days" and isinstance(v, int):
                    offsets.append(v)
                _walk_payload(v)
        elif isinstance(node, list):
            for v in node:
                _walk_payload(v)

    _walk_payload(r.json())
    assert offsets, "the served case carried no timed items at all — check the fixture"
    assert max(offsets) <= 0, f"served payload leaked day +{max(offsets)}"


def test_outcome_delta_excludes_the_decision_day_itself():
    """An item recorded ON the decision day was visible at commitment and is not
    an outcome. Strict ``>``, so a physician cannot verify an expectation against
    a datum they had already read."""
    outcome_case = {
        "lab_panels": [
            {"panel": "LFT", "collected_offset_days": -30},   # parent −30, == decision
            {"panel": "LFT", "collected_offset_days": -10},   # parent −10, after
        ],
    }
    delta = real_cases.outcome_delta(
        outcome_case, outcome_index_offset=0, decision_index_offset=-30)
    days = [p["collected_offset_days"] for p in delta["lab_panels"]]
    assert days == [20]                      # −10 parent → +20 days after the decision
    assert delta["days_after_decision"] == 30


def test_outcome_delta_fails_closed_without_an_axis():
    with pytest.raises(real_cases.RealCaseError):
        real_cases.outcome_delta({"notes": []}, outcome_index_offset=None,
                                 decision_index_offset=-30)


# ═══════════════════════════════════════════════════════════════════════════════
# §9.1 — THE BLOCKER
# ═══════════════════════════════════════════════════════════════════════════════
def test_prd_9_1_priority_sort_scenario():
    """The exact four steps from §9.1, which the label-count-first sort produces
    the moment two physicians work one chart.

    1. A labels point 0.
    2. B labels point 2 — for any reason.
    3. A returns. Point 0 is excluded (they wrote it), and point 2 now carries a
       label, so under ``ORDER BY label_count DESC`` it SORTS FIRST.
    4. Without the gate, A is served point 2 — whose visible chart contains the
       outcome of point 1, the decision A was about to be asked to predict.
    """
    store = _store()
    tid, points = _walk(store, n=4)

    assert _queue(store, "A")[0] == points[0]["task_id"]

    _submit(store, points[0], "A")
    _submit(store, points[2], "B")

    # PRECONDITION — the bug is real. With point 0 removed by the independence
    # clause (A wrote it), the label-count-first sort ranks point 2 ahead of
    # points 1 and 3, which are still at zero. This is the production ORDER BY
    # with the sequence gate deliberately left out.
    with store._conn() as conn:
        ordered = [dict(r)["task_id"] for r in conn.execute(
            "SELECT t.task_id, "
            "(SELECT COUNT(*) FROM submissions s WHERE s.task_id = t.task_id "
            " AND s.verdict IS NOT NULL) AS n "
            "FROM tasks t WHERE t.trajectory_id = ? "
            "  AND NOT EXISTS (SELECT 1 FROM submissions sm "
            "                   WHERE sm.task_id = t.task_id AND sm.evaluator_id = 'A') "
            "ORDER BY n DESC, t.created_at ASC",
            (tid,)).fetchall()]
    assert ordered[0] == points[2]["task_id"], (
        "precondition: without the gate the priority sort serves A point 2 — "
        "whose chart contains the outcome of point 1")

    # …and the gate holds anyway. A is offered point 1 and nothing beyond it.
    served = _queue(store, "A")
    assert points[2]["task_id"] not in served
    assert points[3]["task_id"] not in served
    assert served == [points[1]["task_id"]]


def test_gate_advances_exactly_one_step_per_submission():
    store = _store()
    _tid, points = _walk(store, n=4)
    for i in range(4):
        assert _queue(store, "A") == [points[i]["task_id"]]
        _submit(store, points[i], "A")
    assert _queue(store, "A") == []


def test_gate_is_per_evaluator_not_per_chart():
    """B's progress must not unlock points for A, and A's must not for B."""
    store = _store()
    _tid, points = _walk(store, n=3)
    _submit(store, points[0], "A")
    _submit(store, points[1], "A")
    assert _queue(store, "A") == [points[2]["task_id"]]
    assert _queue(store, "B") == [points[0]["task_id"]]


def test_ordinary_tasks_are_unaffected_by_construction():
    """``trajectory_id IS NULL`` comes first in the clause, so every V1–V4 task
    short-circuits out of the subquery entirely."""
    store = _store()
    plain = [store.insert_task(prompt=f"ordinary {i}", specialty="gastroenterology",
                               case=_traj_case(), max_labels=1) for i in range(3)]
    served = _queue(store, "A")
    assert {t["task_id"] for t in plain} <= set(served)


def test_a_trajectory_row_with_no_position_is_never_served():
    """SQL three-valued logic would wave this through (``x < NULL`` is NULL), while
    the direct-open path refuses it. Two enforcements of one rule that disagree is
    worse than either alone."""
    store = _store()
    tid, points = _walk(store, n=2)
    with store._conn() as conn:
        conn.execute("UPDATE tasks SET sequence_index = NULL WHERE task_id = ?",
                     (points[1]["task_id"],))
    assert points[1]["task_id"] not in _queue(store, "A")
    broken = store.get_task(points[1]["task_id"])
    assert asc_trajectory.blocks_out_of_order(broken, unanswered_earlier=[]) is not None


def test_insert_task_refuses_half_a_trajectory_identity():
    store = _store()
    with pytest.raises(ValueError):
        store.insert_task(prompt="x", trajectory_id="traj-1")
    with pytest.raises(ValueError):
        store.insert_task(prompt="x", sequence_index=0)
    with pytest.raises(ValueError):
        store.insert_task(prompt="x", trajectory_id="traj-1", sequence_index=-1)


def test_second_label_draw_is_gated_too():
    """§9.6 makes double-walking rare, and rare is how an ordering bug survives."""
    store = _store()
    _tid, points = _walk(store, n=3, max_labels=2)
    _submit(store, points[0], "A")
    _submit(store, points[1], "A")
    _submit(store, points[2], "A")
    got = store.next_double_label_for("B", specialty="gastroenterology", allow_real=True)
    assert got is not None and got["task_id"] == points[0]["task_id"]


# ─── the direct-open path: a queue-only fix is not a fix ──────────────────────
def test_direct_open_of_an_out_of_order_point_is_409():
    store = _store()
    tid, points = _walk(store, n=3)
    user = _approved_user(store)
    h = A.headers_for(user)

    r = client.get(f"/api/asclepius/tasks/{points[2]['task_id']}", headers=h)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "trajectory_out_of_order"
    # 409, not 403: the physician is entitled to this case, just not yet — and the
    # response points them at the one they may open.
    assert detail["next_task_id"] == points[0]["task_id"]
    assert detail["n_points"] == 3

    # The first point opens.
    assert client.get(f"/api/asclepius/tasks/{points[0]['task_id']}",
                      headers=h).status_code == 200


def test_every_by_id_surface_is_gated_not_just_the_fetch():
    """reveal / answers / submit — the physician has the task id in the URL."""
    store = _store()
    _tid, points = _walk(store, n=3)
    user = _approved_user(store)
    h = A.headers_for(user)
    late = points[2]["task_id"]

    assert client.post(f"/api/asclepius/tasks/{late}/reveal",
                       json={"text": "my answer, written blind"},
                       headers=h).status_code == 409
    assert client.get(f"/api/asclepius/tasks/{late}/answers", headers=h).status_code == 409
    assert client.post("/api/asclepius/submissions", headers=h, json={
        "task_id": late, "verdict": "A_better", "chosen_id": "a",
        "rejected_id": "b", "confidence": "high", "time_spent_sec": 300,
    }).status_code == 409


def test_admins_are_not_exempt_from_the_seal():
    """The V4 wall exempts admins because the harm there is disclosure. Here the
    harm is that reading forward destroys the reader's own prediction."""
    store = _store()
    _tid, points = _walk(store, n=3)
    admin = _approved_user(store, role="admin")
    r = client.get(f"/api/asclepius/tasks/{points[2]['task_id']}",
                   headers=A.headers_for(admin))
    assert r.status_code == 409


# ═══════════════════════════════════════════════════════════════════════════════
# §9.6 — pricing: single-labelled by default, on BOTH capacity paths
# ═══════════════════════════════════════════════════════════════════════════════
def test_trajectory_points_are_not_double_labelled_by_the_queue():
    store = _store()
    _tid, points = _walk(store, n=2)
    task = store.get_task(points[0]["task_id"])
    # The launch rate is 1.0, so an ordinary real_deid task WOULD be lifted.
    ordinary = store.insert_task(prompt="ordinary", specialty="gastroenterology",
                                 case=_traj_case(), max_labels=1)
    assert asc_routing.wants_second_label(ordinary) is True
    assert asc_routing.wants_second_label(task) is False
    assert asc_routing.effective_capacity(task) == 1


def test_trajectory_points_are_not_double_labelled_by_the_background_sweep():
    """The sweep reaches ``should_double_label`` directly, so a guard placed only
    in ``routing`` would be silently undone a minute later."""
    store = _store()
    _tid, points = _walk(store, n=2)
    task = store.get_task(points[0]["task_id"])
    assert asc_agreement.should_double_label(task, current_rate=0.0) is False
    # …and specifically not via the real_deid rule, which it would otherwise match.
    assert task["case_source"] == "real_deid"

    _submit(store, points[0], "A")
    from asclepius import review as asc_review
    assert asc_review.sweep_double_label_routing(store) == 0
    assert int(store.get_task(points[0]["task_id"])["max_labels"]) == 1


def test_an_explicit_max_labels_2_is_still_honoured():
    """§9.6 removes the DEFAULT, not the ability. Double-walking is a priced
    decision an admin makes explicitly."""
    store = _store()
    _tid, points = _walk(store, n=2, max_labels=2)
    assert asc_routing.target_labels(points[0]) == 2
    assert asc_routing.wants_second_label(points[0]) is True
    assert asc_routing.effective_capacity(points[0]) == 2


# ═══════════════════════════════════════════════════════════════════════════════
# §4.2.4 — the κ pool
# ═══════════════════════════════════════════════════════════════════════════════
def test_a_blinded_trajectory_observation_does_not_enter_kappa():
    """The subtle one. Blinding is about not seeing the other labeler's identity;
    it says nothing about temporal independence."""
    store = _store()
    _tid, points = _walk(store, n=2)
    ordinary = store.insert_task(prompt="ordinary", specialty="gastroenterology",
                                 case=_traj_case(), max_labels=2)
    for task in (points[0], ordinary):
        store.upsert_agreement(
            task_id=task["task_id"], specialty="gastroenterology",
            sub_a="s1", sub_b="s2", verdict_a="A_better", verdict_b="A_better",
            tags_a=[], tags_b=[], jaccard_tags=1.0, verdict_agree=True,
            n_labels=2, flagged=False, blinded=True)

    obs = {o["task_id"]: o for o in store.list_agreement_observations()}
    traj_obs = obs[points[0]["task_id"]]
    plain_obs = obs[ordinary["task_id"]]
    # BOTH are blinded. Only one is excluded, and the reason is stored, not inferred.
    assert traj_obs["blinded"] == 1 and plain_obs["blinded"] == 1
    assert traj_obs["kappa_excluded_reason"] == asc_trajectory.KAPPA_EXCLUSION_SEQUENTIAL
    assert plain_obs["kappa_excluded_reason"] is None

    agg = asc_agreement.aggregate_kappa(list(obs.values()), min_n=1)
    assert agg["n"] == 1                      # only the ordinary case counted
    assert agg["excluded_trajectory"] == 1
    assert agg["exclusion_rationale"]


def test_the_exclusion_is_derived_by_the_store_not_remembered_by_a_caller():
    """"Excluded by construction" has to mean by construction: no caller passes it."""
    store = _store()
    _tid, points = _walk(store, n=1)
    store.upsert_agreement(
        task_id=points[0]["task_id"], specialty="gastroenterology",
        sub_a="s1", sub_b="s2", verdict_a="A_better", verdict_b="B_better",
        tags_a=[], tags_b=[], jaccard_tags=0.0, verdict_agree=False,
        n_labels=2, flagged=False, blinded=True)
    got = store.get_agreement_observation(points[0]["task_id"])
    assert got["kappa_excluded_reason"] == asc_trajectory.KAPPA_EXCLUSION_SEQUENTIAL


def test_a_kappa_excluded_record_never_claims_independent_second_label():
    """The flag means "in the slice a real κ is computed on". A record claiming
    membership of a slice it was excluded from is a false provenance claim."""
    excluded = asc_packaging.supervision_block(
        labeler_id_hashed="h1",
        observation={"blinded": 1,
                     "kappa_excluded_reason": asc_trajectory.KAPPA_EXCLUSION_SEQUENTIAL})
    assert excluded["independent_second_label"] is False
    assert excluded["kappa_excluded_reason"] == asc_trajectory.KAPPA_EXCLUSION_SEQUENTIAL

    ordinary = asc_packaging.supervision_block(
        labeler_id_hashed="h1", observation={"blinded": 1})
    assert ordinary["independent_second_label"] is True
    assert ordinary["kappa_excluded_reason"] is None


# ═══════════════════════════════════════════════════════════════════════════════
# §3.3 / §3.4 — the falsifier and the self-score
# ═══════════════════════════════════════════════════════════════════════════════
def test_expected_trajectory_normalization_keeps_only_usable_predictions():
    got = asc_trajectory.normalize_expected_trajectory({
        "expectations": [
            {"expectation": "enzymes stay down and bilirubin falls", "horizon_days": 21},
            {"expectation": "yes"},                       # too short — dropped
            "GGT remains below 200",                      # bare string — accepted
        ],
        "falsifiers": ["GGT climbs again above 500", "no"],
        "note": "  drainage   worked  ",
    })
    assert len(got["expectations"]) == 2
    assert got["expectations"][0]["horizon_days"] == 21
    assert got["falsifiers"] == ["GGT climbs again above 500"]
    assert got["falsifiable"] is True
    assert got["note"] == "drainage worked"


def test_a_prediction_with_no_falsifier_is_stored_and_marked_unfalsifiable():
    """§7 prices the falsifier corpus, so it must be filterable — not silently
    diluted by predictions that carry no falsifier."""
    got = asc_trajectory.normalize_expected_trajectory({
        "expectations": [{"expectation": "the patient improves over two weeks"}]})
    assert got["falsifiable"] is False
    assert got["falsifiers"] == []


def test_an_empty_prediction_is_None_and_that_is_not_an_error():
    assert asc_trajectory.normalize_expected_trajectory({}) is None
    assert asc_trajectory.normalize_expected_trajectory(None) is None
    assert asc_trajectory.normalize_expected_trajectory({"expectations": []}) is None


def test_horizon_is_clamped_not_rejected():
    got = asc_trajectory.normalize_expected_trajectory({
        "expectations": [{"expectation": "bilirubin falls steadily", "horizon_days": 99999}]})
    assert got["expectations"][0]["horizon_days"] == 400
    got = asc_trajectory.normalize_expected_trajectory({
        "expectations": [{"expectation": "bilirubin falls steadily", "horizon_days": "soon"}]})
    assert got["expectations"][0]["horizon_days"] is None


def test_self_score_drops_marks_that_do_not_line_up_with_the_prediction():
    got = asc_trajectory.normalize_self_score(
        {"marks": [{"index": 0, "state": "held"},
                   {"index": 5, "state": "held"},          # past the prediction
                   {"index": 1, "state": "invented"},      # not a state
                   {"index": 0, "state": "did_not_hold"}], # duplicate index
         "falsifier_fired": True},
        n_expectations=2)
    assert [m["index"] for m in got["marks"]] == [0]
    assert got["n_held"] == 1
    assert got["falsifier_fired"] is True
    assert got["verified"] is True


def test_all_not_assessable_is_not_a_verified_outcome():
    """The RLVR claim must not be made where nothing was actually checkable."""
    got = asc_trajectory.normalize_self_score(
        {"marks": [{"index": 0, "state": "not_assessable"},
                   {"index": 1, "state": "not_assessable"}]},
        n_expectations=2)
    assert got["n_not_assessable"] == 2
    assert got["verified"] is False


def test_outcome_verification_reports_honest_denominators():
    ov = asc_trajectory.outcome_verification([
        {"expected_trajectory": {"falsifiable": True},
         "self_score": {"verified": True, "n_held": 2, "n_did_not_hold": 1,
                        "n_not_assessable": 1, "falsifier_fired": True}},
        {"expected_trajectory": {"falsifiable": False}, "self_score": None},
        {"expected_trajectory": None, "self_score": None},
    ])
    assert ov["n_points"] == 3
    assert ov["n_points_verified"] == 1
    assert ov["n_points_with_falsifier"] == 1
    assert ov["anticipation_rate"] == round(2 / 3, 4)
    assert ov["n_falsifiers_fired"] == 1


def test_anticipation_rate_is_None_not_zero_when_nothing_was_assessable():
    ov = asc_trajectory.outcome_verification([
        {"expected_trajectory": {"falsifiable": True},
         "self_score": {"verified": False, "n_held": 0, "n_did_not_hold": 0,
                        "n_not_assessable": 3}}])
    assert ov["anticipation_rate"] is None


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 4 — the reveal is sealed until the action is committed
# ═══════════════════════════════════════════════════════════════════════════════
def test_the_outcome_is_sealed_until_a_submission_exists():
    """§3.2 — if the physician can see the future, the task collapses into
    narration. The seal converts an opinion into a prediction."""
    store = _store()
    _tid, points = _walk(store, n=2)
    user = _approved_user(store)
    r = client.get(f"/api/asclepius/tasks/{points[0]['task_id']}/trajectory-outcome",
                   headers=A.headers_for(user))
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "commitment_required"


def test_the_reveal_shows_only_what_came_after_the_decision():
    store = _store()
    tid = asc_trajectory.new_trajectory_id()
    early = store.insert_task(
        prompt="point 0", specialty="gastroenterology", max_labels=1,
        case=_traj_case(lab_panels=[{"panel": "LFT", "collected_offset_days": -2,
                                     "results": [{"analyte": "GGT", "value": 1361}]}]),
        generation={"index_event_offset": -60}, trajectory_id=tid, sequence_index=0)
    store.insert_task(
        prompt="point 1", specialty="gastroenterology", max_labels=1,
        case=_traj_case(lab_panels=[
            # parent −62: BEFORE the decision at −60, must not be revealed.
            {"panel": "LFT", "collected_offset_days": -32,
             "results": [{"analyte": "GGT", "value": 1361}]},
            # parent −40: after the decision, IS the outcome.
            {"panel": "LFT", "collected_offset_days": -10,
             "results": [{"analyte": "GGT", "value": 983}]},
        ]),
        generation={"index_event_offset": -30}, trajectory_id=tid, sequence_index=1)

    user = _approved_user(store)
    _submit(store, early, user["id"])
    r = client.get(f"/api/asclepius/tasks/{early['task_id']}/trajectory-outcome",
                   headers=A.headers_for(user))
    assert r.status_code == 200, r.text
    body = r.json()
    days = [p["collected_offset_days"] for p in body["outcome"]["lab_panels"]]
    assert days == [20]                       # parent −40 → +20 days after the decision
    assert body["outcome"]["days_after_decision"] == 30
    # §6 is in front of the physician at the moment they grade, not only in the
    # data dictionary a buyer reads.
    assert any(l["limitation"] == "not_a_controlled_experiment"
               for l in body["limitations"])


def test_the_terminal_point_says_so_rather_than_returning_an_empty_panel():
    store = _store()
    _tid, points = _walk(store, n=2)
    user = _approved_user(store)
    _submit(store, points[1], user["id"])
    r = client.get(f"/api/asclepius/tasks/{points[1]['task_id']}/trajectory-outcome",
                   headers=A.headers_for(user))
    assert r.status_code == 200
    assert r.json()["outcome"] is None
    assert "last decision point" in r.json()["reason"]


def test_a_hole_in_the_walk_does_not_make_the_prior_point_look_terminal():
    """Generation is per-point isolated, so a walk can have a gap. Matching
    ``idx + 1`` exactly would silently drop a verifiable point from the corpus."""
    store = _store()
    tid, points = _walk(store, n=3)
    with store._conn() as conn:
        conn.execute("DELETE FROM tasks WHERE task_id = ?", (points[1]["task_id"],))
    user = _approved_user(store)
    _submit(store, points[0], user["id"])
    r = client.get(f"/api/asclepius/tasks/{points[0]['task_id']}/trajectory-outcome",
                   headers=A.headers_for(user))
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] is not None
    assert r.json()["outcome_task_id"] == points[2]["task_id"]


def test_self_score_requires_a_stored_prediction_to_grade():
    store = _store()
    _tid, points = _walk(store, n=2)
    user = _approved_user(store)
    _submit(store, points[0], user["id"])
    r = client.post(f"/api/asclepius/tasks/{points[0]['task_id']}/trajectory-self-score",
                    json={"marks": [{"index": 0, "state": "held"}]},
                    headers=A.headers_for(user))
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "no_prediction_to_score"


def test_the_full_commit_reveal_score_loop():
    """§3.3's worked example, end to end: the physician's own stated falsifier
    fires, and the chart proves it. No human graded that."""
    store = _store()
    tid = asc_trajectory.new_trajectory_id()
    early = store.insert_task(
        prompt="Bilirubin 15.04 → 17.77, GGT 1361 → 123. What now?",
        specialty="gastroenterology", max_labels=1, case=_traj_case(),
        generation={"index_event_offset": -60}, trajectory_id=tid, sequence_index=0)
    store.insert_task(
        prompt="point 1", specialty="gastroenterology", max_labels=1,
        case=_traj_case(lab_panels=[{"panel": "LFT", "collected_offset_days": -5,
                                     "results": [{"analyte": "GGT", "value": 983}]}]),
        generation={"index_event_offset": -30}, trajectory_id=tid, sequence_index=1)

    user = _approved_user(store)
    h = A.headers_for(user)
    r = client.post("/api/asclepius/submissions", headers=h, json={
        "task_id": early["task_id"], "verdict": "A_better", "chosen_id": "a",
        "rejected_id": "b", "confidence": "high", "time_spent_sec": 900,
        "expected_trajectory": {
            "expectations": [
                {"expectation": "enzymes stay down and bilirubin falls over 2-3 weeks",
                 "horizon_days": 21}],
            "falsifiers": ["if GGT climbs again the stent has occluded"],
        },
    })
    assert r.status_code == 200, r.text
    sid = r.json()["submission_id"]
    stored = store.get_submission(sid)
    # The column and the payload carry the SAME normalized object.
    assert stored["expected_trajectory"]["falsifiable"] is True
    assert stored["payload"]["expected_trajectory"] == stored["expected_trajectory"]

    assert client.get(
        f"/api/asclepius/tasks/{early['task_id']}/trajectory-outcome",
        headers=h).status_code == 200

    r = client.post(f"/api/asclepius/tasks/{early['task_id']}/trajectory-self-score",
                    headers=h, json={
                        "marks": [{"index": 0, "state": "did_not_hold",
                                   "note": "GGT back to 983 — the stent occluded"}],
                        "falsifier_fired": True})
    assert r.status_code == 200, r.text
    score = r.json()["self_score"]
    assert score["n_did_not_hold"] == 1 and score["falsifier_fired"] is True
    assert score["verified"] is True

    # And it reaches the metric — under its own name, never κ.
    ov = asc_trajectory.outcome_verification(store.trajectory_verification_points())
    assert ov["n_points_verified"] == 1
    assert ov["n_falsifiers_fired"] == 1
    assert ov["anticipation_rate"] == 0.0     # one expectation, and it did not hold


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 5 — the session view, and §4.2.5 reassembly
# ═══════════════════════════════════════════════════════════════════════════════
def test_the_session_view_never_serves_case_content():
    """A progress bar that renders every point's chart is the leak wearing a
    different hat."""
    store = _store()
    tid, points = _walk(store, n=3)
    user = _approved_user(store)
    r = client.get(f"/api/asclepius/trajectories/{tid}", headers=A.headers_for(user))
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["points"]) == 3
    for p in body["points"]:
        assert set(p) == {"task_id", "sequence_index", "difficulty", "answered",
                          "openable", "outcome_verifiable"}
    # Only the first point is openable; a walk of 3 has 2 verifiable points.
    assert [p["openable"] for p in body["points"]] == [True, False, False]
    assert [p["outcome_verifiable"] for p in body["points"]] == [True, True, False]
    assert body["progress"]["n_answered"] == 0


def test_progress_is_per_evaluator():
    store = _store()
    tid, points = _walk(store, n=3)
    a = _approved_user(store)
    b = _approved_user(store)
    _submit(store, points[0], a["id"])
    pa = client.get(f"/api/asclepius/trajectories/{tid}",
                    headers=A.headers_for(a)).json()["progress"]
    pb = client.get(f"/api/asclepius/trajectories/{tid}",
                    headers=A.headers_for(b)).json()["progress"]
    assert pa["n_answered"] == 1 and pa["next_task_id"] == points[1]["task_id"]
    assert pb["n_answered"] == 0 and pb["next_task_id"] == points[0]["task_id"]


def test_the_served_task_carries_the_reassembly_key():
    store = _store()
    tid, points = _walk(store, n=3)
    user = _approved_user(store)
    got = client.get(f"/api/asclepius/tasks/{points[0]['task_id']}",
                     headers=A.headers_for(user)).json()["task"]
    assert got["trajectory_id"] == tid
    assert got["sequence_index"] == 0


def test_available_list_shows_only_the_next_openable_point():
    """The dashboard must not advertise a card the next click refuses."""
    store = _store()
    _tid, points = _walk(store, n=4)
    user = _approved_user(store)
    rows = client.get("/api/asclepius/tasks/available?portal_version=v4",
                      headers=A.headers_for(user)).json()["tasks"]
    traj = [t for t in rows if t.get("trajectory_id")]
    assert [t["task_id"] for t in traj] == [points[0]["task_id"]]
    assert traj[0]["sequence_index"] == 0


def test_the_export_annex_carries_the_reassembly_key_and_the_falsifier():
    """§4.2.5 — a buyer who cannot reassemble the sequence has bought thirteen
    single-shot cases at a trajectory price."""
    block = asc_packaging.trajectory_block(
        {"trajectory_id": "traj-abc", "sequence_index": 4},
        {"expected_trajectory": {"expectations": [{"expectation": "x y z"}],
                                 "falsifiers": ["a b c"], "falsifiable": True},
         "trajectory_self_score": {"verified": True, "falsifier_fired": True,
                                   "n_held": 1}})
    assert block["trajectory_id"] == "traj-abc"
    assert block["sequence_index"] == 4
    assert block["outcome_verified"] is True
    assert block["falsifier_fired"] is True


def test_the_annex_is_absent_where_it_would_mean_nothing():
    assert asc_packaging.trajectory_block({"task_id": "t1"}, {"payload": {}}) is None


def test_the_data_dictionary_documents_every_shipped_trajectory_field():
    """An undocumented field in a delivered artifact is indistinguishable from a
    leak — the file says so itself."""
    from asclepius.export import _data_dictionary_md
    doc = _data_dictionary_md("default")
    for field in ("trajectory.trajectory_id", "trajectory.sequence_index",
                  "trajectory.expected_trajectory.falsifiers",
                  "trajectory.expected_trajectory.falsifiable",
                  "trajectory.self_score.marks", "trajectory.outcome_verified",
                  "supervision.kappa_excluded_reason"):
        assert field in doc, f"{field} ships but is not in the data dictionary"
    # §6, stated where a buyer's methodologist will look for it.
    for claim in ("Survivorship", "counterfactual", "study_findings_policy",
                  "Yield per chart is not predictable"):
        assert claim in doc
