"""Audit C follow-up — the findings from `AUDIT_C_credentialing.md`, each named.

The audit found no criticals. Order of work is H1, H2 → M3 → M1 → M2 → UI → M4/LOW, and each
test below names the finding it closes so a future reader can trace the fix to the report.

Two of these are fairness findings about features that reach the score and are
demographic-adjacent but were outside the counsel memo. They are not style issues: a feature
worth −0.80 that correlates with caregiving status is an adverse-impact exposure whether or
not anyone intended it.
"""

from __future__ import annotations

import ast
import json
import pathlib

import pytest
from fastapi.testclient import TestClient

from tests._asclepius import app, fresh_store, headers_for, make_user

from asclepius import tiering

from tests.test_tiering_score import (
    VALID_NPI, _credentials, _passed_calibration, _physician,
    seat_passing_calibration, seed_calibration_items,
)

_TIERING_PY = pathlib.Path(tiering.__file__)


def _score(store, user, domain="nephrology"):
    return tiering.propose(
        user, case_domain=domain, calibration=_passed_calibration(),
        leie_status=store.leie_status(VALID_NPI))


# ═══════════════════════════════════════════════════════════════════════════════
# H1 — currently_practicing was an unpinned sex/caregiving/disability proxy
# ═══════════════════════════════════════════════════════════════════════════════
# The encoding was `1.0 if half_days >= 4 else 0.0`: a hard binary cliff at part-time
# practice, worth −0.80, the third-largest term in the model. Part-time clinical practice is
# not evenly distributed by sex, caregiving status, or disability, and a physician on parental
# or medical leave scored identically to one who had left medicine.

def test_h1_part_time_practice_does_not_fall_off_a_cliff():
    """A physician doing 3 clinical half-days a month is practising. The old encoding scored
    them the same as someone who sees no patients at all."""
    store = fresh_store()
    full = _physician(store, creds=_credentials(clinicalHalfDaysPerMonth=8))
    part = _physician(store, creds=_credentials(clinicalHalfDaysPerMonth=3))
    none = _physician(store, creds=_credentials(clinicalHalfDaysPerMonth=0,
                                                practiceStatus="not_practising"))

    v_full = _score(store, full)["features"]["currently_practicing"]
    v_part = _score(store, part)["features"]["currently_practicing"]
    v_none = _score(store, none)["features"]["currently_practicing"]

    assert v_full == 1.0
    assert v_none == 0.0
    # Part-time sits between, and strictly nearer full practice than no practice at all.
    assert 0.0 < v_part < 1.0
    assert v_part >= 0.5

    penalty = _score(store, full)["score"] - _score(store, part)["score"]
    full_weight = tiering.FEATURES["currently_practicing"][0]
    assert penalty <= full_weight / 2 + 1e-9, (
        f"part-time still costs {penalty:.2f} of a {full_weight} weight")


def test_h1_protected_leave_is_not_disengagement():
    """Parental, medical, military or caregiving leave is a temporary absence from a practice,
    not a departure from medicine. Scoring it as departure is the adverse-impact hazard."""
    store = fresh_store()
    active = _physician(store, creds=_credentials(clinicalHalfDaysPerMonth=8))
    on_leave = _physician(store, creds=_credentials(
        clinicalHalfDaysPerMonth=0, practiceStatus="on_leave",
        halfDaysBeforeLeave=8))

    assert (_score(store, on_leave)["features"]["currently_practicing"]
            == _score(store, active)["features"]["currently_practicing"])
    assert _score(store, on_leave)["score"] == _score(store, active)["score"]


def test_h1_leave_with_no_prior_figure_is_not_punished_for_the_blank():
    """Someone who ticks 'on leave' and leaves the pre-leave number blank must not be scored
    as if they had answered zero. A missing answer is not a negative answer — the same
    tri-state discipline the gates use."""
    store = fresh_store()
    on_leave = _physician(store, creds=_credentials(
        clinicalHalfDaysPerMonth="", practiceStatus="on_leave"))
    assert _score(store, on_leave)["features"]["currently_practicing"] >= 0.5


def test_h1_genuinely_not_practising_still_scores_zero():
    """The feature must keep the signal it exists for. Someone who has left clinical practice
    has illness scripts that are no longer current, and that is a real, non-protected fact."""
    store = fresh_store()
    retired = _physician(store, creds=_credentials(
        clinicalHalfDaysPerMonth=0, practiceStatus="not_practising"))
    assert _score(store, retired)["features"]["currently_practicing"] == 0.0


