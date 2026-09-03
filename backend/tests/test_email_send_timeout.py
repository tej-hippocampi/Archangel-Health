"""Outgoing mail must not be able to freeze the server.

``email_utils.send_html_email_with_reason`` is awaited from request handlers and
from three unattended drain loops, all sharing the one uvicorn event loop, and
the transport underneath it is blocking urllib. With no timeout and no thread
between them, a SendGrid connection that hung held the whole process: every
request, including ``/health``, which under Railway's restart policy turns a
vendor blip into a restart loop.

Two properties hold that closed, and they are separate:

  * the send runs OFF the event loop, so a slow send is slow for one recipient
    and for nobody else;
  * the send has a DEADLINE, so a hung connection ends rather than leaking a
    thread that waits forever.

The third test is the one that keeps the fix honest at the outbox: a timeout has
to come back as an ordinary ``(False, reason)`` send failure, because that is
what every caller retries on. A timeout that escaped as an exception would mark
a physician's mail failed-with-no-retry, or take a drain loop down.
"""

from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import email_utils  # noqa: E402


class _FakeResponse:
    status_code = 202
    body = b""


class _FakeClient:
    """Stands in for python_http_client's Client, which is where the deadline
    has to land: SendGridAPIClient takes no timeout of its own."""

    def __init__(self):
        self.timeout = None


class _FakeSendGrid:
    """Records what the send was configured with, and does what it is told."""

    last = None

    def __init__(self, api_key, on_send=None):
        self.api_key = api_key
        self.client = _FakeClient()
        self._on_send = on_send
        type(self).last = self

    def send(self, message):
        if self._on_send is not None:
            self._on_send()
        return _FakeResponse()


@pytest.fixture()
def sendgrid_env(monkeypatch):
    """A configured SendGrid transport with no dev-mode short circuit."""
    monkeypatch.delenv("EMAIL_DEV_MODE", raising=False)
    monkeypatch.setenv("SENDGRID_API_KEY", "SG.test-key")
    monkeypatch.setenv("SENDGRID_FROM_EMAIL", "noreply@archangelhealth.ai")


def _install(monkeypatch, on_send=None):
    import sendgrid

    monkeypatch.setattr(
        sendgrid, "SendGridAPIClient",
        lambda api_key: _FakeSendGrid(api_key, on_send=on_send))


def test_the_sendgrid_call_carries_an_explicit_deadline(sendgrid_env, monkeypatch):
    """No timeout means urllib waits forever, and forever is the failure."""
    _install(monkeypatch)
    ok, reason = asyncio.run(
        email_utils.send_html_email_with_reason("doc@example.org", "s", "<p>x</p>"))
    assert (ok, reason) == (True, "sent")
    assert _FakeSendGrid.last.client.timeout == email_utils._send_timeout_seconds()
    assert 0 < _FakeSendGrid.last.client.timeout <= 60


def test_the_deadline_is_configurable_but_never_unbounded(monkeypatch):
    monkeypatch.setenv("EMAIL_SEND_TIMEOUT_SECONDS", "5")
    assert email_utils._send_timeout_seconds() == 5.0
    # A typo or a zero must not silently disable the protection.
    for bad in ("", "0", "-1", "soon"):
        monkeypatch.setenv("EMAIL_SEND_TIMEOUT_SECONDS", bad)
        assert email_utils._send_timeout_seconds() > 0


def test_a_slow_send_does_not_stall_the_event_loop(sendgrid_env, monkeypatch):
    """THE test. The blocking call belongs on a worker thread, because the loop
    it used to run on is the one serving the healthcheck."""
    _install(monkeypatch, on_send=lambda: __import__("time").sleep(0.4))

    async def _scenario():
        ticks = 0

        async def _heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        beat = asyncio.create_task(_heartbeat())
        ok, _reason = await email_utils.send_html_email_with_reason(
            "doc@example.org", "s", "<p>x</p>")
        beat.cancel()
        return ok, ticks

    ok, ticks = asyncio.run(_scenario())
    assert ok is True
    # Pinned to the loop, the heartbeat would not have run at all during the
    # send. The bar is deliberately loose: this asserts "the loop kept serving",
    # not a scheduling rate.
    assert ticks >= 5, f"the event loop only ticked {ticks} times during a 0.4s send"


def test_a_timed_out_send_is_an_ordinary_failure_the_outbox_can_retry(
        sendgrid_env, monkeypatch):
    def _hang():
        raise socket.timeout("timed out")

    _install(monkeypatch, on_send=_hang)
    ok, reason = asyncio.run(
        email_utils.send_html_email_with_reason("doc@example.org", "s", "<p>x</p>"))
    assert ok is False
    assert "did not respond" in reason and "retried" in reason


def test_a_connect_timeout_wrapped_in_a_urlerror_is_recognised_too(
        sendgrid_env, monkeypatch):
    """urllib reports a read deadline as socket.timeout and a connect deadline
    as a URLError carrying one, and only the second is the hang this fixed."""
    from urllib.error import URLError

    def _hang():
        raise URLError(socket.timeout("timed out"))

    _install(monkeypatch, on_send=_hang)
    ok, reason = asyncio.run(
        email_utils.send_html_email_with_reason("doc@example.org", "s", "<p>x</p>"))
    assert ok is False
    assert "did not respond" in reason


def test_the_smtp_path_gets_the_same_deadline(monkeypatch):
    """The other transport, which inherits "wait forever" from the global socket
    default when nobody passes a timeout."""
    monkeypatch.delenv("EMAIL_DEV_MODE", raising=False)
    monkeypatch.delenv("SENDGRID_API_KEY", raising=False)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.org")
    monkeypatch.setenv("SMTP_USER", "u")
    monkeypatch.setenv("SMTP_PASS", "p")

    seen = {}

    class _FakeSMTP:
        def __init__(self, host, port, timeout=None):
            seen["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self):
            pass

        def login(self, u, p):
            pass

        def send_message(self, msg):
            pass

    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    ok, _reason = asyncio.run(
        email_utils.send_html_email_with_reason("doc@example.org", "s", "<p>x</p>"))
    assert ok is True
    assert seen["timeout"] == email_utils._send_timeout_seconds()
