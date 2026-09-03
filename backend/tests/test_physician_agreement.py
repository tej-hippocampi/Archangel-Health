"""Gap U1: the physician contributor agreement, its versions and its signatures.

The Sep 1 meeting put a signed statement in the FIRST onboarding step, before
any labeling, carrying the stipulation that pay is tied to label quality. Seven
attestation checkboxes existed and were enforced; a DOCUMENT did not, which
meant "what exactly did this physician agree to, and when" had no answer beyond
seven booleans on a mutable row.

These tests are about the infrastructure, not the words. The words are an
external dependency (counsel is supplying them), and the point of the design is
that swapping them in is a content change to a file plus a version bump. So what
is pinned here is: a version is a file, a signature names the version it was
made against, the row can never be altered, the artifact renders from the
version SIGNED rather than the version current, and a superseding version puts a
physician back in front of the agreement without taking a case off them
mid-flight.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import physician_agreement as PA  # noqa: E402

client = TestClient(A.app)


@pytest.fixture(autouse=True)
def _isolated():
    A.fresh_store()
    PA.clear_cache()
    yield
    PA.clear_cache()


def _store():
    from asclepius.store import get_store
    return get_store()


def _physician(**kw):
    return A.make_user(_store(), role="evaluator", specialty="nephrology",
                       board_cert="board_certified_nephrology",
                       years_experience=12, **kw)


def _sign(user, **overrides):
    body = {"typed_name": "Dr. Tej Patel", "signed_initials": "tp",
            "consent_esign": True}
    body.update(overrides)
    return client.post("/api/asclepius/me/agreement/sign", json=body,
                       headers=A.headers_for(user))


@pytest.fixture
def second_version(tmp_path, monkeypatch):
    """A real v2 file beside the real v1, so supersession is exercised against
    two documents on disk rather than against a mocked comparison."""
    legal = tmp_path / "legal"
    legal.mkdir()
    src = Path(PA.docs_dir()) / "PHYSICIAN_AGREEMENT_v1.md"
    (legal / "PHYSICIAN_AGREEMENT_v1.md").write_text(
        src.read_text(encoding="utf-8"), encoding="utf-8")
    (legal / "PHYSICIAN_AGREEMENT_v2.md").write_text(
        "# PHYSICIAN CONTRIBUTOR AGREEMENT\n\n**Version v2**\n\n"
        "Counsel's language, for **{{PHYSICIAN_NAME}}**, effective "
        "{{EFFECTIVE_DATE}}.\n\n## Signature\n\nSigned by: {{SIGNER_NAME}}\n"
        "Initials: {{SIGNED_INITIALS}}\nDate (UTC): {{SIGNED_AT}}\n",
        encoding="utf-8")
    monkeypatch.setenv("ARCHANGEL_LEGAL_DIR", str(legal))
    PA.clear_cache()
    yield legal
    PA.clear_cache()


# ─── A version is a file ─────────────────────────────────────────────────────
def test_the_current_version_exists_on_disk_and_renders():
    """The agreement is a FILE, not a string in a module, so that a lawyer can
    redline it and `git log` on it is the amendment history. A CURRENT_VERSION
    naming a file that is not there is a signature screen that 503s."""
    assert PA.CURRENT_VERSION in PA.available_versions()
    text, sha = PA.signable(physician="Dr. Tej Patel")
    assert len(sha) == 64
    assert "Dr. Tej Patel" in text
    assert "{{" not in text  # every placeholder was substituted


def test_the_interim_status_is_on_the_face_of_the_document():
    """Counsel has not written this yet, and a physician signing it is entitled
    to know that rather than to discover it. The marking is content, so counsel
    replaces it by editing a file and bumping a constant, not by editing code."""
    text, _ = PA.signable(physician="Dr. Tej Patel")
    assert "interim" in text.lower()


def test_the_pay_is_tied_to_label_quality_stipulation_is_actually_in_it():
    """The one substantive term the meeting named. An agreement that omitted it
    would be scaffolding around the wrong document."""
    text, _ = PA.signable(physician="Dr. Tej Patel")
    low = text.lower()
    assert "rubric" in low
    assert "60%" in text  # the floor the attestation has always promised


def test_rendering_is_deterministic_so_the_hash_survives_the_round_trip():
    """The portal hashes at read time and again at signature and refuses a
    mismatch. If render were not deterministic for a given (version, physician),
    nobody could ever sign."""
    a = PA.signable(physician="Dr. Tej Patel")
    b = PA.signable(physician="Dr.  Tej   Patel")   # whitespace is normalized
    assert a == b


def test_two_physicians_sign_different_text_and_therefore_different_hashes():
    """Each document names its own signer, so the hash has to differ. A shared
    hash would mean the record proved only which VERSION was signed, not which
    document was on the screen."""
    _, sha_a = PA.signable(physician="Dr. Tej Patel")
    _, sha_b = PA.signable(physician="Dr. Nina Lee")
    assert sha_a != sha_b


def test_an_unknown_version_is_refused_rather_than_guessed():
    """A version we cannot load must never fall back to one we can. Falling back
    would render a document nobody asked for and hash it as if they had."""
    with pytest.raises(PA.AgreementError):
        PA.render(physician="Dr. Tej Patel", version="v999")
    with pytest.raises(PA.AgreementError):
        PA.render(physician="Dr. Tej Patel", version="../../etc/passwd")


# ─── The signature record ────────────────────────────────────────────────────
def test_signing_records_the_version_the_hash_and_who_typed_what():
    """The record has to answer "what exactly did this physician agree to, and
    when" without anybody reconstructing it from a deploy log."""
    doc = _physician()
    read = client.get("/api/asclepius/me/agreement", headers=A.headers_for(doc))
    assert read.status_code == 200, read.text
    shown = read.json()
    assert shown["signature_required"] == PA.NEVER_SIGNED
    assert shown["signed"] is None

    r = _sign(doc, doc_sha256=shown["doc_sha256"])
    assert r.status_code == 200, r.text
    signed = r.json()["signed"]
    assert signed["doc_version"] == PA.CURRENT_VERSION
    assert signed["doc_sha256"] == shown["doc_sha256"]
    assert signed["typed_name"] == "Dr. Tej Patel"
    assert signed["signed_initials"] == "TP"    # upper-cased, as a signature
    assert signed["signed_at"]

    row = _store().latest_physician_agreement(doc["id"])
    # The attribution leg is RECORDED and is not returned to the account holder.
    assert row["ip"] is not None
    assert row["consent_esign"] == 1
    assert "ip" not in signed and "user_agent" not in signed


