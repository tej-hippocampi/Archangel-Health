"""PRD-4 — subprocessor BAA gate + PHI de-identification."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("ADMIN_USERNAME", "testadmin")
os.environ.setdefault("ADMIN_PASSWORD", "testadminpass")

import integrations.elevenlabs as el  # noqa: E402
from compliance import subprocessors as sp  # noqa: E402
from main import app  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


# ─── Registry + gate ─────────────────────────────────────────────────────────

def test_registry_baa_status(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_BAA_SIGNED", raising=False)
    monkeypatch.delenv("SENDGRID_BAA_SIGNED", raising=False)
    assert sp.phi_allowed("anthropic_api") is True       # BAA covers first-party API
    assert sp.phi_allowed("twilio_sms") is True
    assert sp.phi_allowed("sendgrid") is False            # never PHI-eligible
    assert sp.phi_allowed("elevenlabs") is False          # unconfirmed by default
    assert sp.phi_allowed("tavus") is False
    assert sp.phi_allowed("unknown_vendor") is False


def test_sendgrid_never_eligible_even_with_flag(monkeypatch):
    # SENDGRID is marked phi_eligible=False, so the BAA flag cannot turn it on.
    monkeypatch.setenv("SENDGRID_BAA_SIGNED", "1")
    assert sp.phi_allowed("sendgrid") is False


def test_baa_env_flag_flips_vendor(monkeypatch):
    assert sp.phi_allowed("elevenlabs") is False
    monkeypatch.setenv("ELEVENLABS_BAA_SIGNED", "1")
    assert sp.phi_allowed("elevenlabs") is True


def test_assert_phi_allowed(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_BAA_SIGNED", raising=False)
    sp.assert_phi_allowed("anthropic_api")  # no raise
    with pytest.raises(sp.SubprocessorPHIError):
        sp.assert_phi_allowed("sendgrid")
    with pytest.raises(sp.SubprocessorPHIError):
        sp.assert_phi_allowed("elevenlabs")


# ─── De-identification ───────────────────────────────────────────────────────

def test_deidentify_scrubs_identifiers():
    txt = ("Hi Maria Lopez, your surgery is on 2026-06-01 (June 1, 2026). "
           "Call 310-555-1234 or maria@example.com. MRN: AB12345. SSN 123-45-6789.")
    out = sp.deidentify_for_vendor(txt, patient_name="Maria Lopez")
    for leak in ("Maria", "Lopez", "2026-06-01", "June 1", "310-555-1234",
                 "maria@example.com", "AB12345", "123-45-6789"):
        assert leak not in out, f"leaked: {leak} -> {out}"


def test_deidentify_empty():
    assert sp.deidentify_for_vendor("") == ""
    assert sp.deidentify_for_vendor(None) == ""


def test_deidentify_is_speakable():
    """The de-identified script must read naturally for TTS: no bracketed tokens,
    natural phrases for identifiers, and possessives become 'your'."""
    out = sp.deidentify_for_vendor(
        "Hey Maria, your appendectomy on June 1, 2026 went well. "
        "Call 310-555-1234. Maria's recovery looks great.",
        patient_name="Maria Lopez",
    )
    assert "[" not in out and "]" not in out          # nothing bracketed is read aloud
    assert "the scheduled date" in out
    assert "the number on file" in out
    assert "your recovery" in out                      # possessive -> "your"
    assert "Maria" not in out and "June" not in out


def test_deidentify_case_sensitive_names_dont_clobber_words():
    # A common-word name ("Mark") must not scrub the ordinary lowercase word "mark".
    out = sp.deidentify_for_vendor(
        "Mark, the mark on your incision is healing.", patient_name="Mark"
    )
    assert "the mark on your incision" in out
    assert out.startswith("you,")


# ─── ElevenLabs boundary ─────────────────────────────────────────────────────

class _FakeResp:
    content = b"audio-bytes"

    def raise_for_status(self):  # noqa: D401
        return None


class _FakeClient:
    captured = None

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        _FakeClient.captured = json
        return _FakeResp()


async def _fake_save_audio(self, audio_bytes, patient_id):
    return "/audio/x.mp3"


@pytest.fixture()
def fake_eleven_http(monkeypatch, tmp_path):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    monkeypatch.setattr(el.httpx, "AsyncClient", _FakeClient)
    # keep the write off /tmp deterministic (must stay awaitable)
    monkeypatch.setattr(el.ElevenLabsClient, "_save_audio", _fake_save_audio)
    _FakeClient.captured = None


def test_elevenlabs_deidentifies_without_baa(monkeypatch, fake_eleven_http):
    monkeypatch.delenv("ELEVENLABS_BAA_SIGNED", raising=False)
    import asyncio
    asyncio.run(el.ElevenLabsClient().synthesize(
        "Hey Maria, your appendectomy on 2026-06-01 went well.",
        "p1", deid_terms=["Maria"],
    ))
    sent = _FakeClient.captured["text"]
    assert "Maria" not in sent and "2026-06-01" not in sent


def test_elevenlabs_sends_raw_with_baa(monkeypatch, fake_eleven_http):
    monkeypatch.setenv("ELEVENLABS_BAA_SIGNED", "1")
    import asyncio
    asyncio.run(el.ElevenLabsClient().synthesize(
        "Hey Maria, your appendectomy went well.", "p1", deid_terms=["Maria"],
    ))
    assert "Maria" in _FakeClient.captured["text"]


# ─── Email transport PHI eligibility ─────────────────────────────────────────

def test_email_phi_allowed_by_transport(monkeypatch):
    from email_utils import active_email_vendor, email_phi_allowed
    # SendGrid configured, no BAA -> not allowed
    monkeypatch.delenv("EMAIL_DEV_MODE", raising=False)
    monkeypatch.setenv("SENDGRID_API_KEY", "SG.test")
    monkeypatch.delenv("SMTP_HOST", raising=False)
    assert active_email_vendor() == "sendgrid"
    assert email_phi_allowed() is False
    # Self-hosted SMTP -> allowed
    monkeypatch.delenv("SENDGRID_API_KEY", raising=False)
    monkeypatch.setenv("SMTP_HOST", "smtp.internal")
    monkeypatch.setenv("SMTP_USER", "u")
    monkeypatch.setenv("SMTP_PASS", "p")
    assert active_email_vendor() == "smtp"
    assert email_phi_allowed() is True


# ─── Admin compliance endpoint ───────────────────────────────────────────────

def test_admin_subprocessors_endpoint(client):
    from routers.admin import _create_token
    h = {"Authorization": f"Bearer {_create_token()}"}
    r = client.get("/admin/compliance/subprocessors", headers=h)
    assert r.status_code == 200, r.text
    data = r.json()
    keys = {row["key"] for row in data["subprocessors"]}
    assert {"anthropic_api", "sendgrid", "elevenlabs", "tavus", "twilio_sms"} <= keys
    assert "email_transport" in data


def test_admin_subprocessors_requires_admin(client):
    assert client.get("/admin/compliance/subprocessors").status_code == 401
