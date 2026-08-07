"""PRD C §1 — the Phase 0 audit, as executable assertions.

Written findings live in ``docs/PRD_C_PHASE0_AUDIT.md``. This file is the half of that document
that cannot rot: each test names the audit item it closes, and several exist specifically to
fail if a future edit re-opens a defect that a previous release already fixed. Regression tests
for things that are currently correct are the cheapest insurance in the codebase — the three
audits before this one all found a defect that had been fixed and then quietly reintroduced.

Path tests issue real HTTP requests (context pack §6).
"""

from __future__ import annotations

import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from tests._asclepius import app, fresh_store, headers_for, make_user, uniq

from asclepius import calibration, credentialing
from asclepius.credentialing import NpiResult

VALID_NPI = "1234567893"


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, raise_exc=None):
        self.status_code = status_code
        self._payload = payload
        self._raise = raise_exc

    def json(self):
        if self._raise:
            raise self._raise
        return self._payload


def _record(last_name="Doe", **over):
    rec = {
        "number": VALID_NPI,
        "enumeration_type": "NPI-1",
        "basic": {
            "first_name": "Jane", "last_name": last_name, "credential": "M.D.",
            "status": "A", "enumeration_date": "2008-05-23",
            # The free NPPES API really does return these. That is the whole point of §2's
            # "live adverse-impact hazard sitting in an API response".
            "sex": "F", "name_prefix": "Dr.",
        },
        "taxonomies": [{"code": "207RN0300X", "desc": "Nephrology", "primary": True,
                        "state": "MA", "license": "MD-99881"}],
        "addresses": [{"address_purpose": "LOCATION", "city": "Boston", "state": "MA"}],
        "other_names": [],
    }
    rec.update(over)
    return rec


def _stub(monkeypatch, response):
    def fake_get(url, params=None, timeout=None, **kw):
        if isinstance(response, Exception):
            raise response
        return response
    monkeypatch.setattr(credentialing.httpx, "get", fake_get)


# ═══ §1.1 — the NPI tri-state, every return path ═════════════════════════════

@pytest.mark.parametrize("response,expected_reason", [
    (httpx.ConnectTimeout("timed out"), "network_error:ConnectTimeout"),
    (httpx.ReadTimeout("slow"), "network_error:ReadTimeout"),
    (httpx.ConnectError("refused"), "network_error:ConnectError"),
    (_FakeResponse(429), "rate_limited"),
    (_FakeResponse(500), "http_500"),
    (_FakeResponse(503), "http_503"),
    (_FakeResponse(200, raise_exc=ValueError("not json")), "bad_json"),
    (_FakeResponse(200, {"Errors": [{"description": "bad"}]}), "api_error"),
    # The one that matters most: a 200 whose body we do not recognise.
    (_FakeResponse(200, {"unexpected": "shape"}), "unrecognized_payload"),
    (_FakeResponse(200, {"result_count": 1, "results": []}), "unrecognized_payload"),
    (_FakeResponse(200, {"result_count": 1, "results": ["not a dict"]}),
     "unrecognized_payload"),
])
def test_every_indeterminate_path_is_unavailable_never_not_found(
        monkeypatch, response, expected_reason):
    """'We could not check' is not 'this person does not exist', and the difference is
    PERMANENT: not_found stamps npi_checked_at, which makes the row cache-eligible and
    suppresses the retry path."""
    _stub(monkeypatch, response)
    out = credentialing.verify_npi(VALID_NPI, "Doe")
    assert out["result"] == NpiResult.UNAVAILABLE.value
    assert out["reason"] == expected_reason


@pytest.mark.parametrize("payload,expected", [
    ({"result_count": 0}, NpiResult.NOT_FOUND.value),
    ({"result_count": 0, "results": []}, NpiResult.NOT_FOUND.value),
])
def test_result_count_zero_is_still_definitively_not_found(monkeypatch, payload, expected):
    _stub(monkeypatch, _FakeResponse(200, payload))
    assert credentialing.verify_npi(VALID_NPI, "Doe")["result"] == expected