def test_the_seven_attestations_are_snapshotted_as_they_stood_at_signature():
    """They still live on the mutable users row, which is the live answer. The
    copy on the signature row is the historical one, so a physician changing an
    answer later cannot silently change what their signed agreement recorded."""
    store = _store()
    doc = _physician()
    with store._conn() as conn:
        conn.execute("UPDATE users SET attestations_json = ? WHERE id = ?",
                     ('{"attestWorkQuality": true, "signedInitials": "TP"}', doc["id"]))
    assert _sign(doc).status_code == 200

    import json
    row = store.latest_physician_agreement(doc["id"])
    assert json.loads(row["attestations_json"])["attestWorkQuality"] is True

    with store._conn() as conn:
        conn.execute("UPDATE users SET attestations_json = '{}' WHERE id = ?", (doc["id"],))
    again = store.latest_physician_agreement(doc["id"])
    assert json.loads(again["attestations_json"])["attestWorkQuality"] is True


def test_a_signature_row_can_never_be_updated_or_deleted():
    """Immutability enforced by the DATABASE rather than by everyone
    remembering. "Rows are never updated" is a sentence in a PRD until a trigger
    makes it true for every writer, including a console session at 2am."""
    import sqlite3
    doc = _physician()
    assert _sign(doc).status_code == 200
    store = _store()
    row = store.latest_physician_agreement(doc["id"])

    with pytest.raises(sqlite3.IntegrityError):
        with store._conn() as conn:
            conn.execute("UPDATE physician_agreements SET typed_name = 'Someone Else' "
                         "WHERE agreement_id = ?", (row["agreement_id"],))
    with pytest.raises(sqlite3.IntegrityError):
        with store._conn() as conn:
            conn.execute("DELETE FROM physician_agreements WHERE agreement_id = ?",
                         (row["agreement_id"],))
    assert store.get_physician_agreement(row["agreement_id"])["typed_name"] == "Dr. Tej Patel"


