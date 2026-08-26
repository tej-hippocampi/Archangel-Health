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
"""

import os
import tempfile

os.environ.setdefault("RATE_LIMIT_ENABLED", "0")

_asclepius_tmp = os.path.join(tempfile.gettempdir(), "asclepius_suite")
os.makedirs(_asclepius_tmp, exist_ok=True)
os.environ.setdefault("ASCLEPIUS_DB_PATH", os.path.join(_asclepius_tmp, "asclepius_suite.db"))
os.environ.setdefault("ASCLEPIUS_EXPORT_DIR", os.path.join(_asclepius_tmp, "exports"))
os.environ.setdefault("COMMUNITY_DB_PATH", os.path.join(_asclepius_tmp, "community_suite.db"))