def test_h1_the_practice_status_field_is_declared_to_the_encoder():
    assert "practiceStatus" in tiering.ENCODER_CREDENTIAL_KEYS
    assert "halfDaysBeforeLeave" in tiering.ENCODER_CREDENTIAL_KEYS


def test_h1_and_h2_are_named_in_the_counsel_memo():
    """AUDIT H1/H2: "add it to the counsel memo... It belongs in departure §7, not outside the
    document." A fairness exposure that only the code knows about is not disclosed."""
    memo = (_TIERING_PY.parents[2] / "docs" / "PRD_C_COUNSEL_MEMO.md").read_text("utf-8")
    for token in ("currently_practicing", "structured_review_exp",
                  "caregiving", "part-time", "IMG"):
        assert token in memo, f"counsel memo does not mention {token!r}"
    review = (_TIERING_PY.parents[2] / "docs" / "PRD_C_PROCESS_REVIEW.md").read_text("utf-8")
    assert "currently_practicing" in review
    assert "structured_review_exp" in review


# ═══════════════════════════════════════════════════════════════════════════════
# H2 — structured_review_exp is a route around the img_status pin
# ═══════════════════════════════════════════════════════════════════════════════
# CEC/DSMB, journal peer review, board item writing, guideline panel, core faculty and PD are
# all gated on academic-medical-center affiliation, which is strongly associated with IMG
# status and national origin. Both of those are pinned to zero; this feature reaches the score
# at −0.70 and correlates with them. It cannot be pinned without gutting the feature, so it is
# MONITORED instead — which means the monitor has to be able to see it.

def test_h2_fairness_monitor_breaks_down_by_feature_not_only_by_tier():
    store = fresh_store()
    # Group A: academic, all have structured review experience, all made reviewer.
    # Group B: community, none do. The monitor must surface the feature gap, not only the
    # tier gap — otherwise the mechanism is invisible and only the outcome shows.
    for i in range(8):
        store.record_fairness_observation(user_id=f"a{i}", demographics={"img": "no"})
        store.stamp_fairness_tier(f"a{i}", "reviewer",
                                  features={"structured_review_exp": 1.0,
                                            "domain_match": 1.0})
    for i in range(8):
        store.record_fairness_observation(user_id=f"b{i}", demographics={"img": "yes"})
        store.stamp_fairness_tier(f"b{i}", "labeler",
                                  features={"structured_review_exp": 0.0,
                                            "domain_match": 1.0})

    rates = store.fairness_selection_rates()
    by_feature = rates["by_feature"]["img"]
    assert by_feature["yes"]["structured_review_exp"]["mean"] == 0.0
    assert by_feature["no"]["structured_review_exp"]["mean"] == 1.0
    # domain_match is identical across groups, so it must NOT be flagged — a monitor that
    # flags everything is a monitor nobody reads.
    flagged = {f["feature"] for f in rates["feature_alerts"]}
    assert "structured_review_exp" in flagged
    assert "domain_match" not in flagged


def test_h2_feature_breakdown_needs_no_join_back_to_users():
    """Same structural guarantee as the tier: the feature vector is COPIED onto the
    pseudonymous row at decision time. If the monitor had to join `users` to read features,
    the non-joinability of the demographics table would be a naming convention again."""
    store = fresh_store()
    doc = _physician(store)
    store.record_fairness_observation(user_id=doc["id"], demographics={"img": "yes"})
    store.stamp_fairness_tier(doc["id"], "labeler",
                              features={"structured_review_exp": 1.0})
    rows = store.fairness_observations()
    assert rows[0]["features"]["structured_review_exp"] == 1.0
    assert "subject_key" not in rows[0]
    # And nothing about the physician's demographics is on the users row.
    assert "yes" not in json.dumps(store.get_user_by_id(doc["id"]), default=str)


def test_h2_approval_copies_the_feature_vector_onto_the_fairness_row():
    store = fresh_store()
    doc = _physician(store)
    seat_passing_calibration(store, doc)
    store.record_fairness_observation(user_id=doc["id"], demographics={"img": "yes"})
    store.set_verification_status(doc["id"], "pending")
    admin = make_user(store, role="admin")
    r = TestClient(app).post(f"/api/asclepius/verify/queue/{doc['id']}/approve",
                             json={"tier": "reviewer"}, headers=headers_for(admin))
    assert r.status_code == 200, r.text
    row = store.fairness_observations()[0]
    assert row["decided_tier"] == "reviewer"
    assert row["features"]["domain_match"] == 1.0


