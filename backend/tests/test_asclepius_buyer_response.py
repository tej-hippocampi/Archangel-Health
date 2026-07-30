"""Buyer Response PRD — V4 ingestion, answer sealing, evidence, credentials.

Covers the PRD §12 acceptance matrix for the phases implemented here:
  * Answer sealing (tests 1-6): sealed ground truth never in the case, unknown
    Basic fails closed, sealed encrypted at rest + audited, Media findings hidden by
    default, DiagnosticReport interpretation split, post-hoc ablation arm.
  * V4 FHIR ingestion (tests 7-17): bare-json magic-link acceptance + door parity,
    Media→Study.asset, merge carries studies, Provenance citations, source_refs not
    grounded, eval-task captured, required-modality quarantine, synthetic flag never
    trusted, patient PHI never in fragments, the full end-to-end.
  * Credentials (tests 25-26): record credential never 'unspecified', record ⊆
    aggregate.

The bundle fixture is the real ``nephrology_pgnmid_bundle.json`` (§12).
"""
from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import ingestion as I  # noqa: E402
from asclepius.adapters import fhir_r4  # noqa: E402

client = TestClient(A.app)

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "nephrology_pgnmid_bundle.json"
_KEY = base64.urlsafe_b64encode(b"buyer-response-test-key-32-bytes").decode()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    A.fresh_store()
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest"))
    monkeypatch.setenv("ASCLEPIUS_ASSET_STORE", str(tmp_path / "assets"))
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", _KEY)
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


def _bundle_bytes() -> bytes:
    return _FIXTURE.read_bytes()


def _bundle_json() -> dict:
    return json.loads(_bundle_bytes())


def _ingest(store, raw: bytes, *, specialty: str = "nephrology",
            filename: str = "bundle.json") -> dict:
    """Ingest a loose partner file exactly as the magic-link door does: wrap →
    store_raw → insert upload → process. Returns the process summary."""
    data = I.wrap_loose_files([{"filename": filename, "content": raw}], specialty=specialty)
    uid = store.new_upload_id()
    rp = I.store_raw(uid, data)
    store.insert_ingest_upload(
        upload_id=uid, link_id="L1", partner_id="P1", filename=filename,
        sha256=I.sha256_hex(data), size_bytes=len(data), raw_path=rp, source_ip=None)
    summary = I.process_upload(store, uid)
    summary["upload_id"] = uid
    return summary


def _ingested_case(store, summary: dict) -> dict:
    cases = store.list_ingest_cases(upload_id=summary["upload_id"])
    assert len(cases) == 1, cases
    return cases[0]


# ─── Answer sealing (write these first) ───────────────────────────────────────
def test_sealed_ground_truth_never_in_case():
    """No string from ``sealed-json`` appears in the assembled case or its prompt."""
    from asclepius.cases import render_case_prompt

    store = _store()
    summary = _ingest(store, _bundle_bytes())
    assert summary["status"] == "ingested"
    ic = _ingested_case(store, summary)
    case = ic["case"]
    prompt = render_case_prompt(case, "diagnose")
    # Distinctive strings only present in the sealed answer key.
    for needle in ("MGRS spectrum", "rituximab", "clone directed therapy",
                   "premature closure", "post-infectious GN without paraffin"):
        assert needle.lower() not in json.dumps(case).lower(), needle
        assert needle.lower() not in prompt.lower(), needle


def test_unknown_basic_fails_closed():
    """A Basic with an unrecognized id is NOT treated as a task (fails closed to
    sealed handling); only the allowlisted eval-task id is a task."""
    assert fhir_r4._basic_disposition({"id": "mystery-basic", "code": {}}) != "task"
    assert fhir_r4._basic_disposition({"id": "eval-task"}) == "task"
    assert fhir_r4._basic_disposition(
        {"id": "x", "code": {"text": "adjudicated answer key"}}) == "sealed"
    # An unknown Basic carrying an answer never enters the case fragments.
    bundle = _bundle_json()
    bundle["entry"].append({"resource": {
        "resourceType": "Basic", "id": "surprise-key",
        "extension": [{"url": "x/sealed-json",
                       "valueString": json.dumps({"secret": "zebrafish tangent motif"})}]}})
    frag = fhir_r4.parse(json.dumps(bundle), specialty="nephrology")
    assert "zebrafish tangent motif" not in json.dumps(
        {k: v for k, v in frag.items() if not str(k).startswith("_")})


