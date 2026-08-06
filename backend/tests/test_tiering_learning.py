"""PRD C §5 — the learning loop.

§8: *"The learning loop is the part most likely to be built wrong in a way nobody notices. A
model that silently stops updating looks exactly like a model that has converged."* So these
tests are written to distinguish those two states, not merely to observe that a number changed:
every assertion about learning also asserts something about what did NOT move.
"""

from __future__ import annotations

import json
import random

import pytest
from fastapi.testclient import TestClient

from tests._asclepius import app, fresh_store, headers_for, make_user

from asclepius import tiering

from tests.test_tiering_score import (
    VALID_NPI, _passed_calibration, _physician, seat_passing_calibration,
)


def _features(*, domain=1.0, board=1.0, subspec=1.0, ge3=1.0, practising=1.0,
              cont=1.0, struct=1.0, cal=1.0, **pinned):
    """A feature dict shaped exactly like feature_vector() output, plus any pinned features a
    test wants to inject non-zero values for."""
    vec = {
        "intercept": 1.0, "board_certified_active": board,
        "subspecialty_certified": subspec, "domain_match": domain,
        "post_residency_ge_3yr": ge3, "currently_practicing": practising,
        "continuing_cert": cont, "structured_review_exp": struct, "calibration_z": cal,
        "measured_quality_z": 0.0,
    }
    vec.update(pinned)
    return vec


def _seed_decisions(store, n, *, tier_rule=None, pinned_value=None, seed=7):
    """N synthetic admin decisions. ``tier_rule`` maps a feature dict to the admin's tier, so
    a test can encode the policy it expects the model to learn."""
    rng = random.Random(seed)
    for i in range(n):
        domain = rng.choice([0.0, 0.5, 1.0])
        board = float(rng.random() < 0.8)
        practising = float(rng.random() < 0.7)
        cal = rng.gauss(0.0, 1.0)
        feats = _features(domain=domain, board=board, practising=practising, cal=cal)
        admin_tier = (tier_rule(feats) if tier_rule
                      else (tiering.TR if domain >= 0.5 and board else tiering.TL))
        if pinned_value is not None:
            # Protected proxies that CORRELATE PERFECTLY with the admin's decision. This is
            # the adversarial case: if the model can rediscover a protected characteristic
            # through admin behaviour, this is the batch that would do it. Injected AFTER the
            # tier is chosen, so the correlation is exact rather than incidental.
            feats["sex"] = 1.0 if admin_tier == tiering.TR else 0.0
            feats["grad_year"] = float(1980 + i % 40)
            feats["practice_region"] = 1.0 if admin_tier == tiering.TR else 0.0
        store.record_tiering_decision(
            user_id=f"u_{i}", case_domain="nephrology", features=feats,
            proposed_tier=tiering.TL, admin_tier=admin_tier, decided_by="admin@example.com",
            score=0.0,
        )


# ═══ DoD §7.3 — 100 decisions sharpen the varying features, pinned move by 0.0 ═══

def test_hundred_decisions_reduce_posterior_variance_and_move_pinned_by_exactly_zero():
    store = fresh_store()
    before = store.get_tiering_weights()
    _seed_decisions(store, 100, pinned_value=True)

    result = tiering.apply_decision_batch(store, actor="admin@example.com")
    after = store.get_tiering_weights()

    assert result["applied"] == 100

    # Posterior variance falls on the features that actually varied in the batch.
    for name in ("domain_match", "board_certified_active", "calibration_z"):
        assert after[name]["q"] > before[name]["q"], name
        assert (1 / after[name]["q"] ** 0.5) < (1 / before[name]["q"] ** 0.5), name

    # Pinned features moved by EXACTLY 0.0 — despite being fed values that correlate
    # perfectly with the admin's decision, which is the only version of this test that means
    # anything. A pinned feature that is always 0 in the data cannot move for trivial reasons.
    for name in tiering.PINNED_ZERO:
        assert after[name]["m"] == 0.0, name
        assert float(after[name]["m"]) - float(before[name]["m"]) == 0.0
        assert after[name]["q"] == tiering.PINNED_PRECISION, name
    tiering.assert_pinned_immobile(after)

    # And the batch genuinely learned something — otherwise the assertion above is vacuous.
    assert any(abs(v) > 1e-6 for k, v in result["deltas"].items()
               if k not in tiering.PINNED_ZERO)