# ═══════════════════════════════════════════════════════════════════════════════
# M3 — one NaN silently kills a batch's learning AND consumes it
# ═══════════════════════════════════════════════════════════════════════════════

def _clean_features(**over):
    vec = {"intercept": 1.0, "board_certified_active": 1.0, "subspecialty_certified": 1.0,
           "domain_match": 1.0, "post_residency_ge_3yr": 1.0, "currently_practicing": 1.0,
           "continuing_cert": 1.0, "structured_review_exp": 1.0, "calibration_z": 0.5,
           "measured_quality_z": 0.0}
    vec.update(over)
    return vec


def test_m3_one_nan_row_does_not_kill_the_batch_or_get_consumed():
    """`isinstance(v, (int, float))` lets `float('nan')` through, and it round-trips through
    JSON. It propagates into _solve, the Armijo test is False for NaN so all 30 backtracks
    fail, the loop breaks at the first step leaving w = m0 — and then every decision in the
    batch is stamped applied. The observations are consumed forever with zero learning, and
    the only signal is the log line that cannot distinguish this from convergence."""
    store = fresh_store()
    before = store.get_tiering_weights()
    for i in range(40):
        store.record_tiering_decision(
            user_id=f"ok{i}", case_domain="nephrology",
            features=_clean_features(domain_match=1.0 if i % 2 else 0.0),
            proposed_tier=tiering.TL,
            admin_tier=tiering.TR if i % 2 else tiering.TL)
    poison = store.record_tiering_decision(
        user_id="poison", case_domain="nephrology",
        features=_clean_features(calibration_z=float("nan")),
        proposed_tier=tiering.TL, admin_tier=tiering.TR)

    result = tiering.apply_decision_batch(store)

    # The 40 healthy rows still taught the model something.
    assert result["applied"] == 40
    after = store.get_tiering_weights()
    assert any(abs(after[k]["m"] - before[k]["m"]) > 1e-6
               for k in after if k not in tiering.PINNED_ZERO)
    # The poison row is quarantined, reported, and NOT consumed — it is recoverable.
    assert result["quarantined"] == 1
    assert poison["decision_id"] in result["quarantined_ids"]
    assert any(r["decision_id"] == poison["decision_id"]
               for r in store.pending_tiering_decisions())


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_m3_every_non_finite_value_is_quarantined(bad):
    store = fresh_store()
    store.record_tiering_decision(
        user_id="u1", case_domain=None, features=_clean_features(domain_match=bad),
        proposed_tier=tiering.TL, admin_tier=tiering.TR)
    result = tiering.apply_decision_batch(store)
    assert result["applied"] == 0
    assert result["quarantined"] == 1
    assert len(store.pending_tiering_decisions()) == 1


def test_m3_a_wholly_poisoned_batch_stamps_nothing():
    """The failure mode this guards: a batch that learns nothing AND loses everything."""
    store = fresh_store()
    snapshot = {k: dict(v) for k, v in store.get_tiering_weights().items()}
    for i in range(5):
        store.record_tiering_decision(
            user_id=f"u{i}", case_domain=None,
            features=_clean_features(calibration_z=float("nan")),
            proposed_tier=tiering.TL, admin_tier=tiering.TL)
    result = tiering.apply_decision_batch(store)
    assert result["applied"] == 0 and result["quarantined"] == 5
    assert store.get_tiering_weights() == snapshot
    assert len(store.pending_tiering_decisions()) == 5


def test_m3_quarantine_is_visible_over_http():
    """A quarantined row that only appears in a log line is a row nobody fixes."""
    store = fresh_store()
    admin = make_user(store, role="admin")
    store.record_tiering_decision(
        user_id="u1", case_domain=None, features=_clean_features(domain_match=float("nan")),
        proposed_tier=tiering.TL, admin_tier=tiering.TR)
    r = TestClient(app).post("/api/asclepius/verify/tiering-weights/apply",
                             headers=headers_for(admin))
    assert r.status_code == 200, r.text
    assert r.json()["quarantined"] == 1


# ═══════════════════════════════════════════════════════════════════════════════
# M1 — there are TWO day-one TR blockers; the docs named one
# ═══════════════════════════════════════════════════════════════════════════════

