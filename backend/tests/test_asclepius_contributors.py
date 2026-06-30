"""Contributors view + tiered export tests (Contributors feature spec).

Covers the governing rule: "Export Data" ships Tier A credential attributes only;
"Further Credential Summary" releases the Tier B vault under NDA. Asserts the
hard Tier B leak gate, the org/contributor drill-down directory, the dossier
(PDF + JSON) generation + audit, and the per-org/per-contributor metrics.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import credentials as asc_credentials  # noqa: E402
from asclepius import pipeline as asc_pipeline  # noqa: E402
from asclepius import profiles as asc_profiles  # noqa: E402

client = TestClient(A.app)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    A.fresh_store()
    asc_profiles.clear_cache()

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


def _admin_h():
    return A.headers_for(A.make_user(_store(), role="admin", email=f"admin-{uuid.uuid4().hex[:6]}@asclepius.example"))


_SHIP = {
    "degree": "MD",
    "board_certifications": "ABIM Nephrology (active)",
    "primary_specialty": "nephrology",
    "subspecialties": ["dialysis", "transplant"],
    "years_in_active_practice": 17,
    "active_practice": True,
    "practice_setting_type": "private_practice",
    "languages": ["English"],
    "fellowship_trained": True,
    "credentials_verified": True,
}
_VERIFY = {
    "full_legal_name": "Jane A. Doe, MD",
    "npi": "1234567893",
    "medical_license_number": "A-104872",
    "license_state": "CA",
    "medical_school": "UCSF",
    "residency": "Stanford",
    "fellowship": "UCLA",
    "practice_name": "Riverside Nephrology Associates",
    "practice_address": "1200 Riverside Dr, Sacramento, CA",
}


def _make_contributor(org="Riverside Nephrology Associates", verify=True):
    store = _store()
    user = A.make_user(store, role="evaluator", specialty="nephrology",
                       board_cert="board_certified_nephrology", years_experience=17,
                       organization=org)
    store.upsert_contributor_credentials(
        id_hashed=user["id_hashed"], user_id=user["id"], organization=org,
        role_title="Physician (MD)", credentials_verified=True,
        ship=_SHIP, verify=_VERIFY if verify else {},
    )
    return user


def _submit_export_ready(admin_h, ev_h):
    body = {
        "specialty": "nephrology", "difficulty": "hard", "max_labels": 1,
        "prompt": f"Hyperkalemia case {A.uniq(8)}?",
        "candidate_answers": [{"id": "A", "text": "Calcium then dialyze."},
                              {"id": "B", "text": "Dialysate K+ 1.0."}],
    }
    tid = client.post("/api/asclepius/tasks", json={"tasks": [body]}, headers=admin_h).json()["created"][0]
    sid = "s-" + uuid.uuid4().hex[:12]
    r = client.post("/api/asclepius/submissions", json={
        "submission_id": sid, "task_id": tid, "verdict": "A_better",
        "chosen_id": "A", "rejected_id": "B", "time_spent_sec": 130,
        "prompt_review": {"reviewed": True, "verdict": "valid"},
        "independent_answer": {"text": "Stabilize with IV calcium, shift potassium with insulin and dextrose, then dialyze given the ESRD."},
        "chosen_revision": {"edited": False, "why_better_notes": "B over-lowers K+"},
        "rejected_critique": {"error_tags": ["dosing_error"], "why_worse": "too aggressive"},
    }, headers=ev_h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "export_ready"
    return sid


# ─── Directory / drill-down ───────────────────────────────────────────────────
def test_organizations_and_contributors_directory():
    admin_h = _admin_h()
    user = _make_contributor()
    _submit_export_ready(admin_h, A.headers_for(user))

    orgs = client.get("/api/asclepius/organizations", headers=admin_h).json()["organizations"]
    org = next(o for o in orgs if o["organization"] == "Riverside Nephrology Associates")
    assert org["contributor_count"] == 1
    assert org["record_count"] >= 1
    assert org["verified_count"] == 1
    assert org["last_labeled_at"]

    contributors = client.get(
        "/api/asclepius/contributors", headers=admin_h,
        params={"organization": "Riverside Nephrology Associates"},
    ).json()["contributors"]
    assert len(contributors) == 1
    c = contributors[0]
    assert c["id_hashed"] == user["id_hashed"]
    assert c["primary_specialty"] == "nephrology"
    assert c["record_count"] >= 1
    assert c["credentials_verified"] is True
    assert c["last_labeled_at"]


def test_contributor_profile_blurb_and_masked_verify():
    admin_h = _admin_h()
    user = _make_contributor()
    prof = client.get(f"/api/asclepius/contributors/{user['id_hashed']}", headers=admin_h).json()
    assert "Board-certified" in prof["blurb"]
    assert prof["buttons"] == ["export_data", "further_credential_summary"]
    # Tier B values are masked on the plain profile read.
    assert "verify" not in prof["credentials"]
    assert prof["credentials"]["has_verify_vault"] is True

    # include_verify exposes the vault (admin edit path).
    prof2 = client.get(
        f"/api/asclepius/contributors/{user['id_hashed']}",
        headers=admin_h, params={"include_verify": "true"},
    ).json()
    assert prof2["credentials"]["verify"]["npi"] == "1234567893"


# ─── Export Data (Tier A only) ────────────────────────────────────────────────
def test_contributor_export_is_tier_a_only():
    admin_h = _admin_h()
    user = _make_contributor()
    _submit_export_ready(admin_h, A.headers_for(user))

    manifest = client.post(
        f"/api/asclepius/contributors/{user['id_hashed']}/export",
        json={"profile": "default", "note": "buyer batch"}, headers=admin_h,
    ).json()
    assert manifest["record_count"] >= 1
    assert manifest["scope"]["type"] == "contributor"
    assert manifest["tier_b_leak_gate"] == "passed"
    assert manifest["filters"]["annotator_id_hashed"] == user["id_hashed"]

    out_dir = Path(manifest["dir_path"])
    jsonl = (out_dir / "records.jsonl").read_text()
    # No Tier B field name or value ever appears in the shipped batch.
    for forbidden in ("full_legal_name", "npi", "medical_license_number", "Jane A. Doe", "1234567893", "UCSF"):
        assert forbidden not in jsonl, forbidden
    # The datasheet carries the auto-generated aggregate credential line (Tier A).
    datasheet = (out_dir / "datasheet.md").read_text()
    assert "Contributor scope" in datasheet
    assert "Board-certified" in datasheet


def test_onboarding_tier_b_never_ships_in_export():
    """Identifying credentials collected by the onboarding flow land on the users
    table (full_name, npi, license). They must never appear in an Export Data
    batch — the annotator block only carries Tier A attributes."""
    admin_h = _admin_h()
    store = _store()
    user = store.provision_user(
        email=f"onb-{A.uniq(6)}@hosp.example", password="pw-12345678", role="evaluator",
        full_name="Gregory House", org_name="Princeton-Plainsboro Teaching Hospital",
        clinical_role="Physician (MD)", specialty="nephrology",
        board_cert="board_certified_nephrology", npi="9998887776", years_experience=20,
        credentials={"medical_license_number": "XZ-99021"},
    )
    store.upsert_contributor_credentials(
        id_hashed=user["id_hashed"], user_id=user["id"],
        organization="Princeton-Plainsboro Teaching Hospital", role_title="Physician (MD)",
        credentials_verified=True, ship={"primary_specialty": "nephrology", "degree": "MD"}, verify={},
    )
    _submit_export_ready(admin_h, A.headers_for(user))

    manifest = client.post(
        f"/api/asclepius/contributors/{user['id_hashed']}/export",
        json={"profile": "default"}, headers=admin_h,
    ).json()
    assert manifest["record_count"] >= 1
    jsonl = (Path(manifest["dir_path"]) / "records.jsonl").read_text()
    for forbidden in ("Gregory House", "9998887776", "XZ-99021", "Princeton-Plainsboro",
                      "full_name", "org_name", "npi"):
        assert forbidden not in jsonl, forbidden


def test_onboarding_identifier_in_record_content_blocks_export():
    """Defense in depth: if a physician's real name (from onboarding) ever appears
    in a record's free text, the scoped-export value scan blocks the batch."""
    admin_h = _admin_h()
    store = _store()
    user = store.provision_user(
        email=f"leak-{A.uniq(6)}@hosp.example", password="pw-12345678", role="evaluator",
        full_name="Gregory House", specialty="nephrology",
        board_cert="board_certified_nephrology", npi="9998887776", years_experience=20,
    )
    store.upsert_contributor_credentials(
        id_hashed=user["id_hashed"], user_id=user["id"], organization="Org",
        role_title="Physician (MD)", credentials_verified=True,
        ship={"primary_specialty": "nephrology"}, verify={},
    )
    ev_h = A.headers_for(user)
    tid = client.post("/api/asclepius/tasks", json={"tasks": [{
        "specialty": "nephrology", "difficulty": "hard", "max_labels": 1,
        "prompt": f"Hyperkalemia case {A.uniq(8)}?",
        "candidate_answers": [{"id": "A", "text": "Calcium then dialyze."},
                              {"id": "B", "text": "Dialysate K+ 1.0."}],
    }]}, headers=admin_h).json()["created"][0]
    sid = "s-" + uuid.uuid4().hex[:12]
    r = client.post("/api/asclepius/submissions", json={
        "submission_id": sid, "task_id": tid, "verdict": "A_better",
        "chosen_id": "A", "rejected_id": "B", "time_spent_sec": 130,
        "independent_answer": {"text": "Stabilize with IV calcium, shift potassium with insulin and dextrose, then dialyze given the ESRD."},
        # Real physician name slips into the rationale.
        "chosen_revision": {"edited": False, "why_better_notes": "Reviewed and confirmed by Gregory House."},
        "rejected_critique": {"error_tags": ["dosing_error"], "why_worse": "too aggressive"},
    }, headers=ev_h)
    assert r.status_code == 200 and r.json()["status"] == "export_ready", r.text

    blocked = client.post(
        f"/api/asclepius/contributors/{user['id_hashed']}/export",
        json={"profile": "default"}, headers=admin_h,
    )
    assert blocked.status_code == 422, blocked.text
    assert "value leak" in blocked.text.lower()


