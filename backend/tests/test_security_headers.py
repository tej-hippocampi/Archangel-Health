"""PRD-2 — HTTP security hardening (CORS, headers, CSP, rate limiting, secret guard)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import http_security  # noqa: E402
import ratelimit  # noqa: E402
from main import app  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


# ─── Security headers ────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/", "/api/patients"])
def test_core_security_headers_present(client, path):
    r = client.get(path)
    assert r.headers.get("x-frame-options") == "DENY"
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("referrer-policy") == "strict-origin-when-cross-origin"
    assert "microphone=(self)" in r.headers.get("permissions-policy", "")


def test_csp_report_only_by_default(client):
    r = client.get("/")
    assert "content-security-policy-report-only" in r.headers
    assert "default-src 'self'" in r.headers["content-security-policy-report-only"]


def test_hsts_only_in_production(client, monkeypatch):
    # Not production by default.
    assert "strict-transport-security" not in client.get("/").headers
    # is_production() is evaluated per-request, so toggling ENV flips HSTS on.
    monkeypatch.setenv("ENV", "production")
    assert "strict-transport-security" in client.get("/").headers


# ─── CORS allowlist ──────────────────────────────────────────────────────────

def test_cors_allows_listed_origin(client):
    r = client.options(
        "/api/patients",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_cors_blocks_unlisted_origin(client):
    r = client.options(
        "/api/patients",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert r.headers.get("access-control-allow-origin") != "https://evil.example.com"


@pytest.mark.parametrize(
    "origin",
    [
        "https://archangelhealth.ai",
        "https://www.archangelhealth.ai",
        "https://app.archangelhealth.ai",
        "https://admin.archangelhealth.ai",
    ],
)
def test_cors_allows_product_domains_without_env(client, origin):
    """Regression: landing sign-in died with a network error because the
    deployed landing origin was missing from ALLOWED_ORIGINS. The product's own
    https domains must pass preflight even with no CORS env vars set."""
    r = client.options(
        "/api/tenant/archangel-triage-demo/auth/login",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == origin


@pytest.mark.parametrize(
    "origin",
    [
        "http://archangelhealth.ai",  # https only
        "https://archangelhealth.ai.evil.com",  # suffix spoof
        "https://evilarchangelhealth.ai",  # prefix spoof
    ],
)
def test_cors_product_domain_regex_rejects_lookalikes(client, origin):
    r = client.options(
        "/api/tenant/archangel-triage-demo/auth/login",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
        },
    )
    assert r.headers.get("access-control-allow-origin") != origin


# ─── Rate limiting ───────────────────────────────────────────────────────────

def test_by_codes_rate_limited(client, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "1")
    ratelimit.reset()
    statuses = []
    for _ in range(11):
        resp = client.get("/api/patient/by-codes",
                          params={"clinic_code": "X", "resource_code": "Y"})
        statuses.append(resp.status_code)
    assert statuses[-1] == 429, statuses
    # The 11th response advertises Retry-After.
    last = client.get("/api/patient/by-codes",
                      params={"clinic_code": "X", "resource_code": "Y"})
    assert last.status_code == 429
    assert int(last.headers.get("retry-after", "0")) >= 1


def test_rate_limit_disabled_is_noop(client, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "0")
    ratelimit.reset()
    # 20 calls, none should 429 when disabled.
    codes = {client.get("/api/patient/by-codes",
                        params={"clinic_code": "X", "resource_code": "Y"}).status_code
             for _ in range(20)}
    assert 429 not in codes


# ─── Production secret guard ─────────────────────────────────────────────────

def test_secret_guard_flags_defaults(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("AUTH_SECRET", "change-me-in-production-elysium")
    monkeypatch.setenv("INTERNAL_TOOL_SECRET", "change-me-internal-secret")
    monkeypatch.setenv("ADMIN_PASSWORD", "change-me-strong-password")
    problems = http_security.production_secret_problems()
    assert {"AUTH_SECRET", "INTERNAL_TOOL_SECRET", "ADMIN_PASSWORD"} <= set(problems)
    with pytest.raises(RuntimeError):
        http_security.assert_production_secrets()


def test_secret_guard_allows_unset_optional_secrets(monkeypatch):
    # A strong AUTH_SECRET with admin/internal features simply not configured
    # (unset) must NOT block a production boot — only the dangerous default does.
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("AUTH_SECRET", "a" * 40)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("INTERNAL_TOOL_SECRET", raising=False)
    assert http_security.production_secret_problems() == []
    http_security.assert_production_secrets()  # no raise


def test_secret_guard_passes_with_strong_values(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("AUTH_SECRET", "a" * 40)
    monkeypatch.setenv("INTERNAL_TOOL_SECRET", "b" * 40)
    monkeypatch.setenv("ADMIN_PASSWORD", "c" * 16)
    assert http_security.production_secret_problems() == []
    http_security.assert_production_secrets()  # no raise


def test_secret_guard_noop_outside_production(monkeypatch):
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.setenv("AUTH_SECRET", "change-me-in-production-elysium")
    http_security.assert_production_secrets()  # no raise outside production
