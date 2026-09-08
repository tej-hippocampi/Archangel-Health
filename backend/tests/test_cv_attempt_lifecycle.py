"""An older CV extraction cannot speak for the document that replaced it.

Onboarding Master PRD, Phase 3 — PRD C §6-A, and §2 invariant 2.

WHAT WENT WRONG, reproduced against the unchanged worker before this shipped
(evidence/cv-extraction-evidence/worker_race_result.json): upload A finishing
after upload B wrote A's asset sha and A's parse over B's, while keeping B's
filename. A physician who replaced their CV got a review page built from the
document they had just replaced, labelled with the name of the one they meant.

Three things made it possible, and each has a test here:

  * there was no attempt identity, so a late write had nothing to be stale
    ABOUT — it could not know it had been superseded, and neither could the
    poll reading the result;
  * ``_record_cv_on_person`` read the whole credential object, edited a few CV
    keys and wrote it all back, so every edit the physician made in between went
    with it;
  * the terminal stage and the result were two separate writes, so a poll landing
    between them saw a finished extraction with the previous attempt's payload
    under it.

Nothing is deleted to fix any of this. A superseded attempt is retained as
history (§2 invariant 3); what it may no longer do is write.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from tests._asclepius import TMP_DIR, uniq

from team_store import TeamStore

_WIZARD = (pathlib.Path(__file__).resolve().parents[2] / "landing" / "src" / "app"
           / "components" / "OnboardingWizard.tsx").read_text(encoding="utf-8")


@pytest.fixture()
def store():
    import os
    path = os.path.join(TMP_DIR, f"team_cv_{uniq()}.db")
    return TeamStore(path)


@pytest.fixture()
def person(store):
    """A health system with a director, which is who uploads a CV."""
    email = f"dr-{uniq()}@example.com"
    hs = store.create_health_system_invite(
        invite_base_url="https://landing.test", director_email=email,
        product="asclepius")
    hs_id = hs["health_system_id"]
    store.upsert_asclepius_person(hs_id, email=email, full_name="Dr Example",
                                  clinical_role="attending", is_director=True)
    return hs_id, email


# ── Beginning B makes B current, in one step ────────────────────────────────

def test_the_first_upload_is_current(store, person):
    hs_id, email = person
    a = store.start_cv_attempt(hs_id, email, asset_sha="sha-a", filename="a.pdf")
    assert store.cv_attempt_is_current(a)
    assert (store.current_cv_attempt(hs_id, email) or {})["attempt_id"] == a


def test_a_second_upload_supersedes_the_first(store, person):
    hs_id, email = person
    a = store.start_cv_attempt(hs_id, email, asset_sha="sha-a", filename="a.pdf")
    b = store.start_cv_attempt(hs_id, email, asset_sha="sha-b", filename="b.pdf")
    assert not store.cv_attempt_is_current(a)
    assert store.cv_attempt_is_current(b)
    assert (store.current_cv_attempt(hs_id, email) or {})["attempt_id"] == b


def test_the_same_file_uploaded_twice_is_two_attempts(store, person):
    """A sha is not identity. §6-A says so explicitly: the same document
    uploaded again is a new attempt, and only the second is current — otherwise
    a re-upload of an identical file could be completed by the first run."""
    hs_id, email = person
    a = store.start_cv_attempt(hs_id, email, asset_sha="same", filename="cv.pdf")
    b = store.start_cv_attempt(hs_id, email, asset_sha="same", filename="cv.pdf")
    assert a != b
    assert not store.cv_attempt_is_current(a)
    assert store.cv_attempt_is_current(b)


def test_a_superseded_attempt_is_retained_as_history(store, person):
    """Retained, not deleted (§2 invariant 3). What happened to a physician's
    first upload is a thing an admin may need to answer."""
    hs_id, email = person
    a = store.start_cv_attempt(hs_id, email, asset_sha="sha-a", filename="a.pdf")
    store.start_cv_attempt(hs_id, email, asset_sha="sha-b", filename="b.pdf")
    kept = store.get_cv_attempt(a)
    assert kept is not None
    assert kept["state"] == "superseded"
    assert kept["filename"] == "a.pdf"


# ── A superseded attempt cannot write ───────────────────────────────────────

def test_a_late_success_from_the_old_upload_is_refused(store, person):
    """THE RECORDED INCIDENT. A finishing after B started must change nothing:
    not B's state, not B's result, not B's filename."""
    hs_id, email = person
    a = store.start_cv_attempt(hs_id, email, asset_sha="sha-a", filename="a.pdf")
    b = store.start_cv_attempt(hs_id, email, asset_sha="sha-b", filename="b.pdf")

    assert store.advance_cv_attempt(
        a, "ready", result={"ok": True, "full_name": "From document A"},
        terminal=True) is False

    stale = store.get_cv_attempt(a)
    assert stale["state"] == "superseded"
    assert stale["result"] is None
    current = store.get_cv_attempt(b)
    assert current["state"] == "queued"
    assert current["filename"] == "b.pdf"


def test_a_late_failure_from_the_old_upload_is_refused(store, person):
    """The other direction, and the one §6-A calls out by name: a failed A must
    not land as B's failure."""
    hs_id, email = person
    a = store.start_cv_attempt(hs_id, email, asset_sha="sha-a")
    b = store.start_cv_attempt(hs_id, email, asset_sha="sha-b")
    store.advance_cv_attempt(b, "reading")
    assert store.advance_cv_attempt(a, "failed", terminal=True) is False
    assert store.get_cv_attempt(b)["state"] == "reading"