def test_the_model_learns_the_direction_the_admins_actually_used():
    """Admins who consistently promote on domain match should push domain_match UP. If this
    ever passes while the deltas are all zero, the loop is dead and the test is the only
    thing that can tell you."""
    store = fresh_store()
    before = store.get_tiering_weights()
    _seed_decisions(store, 120,
                    tier_rule=lambda f: tiering.TR if f["domain_match"] >= 1.0 else tiering.TL)
    tiering.apply_decision_batch(store)
    after = store.get_tiering_weights()
    assert after["domain_match"]["m"] > before["domain_match"]["m"]


# ═══ DoD §7.4 — replaying a batch changes nothing ════════════════════════════

def test_replaying_the_same_batch_twice_changes_nothing():
    """applied_at is the guard. Each event contributes precision ONCE, or you fabricate
    confidence — and fabricated confidence is invisible: the weights look more certain and
    nothing anywhere reports that they should not be."""
    store = fresh_store()
    _seed_decisions(store, 40)
    first = tiering.apply_decision_batch(store)
    snapshot = {k: dict(v) for k, v in store.get_tiering_weights().items()}
    second = tiering.apply_decision_batch(store)
    assert second["applied"] == 0
    assert store.get_tiering_weights() == snapshot
    assert first["applied"] == 40


def test_applied_rows_are_stamped_in_the_same_transaction_as_the_weights():
    store = fresh_store()
    _seed_decisions(store, 10)
    assert len(store.pending_tiering_decisions()) == 10
    tiering.apply_decision_batch(store)
    assert store.pending_tiering_decisions() == []
    history = store.tiering_decision_history(limit=20)
    assert all(r["applied_at"] for r in history)


def test_undecided_rows_are_not_observations():
    """A proposal with no admin decision is not a label. Including it would train the model on
    its own output, which converges instantly and means nothing."""
    store = fresh_store()
    store.record_tiering_decision(
        user_id="u1", case_domain="nephrology", features=_features(),
        proposed_tier=tiering.TR, admin_tier=None)
    assert store.pending_tiering_decisions() == []
    assert tiering.apply_decision_batch(store)["applied"] == 0


# ═══ §5.2 guardrails ═════════════════════════════════════════════════════════

def test_delta_is_clipped_at_quarter_point_per_batch():
    """One opinionated admin on one bad night must not rewrite the model."""
    store = fresh_store()
    before = store.get_tiering_weights()
    # 200 decisions all flatly contradicting the prior on one feature.
    for i in range(200):
        store.record_tiering_decision(
            user_id=f"u{i}", case_domain="nephrology",
            features=_features(domain=1.0, board=1.0, subspec=1.0, cal=0.0),
            proposed_tier=tiering.TR, admin_tier=tiering.TL, decided_by="a@b.c")
    tiering.apply_decision_batch(store)
    after = store.get_tiering_weights()
    for name in after:
        delta = abs(float(after[name]["m"]) - float(before.get(name, {}).get("m", 0.0)))
        assert delta <= tiering.MAX_DELTA_M + 1e-9, f"{name} moved {delta}"
    # ...and at least one feature actually hit the clip, or this proves nothing.
    assert any(abs(float(after[n]["m"]) - float(before[n]["m"])) == pytest.approx(
        tiering.MAX_DELTA_M, abs=1e-6) for n in after if n not in tiering.PINNED_ZERO)


def test_precision_is_capped_so_the_model_never_fully_freezes():
    """Not in the PRD, added deliberately. Under the stated update q only ever grows, so after
    enough batches every weight is immovable and a genuine shift in admin policy can never be
    tracked — and a frozen model is indistinguishable from a converged one from the outside."""
    store = fresh_store()
    for batch in range(6):
        _seed_decisions(store, 150, seed=batch)
        tiering.apply_decision_batch(store)
    for name, row in store.get_tiering_weights().items():
        if name in tiering.PINNED_ZERO:
            continue
        assert row["q"] <= tiering.Q_MAX + 1e-9, name