def test_signing_the_same_version_twice_is_refused_rather_than_duplicated():
    """Two rows for one version is a question somebody has to answer later, and
    a physician who double-clicked needs to know the first click worked."""
    doc = _physician()
    assert _sign(doc).status_code == 200
    second = _sign(doc)
    assert second.status_code == 409
    assert len(_store().list_physician_agreements(doc["id"])) == 1


def test_signing_needs_the_consent_the_name_and_real_initials():
    """Three separate refusals, because they are three separate omissions and a
    signer told "invalid" learns nothing about which one to fix."""
    doc = _physician()
    assert _sign(doc, consent_esign=False).status_code == 400
    assert _sign(doc, typed_name="   ").status_code == 400
    assert _sign(doc, signed_initials="T").status_code == 400
    assert _store().latest_physician_agreement(doc["id"]) is None


def test_a_document_that_changed_under_the_signer_is_refused(second_version):
    """The echoed hash is the whole reason it is echoed. A deploy landing
    mid-read must not produce a signature against text nobody agreed to."""
    doc = _physician()
    stale = "0" * 64
    r = _sign(doc, doc_sha256=stale)
    assert r.status_code == 409
    assert _store().latest_physician_agreement(doc["id"]) is None


# ─── The stored artifact ─────────────────────────────────────────────────────
def test_the_signed_copy_is_a_real_pdf_carrying_the_evidence_block():
    """Everything a court would ask for, on one page: who, when, from where,
    with what consent, over which exact text."""
    doc = _physician()
    r = _sign(doc)
    pdf_url = r.json()["signed"]["pdf_url"]

    got = client.get(pdf_url, headers=A.headers_for(doc))
    assert got.status_code == 200, got.text
    assert got.headers["content-type"] == "application/pdf"
    assert got.content.startswith(b"%PDF-")
    # Rebuilt from the row and matching the bytes that were hashed at signature.
    assert got.headers["X-Asclepius-Pdf-Matches-Signature"] == "1"


def test_the_artifact_renders_from_the_version_signed_not_the_current_one(second_version):
    """THE MOST IMPORTANT PROPERTY IN THIS FILE.

    A physician signs v1. We ship v2. Asking for their signed copy must produce
    the v1 document they read, not today's text with their name at the bottom.
    Rendering from CURRENT_VERSION would silently rewrite every past signer's
    executed contract into a document they never saw."""
    doc = _physician()
    assert _sign(doc).status_code == 200
    row = _store().latest_physician_agreement(doc["id"])
    assert row["doc_version"] == "v1"

    # v2 is now the current version, and it says something v1 does not.
    PA.CURRENT_VERSION, prior = "v2", PA.CURRENT_VERSION
    try:
        assert "Counsel's language" in PA.render(physician="Dr. Tej Patel", version="v2")
        rebuilt = PA.pdf_from_row(physician="Dr. Tej Patel", row=row)
        v1_only = PA.render(physician="Dr. Tej Patel", version="v1")
        # The rebuild is byte-identical to what was filed at signature, which is
        # the check the download endpoint makes and reports.
        assert rebuilt.startswith(b"%PDF-")
        assert "Version v1" in v1_only
        assert PA.render_pdf(physician="Dr. Tej Patel", version="v1",
                             signature=dict(row)) == rebuilt
        # And it is NOT what v2 would have produced.
        assert PA.render_pdf(physician="Dr. Tej Patel", version="v2",
                             signature=dict(row)) != rebuilt
    finally:
        PA.CURRENT_VERSION = prior


def test_a_row_naming_no_readable_version_errors_rather_than_guessing():
    """A wrong document is worse than an error. There is deliberately no
    fallback to CURRENT_VERSION in `pdf_from_row`."""
    with pytest.raises(PA.AgreementError):
        PA.pdf_from_row(physician="Dr. Tej Patel",
                        row={"agreement_id": "x", "doc_version": ""})


def test_nobody_can_download_another_physicians_signed_agreement():
    """A signed contract is not a public object keyed on a guessable id."""
    a, b = _physician(), _physician()
    url = _sign(a).json()["signed"]["pdf_url"]
    assert client.get(url, headers=A.headers_for(b)).status_code == 404