def test_org_export_covers_all_contributors():
    admin_h = _admin_h()
    org = "Riverside Nephrology Associates"
    u1 = _make_contributor(org=org)
    u2 = _make_contributor(org=org)
    _submit_export_ready(admin_h, A.headers_for(u1))
    _submit_export_ready(admin_h, A.headers_for(u2))

    manifest = client.post(
        f"/api/asclepius/organizations/{org}/export",
        json={"profile": "default"}, headers=admin_h,
    ).json()
    assert manifest["scope"]["type"] == "organization"
    assert manifest["record_count"] >= 2
    assert set(manifest["filters"]["annotator_ids"]) == {u1["id_hashed"], u2["id_hashed"]}


def test_onboarding_org_name_populates_directory_organization():
    """An onboarded contributor's health-system name (stored on the users.org_name
    column by provision_user) must surface as their organization in the directory,
    NOT collapse to "Unaffiliated" (bug: directory read only users.organization)."""
    admin_h = _admin_h()
    store = _store()
    user = store.provision_user(
        email=f"onb-{A.uniq(6)}@hosp.example", password="pw-12345678", role="evaluator",
        full_name="Avery Smith", org_name="Northridge Nephrology",
        clinical_role="Physician (MD)", specialty="nephrology",
        board_cert="board_certified_nephrology", years_experience=12,
    )
    # New onboarding writes the canonical organization column directly.
    assert user["organization"] == "Northridge Nephrology"
    _submit_export_ready(admin_h, A.headers_for(user))

    contributors = client.get(
        "/api/asclepius/contributors", headers=admin_h,
        params={"organization": "Northridge Nephrology"},
    ).json()["contributors"]
    assert [c["email"] for c in contributors] == [user["email"]]
    assert contributors[0]["organization"] == "Northridge Nephrology"

    orgs = client.get("/api/asclepius/organizations", headers=admin_h).json()["organizations"]
    assert any(o["organization"] == "Northridge Nephrology" for o in orgs)
    assert all(o["organization"] != "Unaffiliated" for o in orgs)


