"""36-hour eligibility, durable delivery, and the approved email contract."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import socket

import pytest

from asclepius import exam_reminder as R
import tests._asclepius as A

NOW = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)


class Team:
    def __init__(self, rows=()):
        self.rows = rows

    def completed_physician_applications(self):
        return self.rows


@pytest.fixture
def store():
    return A.fresh_store()


@pytest.fixture
def mail(monkeypatch):
    import email_utils
    monkeypatch.setenv("ASCLEPIUS_PORTAL_URL", "https://app.archangelhealth.ai")
    monkeypatch.setenv("ASCLEPIUS_EXAM_REMINDER_ENABLED", "1")
    monkeypatch.setattr(R, "utcnow", lambda: NOW)
    monkeypatch.setattr(email_utils, "is_email_transport_configured", lambda: True)
    monkeypatch.setattr(email_utils, "is_email_dev_mode", lambda: False)
    sent = []

    async def send(to, subject, body, **kwargs):
        kwargs["delivery_info"].update(outcome="accepted", provider_id="provider-test")
        sent.append((to, subject, body, kwargs["text_body"]))
        return True, "sent"

    monkeypatch.setattr(email_utils, "send_html_email_with_reason", send)
    return sent


def applicant(store, *, age=36, state="not_started", completed=True, **changes):
    user = A.make_user(store, tier=None, specialty="nephrology", practice_case=True)
    fields = {"verification_status": "pending", "cv_asset_sha": "cv-sha",
              "full_name": "Dr. Morgan", "application_completed_at": R.iso(NOW - timedelta(hours=age)) if completed else None,
              "created_at": R.iso(NOW - timedelta(days=30)), **changes}
    with store._conn() as conn:
        conn.execute("UPDATE users SET " + ", ".join(f"{key} = ?" for key in fields) + " WHERE id = ?", (*fields.values(), user["id"]))
    tutorial = store.get_tutorial_state(user["id"])
    tutorial["exam"] = {"state": state}
    store.set_tutorial_state(user["id"], tutorial)
    return store.get_user_by_id(user["id"])


def sweep(store, team=None, **kwargs):
    return asyncio.run(R.sweep(store, team or Team(), **kwargs))


def test_exact_boundary_and_not_account_creation(store, mail):
    user = applicant(store, age=36 - 1 / 3600)
    assert sweep(store) == 0
    with store._conn() as conn:
        conn.execute("UPDATE users SET application_completed_at = ? WHERE id = ?", (R.iso(NOW - timedelta(hours=36)), user["id"]))
    assert sweep(store) == 1
    assert sweep(store) == 0
    assert len(mail) == 1
    delivery = R.history(store)[0]
    assert delivery["status"] == "accepted" and delivery["provider_id"] == "provider-test"
    assert store.get_user_by_id(user["id"])["nudge_exam_sent_at"]


@pytest.mark.parametrize("state,changes,reason", [
    ("submitted", {}, "examination_submitted"),
    ("corrupt", {}, "examination_state_unknown"),
    ("not_started", {"cv_asset_sha": None}, "credentials_missing"),
    ("not_started", {"verification_status": "approved"}, "application_decided"),
    ("not_started", {"verification_status": "rejected"}, "application_decided"),
    ("not_started", {"active": 0}, "inactive_or_test_account"),
    ("not_started", {"role": "admin"}, "not_physician"),
    ("not_started", {"account_kind": "advisor"}, "not_physician"),
    ("not_started", {"email": "a@example.org\nBcc: b@example.org"}, "invalid_email"),
    ("not_started", {"nudge_exam_sent_at": "2026-09-09"}, "previously_reminded"),
])
def test_suppression(store, mail, state, changes, reason):
    user = applicant(store, state=state, **changes)
    assert R.skip_reason(user, user["application_completed_at"], now=NOW) == reason
    assert sweep(store) == 0
    assert mail == []


def test_in_progress_qualifies_and_edits_do_not_restart_clock(store, mail):
    user = applicant(store, state="in_progress")
    assert not store.mark_application_completed(user["id"], R.iso(NOW))
    assert sweep(store) == 1


def test_historical_evidence_and_preview_are_read_only(store, mail):
    user = applicant(store, completed=False)
    report = R.preview(store, Team(), now=NOW)
    assert report["counts"] == {"completion_time_unknown": 1}
    team = Team([{"email": user["email"], "completed_at": R.iso(NOW - timedelta(hours=40))}])
    assert R.preview(store, team, now=NOW)["counts"] == {"due": 1}
    assert R.history(store) == []
    assert store.get_user_by_id(user["id"])["application_completed_at"] is None
    assert sweep(store, team) == 1
    assert store.get_user_by_id(user["id"])["application_completed_at"] == team.rows[0]["completed_at"]


def test_racing_workers_have_one_claim(store, mail):
    user = applicant(store)
    with ThreadPoolExecutor(max_workers=5) as pool:
        claims = list(pool.map(lambda _: R.claim(store, user["id"], user["application_completed_at"], now=NOW), range(5)))
    assert sum(bool(token) for token in claims) == 1
    assert sweep(store) == 0  # restart while outcome is unknown cannot resend
    assert not store.get_user_by_id(user["id"])["nudge_exam_sent_at"]


def test_batch_drains_past_ineligible_rows(store, mail):
    for _ in range(4):
        applicant(store, age=80, state="submitted")
    for _ in range(3):
        applicant(store, age=40)
    assert sweep(store, limit=2) == 2
    assert sweep(store, limit=2) == 1
    assert sweep(store, limit=2) == 0
    assert len(mail) == 3


def test_submission_between_selection_and_send_is_suppressed(store, mail, monkeypatch):
    user = applicant(store)
    original = R.claim

    def claim_then_submit(*args, **kwargs):
        token = original(*args, **kwargs)
        state = store.get_tutorial_state(user["id"])
        state["exam"] = {"state": "submitted"}
        store.set_tutorial_state(user["id"], state)
        return token

    monkeypatch.setattr(R, "claim", claim_then_submit)
    assert sweep(store) == 0
    assert not mail
    assert R.history(store)[0]["status"] == "suppressed"


def test_confirmed_failure_retries_but_unknown_does_not(store, mail, monkeypatch):
    import email_utils
    user = applicant(store)
    real_fake = email_utils.send_html_email_with_reason

    async def rejected(*args, **kwargs):
        kwargs["delivery_info"]["outcome"] = "retryable"
        return False, "429"

    monkeypatch.setattr(email_utils, "send_html_email_with_reason", rejected)
    assert sweep(store) == 0
    assert R.history(store)[0]["status"] == "retryable"
    assert not store.get_user_by_id(user["id"])["nudge_exam_sent_at"]
    assert sweep(store) == 0
    monkeypatch.setattr(R, "utcnow", lambda: NOW + timedelta(minutes=16))
    monkeypatch.setattr(email_utils, "send_html_email_with_reason", real_fake)
    assert sweep(store) == 1
    assert R.history(store)[0]["attempts"] == 2

    other = applicant(store)

    async def timeout(*args, **kwargs):
        raise socket.timeout("response lost")

    monkeypatch.setattr(email_utils, "send_html_email_with_reason", timeout)
    assert sweep(store) == 0
    monkeypatch.setattr(email_utils, "send_html_email_with_reason", real_fake)
    assert sweep(store) == 0
    assert next(row for row in R.history(store) if row["user_id"] == other["id"])["status"] == "unknown"


def test_pause_dev_mode_and_invalid_portal_never_consume_reminder(store, mail, monkeypatch):
    import email_utils
    applicant(store)
    monkeypatch.setenv("ASCLEPIUS_EXAM_REMINDER_ENABLED", "0")
    assert sweep(store) == 0
    monkeypatch.setenv("ASCLEPIUS_EXAM_REMINDER_ENABLED", "1")
    monkeypatch.setattr(email_utils, "is_email_dev_mode", lambda: True)
    assert sweep(store) == 0
    monkeypatch.setattr(email_utils, "is_email_dev_mode", lambda: False)
    monkeypatch.setenv("ASCLEPIUS_PORTAL_URL", "http://localhost:8000")
    assert sweep(store) == 0
    assert not R.history(store) and not mail


def test_email_matches_approved_copy_and_escapes_name():
    from onboarding_emails import build_exam_nudge_email, build_exam_nudge_text
    from html.parser import HTMLParser
    class Tags(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tags = []
        def handle_starttag(self, tag, attrs):
            self.tags.append((tag, dict(attrs)))
    body = build_exam_nudge_email(first_name='<img src=x onerror="boom">', portal_url="https://app.archangelhealth.ai/asclepius")
    parsed = Tags()
    parsed.feed(body)
    assert ">This is the last step!</h1>" in body
    assert not any(tag == "img" for tag, _ in parsed.tags)
    links = [attrs for tag, attrs in parsed.tags if tag == "a"]
    assert len(links) == 1
    assert links[0]["href"] == "https://app.archangelhealth.ai/asclepius#examination"
    assert "#fbfcfa" in body and "#f4f5f3" in body
    assert "in and read" not in body and "Confidential" not in body
    assert "Hello," in build_exam_nudge_text(first_name="", portal_url="https://app.archangelhealth.ai/asclepius")


def test_sendgrid_plain_text_and_uncertain_timeout(monkeypatch):
    import email_utils
    import sendgrid
    monkeypatch.setenv("EMAIL_DEV_MODE", "0")
    monkeypatch.setenv("SENDGRID_API_KEY", "SG.test-key")
    from tests.test_email_send_timeout import _FakeSendGrid
    captured = []

    class Sender(_FakeSendGrid):
        def send(self, message):
            captured.append(message.get())
            raise socket.timeout("accepted but reply lost")

    monkeypatch.setattr(sendgrid, "SendGridAPIClient", Sender)
    info = {"reminder_id": "test-claim"}
    ok, detail = asyncio.run(email_utils.send_html_email_with_reason(
        "test@example.org", "subject", "<p>HTML</p>", text_body="Plain text", delivery_info=info))
    assert not ok and info["outcome"] == "unknown" and "unknown" in detail
    assert {part["type"] for part in captured[0]["content"]} == {"text/plain", "text/html"}
    assert captured[0]["custom_args"]["exam_reminder_id"] == "test-claim"


def test_sandbox_link_retains_realm_and_failed_capture_is_not_accepted(store, mail, monkeypatch):
    import realm
    import email_utils
    from urllib.parse import urlsplit, parse_qs
    applicant(store)
    async def fail_capture(*args, **kwargs):
        kwargs["delivery_info"]["outcome"] = "sandbox_outbox"
        return False, "sandbox_outbox_failed"
    monkeypatch.setattr(email_utils, "send_html_email_with_reason", fail_capture)
    with realm.scoped("sandbox"):
        url = urlsplit(R.portal_url())
        assert parse_qs(url.query) == {"realm": ["sandbox"]}
        assert url.fragment == "examination"
        assert sweep(store) == 0
    assert R.history(store)[0]["status"] == "retryable"
    assert R.history(store)[0]["accepted_at"] is None


def test_lost_worker_is_visible_as_unknown_and_never_retried(store, mail, monkeypatch):
    user = applicant(store)
    assert R.claim(store, user["id"], user["application_completed_at"], now=NOW)
    monkeypatch.setattr(R, "utcnow", lambda: NOW + timedelta(minutes=16))
    assert sweep(store) == 0
    assert R.history(store)[0]["status"] == "unknown"
    assert not mail


def test_retries_stop_after_three_confirmed_refusals(store, mail, monkeypatch):
    import email_utils
    applicant(store)
    async def rejected(*args, **kwargs):
        kwargs["delivery_info"]["outcome"] = "retryable"
        return False, "429"
    monkeypatch.setattr(email_utils, "send_html_email_with_reason", rejected)
    for minutes in (0, 16, 47, 120):
        monkeypatch.setattr(R, "utcnow", lambda: NOW + timedelta(minutes=minutes))
        assert sweep(store) == 0
    row = R.history(store)[0]
    assert row["attempts"] == 3 and row["status"] == "failed"


@pytest.mark.parametrize("failure,expected", [("data_451", "retryable"), ("data_550", "failed"), ("timeout", "unknown"), ("quit_451", "accepted")])
def test_smtp_outcome_classifies_rejections_and_acceptance(monkeypatch, failure, expected):
    import email_utils
    import smtplib
    monkeypatch.setenv("EMAIL_DEV_MODE", "0")
    monkeypatch.setenv("SENDGRID_API_KEY", "")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.org")
    monkeypatch.setenv("SMTP_USER", "test")
    monkeypatch.setenv("SMTP_PASS", "test")
    messages = []
    class SMTP:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, kind, value, tb):
            if failure == "quit_451":
                raise smtplib.SMTPResponseException(451, b"QUIT failed after acceptance")
        def starttls(self): pass
        def login(self, *args): pass
        def send_message(self, message):
            messages.append(message)
            if failure.startswith("data_"):
                raise smtplib.SMTPDataError(int(failure[5:]), b"DATA rejected")
            if failure == "timeout":
                raise socket.timeout("DATA response lost")
            return {}
    monkeypatch.setattr(smtplib, "SMTP", SMTP)
    info = {"reminder_id": "smtp-test"}
    ok, reason = asyncio.run(email_utils.send_html_email_with_reason("test@example.org", "s", "<p>HTML</p>", text_body="Plain", delivery_info=info))
    assert info["outcome"] == expected
    assert ok == (expected == "accepted")
    assert messages[0]["X-Archangel-Reminder-ID"] == "smtp-test"
    assert {part.get_content_type() for part in messages[0].get_payload()} == {"text/plain", "text/html"}