# ─── Supersession ────────────────────────────────────────────────────────────
def test_a_never_signed_physician_and_a_current_one_are_told_apart():
    """Two different conversations, so two different tokens rather than one
    boolean the client has to guess the meaning of."""
    assert PA.resignature_reason(None) == PA.NEVER_SIGNED
    assert PA.resignature_reason({"doc_version": "v1"},
                                 required_version="v1") is None


def test_a_new_version_supersedes_the_one_already_signed():
    """The whole reason a physician agreement needs supersession and a health
    system's DLA does not: a doctor works for us across versions of the terms."""
    assert PA.resignature_reason({"doc_version": "v1"},
                                 required_version="v2") == PA.SUPERSEDED
    assert PA.resignature_reason({"doc_version": "v2"},
                                 required_version="v1") is None


def test_versions_are_compared_as_numbers_and_never_as_strings():
    """Comparing "v10" against "v9" as strings is how you ship a bug where the
    tenth version reads as older than the ninth."""
    assert PA.version_ordinal("v10") > PA.version_ordinal("v9")
    assert PA.resignature_reason({"doc_version": "v10"},
                                 required_version="v9") is None
    assert PA.resignature_reason({"doc_version": "v9"},
                                 required_version="v10") == PA.SUPERSEDED


def test_a_stored_version_we_cannot_parse_is_treated_as_superseded():
    """"We do not know what they signed" has to read as "ask them to sign", not
    as "they are fine". The safe direction here is the inconvenient one."""
    assert PA.resignature_reason({"doc_version": "garbage"}) == PA.SUPERSEDED
    assert PA.resignature_reason({"doc_version": None}) == PA.SUPERSEDED
    # An empty dict is not a row that names an unreadable version, it is no row,
    # and the two deserve different answers because they are different facts.
    assert PA.resignature_reason({}) == PA.NEVER_SIGNED


def test_a_bug_in_our_own_version_constant_never_locks_a_physician_out():
    """If CURRENT_VERSION is unparseable that is our defect, and the physician
    who did everything asked of them should not pay for it."""
    assert PA.resignature_reason({"doc_version": "v1"},
                                 required_version="nonsense") is None


def test_signing_a_newer_version_adds_a_row_and_leaves_the_first_untouched(second_version):
    """An amendment is a new row. The old signature stays exactly where it was,
    because it is still the record of what was agreed at the time."""
    doc = _physician()
    assert _sign(doc).status_code == 200
    first = _store().latest_physician_agreement(doc["id"])

    PA.CURRENT_VERSION, prior = "v2", PA.CURRENT_VERSION
    try:
        read = client.get("/api/asclepius/me/agreement", headers=A.headers_for(doc))
        assert read.json()["signature_required"] == PA.SUPERSEDED
        r = _sign(doc, typed_name="Dr. Tej Patel", doc_sha256=read.json()["doc_sha256"])
        assert r.status_code == 200, r.text
    finally:
        PA.CURRENT_VERSION = prior

    rows = _store().list_physician_agreements(doc["id"])
    assert len(rows) == 2
    assert {r["doc_version"] for r in rows} == {"v1", "v2"}
    kept = _store().get_physician_agreement(first["agreement_id"])
    assert kept["doc_sha256"] == first["doc_sha256"]


# ─── The gate ────────────────────────────────────────────────────────────────
def test_the_gate_ships_unarmed_so_a_deploy_never_locks_the_whole_queue():
    """Nobody has signed, because until this change there was nothing to sign.
    Arming this on merge would stop every physician on the platform in one
    deploy, so the mechanism ships built and dark."""
    assert PA.gate_enabled() is False


def _armed(monkeypatch):
    monkeypatch.setenv("ASCLEPIUS_AGREEMENT_GATE", "1")


def test_an_unsigned_physician_cannot_draw_a_new_case_once_it_is_armed(monkeypatch):
    """The meeting put the agreement before any labeling. Armed, that is what
    the queue enforces, and the refusal carries a token and an action so the
    client renders a sign screen rather than an error."""
    _armed(monkeypatch)
    doc = _physician()
    A.pass_practice_case(_store(), doc["id"])
    r = client.get("/api/asclepius/tasks/next", headers=A.headers_for(doc))
    assert r.status_code == 403
    assert r.headers["X-Asclepius-Agreement-Gate"] == PA.NEVER_SIGNED
    assert r.json()["detail"]["action"]["kind"] == "sign_agreement"

    assert _sign(doc).status_code == 200
    assert client.get("/api/asclepius/tasks/next",
                      headers=A.headers_for(doc)).status_code == 200