def test_legacy_onboarded_user_with_only_org_name_resolves_via_coalesce():
    """A user persisted before the fix (organization NULL, org_name set) still
    resolves to their real org via the directory COALESCE — no migration needed."""
    admin_h = _admin_h()
    store = _store()
    user = store.provision_user(
        email=f"legacy-{A.uniq(6)}@hosp.example", password="pw-12345678", role="evaluator",
        org_name="Lakeside Kidney Institute", specialty="nephrology",
    )
    # Simulate the legacy row shape: canonical column empty, only org_name set.
    with store._conn() as conn:  # noqa: SLF001 — test reaching into the store
        conn.execute("UPDATE users SET organization = NULL WHERE id = ?", (user["id"],))
    _submit_export_ready(admin_h, A.headers_for(user))

    contributors = client.get(
        "/api/asclepius/contributors", headers=admin_h,
        params={"organization": "Lakeside Kidney Institute"},
    ).json()["contributors"]
    assert [c["email"] for c in contributors] == [user["email"]]


def test_contributor_export_is_rerunnable_after_records_exported():
    """The per-contributor "Export Data" button packages the contributor's full
    labelled corpus EVERY time — including after a prior export (or a prior
    org-wide export) already marked those records ``exported``. Bug: scoped export
    defaulted include_exported=False, so the 2nd run returned "no records"."""
    admin_h = _admin_h()
    user = _make_contributor()
    _submit_export_ready(admin_h, A.headers_for(user))

    first = client.post(
        f"/api/asclepius/contributors/{user['id_hashed']}/export",
        json={"profile": "default"}, headers=admin_h,
    )
    assert first.status_code == 200, first.text
    n = first.json()["record_count"]
    assert n >= 1

    # Records are now status=exported. A second click must still package them.
    second = client.post(
        f"/api/asclepius/contributors/{user['id_hashed']}/export",
        json={"profile": "default"}, headers=admin_h,
    )
    assert second.status_code == 200, second.text
    assert second.json()["record_count"] == n