def test_sealed_encrypted_at_rest():
    """Sealed content is unreadable on disk without the key; every read audits."""
    store = _store()
    summary = _ingest(store, _bundle_bytes())
    ic = _ingested_case(store, summary)
    raw = store.get_sealed_ground_truth_raw(ic["ingest_case_id"])
    assert raw and raw.startswith("enc:"), "sealed payload must be encrypted at rest"
    assert "MGRS" not in raw and "rituximab" not in raw
    before = len(store.list_events(entity_type="sealed_ground_truth"))
    got = store.get_sealed_ground_truth(ic["ingest_case_id"], actor="adjudicator-1")
    assert got["payload"]["answer_key"]["diagnosis"]
    after = store.list_events(entity_type="sealed_ground_truth")
    assert len(after) == before + 1
    assert after[0]["event_type"] == "sealed_ground_truth_read"


def test_media_findings_hidden_by_default():
    """V4 study with an asset → policy hidden; interpretation absent from the prompt,
    neutral label present."""
    from asclepius.cases import render_case_prompt

    store = _store()
    summary = _ingest(store, _bundle_bytes())
    case = _ingested_case(store, summary)["case"]
    assert case["study_findings_policy"] == "hidden"
    prompt = render_case_prompt(case, "diagnose")
    # The pronase interpretation is the answer — it must not be in the prompt.
    assert "monotypic deposits" not in prompt.lower()
    assert "absent lambda" not in prompt.lower()
    # The neutral label is present.
    assert "pronase" in prompt.lower()
    # …but the interpretation is preserved on the study for the annotator/key.
    pron = [s for s in case["studies"] if "ronase" in s["label"]][0]
    assert "monotypic" in pron["findings"].lower()


def test_diagnostic_report_interpretation_split():
    """The flow-cytometry interpretive tail is model_visible False; objective visible."""
    store = _store()
    summary = _ingest(store, _bundle_bytes())
    case = _ingested_case(store, summary)["case"]
    flow_notes = [n for n in case["notes"] if "flow" in (n.get("note_type") or "").lower()]
    visible = [n for n in flow_notes if n.get("model_visible") is not False]
    hidden = [n for n in flow_notes if n.get("model_visible") is False]
    assert visible and hidden, flow_notes
    assert any("population is present" in n["text"].lower() for n in visible)
    assert any("clinically significant" in n["text"].lower() for n in hidden)
    assert all(n.get("withheld_reason") == "interpretive_conclusion" for n in hidden)


def test_post_hoc_ablation_arm():
    """The same case renders with and without findings; both hashes recorded/differ."""
    from asclepius.cases import render_case_prompt

    store = _store()
    summary = _ingest(store, _bundle_bytes())
    case = _ingested_case(store, summary)["case"]
    hidden = dict(case)
    hidden["study_findings_policy"] = "hidden"
    visible = dict(case)
    visible["study_findings_policy"] = "visible"
    ph = render_case_prompt(hidden, "q")
    pv = render_case_prompt(visible, "q")
    assert ph != pv
    assert "monotypic deposits" in pv.lower()
    assert "monotypic deposits" not in ph.lower()


# ─── V4 FHIR ingestion ────────────────────────────────────────────────────────
def test_bare_json_accepted_via_magic_link():
    """The literal bundle, unmodified, through the magic-link door, ingests.
    Regression for A1 (the door used to reject a non-zip)."""
    admin = A.headers_for(A.make_user(_store(), role="admin"))
    link = client.post("/api/asclepius/admin/upload-links",
                       json={"partner_id": "mercy", "specialty": "nephrology",
                             "expires_hours": 24, "one_time": True},
                       headers=admin).json()
    r = client.post(f"/api/asclepius/partner/uploads?t={link['token']}",
                    files={"file": ("bundle.json", _bundle_bytes(), "application/json")})
    assert r.status_code == 200, r.text
    res = r.json()
    st = client.get(f"/api/asclepius/partner/uploads/{res['upload_id']}?t={link['token']}")
    assert st.json()["status"] == "ingested", st.json()


def test_both_upload_doors_identical():
    """Same bytes wrapped by either door produce byte-identical bundles + cases."""
    from routers import asclepius_provider as prov

    raw = _bundle_bytes()
    files = [{"filename": "bundle.json", "content": raw}]
    a = I.wrap_loose_files(files, specialty="nephrology")
    b = prov._bundle_zip(files, specialty="nephrology")
    assert a == b, "both doors must wrap identically"
    # Determinism: wrapping again yields the same bytes (no timestamps leak in).
    assert I.wrap_loose_files(files, specialty="nephrology") == a


def test_media_becomes_study_asset():
    """All 5 Media → 5 studies with resolvable assets; _imaging_skipped == 0."""
    from asclepius.cases import study_has_valid_asset

    frag = fhir_r4.parse(_bundle_bytes(), specialty="nephrology")
    assert len(frag["studies"]) == 5
    assert frag["_imaging_skipped"] == 0
    assert all(study_has_valid_asset(s) for s in frag["studies"])