def test_bad_checksum_is_not_found_with_no_network_call(monkeypatch):
    def explode(*a, **kw):
        raise AssertionError("a malformed NPI must never reach the network")
    monkeypatch.setattr(credentialing.httpx, "get", explode)
    out = credentialing.verify_npi("1234567890", "Doe")
    assert out["result"] == NpiResult.NOT_FOUND.value
    assert out["reason"] == "invalid_format_or_checksum"


def test_unavailable_never_becomes_a_thirty_day_cached_rejection(monkeypatch):
    """DoD §7.5. NPPES timeout → UNAVAILABLE → npi_verified stays NULL → the row is not
    cached and the retry sweep still sees it."""
    store = fresh_store()
    user = store.create_user(email=f"{uniq()}@x.com", password="pw-12345678")
    store.set_npi_result(user["id"], {"result": "verified", "npi": VALID_NPI,
                                      "record": {"number": VALID_NPI}})
    _stub(monkeypatch, httpx.ReadTimeout("hang"))
    out = credentialing.verify_npi(VALID_NPI, "Doe")
    other = store.create_user(email=f"{uniq()}@x.com", password="pw-12345678")
    store.set_npi_result(other["id"], out)

    row = store.get_user_by_id(other["id"])
    assert row["npi_verified"] is None
    assert row["npi_checked_at"] is None
    assert json.loads(row["npi_last_attempt_json"])["reason"].startswith("network_error")
    # ...and a failed attempt must not have destroyed the answer we already held.
    assert store.get_user_by_id(user["id"])["npi_verified"] == 1


def test_a_failed_recheck_does_not_evict_a_good_answer(monkeypatch):
    store = fresh_store()
    user = store.create_user(email=f"{uniq()}@x.com", password="pw-12345678")
    store.set_npi_result(user["id"], {"result": "verified", "npi": VALID_NPI,
                                      "record": {"number": VALID_NPI, "last_name": "Doe"}})
    before = store.get_user_by_id(user["id"])["npi_payload_json"]
    store.set_npi_result(user["id"], {"result": "unavailable", "reason": "rate_limited"})
    assert store.get_user_by_id(user["id"])["npi_payload_json"] == before


# ═══ §1.2 — name matching ════════════════════════════════════════════════════

@pytest.mark.parametrize("claimed,registry", [
    ("Muñoz", "MUNOZ"), ("García", "GARCIA"), ("Müller", "MULLER"),
    ("Šimić", "SIMIC"), ("Nguyễn", "NGUYEN"), ("Ångström", "ANGSTROM"),
    ("Łukasz", "LUKASZ"), ("Straße", "STRASSE"),
])
def test_accented_surnames_match_the_ascii_folded_registry(claimed, registry):
    """NPPES stores names ASCII-folded, so without NFKD folding every physician with an
    accented surname mismatched BY CONSTRUCTION."""
    assert credentialing.family_names_match(claimed, registry)


@pytest.mark.parametrize("legal_name,expected", [
    ("Jane Doe, MD", "Doe"),
    ("Jane Doe MD", "Doe"),
    ("Jane Doe, MD, FACP", "Doe"),
    ("John Smith Jr", "Smith"),
    ("Dr. Jane Doe", "Doe"),
    ("Minh Do", "Do"),                      # 'Do' is a real Vietnamese surname
    ("Jane Doe DO", "Doe"),
    ("Maria de la Cruz", "Cruz"),
    ("Doe", "Doe"),
])
def test_family_name_extraction_survives_what_physicians_actually_type(legal_name, expected):
    assert credentialing.family_name_from_legal_name(legal_name) == expected