def test_hard_gates_have_no_weight_to_learn():
    """§5.2: hard gates are frozen. Structural, not enforced — there is no gate in the weight
    table, so there is no path from the learning loop to eligibility."""
    weights = store_weights = fresh_store().get_tiering_weights()
    for gate_id in tiering.GATE_LABELS:
        assert gate_id not in weights
    assert set(store_weights) == set(tiering.ALL_WEIGHT_NAMES)


def test_a_batch_that_moves_nothing_is_reported_not_swallowed(caplog):
    """The specific failure §8 names. An empty-featured batch must produce a loud line, because
    from the outside it looks exactly like convergence."""
    store = fresh_store()
    for i in range(5):
        store.record_tiering_decision(user_id=f"u{i}", case_domain=None, features={},
                                      proposed_tier=None, admin_tier=tiering.TL)
    with caplog.at_level("WARNING", logger="asclepius.tiering"):
        tiering.apply_decision_batch(store)
    assert any("moved NO weight" in r.message or "moved NO weight" in r.getMessage()
               for r in caplog.records)


# ═══ §5.3 exploration ════════════════════════════════════════════════════════

def test_thompson_sampling_perturbs_unpinned_weights_and_never_pinned_ones():
    """Sampling w̃ ~ N(m, diag(q⁻¹)) over-promotes candidates the model is UNCERTAIN about
    rather than ones it thinks are weak. A pinned weight with q = 1e6 would draw ±0.001, and
    'approximately zero forever' is not the guarantee §3.3 makes — so pinned rows are held
    exactly, not sampled narrowly."""
    weights = tiering.default_weights()
    sampled = tiering.thompson_sample(weights, rng=random.Random(1))
    assert sampled["domain_match"]["m"] != weights["domain_match"]["m"]
    for name in tiering.PINNED_ZERO:
        assert sampled[name]["m"] == 0.0


def test_exploration_is_deterministic_under_an_injected_rng():
    """An untestable stochastic branch in a decision system is a branch nobody ever verifies."""
    w = tiering.default_weights()
    a = tiering.thompson_sample(w, rng=random.Random(42))
    b = tiering.thompson_sample(w, rng=random.Random(42))
    assert a == b


def test_exploration_is_flagged_on_the_proposal_and_persisted():
    """A small Exploration badge is only possible if the flag survives to the row: an admin
    must know a proposal was a deliberate probe rather than the model's best guess."""
    store = fresh_store()
    doc = _physician(store)
    out = tiering.propose(doc, case_domain="nephrology", calibration=_passed_calibration(),
                          leie_status=store.leie_status(VALID_NPI), explore=True,
                          rng=random.Random(3))
    assert out["was_exploration"] is True
    row = store.record_tiering_decision(
        user_id=doc["id"], case_domain="nephrology", features=out["features"],
        proposed_tier=out["proposed_tier"], admin_tier=tiering.TR,
        was_exploration=True, decided_by="a@b.c")
    assert row["was_exploration"] == 1


def test_shadow_tr_outcomes_are_weighted_double():
    """A shadow-TR trial scored against a settled gold adjudication is a REAL outcome label,
    not an opinion label — it is the only thing that breaks the circularity of learning from
    decisions the system itself routed to an admin. So it counts twice."""
    store_a, store_b = fresh_store(), None
    feats = _features(domain=1.0, board=1.0)
    for i in range(10):
        store_a.record_tiering_decision(
            user_id=f"u{i}", case_domain="nephrology", features=feats,
            proposed_tier=tiering.TL, admin_tier=tiering.TR, outcome_source="admin")
    a = tiering.apply_decision_batch(store_a)

    store_b = fresh_store()
    for i in range(10):
        store_b.record_tiering_decision(
            user_id=f"u{i}", case_domain="nephrology", features=feats,
            proposed_tier=tiering.TL, admin_tier=tiering.TR, outcome_source="shadow_tr")
    b = tiering.apply_decision_batch(store_b)

    assert (store_b.get_tiering_weights()["domain_match"]["q"]
            > store_a.get_tiering_weights()["domain_match"]["q"])
    assert a["applied"] == b["applied"] == 10