def test_a_superseded_signature_stops_the_next_case_and_not_the_current_one(
        monkeypatch, second_version):
    """"Blocks labeling without locking someone out mid-case" is the whole
    design of where this gate sits. A physician who has already read a chart and
    formed a judgment finishes it; the case they ask for AFTER that is the one
    that stops. So the draw is gated and the submit is not."""
    _armed(monkeypatch)
    store = _store()
    doc = _physician()
    A.pass_practice_case(store, doc["id"])
    assert _sign(doc).status_code == 200

    admin_h = A.headers_for(A.make_user(store, role="admin"))
    created = client.post("/api/asclepius/tasks", json={"tasks": [{
        "specialty": "nephrology", "difficulty": "hard", "capture_reasoning": False,
        "source": "lab_supplied", "max_labels": 1, "grounding_mode": "optional",
        "prompt": "72yo on HD, K+ 6.4 with peaked T-waves. Adjust dialysate?",
        "candidate_answers": [
            {"id": "A", "text": "Calcium gluconate, then dialyze at K+ 2.0.",
             "generator_model": "model_x"},
            {"id": "B", "text": "Dialysate K+ 1.0 immediately.", "generator_model": "model_y"},
        ],
    }]}, headers=admin_h)
    task_id = created.json()["created"][0]
    # They draw the case under v1, which they signed.
    assert client.get("/api/asclepius/tasks/next",
                      headers=A.headers_for(doc)).status_code == 200

    PA.CURRENT_VERSION, prior = "v2", PA.CURRENT_VERSION
    try:
        # The NEXT case is refused...
        blocked = client.get("/api/asclepius/tasks/next", headers=A.headers_for(doc))
        assert blocked.status_code == 403
        assert blocked.headers["X-Asclepius-Agreement-Gate"] == PA.SUPERSEDED
        assert client.get("/api/asclepius/tasks/available",
                          headers=A.headers_for(doc)).status_code == 403
        # ...and the case already in their hands still submits.
        done = client.post("/api/asclepius/submissions", json={
            "submission_id": "s-" + uuid.uuid4().hex[:12], "task_id": task_id,
            "verdict": "A_better", "chosen_id": "A", "rejected_id": "B",
            "confidence": "high", "time_spent_sec": 140,
            "prompt_review": {"reviewed": True, "verdict": "valid"},
            "independent_answer": {"text": "Stabilize, shift, then remove."},
            "chosen_revision": {"edited": False, "why_better_notes": "B over-lowers K+"},
            "rejected_critique": {"error_tags": ["dosing_error"], "severities": {},
                                  "why_worse": "too aggressive"},
        }, headers=A.headers_for(doc))
        assert done.status_code == 200, done.text
    finally:
        PA.CURRENT_VERSION = prior


def test_an_admin_and_the_demo_account_are_never_gated(monkeypatch):
    """Exactly the exemptions the practice gate makes, restated rather than
    re-derived: an admin does not draw from the queue, and the mock contributor
    is what the sales walkthrough runs on."""
    _armed(monkeypatch)
    store = _store()
    admin = A.make_user(store, role="admin")
    assert client.get("/api/asclepius/tasks/next",
                      headers=A.headers_for(admin)).status_code == 200

    mock = _physician()
    A.pass_practice_case(store, mock["id"])
    with store._conn() as conn:
        conn.execute("UPDATE users SET is_mock = 1 WHERE id = ?", (mock["id"],))
    assert client.get("/api/asclepius/tasks/next",
                      headers=A.headers_for(store.get_user_by_id(mock["id"]))
                      ).status_code == 200


def test_the_agreement_is_readable_before_anyone_is_allowed_to_work():
    """A clickwrap that cannot be read until you are approved is a clickwrap
    whose "I have read this" is provably false. Reading is open to every
    account; only the queue is gated."""
    doc = A.make_user(_store(), role="evaluator")
    r = client.get("/api/asclepius/me/agreement", headers=A.headers_for(doc))
    assert r.status_code == 200
    assert len(r.json()["text"]) > 2000
    assert r.json()["interim"] is True