def test_m1_readiness_endpoint_names_both_blockers():
    """Loading a LEIE snapshot and keying 12 exam items are two separate operational loops
    with two separate owners. A launch plan that resolves only the item bank ships zero TRs."""
    store = fresh_store()
    admin = make_user(store, role="admin")
    client = TestClient(app)

    r = client.get("/api/asclepius/verify/readiness", headers=headers_for(admin))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tr_possible"] is False
    blockers = {b["id"]: b for b in body["blockers"]}
    assert set(blockers) == {"leie_snapshot", "calibration_item_bank"}
    assert all(b["blocking"] for b in blockers.values())
    # Each names an owner and a concrete action, or it is not a checklist item.
    for b in blockers.values():
        assert b["action"] and b["owner"]

    # Resolve ONE. Still zero TRs — that is the whole finding.
    store.replace_leie_exclusions([{"npi": "9999999999"}], source_note="t")
    body = client.get("/api/asclepius/verify/readiness", headers=headers_for(admin)).json()
    assert body["tr_possible"] is False
    assert [b["id"] for b in body["blockers"] if b["blocking"]] == ["calibration_item_bank"]

    # Resolve BOTH.
    seed_calibration_items(store)
    body = client.get("/api/asclepius/verify/readiness", headers=headers_for(admin)).json()
    assert body["tr_possible"] is True
    assert [b for b in body["blockers"] if b["blocking"]] == []


def test_m1_both_blockers_are_on_the_launch_checklist_doc():
    docs = _TIERING_PY.parents[2] / "docs"
    checklist = (docs / "PRD_C_LAUNCH_CHECKLIST.md").read_text("utf-8")
    assert "LEIE" in checklist and "calibration" in checklist.lower()
    readme = (docs / "PRD_C_PHASE0_AUDIT.md").read_text("utf-8")
    assert "PRD_C_LAUNCH_CHECKLIST" in readme


def test_m1_a_physician_with_everything_else_is_still_blocked_by_the_leie_gap():
    """The mechanism, not just the doc: propose() short-circuits on an undetermined gate, so
    an unloaded exclusion list holds back a physician who has passed the exam."""
    store = fresh_store()
    doc = _physician(store)
    seat_passing_calibration(store, doc)
    admin = make_user(store, role="admin")
    client = TestClient(app)

    with_leie = client.get(f"/api/asclepius/verify/tiering/{doc['id']}",
                           headers=headers_for(admin)).json()
    assert with_leie["proposed_tier"] == "reviewer"

    # Now wipe the snapshot — the state every fresh deploy is in.
    with store._conn() as conn:
        conn.execute("DELETE FROM leie_meta")
    without = client.get(f"/api/asclepius/verify/tiering/{doc['id']}",
                         headers=headers_for(admin)).json()
    assert without["proposed_tier"] is None
    assert without["gates"]["A5"]["state"] == tiering.UNKNOWN


# ═══════════════════════════════════════════════════════════════════════════════
# M4 — _solve returned silent garbage on a singular system
# ═══════════════════════════════════════════════════════════════════════════════

def test_m4_a_singular_system_raises_rather_than_returning_garbage():
    """`_solve([[0,0],[0,0]], [1,1])` returned `[1e9, 1e9]` with no exception. Harmless today
    because the Hessian is PD by construction, but the property is load-bearing: it is what
    lets fit_batch trust the Newton direction."""
    with pytest.raises(tiering.SingularSystem):
        tiering._solve([[0.0, 0.0], [0.0, 0.0]], [1.0, 1.0])
    with pytest.raises(tiering.SingularSystem):
        tiering._solve([[1.0, 2.0], [2.0, 4.0]], [1.0, 2.0])   # rank-deficient
    # A well-conditioned system still solves.
    assert tiering._solve([[2.0, 1.0], [1.0, 3.0]], [5.0, 10.0]) == pytest.approx([1.0, 3.0])


def test_m4_a_singular_hessian_leaves_the_prior_untouched_instead_of_crashing():
    """fit_batch must degrade to 'learn nothing from this batch' rather than 500 — and must
    say so, because silently learning nothing is the §8 failure."""
    store = fresh_store()
    before = store.get_tiering_weights()
    weights = {"intercept": {"m": 0.0, "q": 0.0, "pinned": 0},
               "domain_match": {"m": 0.0, "q": 0.0, "pinned": 0}}
    out = tiering.fit_batch(weights, [({"intercept": 0.0, "domain_match": 0.0}, 1, 1.0)])
    assert out["intercept"]["m"] == 0.0
    assert store.get_tiering_weights() == before


# ═══════════════════════════════════════════════════════════════════════════════
# LOW — the encoder guard was a declaration check, not a read check
# ═══════════════════════════════════════════════════════════════════════════════

_ENCODER_ENTRY_POINTS = ("feature_vector", "domain_match")


