"""Shared pytest configuration.

Disable application-layer rate limiting by default so the suite (which fires many
requests from a single TestClient IP) does not trip the brute-force throttles
added in PRD-2. Tests that specifically exercise rate limiting re-enable it.

Also point the standalone Asclepius portal (own SQLite DB + own export dir) at
throwaway temp paths for the whole suite so importing ``main`` never seeds a
bootstrap admin into a stray ``backend/asclepius.db`` in the repo. Individual
Asclepius test modules still set their own paths before importing ``main`` and
those take precedence (these are ``setdefault`` fallbacks).

The community plane gets the same treatment. Without it the community store
falls back to ``backend/community.db`` — the committed file — and every test
that uploads a task or provisions a member writes a real announcement or
welcome post into it. Those rows outlive the run, and because their authors are
throwaway test users that no longer exist in the users plane, they render to
real members as "Former member". Keep this pointed at a temp path.

**Outgoing email is put in dev mode for the whole suite, and that is an
ORDER-INDEPENDENCE fix, not a convenience.** Fourteen test files were setting
``EMAIL_DEV_MODE`` at module scope for themselves — process-wide, never cleaned
up — so any file collected after one of them inherited a configured transport
for free. Files that needed one but did not set it were green by collection
order alone, and CI shards by a bin-packer: adding a single test file anywhere
in the suite repacks the shards, and two files (``test_referral_program``,
``test_doctor_email_verification``) went red the first time that happened, on
endpoints that 503 without a transport. Setting it here makes that whole class
of failure impossible rather than fixing it one file at a time.

Safe for the tests that assert the UNCONFIGURED path: they monkeypatch
``is_email_transport_configured`` / ``_email_configured`` directly rather than
clearing an env var, so this default does not reach them. And dev mode never
sends — it prints to stdout — so this also removes any chance of a test reaching
a real transport.
"""

import os
import tempfile

os.environ.setdefault("RATE_LIMIT_ENABLED", "0")
os.environ.setdefault("EMAIL_DEV_MODE", "1")

_asclepius_tmp = os.path.join(tempfile.gettempdir(), "asclepius_suite")
os.makedirs(_asclepius_tmp, exist_ok=True)
os.environ.setdefault("ASCLEPIUS_DB_PATH", os.path.join(_asclepius_tmp, "asclepius_suite.db"))
os.environ.setdefault("ASCLEPIUS_EXPORT_DIR", os.path.join(_asclepius_tmp, "exports"))
os.environ.setdefault("COMMUNITY_DB_PATH", os.path.join(_asclepius_tmp, "community_suite.db"))

# ── Fake LLM transport (Fake LLM Provider PRD §2) ────────────────────────────
# The suite runs with NO API key. Every LLM path returns a deterministic,
# schema-valid fixture instead of reaching a real vendor, so generation, judging
# and extraction paths are genuinely exercised rather than stubbed per file or
# skipped. setdefault, so a run that deliberately wants a real provider
# (the `llm-smoke` workflow) just exports ASCLEPIUS_LLM_PROVIDER itself.
os.environ.setdefault("ASCLEPIUS_LLM_PROVIDER", "fake")