def test_was_flip_is_derived_not_trusted():
    store = fresh_store()
    agree = store.record_tiering_decision(
        user_id="u1", case_domain=None, features=_features(),
        proposed_tier=tiering.TR, admin_tier=tiering.TR)
    flip = store.record_tiering_decision(
        user_id="u2", case_domain=None, features=_features(),
        proposed_tier=tiering.TR, admin_tier=tiering.TL)
    assert agree["was_flip"] == 0 and flip["was_flip"] == 1


# ═══ §5.4 — One-Coin Dawid–Skene ═════════════════════════════════════════════

def _synthetic_labels(n_items, accuracies, seed, gold_frac=0.0):
    truth = {f"t{i}": ("a" if i % 2 == 0 else "b") for i in range(n_items)}
    rng = random.Random(seed)
    obs = []
    for item, label in truth.items():
        for name, p in accuracies.items():
            obs.append((item, name,
                        label if rng.random() < p else ("b" if label == "a" else "a")))
    gold = {k: v for i, (k, v) in enumerate(truth.items()) if i < n_items * gold_frac}
    return obs, truth, gold


@pytest.mark.parametrize("seed", range(12))
def test_dawid_skene_separates_a_reliable_annotator_from_a_weak_one(seed):
    """Robust across seeds — this is the claim the model actually leans on."""
    obs, truth, gold = _synthetic_labels(
        60, {"strong": 0.95, "weak": 0.55}, seed, gold_frac=0.3)
    est = tiering.one_coin_dawid_skene(obs, gold=gold)
    assert est["accuracy"]["strong"] > est["accuracy"]["weak"]
    # Consensus comfortably beats the weak annotator's own 0.55 — the aggregation is doing
    # work. Not pinned tighter than that: with 70% of items unaided by gold and one annotator
    # near chance, the exact count moves a couple either way with the seed.
    assert sum(est["consensus"][i] == truth[i] for i in truth) >= 51


def test_dawid_skene_fine_grained_ordering_is_noisy_and_that_is_why_z_is_shrunk():
    """The honest limit, asserted rather than assumed.

    Over 60 synthetic runs the estimator separated 0.95 from 0.55 in 60/60, but got the
    fine-grained 0.95 > 0.75 > 0.55 ordering wrong in 8/60. That variance is why
    ``measured_quality_z`` — which carries the LARGEST prior weight in the model — is shrunk
    by n/(n+40) on the number of co-labelled items. Without the shrinkage a coin flip in the
    EM fixed point would swing ``s`` by more than the entire domain_match term.
    """
    failures = 0
    for seed in range(60):
        obs, _t, gold = _synthetic_labels(
            60, {"strong": 0.95, "mid": 0.75, "weak": 0.55}, seed, gold_frac=0.3)
        a = tiering.one_coin_dawid_skene(obs, gold=gold)["accuracy"]
        if not (a["strong"] > a["mid"] > a["weak"]):
            failures += 1
    assert 0 < failures < 20, f"{failures}/60 — the noise floor moved; re-derive the shrinkage"


def test_measured_quality_z_is_shrunk_by_the_number_of_co_labelled_items():
    population = [0.6, 0.7, 0.8, 0.9, 0.95]
    thin = tiering.measured_quality_z(0.95, population, n_items=10)
    thick = tiering.measured_quality_z(0.95, population, n_items=400)
    assert 0 < thin < thick
    assert thin == pytest.approx(thick * (10 / 50) / (400 / 440), rel=1e-6)
    # Below the floor there is no estimate at all — not a small one.
    assert tiering.measured_quality_z(0.95, population, n_items=3) is None


def test_dawid_skene_respects_a_tr_adjudication_as_near_gold():
    obs = [("t1", "a1", "x"), ("t1", "a2", "y"), ("t2", "a1", "x"), ("t2", "a2", "y")]
    est = tiering.one_coin_dawid_skene(obs, gold={"t1": "y", "t2": "y"})
    assert est["consensus"]["t1"] == "y"
    assert est["accuracy"]["a2"] > est["accuracy"]["a1"]