def test_progress_from_the_old_upload_is_refused_too(store, person):
    """Not only the terminal write. A stale stage would move the physician's
    progress captions backwards on a document that is not being read."""
    hs_id, email = person
    a = store.start_cv_attempt(hs_id, email, asset_sha="sha-a")
    b = store.start_cv_attempt(hs_id, email, asset_sha="sha-b")
    store.advance_cv_attempt(b, "extracting")
    assert store.advance_cv_attempt(a, "reading") is False
    assert store.get_cv_attempt(b)["state"] == "extracting"


# ── Terminal state and result are one write ─────────────────────────────────

def test_a_terminal_state_carries_its_result(store, person):
    """There is no window in which an attempt reads finished with nothing, or
    with the previous attempt's payload, under it."""
    hs_id, email = person
    a = store.start_cv_attempt(hs_id, email, asset_sha="sha-a")
    store.advance_cv_attempt(a, "ready", result={"ok": True, "full_name": "Dr A"},
                             terminal=True)
    rec = store.get_cv_attempt(a)
    assert rec["state"] == "ready"
    assert rec["result"]["full_name"] == "Dr A"
    assert rec["finished_at"], "a terminal attempt records when it finished"


def test_a_non_terminal_advance_does_not_stamp_a_finish(store, person):
    hs_id, email = person
    a = store.start_cv_attempt(hs_id, email, asset_sha="sha-a")
    store.advance_cv_attempt(a, "extracting")
    assert store.get_cv_attempt(a)["finished_at"] is None


# ── The physician's edits survive the worker ────────────────────────────────

def test_a_worker_write_does_not_erase_a_concurrent_edit(store, person):
    """The whole-object save, from the physician's side. They edit the review
    page while the parse is still running; the worker reads the credentials,
    edits four CV keys and writes the whole blob back — over everything they
    just typed. Merging named keys is what makes both survive."""
    hs_id, email = person
    store.save_asclepius_credentials(hs_id, email, {"fullLegalName": "Dr Original"})

    # The worker's read happens here, in the middle of the physician's edit.
    before = (store.get_asclepius_person(hs_id, email) or {}).get("credentials") or {}
    assert before["fullLegalName"] == "Dr Original"

    # The physician corrects their name while the parse runs.
    store.save_asclepius_credentials(hs_id, email, {"fullLegalName": "Dr Corrected",
                                                    "phone": "+1 202 555 0147"})
    # And now the worker lands.
    merged = store.merge_asclepius_credentials(
        hs_id, email, {"cvParseStage": "done", "cvParsed": {"ok": True}})

    assert merged["fullLegalName"] == "Dr Corrected", "the worker overwrote an edit"
    assert merged["phone"] == "+1 202 555 0147"
    assert merged["cvParseStage"] == "done"


def test_the_merge_touches_only_the_keys_it_is_given(store, person):
    hs_id, email = person
    store.save_asclepius_credentials(hs_id, email, {"a": 1, "b": 2})
    merged = store.merge_asclepius_credentials(hs_id, email, {"b": 3, "c": 4})
    assert merged == {"a": 1, "b": 3, "c": 4}


def test_merging_onto_a_person_who_does_not_exist_is_harmless(store):
    assert store.merge_asclepius_credentials("no-such-hs", "nobody@example.com",
                                             {"x": 1}) == {}


# ── The client stops polling for a document it replaced ─────────────────────

def test_the_poll_carries_the_attempt_it_is_watching():
    assert "pollCvParse = useCallback(async (attemptId?: string)" in _WIZARD
    assert "cvAttemptRef" in _WIZARD


def test_the_poll_checks_itself_at_every_await_boundary():
    """A single check at the top would still apply a result that arrived during
    the request it was already making."""
    start = _WIZARD.index("const pollCvParse")
    body = _WIZARD[start:_WIZARD.index("const uploadCv", start)]
    assert body.count("if (obsolete()) return;") >= 3, body.count("obsolete()")


def test_the_poll_also_trusts_the_servers_answer_about_whose_result_it_is():
    """Belt and braces with the ref: this catches a response that was already in
    flight when the new upload started."""
    start = _WIZARD.index("const pollCvParse")
    body = _WIZARD[start:_WIZARD.index("const uploadCv", start)]
    assert "body.attempt_id" in body
    assert "served !== attemptId" in body


def test_unmounting_the_wizard_stops_the_poll():
    assert "CV_POLL_CANCELLED" in _WIZARD
    assert "useEffect(() => () => { cvAttemptRef.current = CV_POLL_CANCELLED; }, []);" in _WIZARD


def test_a_re_upload_claims_the_attempt_before_polling():
    upload = _WIZARD[_WIZARD.index("const uploadCv"):]
    upload = upload[:upload.index("const skipCv")]
    assert "cvAttemptRef.current = attemptId;" in upload
    assert "void pollCvParse(attemptId);" in upload
    assert upload.index("cvAttemptRef.current = attemptId;") < upload.index("void pollCvParse")


def test_an_older_server_that_sends_no_attempt_id_still_works():
    """The compatibility half of §6-A: a poll with no attempt id behaves exactly
    as it did before, rather than refusing every response."""
    start = _WIZARD.index("const pollCvParse")
    body = _WIZARD[start:_WIZARD.index("const uploadCv", start)]
    assert "attemptId !== undefined" in body
    assert "served !== undefined" in body


# ── The attempt is server-owned ─────────────────────────────────────────────

def test_a_client_cannot_choose_its_own_attempt_id():
    """Same rule as the asset sha: a client-set attempt id would let a signup
    point its own dossier at somebody else's extraction."""
    from routers import onboarding as onb
    assert "cvAttemptId" in onb._SERVER_CV_KEYS


def test_the_parser_version_is_recorded():
    """A result in the database is undatable without it: "the parser used to get
    this wrong" is unanswerable when there is no record of which parser ran."""
    from asclepius import credentialing
    assert credentialing.PARSER_VERSION