def _score_path_functions() -> dict:
    """Every module-level function reachable from the encoder entry points, transitively.

    Following the call graph is the difference between a real check and a decorative one: the
    part-time-practice fix (H1) moved every `creds.get(...)` out of `feature_vector` and into
    `_practising_value`, so a walk that stopped at the entry points would have gone from
    "reads nine declared keys" to "reads nothing" and passed for the wrong reason.
    """
    tree = ast.parse(_TIERING_PY.read_text("utf-8"))
    defs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    seen, stack = {}, [f for f in _ENCODER_ENTRY_POINTS if f in defs]
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen[name] = defs[name]
        for call in ast.walk(defs[name]):
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
                if call.func.id in defs and call.func.id not in seen:
                    stack.append(call.func.id)
    return seen


def _encoder_reads(attr_of: str) -> set:
    """Every string literal the score path passes to ``<attr_of>.get(...)``, plus — when
    reading the users row — to ``credentialing._json_field(user, ...)``.

    AUDIT LOW: the shipped guard compares two hand-maintained lists, so adding
    `creds.get("sex")` to the encoder without touching the declared list passes cleanly. The
    audit verified the two were consistent with a 10-line AST comparison and said *that
    comparison should be the test*. This is it, made transitive.
    """
    keys = set()
    for node in _score_path_functions().values():
        for call in ast.walk(node):
            if not isinstance(call, ast.Call) or not call.args:
                continue
            # <attr_of>.get("literal")
            if (isinstance(call.func, ast.Attribute) and call.func.attr == "get"
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id == attr_of
                    and isinstance(call.args[0], ast.Constant)
                    and isinstance(call.args[0].value, str)):
                keys.add(call.args[0].value)
            # credentialing._json_field(user, "literal") — the other way a users column is
            # read, and the one a `.get` walk alone would miss entirely.
            if (attr_of == "user" and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "_json_field" and len(call.args) >= 2
                    and isinstance(call.args[0], ast.Name) and call.args[0].id == "user"
                    and isinstance(call.args[1], ast.Constant)
                    and isinstance(call.args[1].value, str)):
                keys.add(call.args[1].value)
    return keys


def test_low_the_ast_walk_follows_helpers_out_of_the_entry_points():
    """Guard on the guard. If this shrinks to just the entry points, every assertion below
    starts passing vacuously."""
    reached = set(_score_path_functions())
    assert {"feature_vector", "domain_match"} <= reached
    assert "_practising_value" in reached, "the walk stopped at the entry points"
    assert "_creds" in reached


def test_low_the_encoder_actually_reads_only_what_it_declares():
    read = _encoder_reads("creds")
    assert read, "AST walk found no creds.get(...) calls — the walk is broken, not the code"
    undeclared = read - tiering.ENCODER_CREDENTIAL_KEYS
    assert not undeclared, (
        f"feature_vector/domain_match read undeclared credential keys {sorted(undeclared)}")
    forbidden = read & tiering.FORBIDDEN_CREDENTIAL_KEYS
    assert not forbidden, f"the encoder reads protected proxies {sorted(forbidden)}"


def test_low_the_encoders_users_row_blast_radius_is_declared():
    """`feature_vector` reads the users row directly, so without an allowlist the encoder's
    blast radius is `SELECT *` — every column any other agent ever adds."""
    read = _encoder_reads("user")
    assert read
    undeclared = read - tiering.ENCODER_USER_COLUMNS
    assert not undeclared, f"the encoder reads undeclared users columns {sorted(undeclared)}"
    assert not (tiering.ENCODER_USER_COLUMNS & tiering.FORBIDDEN_CREDENTIAL_KEYS)


def test_low_the_ast_check_would_actually_catch_a_forbidden_read():
    """A guard nobody has seen fail is a guard nobody should trust. This runs the same AST
    comparison against a deliberately poisoned copy of the module source."""
    src = _TIERING_PY.read_text("utf-8").replace(
        '    cont_cert = 1.0 if _truthy(creds.get("continuingCertification")) is True else 0.0',
        '    cont_cert = 1.0 if _truthy(creds.get("sex")) is True else 0.0', 1)
    tree = ast.parse(src)
    keys = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "feature_vector":
            for call in ast.walk(node):
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "get" and call.args
                        and isinstance(call.func.value, ast.Name)
                        and call.func.value.id == "creds"
                        and isinstance(call.args[0], ast.Constant)):
                    keys.add(call.args[0].value)
    assert "sex" in keys, "the poisoned source did not take — fix the fixture, not the guard"
    assert keys & tiering.FORBIDDEN_CREDENTIAL_KEYS == {"sex"}