def test_dawid_skene_degrades_rather_than_inventing_a_number():
    """A physician with no measured work is not a physician with measured-zero work."""
    assert tiering.one_coin_dawid_skene([])["reason"] == "insufficient_data"
    assert tiering.measured_quality_z(0.9, [0.8, 0.85, 0.9]) is None       # population < 5
    assert tiering.measured_quality_z(0.95, [0.8] * 6) == 0.0              # no spread


# ═══ PATH TESTS — real HTTP ══════════════════════════════════════════════════

def test_approving_a_physician_records_the_override_as_a_training_observation():
    """The override IS the training signal. If approval does not write a row, the loop is fed
    nothing and will look permanently converged."""
    store = fresh_store()
    doc = _physician(store)
    seat_passing_calibration(store, doc)
    store.set_verification_status(doc["id"], "pending")
    admin = make_user(store, role="admin")
    client = TestClient(app)

    r = client.post(f"/api/asclepius/verify/queue/{doc['id']}/approve",
                    json={"tier": "labeler", "note": "starting them on labels"},
                    headers=headers_for(admin))
    assert r.status_code == 200, r.text
    rows = store.tiering_decision_history(user_id=doc["id"])
    assert len(rows) == 1
    assert rows[0]["admin_tier"] == "labeler"
    assert rows[0]["proposed_tier"] == "reviewer"     # the model disagreed...
    assert rows[0]["was_flip"] == 1                   # ...and that disagreement is the label
    assert json.loads(rows[0]["features_json"])["domain_match"] == 1.0


def test_agreement_is_recorded_too_not_only_disagreement():
    """A model that only ever sees its mistakes learns that it is always wrong."""
    store = fresh_store()
    doc = _physician(store)
    seat_passing_calibration(store, doc)
    store.set_verification_status(doc["id"], "pending")
    admin = make_user(store, role="admin")
    TestClient(app).post(f"/api/asclepius/verify/queue/{doc['id']}/approve",
                         json={"tier": "reviewer"}, headers=headers_for(admin))
    rows = store.tiering_decision_history(user_id=doc["id"])
    assert rows[0]["was_flip"] == 0


def test_weights_and_apply_endpoints_round_trip():
    store = fresh_store()
    admin = make_user(store, role="admin")
    client = TestClient(app)
    _seed_decisions(store, 60)

    r = client.get("/api/asclepius/verify/tiering-weights", headers=headers_for(admin))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pending_decisions"] == 60
    by_name = {w["feature"]: w for w in body["weights"]}
    assert by_name["sex"]["pinned"] is True and by_name["sex"]["m"] == 0.0

    r = client.post("/api/asclepius/verify/tiering-weights/apply", headers=headers_for(admin))
    assert r.status_code == 200, r.text
    assert r.json()["applied"] == 60
    # Every run reports its delta — the only cheap way to tell a converged model from a dead
    # one is to watch the number that should be shrinking actually shrink.
    assert "deltas" in r.json()

    after = client.get("/api/asclepius/verify/tiering-weights",
                       headers=headers_for(admin)).json()
    assert after["pending_decisions"] == 0
    assert {w["feature"]: w["m"] for w in after["weights"]}["sex"] == 0.0
    assert any(abs(w["drift"]) > 1e-6 for w in after["weights"] if not w["pinned"])


def test_explicit_tier_decision_endpoint_is_a_label_without_reapproving():
    store = fresh_store()
    doc = _physician(store)
    admin = make_user(store, role="admin")
    client = TestClient(app)
    r = client.post(f"/api/asclepius/verify/tiering/{doc['id']}/decide",
                    json={"tier": "reviewer", "case_domain": "cardiology",
                          "note": "trust them across IM broadly"},
                    headers=headers_for(admin))
    assert r.status_code == 200, r.text
    row = r.json()["decision"]
    assert row["admin_tier"] == "reviewer"
    assert row["case_domain"] == "cardiology"
    # The model proposed nothing on an off-domain case; the admin said reviewer. That is a
    # flip, and it is exactly the observation §5.3 says the loop is starved of.
    assert row["was_flip"] == 1
    # The approval lifecycle is untouched — a tier judgment is not an approval.
    assert store.get_user_by_id(doc["id"])["verification_status"] is None