def test_org_export_rerunnable_after_org_export():
    """An org-wide export followed by a per-contributor export of a member in that
    org still finds the member's records (regression for the cross-button
    interaction that previously zeroed out per-contributor exports)."""
    admin_h = _admin_h()
    org = "Cascade Renal Group"
    u1 = _make_contributor(org=org)
    _submit_export_ready(admin_h, A.headers_for(u1))

    org_exp = client.post(
        f"/api/asclepius/organizations/{org}/export",
        json={"profile": "default"}, headers=admin_h,
    )
    assert org_exp.status_code == 200, org_exp.text

    contrib_exp = client.post(
        f"/api/asclepius/contributors/{u1['id_hashed']}/export",
        json={"profile": "default"}, headers=admin_h,
    )
    assert contrib_exp.status_code == 200, contrib_exp.text
    assert contrib_exp.json()["record_count"] >= 1


# ─── Tier B leak gate (hard guardrail) ────────────────────────────────────────
def test_tier_b_leak_gate_blocks_export(tmp_path, monkeypatch):
    # A misconfigured buyer profile that maps a record field to a Tier B key name
    # must fail the WHOLE batch loudly (422), never ship.
    leaky = {
        "name": "leaky",
        "preference_variant": "flat",
        "record_types": ["preference"],
        "field_maps": {"flat": {}, "preference": {"flat": {
            "type": "type", "prompt": "prompt", "chosen": "chosen", "rejected": "rejected",
            "annotator_credential": "npi",  # <-- smuggles a Tier B key name
        }}},
    }
    pdir = tmp_path / "profiles"
    pdir.mkdir()
    (pdir / "leaky.json").write_text(json.dumps(leaky))
    monkeypatch.setenv("ASCLEPIUS_PROFILES_DIR", str(pdir))
    asc_profiles.clear_cache()

    admin_h = _admin_h()
    user = _make_contributor()
    _submit_export_ready(admin_h, A.headers_for(user))

    r = client.post(
        f"/api/asclepius/contributors/{user['id_hashed']}/export",
        json={"profile": "leaky"}, headers=admin_h,
    )
    assert r.status_code == 422, r.text
    assert "npi" in r.text and "Tier B leak" in r.text


# ─── Further Credential Summary (dossier) ─────────────────────────────────────
def test_credential_summary_requires_ack():
    admin_h = _admin_h()
    user = _make_contributor()
    r = client.post(
        f"/api/asclepius/contributors/{user['id_hashed']}/credential-summary",
        json={"recipient": "Acme Labs", "acknowledged": False}, headers=admin_h,
    )
    assert r.status_code == 400
    assert "acknowledge" in r.text.lower()