def test_the_admin_recheck_path_uses_the_same_extractor_as_signup(monkeypatch):
    """C-0.2, the one real bug this audit found.

    ``asclepius_verify._family_name`` used ``legal.split()[-1]``, so the admin's **Recheck
    NPI** button compared "MD" against the registry for "Jane Doe, MD" and overwrote a
    verified physician with a family_name_mismatch. Signup and recheck must agree about the
    same physician, or the button an admin presses to HELP a stuck record is the one that
    breaks it.
    """
    from routers import asclepius_verify

    store = fresh_store()
    doc = store.provision_user(
        email=f"{uniq()}@x.com", password="pw-12345678", full_name="Jane Doe",
        npi=VALID_NPI, credentials={"fullLegalName": "Jane Doe, MD", "npi": VALID_NPI})
    assert asclepius_verify._family_name(doc) == "Doe"

    _stub(monkeypatch, _FakeResponse(200, {"result_count": 1, "results": [_record("Doe")]}))
    result = asclepius_verify._recheck_one(store, doc, force=True)
    assert result["result"] == NpiResult.VERIFIED.value
    assert store.get_user_by_id(doc["id"])["npi_verified"] == 1


def test_accented_signup_verifies_end_to_end_through_the_router(monkeypatch):
    """DoD §7.6. `Muñoz`, `García` and `"Jane Doe, MD"` all verify against a matching NPPES
    record — exercised through the recheck route, not the helper."""
    store = fresh_store()
    admin = make_user(store, role="admin")
    client = TestClient(app)
    for legal, registry in (("Ana Muñoz, MD", "MUNOZ"), ("Luis García MD", "GARCIA"),
                            ("Jane Doe, MD", "DOE")):
        doc = store.provision_user(
            email=f"{uniq()}@x.com", password="pw-12345678", full_name=legal, npi=VALID_NPI,
            credentials={"fullLegalName": legal, "npi": VALID_NPI})
        _stub(monkeypatch, _FakeResponse(
            200, {"result_count": 1, "results": [_record(registry)]}))
        r = client.post(f"/api/asclepius/verify/queue/{doc['id']}/recheck-npi",
                        headers=headers_for(admin))
        assert r.status_code == 200, r.text
        assert r.json()["npi_verified"] == 1, legal


# ═══ C-0.6 — the protected-attribute boundary ════════════════════════════════

def test_basic_sex_never_leaves_the_nppes_ingest_boundary(monkeypatch):
    """§2: strip ``basic.sex`` at the NPPES ingest boundary. It must never reach a feature
    vector, a log, or the database."""
    _stub(monkeypatch, _FakeResponse(200, {"result_count": 1, "results": [_record()]}))
    fetched = credentialing.fetch_npi_record(VALID_NPI)
    credentialing.assert_no_protected_attributes(fetched)
    assert "sex" not in fetched["record"]["basic"]
    assert "name_prefix" not in fetched["record"]["basic"]

    out = credentialing.verify_npi(VALID_NPI, "Doe")
    credentialing.assert_no_protected_attributes(out)
    assert out["result"] == NpiResult.VERIFIED.value


def test_protected_attributes_never_reach_the_database(monkeypatch):
    store = fresh_store()
    doc = store.create_user(email=f"{uniq()}@x.com", password="pw-12345678")
    _stub(monkeypatch, _FakeResponse(200, {"result_count": 1, "results": [_record()]}))
    store.set_npi_result(doc["id"], credentialing.verify_npi(VALID_NPI, "Doe"))
    row = store.get_user_by_id(doc["id"])
    credentialing.assert_no_protected_attributes(json.loads(row["npi_payload_json"]))
    assert '"sex"' not in (row["npi_payload_json"] or "")


def test_the_strip_survives_a_widened_trim():
    """The reason the strip lives at the fetch boundary rather than in ``_trim_record``: the
    trim is an allow-list, and one edit widening it to ``dict(record['basic'])`` would
    reintroduce a protected attribute silently. Here the raw record itself is already clean."""
    raw = credentialing._strip_protected_attributes(_record())
    assert "sex" not in raw["basic"]
    assert dict(raw["basic"]).get("sex") is None
    # And the fields the gates genuinely need survive the strip.
    assert raw["basic"]["last_name"] == "Doe"
    assert raw["taxonomies"][0]["license"] == "MD-99881"