def test_decide_endpoint_rejects_an_unknown_tier():
    store = fresh_store()
    doc = _physician(store)
    admin = make_user(store, role="admin")
    r = TestClient(app).post(f"/api/asclepius/verify/tiering/{doc['id']}/decide",
                             json={"tier": "superdoctor"}, headers=headers_for(admin))
    assert r.status_code == 400


# ═══ §6 — the fairness monitor ═══════════════════════════════════════════════

def test_demographics_never_land_on_the_users_row():
    """§6: stored in a table that is NOT joinable into the feature store. Every feature path
    loads a physician with SELECT * FROM users, so a column there is a column the model can
    reach — 'we remembered not to read it' is not the guarantee."""
    store = fresh_store()
    doc = _physician(store)
    store.record_fairness_observation(user_id=doc["id"],
                                      demographics={"gender": "woman", "img": "yes"})
    row = store.get_user_by_id(doc["id"])
    blob = json.dumps(row, default=str).lower()
    assert "woman" not in blob
    assert not any("demograph" in k for k in row)
    # And the scored features cannot see it either.
    out = tiering.propose(doc, case_domain="nephrology", calibration=_passed_calibration(),
                          leie_status=store.leie_status(VALID_NPI))
    assert not any(k in out["features"] for k in ("gender", "img", "img_status"))


def test_fairness_monitor_flags_the_four_fifths_threshold():
    store = fresh_store()
    # Group A: 8 of 10 promoted. Group B: 2 of 10. Impact ratio 0.25 — well under 0.8.
    for i in range(10):
        uid = f"a{i}"
        store.record_fairness_observation(user_id=uid, demographics={"gender": "man"})
        store.stamp_fairness_tier(uid, "reviewer" if i < 8 else "labeler")
    for i in range(10):
        uid = f"b{i}"
        store.record_fairness_observation(user_id=uid, demographics={"gender": "woman"})
        store.stamp_fairness_tier(uid, "reviewer" if i < 2 else "labeler")
    rates = store.fairness_selection_rates()
    assert rates["dimensions"]["gender"]["woman"]["impact_ratio"] == pytest.approx(0.25)
    assert any(a["group"] == "woman" for a in rates["alerts"])


def test_small_groups_are_reported_but_do_not_trigger_the_alert_alone():
    """The four-fifths rule is a screening signal, not a verdict — small groups swing wildly."""
    store = fresh_store()
    for i in range(10):
        store.record_fairness_observation(user_id=f"a{i}", demographics={"gender": "man"})
        store.stamp_fairness_tier(f"a{i}", "reviewer")
    store.record_fairness_observation(user_id="b0", demographics={"gender": "woman"})
    store.stamp_fairness_tier("b0", "labeler")
    rates = store.fairness_selection_rates()
    assert rates["dimensions"]["gender"]["woman"]["counted"] is False
    assert rates["alerts"] == []


def test_prefer_not_to_say_is_not_a_group():
    store = fresh_store()
    for i in range(6):
        store.record_fairness_observation(
            user_id=f"x{i}", demographics={"gender": "prefer_not_to_say"})
        store.stamp_fairness_tier(f"x{i}", "labeler")
    assert fresh_store  # keep the import honest
    assert "prefer_not_to_say" not in (
        store.fairness_selection_rates()["dimensions"].get("gender") or {})


def test_fairness_endpoint_path():
    store = fresh_store()
    admin = make_user(store, role="admin")
    doc = _physician(store)
    client = TestClient(app)
    r = client.post(f"/api/asclepius/verify/demographics/{doc['id']}",
                    json={"demographics": {"gender": "woman"}}, headers=headers_for(doc))
    assert r.status_code == 200, r.text
    r = client.get("/api/asclepius/verify/fairness", headers=headers_for(admin))
    assert r.status_code == 200
    assert r.json()["total_observations"] == 1


def test_a_physician_cannot_submit_someone_elses_demographics():
    store = fresh_store()
    a, b = _physician(store), _physician(store)
    r = TestClient(app).post(f"/api/asclepius/verify/demographics/{b['id']}",
                             json={"demographics": {"gender": "man"}}, headers=headers_for(a))
    assert r.status_code == 403