def test_merge_fragments_carries_studies():
    """Bundle assembly does not drop adapter studies/source_refs.
    Regression for the missing ``studies`` key."""
    frag = fhir_r4.parse(_bundle_bytes(), specialty="nephrology")
    merged = I._merge_fragments([frag])
    assert len(merged["studies"]) == 5
    assert len(merged["source_refs"]) == 7


def test_provenance_citations_captured():
    """All 7 Provenance.entity sources land in source_refs with identifiers."""
    store = _store()
    summary = _ingest(store, _bundle_bytes())
    case = _ingested_case(store, summary)["case"]
    refs = case["source_refs"]
    assert len(refs) == 7
    assert all(r.get("identifier") for r in refs)
    types = {r["source_type"] for r in refs}
    assert {"guideline", "consensus", "primary", "benchmark"} <= types


def test_source_refs_excluded_from_grounded():
    """Partner citations do NOT increment the annotator grounded count."""
    # source_refs live on the case, never in a record payload's grounded flag.
    store = _store()
    summary = _ingest(store, _bundle_bytes())
    case = _ingested_case(store, summary)["case"]
    assert case["source_refs"]
    # The case model has no 'grounded' field; grounded is computed from annotator
    # anchors in packaging/export, never from source_refs.
    assert "grounded" not in case


def test_eval_task_captured():
    """task-prompt, declared_difficulty, required-modalities stored; difficulty does
    not write empirical_difficulty."""
    store = _store()
    summary = _ingest(store, _bundle_bytes())
    ic = _ingested_case(store, summary)
    case = ic["case"]
    assert case["declared_difficulty"] == "frontier-hard"
    assert len(case["required_modalities"]) == 8
    # A CLAIM, never the measured field.
    assert "empirical_difficulty" not in case
    assert ic["report"]["eval_task"]["task_prompt"].startswith("Given the longitudinal")


def test_required_modality_missing_quarantines():
    """Drop the pronase Media → the case quarantines rather than shipping unanswerable."""
    store = _store()
    bundle = _bundle_json()
    bundle["entry"] = [e for e in bundle["entry"]
                       if e["resource"].get("id") != "media-pronase"]
    summary = _ingest(store, json.dumps(bundle).encode())
    assert summary["status"] == "quarantined", summary
    ic = _ingested_case(store, summary)
    assert "pronase" in json.dumps(ic["report"].get("missing_modalities") or []).lower()


def test_synthetic_flag_never_trusted():
    """With synthetic-patient: true and a real-looking name, the case still quarantines
    (the name trips the guard); the flag is recorded, never honored."""
    store = _store()
    # Inject the name into a NOTE so it reaches the case body (Patient.name is
    # discarded); the synthetic flag must not exempt it from the guard.
    bundle = _bundle_json()
    for e in bundle["entry"]:
        if e["resource"].get("id") == "doc-1":
            e["resource"]["content"][0]["attachment"]["data"] = base64.b64encode(
                b"Patient Eleanor Price contacted at 555-010-8821 regarding results."
            ).decode()
    summary = _ingest(store, json.dumps(bundle).encode())
    # The bundle declares synthetic=true, yet the identifiers still quarantine it.
    assert summary["status"] == "quarantined", summary
    # And the flag was captured only as provenance metadata.
    frag = fhir_r4.parse(json.dumps(bundle), specialty="nephrology")
    assert frag["_synthetic_declared"] is True


def test_patient_phi_never_in_fragments():
    """Name, phone, MRN, ZIP appear nowhere in the assembled case."""
    store = _store()
    summary = _ingest(store, _bundle_bytes())
    case = _ingested_case(store, summary)["case"]
    blob = json.dumps(case)
    for needle in ("Eleanor", "Price", "010-8821", "SYN-MRN", "958", "Sacramento"):
        assert needle not in blob, needle


def test_full_bundle_end_to_end():
    """The complete bundle → one ingested V4 case with all sections + sealed stored.
    The headline integration test."""
    store = _store()
    summary = _ingest(store, _bundle_bytes())
    assert summary == {**summary, "status": "ingested", "ingested": 1, "quarantined": 0}
    ic = _ingested_case(store, summary)
    case = ic["case"]
    assert case["case_source"] == "real_deid"
    assert len(case["studies"]) == 5
    assert sum(len(p["results"]) for p in case["lab_panels"]) >= 40
    assert len(case["notes"]) >= 6
    assert len(case["problem_list"]) == 5
    assert len(case["medications"]) == 6
    assert len(case["source_refs"]) == 7
    # Sealed answer key stored separately, not on the case.
    assert store.get_sealed_ground_truth(ic["ingest_case_id"]) is not None
    assert "sealed_ground_truth" not in case