def test_credential_summary_generates_pdf_json_and_audit():
    admin_h = _admin_h()
    user = _make_contributor()
    res = client.post(
        f"/api/asclepius/contributors/{user['id_hashed']}/credential-summary",
        json={"recipient": "Acme Verification Lab", "acknowledged": True}, headers=admin_h,
    ).json()
    sid = res["summary_id"]
    assert res["watermark"].startswith("CONFIDENTIAL")
    assert res["verification_handles"]["nppes_npi_lookup"].endswith("1234567893")

    base = f"/api/asclepius/contributors/{user['id_hashed']}/credential-summary/{sid}/download"
    pdf = client.get(base, headers=admin_h, params={"format": "pdf"})
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.content[:4] == b"%PDF"

    js = client.get(base, headers=admin_h, params={"format": "json"}).json()
    # Dossier carries Tier B + Tier A + the §9 notice, keyed by hashed id.
    assert js["hashed_annotator_id"] == user["id_hashed"]
    assert js["identifying_credentials"]["npi"] == "1234567893"
    assert js["credential_attributes"]["primary_specialty"] == "nephrology"
    assert "twenty-four (24) months" in js["non_circumvention_notice"]

    # Generation is audited.
    audit = client.get(
        f"/api/asclepius/contributors/{user['id_hashed']}/credential-summaries", headers=admin_h,
    ).json()["summaries"]
    assert any(s["summary_id"] == sid and s["recipient"] == "Acme Verification Lab" for s in audit)


# ─── Metrics ──────────────────────────────────────────────────────────────────
def test_metrics_org_and_contributor_with_last_labeled():
    admin_h = _admin_h()
    user = _make_contributor()
    _submit_export_ready(admin_h, A.headers_for(user))

    orgs = client.get("/api/asclepius/metrics/organizations", headers=admin_h).json()["organizations"]
    org = next(o for o in orgs if o["organization"] == "Riverside Nephrology Associates")
    assert org["last_labeled_at"]
    assert org["submission_count"] >= 1

    contribs = client.get("/api/asclepius/metrics/contributors", headers=admin_h).json()["contributors"]
    c = next(c for c in contribs if c["id_hashed"] == user["id_hashed"])
    assert c["last_labeled_at"]
    assert c["submission_count"] >= 1


def test_credential_summary_download_rejects_mismatched_contributor():
    admin_h = _admin_h()
    u1 = _make_contributor()
    u2 = _make_contributor()
    res = client.post(
        f"/api/asclepius/contributors/{u1['id_hashed']}/credential-summary",
        json={"acknowledged": True}, headers=admin_h,
    ).json()
    sid = res["summary_id"]
    # The summary belongs to u1; requesting it under u2's path must 404.
    bad = client.get(
        f"/api/asclepius/contributors/{u2['id_hashed']}/credential-summary/{sid}/download",
        headers=admin_h, params={"format": "json"},
    )
    assert bad.status_code == 404


# ─── Unit: blurb + leak scanner ───────────────────────────────────────────────
def test_blurb_and_leak_unit():
    blurb = asc_credentials.generalized_blurb(_SHIP)
    assert "Board-certified" in blurb and "fellowship-trained" in blurb and "NPI-verified" in blurb
    assert asc_credentials.find_tier_b_leak({"prompt": "x", "license": "CC", "annotator_credential": "y"}) is None
    assert asc_credentials.find_tier_b_leak({"a": {"npi": "1"}}) == "npi"


def test_value_scan_ignores_institutions_and_years_keeps_identifiers():
    # The value scan must NOT false-positive on institution names / years that can
    # legitimately appear in clinical text — only on locator/identifier values.
    verify = {
        "full_legal_name": "Jane A. Doe, MD", "npi": "1234567893",
        "medical_license_number": "A-104872", "license_state": "CA",
        "medical_school": "UCSF", "medical_school_year": "2004",
        "residency": "Stanford", "fellowship": "UCLA",
    }
    vals = asc_credentials.collect_verify_values([verify])
    legit = {"prompt": "Patient on dialysis since 2004 per the UCSF/Stanford protocol; UCLA criteria."}
    assert asc_credentials.find_tier_b_value_leak(legit, vals) is None
    assert asc_credentials.find_tier_b_value_leak({"r": "seen by Jane A. Doe, MD"}, vals) == "Jane A. Doe, MD"
    assert asc_credentials.find_tier_b_value_leak({"r": "npi 1234567893"}, vals) == "1234567893"
