"""Shared pytest configuration.

Disable application-layer rate limiting by default so the suite (which fires many
requests from a single TestClient IP) does not trip the brute-force throttles
added in PRD-2. Tests that specifically exercise rate limiting re-enable it.
"""

import os

os.environ.setdefault("RATE_LIMIT_ENABLED", "0")