# ─── Audit hardening regressions ──────────────────────────────────────────────
def test_direct_assertion_conclusion_withheld():
    """A DiagnosticReport conclusion that NAMES the diagnosis (no hedge word) is
    withheld from the model, not shipped as an objective note (audit fix, §2 A6)."""
    for sentence in ("Findings are diagnostic of IgA nephropathy.",
                     "No evidence of malignancy.",
                     "This represents crescentic glomerulonephritis."):
        obj, interp = fhir_r4._split_report_conclusion(sentence)
        assert interp and not obj, (sentence, obj, interp)


def test_diagnostic_media_title_demoted():
    """A bare-diagnosis Media title is demoted out of the always-visible label
    (audit fix, §3 B2) while a neutral modality descriptor is kept."""
    label, findings = fhir_r4._split_media_caption(
        {"content": {"title": "Crescentic glomerulonephritis"}, "note": []})
    assert "glomerulonephritis" not in label.lower()
    assert "glomerulonephritis" in findings.lower()
    # A neutral descriptor is preserved.
    keep_label, _ = fhir_r4._split_media_caption(
        {"content": {"title": "Routine frozen IF, C3"}, "note": []})
    assert keep_label == "Routine frozen IF, C3"


def test_malformed_bundle_entry_skipped_not_fatal():
    """A non-dict entry (null/string) skips only that entry, not the whole bundle
    (audit fix)."""
    bundle = _bundle_json()
    bundle["entry"].insert(0, None)
    bundle["entry"].insert(0, "garbage")
    frag = fhir_r4.parse(json.dumps(bundle), specialty="nephrology")
    assert len(frag["studies"]) == 5  # the good resources still parse


def test_short_sealed_answer_leak_detected():
    """A short (2-token) sealed answer appearing in the visible case is caught by the
    leakage guard (audit fix, §3 B1)."""
    sealed = {"answer_key": {"diagnosis": "membranous nephropathy"}}
    leak_case = {"notes": [{"text": "The biopsy shows membranous nephropathy pattern.",
                            "model_visible": True}]}
    with pytest.raises(I.AnswerLeakageError):
        I.assert_no_answer_leakage(leak_case, sealed)
    # A single-token answer is NOT flagged (would false-positive on differentials).
    I.assert_no_answer_leakage(
        {"notes": [{"text": "amyloidosis is on the differential", "model_visible": True}]},
        {"answer_key": {"dx": "amyloidosis"}})


# ─── Credentials (Buyer Response PRD §6 E1) ───────────────────────────────────
def _cred_task():
    return {"task_id": "t1", "specialty": "nephrology",
            "prompt": "K+ 6.4 with peaked T-waves — manage.",
            "candidate_answers": [{"id": "A", "text": "calcium then dialyze"},
                                  {"id": "B", "text": "insulin-dextrose only"}]}


def _cred_submission(annotator, **kw):
    base = {"submission_id": "s1", "task_id": "t1", "verdict": "A_better",
            "chosen_id": "A", "rejected_id": "B", "confidence": "high",
            "created_at": "2026-06-26T12:00:00", "annotator": annotator,
            "payload": {"verdict": "A_better", "chosen_id": "A", "rejected_id": "B"}}
    base.update(kw)
    return base


def test_record_credential_never_unspecified():
    """An unhydrated annotator raises rather than defaulting to 'unspecified'.
    Regression for the buyer's finding."""
    from asclepius.packaging import PackagingError, package_submission

    # No annotator dict and no store → cannot resolve → must raise, never ship.
    with pytest.raises(PackagingError):
        package_submission(_cred_task(), _cred_submission({}), None)

    # With a store, hydrate from the source of truth by evaluator id.
    store = _store()
    user = A.make_user(store, role="evaluator", specialty="nephrology",
                       board_cert="board_certified_nephrology")
    sub = _cred_submission({}, evaluator_id=user["id"])
    recs = package_submission(_cred_task(), sub, store)
    creds = {r["annotator_credential"] for r in recs}
    assert creds == {"board_certified_nephrology"}
    assert "unspecified" not in creds


def test_record_credentials_subset_of_aggregate():
    """Record-level credential values ⊆ aggregate list — the contradiction the buyer
    found becomes structurally impossible."""
    from asclepius.packaging import package_submission

    store = _store()
    user = A.make_user(store, role="evaluator", specialty="nephrology",
                       board_cert="board_certified_nephrology")
    block = store.annotator_block(user)
    # annotator_block emits the canonical 'credential' plus the deprecated alias.
    assert block["credential"] == "board_certified_nephrology"
    assert block["credentials"] == block["credential"]
    sub = _cred_submission(block, evaluator_id=user["id"])
    recs = package_submission(_cred_task(), sub, store)
    record_creds = {r["annotator_credential"] for r in recs}
    aggregate = {block["credential"]}
    assert record_creds <= aggregate
    assert "unspecified" not in record_creds
