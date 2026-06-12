"""SMART Backend Services auth tests — hermetic via httpx.MockTransport.

Verifies the RFC 7523 assertion shape (RS384, iss=sub=client_id, aud=token
endpoint, short exp), discovery, token caching, and error surfacing.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from integrations.fhir.config import FhirSettings  # noqa: E402
from integrations.fhir.smart_auth import (  # noqa: E402
    ASSERTION_LIFETIME_SEC,
    FhirAuthError,
    SmartBackendAuth,
)

BASE = "http://fhir.test/fhir"
TOKEN_URL = "http://auth.test/token"


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


def make_settings(private_pem: str, *, token_url: str = "", key_id: str = "test-kid-1") -> FhirSettings:
    return FhirSettings(
        enabled=True,
        base_url=BASE,
        auth_mode="smart_backend",
        client_id="archangel-client",
        scopes="system/Patient.rs system/Coverage.rs",
        token_url=token_url,
        private_key_pem=private_pem,
        key_id=key_id,
        timeout_seconds=5,
        max_search_pages=5,
    )


def test_assertion_claims_and_signature(keypair):
    private_pem, public_pem = keypair
    auth = SmartBackendAuth(make_settings(private_pem))
    assertion = auth.build_assertion(TOKEN_URL, now=1_000_000)

    header = jwt.get_unverified_header(assertion)
    assert header["alg"] == "RS384"
    assert header["kid"] == "test-kid-1"

    claims = jwt.decode(assertion, public_pem, algorithms=["RS384"], audience=TOKEN_URL,
                        options={"verify_exp": False})
    assert claims["iss"] == "archangel-client"
    assert claims["sub"] == "archangel-client"
    assert claims["aud"] == TOKEN_URL
    assert claims["exp"] == 1_000_000 + ASSERTION_LIFETIME_SEC
    assert claims["jti"]  # unique per assertion


def test_assertion_omits_kid_when_unset(keypair):
    private_pem, _ = keypair
    auth = SmartBackendAuth(make_settings(private_pem, key_id=""))
    header = jwt.get_unverified_header(auth.build_assertion(TOKEN_URL))
    assert "kid" not in header


def _token_server(token_calls: list, *, expires_in: int = 3600):
    """MockTransport serving SMART discovery + token endpoint."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/.well-known/smart-configuration"):
            return httpx.Response(200, json={"token_endpoint": TOKEN_URL})
        if str(request.url) == TOKEN_URL:
            body = dict(pair.split("=", 1) for pair in request.content.decode().split("&"))
            token_calls.append(body)
            return httpx.Response(
                200,
                json={"access_token": f"tok-{len(token_calls)}", "token_type": "bearer",
                      "expires_in": expires_in},
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_token_exchange_via_discovery_and_caching(keypair):
    private_pem, _ = keypair
    auth = SmartBackendAuth(make_settings(private_pem))
    token_calls: list = []

    async def run():
        async with httpx.AsyncClient(transport=_token_server(token_calls)) as http:
            first = await auth.get_token(http)
            second = await auth.get_token(http)
            return first, second

    first, second = asyncio.run(run())
    assert first == second == "tok-1"
    assert len(token_calls) == 1  # cached — no second exchange
    assert token_calls[0]["grant_type"] == "client_credentials"
    assert "jwt-bearer" in token_calls[0]["client_assertion_type"]


def test_token_refreshes_when_near_expiry(keypair):
    private_pem, _ = keypair
    auth = SmartBackendAuth(make_settings(private_pem, token_url=TOKEN_URL))
    token_calls: list = []

    async def run():
        # expires_in=30 < 60s refresh skew → every call re-exchanges
        async with httpx.AsyncClient(transport=_token_server(token_calls, expires_in=30)) as http:
            a = await auth.get_token(http)
            b = await auth.get_token(http)
            return a, b

    a, b = asyncio.run(run())
    assert (a, b) == ("tok-1", "tok-2")
    assert len(token_calls) == 2


def test_token_exchange_failure_raises(keypair):
    private_pem, _ = keypair
    auth = SmartBackendAuth(make_settings(private_pem, token_url=TOKEN_URL))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_client"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            await auth.get_token(http)

    with pytest.raises(FhirAuthError, match="invalid_client"):
        asyncio.run(run())


def test_discovery_without_token_endpoint_raises(keypair):
    private_pem, _ = keypair
    auth = SmartBackendAuth(make_settings(private_pem))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"authorization_endpoint": "http://auth.test/authorize"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            await auth.get_token(http)

    with pytest.raises(FhirAuthError, match="token_endpoint"):
        asyncio.run(run())