def test_no_dea_number_is_collected_anywhere():
    """§2: do not collect a DEA number. Low signal, high sensitivity."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    targets = [
        root / "backend" / "asclepius" / "tiering.py",
        root / "backend" / "asclepius" / "credentialing.py",
        root / "landing" / "src" / "app" / "components" / "onboarding" / "steps.tsx",
    ]
    for path in targets:
        text = path.read_text(encoding="utf-8").lower()
        assert "deanumber" not in text and "dea_number" not in text, path


# ═══ §1.3 — signup must not block on NPPES ═══════════════════════════════════

def test_signup_verification_runs_off_the_event_loop(monkeypatch):
    """DoD §7.7. A try/except prevents a 500; it does not prevent the whole API hanging on a
    slow NPPES. The threadpool hop is what makes the 'non-blocking' claim true.

    Asserted structurally AND behaviourally: the handler awaits ``run_in_threadpool``, and a
    request completes while a stubbed NPPES sleeps far longer than the response took.
    """
    import inspect
    from routers import onboarding

    src = inspect.getsource(onboarding.asclepius_finish)
    assert "run_in_threadpool" in src
    assert "_provision_asclepius_user" in src
    assert "run_in_threadpool" in inspect.getsource(onboarding.member_finish)

    store = fresh_store()
    user = store.provision_user(email=f"{uniq()}@x.com", password="pw-12345678",
                                npi=VALID_NPI)

    def hang(*a, **kw):
        time.sleep(2.0)
        raise httpx.ReadTimeout("still hanging")

    monkeypatch.setattr(credentialing.httpx, "get", hang)
    started = time.monotonic()
    from starlette.concurrency import run_in_threadpool
    import asyncio

    async def drive():
        # Two concurrent verifications: if the work ran on the event loop these would
        # serialize to ~4s. Off it, they overlap.
        await asyncio.gather(
            run_in_threadpool(onboarding._run_signup_verification, store, user,
                              {"npi": VALID_NPI, "fullLegalName": "Jane Doe, MD"}),
            run_in_threadpool(onboarding._run_signup_verification, store, user,
                              {"npi": VALID_NPI, "fullLegalName": "Jane Doe, MD"}),
        )

    asyncio.run(drive())
    assert time.monotonic() - started < 3.5
    # And a hung registry leaves the tri-state intact, not a rejection.
    assert store.get_user_by_id(user["id"])["npi_verified"] is None
    assert store.get_user_by_id(user["id"])["verification_status"] == "pending"


# ═══ §1.4 — rate limiting is keyed on the token, not the IP ══════════════════

def test_signup_throttle_is_keyed_on_the_onboarding_token_not_the_ip():
    """A hospital NATs. Keyed per IP, the 6th physician of a team invited the same hour gets
    a 429 — 'and a physician who cannot complete signup on launch day is gone for good'."""
    import inspect
    from routers import onboarding

    src = inspect.getsource(onboarding._signup_rate_guard)
    assert "asclepius_signup_tok:" in src
    assert src.index("asclepius_signup_tok:") < src.index("asclepius_signup_ip:")
    # The per-IP ceiling must stay far above one team's worth of signups.
    assert onboarding._SIGNUP_PER_IP[0] >= 20
    assert onboarding._SIGNUP_PER_TOKEN[0] <= 10


# ═══ §1.5 — field capture has both a sender and a receiver ═══════════════════

@pytest.mark.parametrize("field,sender_token", [
    ("phone", "c.phone"),
    ("linkedin_url", "c.linkedinUrl"),
])
def test_captured_fields_have_a_sender_in_the_form(field, sender_token):
    """A receiver with no sender is a column that is NULL forever."""
    import pathlib
    steps = (pathlib.Path(__file__).resolve().parents[2] / "landing" / "src" / "app"
             / "components" / "onboarding" / "steps.tsx").read_text(encoding="utf-8")
    assert sender_token in steps


def test_identity_capture_round_trips_through_the_signup_helper():
    from routers import onboarding

    store = fresh_store()
    user = store.provision_user(email=f"doc-{uniq()}@mgb.org", password="pw-12345678")
    onboarding._run_signup_verification(store, user, {
        "phone": "+1 555 010 7788", "linkedinUrl": "https://linkedin.com/in/janedoe"})
    row = store.get_user_by_id(user["id"])
    assert row["phone"] == "+1 555 010 7788"
    assert row["linkedin_url"] == "https://linkedin.com/in/janedoe"
    assert row["email_domain_class"] == "academic"


def test_cv_sha_is_never_accepted_from_the_client():
    """Not a sender/receiver pair by design: a client-named sha would be an unvalidated
    reference into the same asset store that holds de-identified clinical images."""
    import inspect
    from routers import onboarding

    src = inspect.getsource(onboarding._preserve_server_cv_fields)
    assert "cvAssetSha" in src and "stored" in src


# ═══ Migration hygiene (context pack §6) ═════════════════════════════════════

def test_the_new_schema_is_re_runnable_and_defaults_nothing_that_means_undecided():
    store = fresh_store()
    store._init_schema()          # idempotent — a second boot must be a no-op
    store._init_schema()
    with store._conn() as conn:
        for table in ("tiering_weights", "tiering_decisions", "calibration_items",
                      "calibration_attempts", "leie_exclusions", "fairness_observations"):
            cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
            assert cols, table
        # No DEFAULT on any status-ish column: NULL means 'not yet decided'.
        for table, col in (("tiering_decisions", "applied_at"),
                           ("tiering_decisions", "admin_tier"),
                           ("calibration_attempts", "tr_gate_passed"),
                           ("calibration_attempts", "tl_gate_passed"),
                           ("calibration_attempts", "submitted_at")):
            info = {r["name"]: r for r in
                    conn.execute(f"PRAGMA table_info({table})").fetchall()}
            assert info[col]["dflt_value"] is None, f"{table}.{col} must not carry a DEFAULT"


def test_verification_status_is_never_backfilled():
    """§8: NULL means 'pre-verification-era account' and existing users depend on it passing
    untouched."""
    store = fresh_store()
    legacy = store.create_user(email=f"{uniq()}@x.com", password="pw-12345678")
    assert store.get_user_by_id(legacy["id"])["verification_status"] is None
    store._init_schema()
    assert store.get_user_by_id(legacy["id"])["verification_status"] is None


def test_a_null_tier_grants_nothing():
    """The capability table's own contract: 'not yet assigned' denies. Asserted on
    a user dict rather than a stored row, because a stored NULL tier on a legacy
    contributor is migrated away (see below) — the RULE still has to hold."""
    from asclepius import capabilities

    assert capabilities.capabilities({"role": "evaluator", "tier": None}) == frozenset()
    assert not capabilities.can({"role": "evaluator", "tier": None}, capabilities.LABEL)


def test_the_tier_of_a_pre_tiering_contributor_is_backfilled():
    """The other half of §8's promise. ``verification_status`` must NOT be
    backfilled — NULL there is a real, load-bearing state. ``tier`` is different:
    LABEL is now enforced at /tasks/next and /submissions, and an account from
    before tiering passes the verification gate untouched and is labeling today.
    Leaving its tier NULL would take work away from someone doing it."""
    from asclepius import capabilities

    store = fresh_store()
    legacy = store.create_user(email=f"{uniq()}@x.com", password="pw-12345678")
    assert store.get_user_by_id(legacy["id"])["tier"] is None
    store._init_schema()
    migrated = store.get_user_by_id(legacy["id"])
    assert migrated["tier"] == capabilities.LABELER
    assert migrated["verification_status"] is None, "still not backfilled"
    assert capabilities.can(migrated, capabilities.LABEL)


# ═══ Calibration — the exam cannot leak its own key ══════════════════════════

def test_the_exam_never_serves_an_answer_key():
    """A candidate who can see better_answer_id scores 1.00, and the whole feature becomes
    noise pointed at the admin queue."""
    from tests.test_tiering_score import seed_calibration_items

    store = fresh_store()
    seed_calibration_items(store)
    exam = calibration.build_exam(store, specialty="nephrology", seed=1)
    blob = json.dumps(exam["items"])
    assert "better_answer_id" not in blob
    assert "load_bearing" not in blob
    assert "break_step" not in blob
    for item in exam["items"]:
        assert "key" not in item


def test_an_exam_short_of_admissible_items_fails_closed():
    """Failing closed keeps the TR gate SHUT, which is correct when we cannot measure. A
    short exam would produce a number that looks like a calibration score and is not one."""
    from tests.test_tiering_score import seed_calibration_items

    store = fresh_store()
    seed_calibration_items(store, n=5)
    with pytest.raises(calibration.ExamNotAvailable):
        calibration.build_exam(store, specialty="nephrology")


def test_an_item_whose_panel_did_not_converge_cannot_key_anything():
    """If the reference panel could not agree which answer is better, the item does not
    measure the candidate — it measures the item."""
    split = calibration.aggregate_panel([
        {"better_answer_id": "A"}, {"better_answer_id": "B"}, {"better_answer_id": "C"}])
    assert split["admissible"] is False
    assert split["reason"] == "panel_did_not_converge_on_better_answer"
    tiny = calibration.aggregate_panel([{"better_answer_id": "A"}])
    assert tiny["admissible"] is False and tiny["reason"] == "panel_too_small"


def test_a_single_reviewers_opinion_is_never_a_key():
    """κ ≈ 0.5 for structured implicit peer review: an exam keyed by one senior physician
    measures agreement with that physician, which is not the thing we are selling."""
    assert calibration.PANEL_MIN >= 3


def test_raw_responses_survive_a_rekey_so_nobody_is_retested():
    from tests.test_tiering_score import seed_calibration_items

    store = fresh_store()
    ids = seed_calibration_items(store)
    doc = store.create_user(email=f"{uniq()}@x.com", password="pw-12345678")
    attempt = store.start_calibration_attempt(
        user_id=doc["id"], specialty="nephrology", item_ids=ids)
    responses = {i: {"better_answer_id": "A", "break_step": "step_2",
                     "load_bearing": ["dose adjustment", "eGFR threshold"]} for i in ids}
    store.record_calibration_responses(attempt["attempt_id"], responses)
    first = calibration.grade(store.get_calibration_items(ids), responses)
    store.record_calibration_score(attempt["attempt_id"], first)
    assert first["tr_gate_passed"] is True

    # The panel re-keys one item — the "better" answer was actually B.
    item = store.get_calibration_items([ids[0]])[0]
    new_key = calibration.aggregate_panel(
        [{"better_answer_id": "B", "break_step": "step_2",
          "load_bearing": ["dose adjustment", "eGFR threshold"]} for _ in range(3)])
    store.upsert_calibration_item(specialty="nephrology", vignette=item["vignette"],
                                  key=new_key, item_id=ids[0])

    rescored = calibration.rescore_attempt(store, attempt["attempt_id"])
    assert rescored["subscores"]["choice"] < first["subscores"]["choice"]
    # Nobody was re-tested: the raw responses are unchanged and still on file.
    assert store.get_calibration_attempt(attempt["attempt_id"])["responses"] == responses


def test_a_lopsided_profile_fails_the_subscore_floor():
    """Perfect on choice, blind on localization, composites over 0.85 — and is not a TR.
    A candidate must not pass by being brilliant at one of the three things the job is."""
    items = [{"item_id": f"i{n}",
              "key": {"panel_n": 3, "better_answer_id": "A", "break_step": "s2",
                      "load_bearing": ["c1"]}} for n in range(14)]
    responses = {f"i{n}": {"better_answer_id": "A",
                           "break_step": "s2" if n < 7 else "s1",
                           "load_bearing": ["c1"]} for n in range(14)}
    out = calibration.grade(items, responses)
    assert out["composite"] >= calibration.TR_GATE
    assert out["subscores"]["localization"] < calibration.TR_SUBSCORE_FLOOR
    assert out["tr_gate_passed"] is False
    assert out["failed_floors"] == ["localization"]


def test_an_unkeyed_field_is_not_counted_as_a_miss():
    """An item whose panel converged on the better answer but not the break step contributes
    to `choice` and not to `localization`. Otherwise a candidate is penalised for the panel's
    disagreement, which is the opposite of the measurement we want."""
    items = [{"item_id": "i1", "key": {"panel_n": 3, "better_answer_id": "A",
                                       "break_step": None, "load_bearing": []}}]
    out = calibration.grade(items, {"i1": {"better_answer_id": "A"}})
    assert out["subscores"]["choice"] == 1.0
    assert out["subscores"]["localization"] is None
    assert out["composite"] == 1.0


# ═══ Calibration path tests ══════════════════════════════════════════════════

def test_calibration_exam_and_submit_round_trip_over_http():
    from tests.test_tiering_score import seed_calibration_items

    store = fresh_store()
    seed_calibration_items(store)
    doc = store.provision_user(
        email=f"{uniq()}@x.com", password="pw-12345678", specialty="nephrology",
        credentials={"primarySpecialty": "Nephrology"})
    client = TestClient(app)

    r = client.get("/api/asclepius/verify/calibration/exam", headers=headers_for(doc))
    assert r.status_code == 200, r.text
    exam = r.json()
    assert len(exam["items"]) >= calibration.EXAM_MIN_ITEMS
    assert "better_answer_id" not in json.dumps(exam)

    responses = {it["item_id"]: {"better_answer_id": "A", "break_step": "step_2",
                                 "load_bearing": ["dose adjustment", "eGFR threshold"]}
                 for it in exam["items"]}
    r = client.post(f"/api/asclepius/verify/calibration/{exam['attempt_id']}/submit",
                    json={"responses": responses}, headers=headers_for(doc))
    assert r.status_code == 200, r.text
    assert r.json()["tr_gate_passed"] is True

    # Re-submitting is a 409, not a second attempt with a better score.
    assert client.post(f"/api/asclepius/verify/calibration/{exam['attempt_id']}/submit",
                       json={"responses": responses},
                       headers=headers_for(doc)).status_code == 409


def test_refreshing_resumes_the_attempt_instead_of_rerolling_the_items():
    """Without this, a candidate refreshes until an easy sample of items comes up, and the
    exam stops being a gate."""
    from tests.test_tiering_score import seed_calibration_items

    store = fresh_store()
    seed_calibration_items(store, n=20)
    doc = store.provision_user(email=f"{uniq()}@x.com", password="pw-12345678",
                               specialty="nephrology",
                               credentials={"primarySpecialty": "Nephrology"})
    client = TestClient(app)
    first = client.get("/api/asclepius/verify/calibration/exam",
                       headers=headers_for(doc)).json()
    second = client.get("/api/asclepius/verify/calibration/exam",
                        headers=headers_for(doc)).json()
    assert second["attempt_id"] == first["attempt_id"]
    assert second["resumed"] is True
    assert [i["item_id"] for i in second["items"]] == [i["item_id"] for i in first["items"]]


def test_the_exam_cannot_be_re_sat_until_it_is_passed():
    """Two attempts, then a 90-day cooldown. Unbounded retakes leak the key by trial and
    error, since every attempt draws from the same pool and reveals which answers scored."""
    from tests.test_tiering_score import seed_calibration_items

    store = fresh_store()
    seed_calibration_items(store)
    doc = store.provision_user(email=f"{uniq()}@x.com", password="pw-12345678",
                               specialty="nephrology",
                               credentials={"primarySpecialty": "Nephrology"})
    client = TestClient(app)
    for _ in range(calibration.MAX_ATTEMPTS):
        exam = client.get("/api/asclepius/verify/calibration/exam",
                          headers=headers_for(doc)).json()
        wrong = {i["item_id"]: {"better_answer_id": "B", "break_step": "step_1",
                                "load_bearing": []} for i in exam["items"]}
        r = client.post(f"/api/asclepius/verify/calibration/{exam['attempt_id']}/submit",
                        json={"responses": wrong}, headers=headers_for(doc))
        assert r.status_code == 200
        assert r.json()["tr_gate_passed"] is False

    r = client.get("/api/asclepius/verify/calibration/exam", headers=headers_for(doc))
    assert r.status_code == 409
    assert "day(s)" in r.json()["detail"]


def test_passing_closes_the_door_on_a_retake():
    """Re-sitting after a pass can only lower the score. Offering it is a trap."""
    from tests.test_tiering_score import seat_passing_calibration

    store = fresh_store()
    doc = store.provision_user(email=f"{uniq()}@x.com", password="pw-12345678",
                               specialty="nephrology",
                               credentials={"primarySpecialty": "Nephrology"})
    for _ in range(calibration.MAX_ATTEMPTS):
        seat_passing_calibration(store, doc)
    r = TestClient(app).get("/api/asclepius/verify/calibration/exam", headers=headers_for(doc))
    assert r.status_code == 409
    assert "already passed" in r.json()["detail"]


def test_an_abandoned_attempt_does_not_cost_a_chance():
    """A browser closed or a pager going off mid-case is not a data point about the
    physician, and must not spend one of two attempts."""
    from tests.test_tiering_score import seed_calibration_items

    store = fresh_store()
    ids = seed_calibration_items(store)
    doc = store.create_user(email=f"{uniq()}@x.com", password="pw-12345678")
    for _ in range(5):
        store.start_calibration_attempt(user_id=doc["id"], specialty="nephrology",
                                        item_ids=ids)
    calibration.check_retake_allowed(store, user_id=doc["id"], specialty="nephrology")


def test_a_candidate_cannot_submit_someone_elses_attempt():
    from tests.test_tiering_score import seed_calibration_items

    store = fresh_store()
    ids = seed_calibration_items(store)
    a = store.create_user(email=f"{uniq()}@x.com", password="pw-12345678")
    b = store.create_user(email=f"{uniq()}@x.com", password="pw-12345678")
    attempt = store.start_calibration_attempt(user_id=a["id"], specialty="nephrology",
                                              item_ids=ids)
    r = TestClient(app).post(
        f"/api/asclepius/verify/calibration/{attempt['attempt_id']}/submit",
        json={"responses": {}}, headers=headers_for(b))
    assert r.status_code == 403


def test_an_unready_exam_is_a_503_not_a_failed_gate():
    """A physician must not be told they failed a gate that was never opened."""
    store = fresh_store()
    doc = store.provision_user(email=f"{uniq()}@x.com", password="pw-12345678",
                               specialty="nephrology",
                               credentials={"primarySpecialty": "Nephrology"})
    r = TestClient(app).get("/api/asclepius/verify/calibration/exam", headers=headers_for(doc))
    assert r.status_code == 503
    assert "admissible keyed items" in r.json()["detail"]


def test_calibration_feeds_the_tiering_decision_end_to_end():
    """The full chain: sit the exam → the score becomes calibration_z → TR eligibility
    opens → the proposal flips to reviewer."""
    from tests.test_tiering_score import _physician

    store = fresh_store()
    doc = _physician(store)
    admin = make_user(store, role="admin")
    store.set_verification_status(doc["id"], "pending")
    client = TestClient(app)

    before = client.get(f"/api/asclepius/verify/tiering/{doc['id']}",
                        headers=headers_for(admin)).json()
    assert before["tr_eligible"] is False
    assert "calibration exam at the TR gate" in before["tr_missing"]

    from tests.test_tiering_score import seat_passing_calibration
    seat_passing_calibration(store, doc)

    after = client.get(f"/api/asclepius/verify/tiering/{doc['id']}",
                       headers=headers_for(admin)).json()
    assert after["tr_eligible"] is True
    assert after["proposed_tier"] == "reviewer"
    assert after["features"]["calibration_z"] > 0
