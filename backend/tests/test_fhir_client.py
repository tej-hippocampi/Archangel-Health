"""FHIR client tests — pagination, error mapping, auth header injection,
single 401 retry. Hermetic via httpx.MockTransport."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from integrations.fhir.client import FhirClient, FhirError  # noqa: E402
from integrations.fhir.config import FhirSettings  # noqa: E402

BASE = "http://fhir.test/fhir"


def open_settings(**overrides) -> FhirSettings:
    kw = dict(
        enabled=True,
        base_url=BASE,
        auth_mode="none",
        client_id="",
        scopes="system/*.rs",
        token_url="",
        private_key_pem="",
        key_id="",
        timeout_seconds=5,
        max_search_pages=5,
    )
    kw.update(overrides)
    return FhirSettings(**kw)


def _bundle(resources, next_url=None):
    out = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": r} for r in resources],
    }
    if next_url:
        out["link"] = [{"relation": "next", "url": next_url}]
    return out


def test_search_follows_pagination_and_skips_outcomes():
    page2_url = f"{BASE}/Patient?page=2"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=_bundle([{"resourceType": "Patient", "id": "c"}]))
        return httpx.Response(
            200,
            json=_bundle(
                [
                    {"resourceType": "Patient", "id": "a"},
                    {"resourceType": "OperationOutcome", "issue": []},  # search warning — skip
                    {"resourceType": "Patient", "id": "b"},
                ],
                next_url=page2_url,
            ),
        )

    async def run():
        async with FhirClient(open_settings(), transport=httpx.MockTransport(handler)) as client:
            return await client.search("Patient", {"name": "test"})

    resources = asyncio.run(run())
    assert [r["id"] for r in resources] == ["a", "b", "c"]


def test_search_respects_page_cap():
    def handler(request: httpx.Request) -> httpx.Response:
        # Every page links to another — an unbounded server
        return httpx.Response(
            200,
            json=_bundle([{"resourceType": "Patient", "id": "x"}], next_url=f"{BASE}/Patient?more=1"),
        )

    async def run():
        async with FhirClient(open_settings(max_search_pages=3), transport=httpx.MockTransport(handler)) as client:
            return await client.search("Patient", {})

    assert len(asyncio.run(run())) == 3


def test_error_surfaces_operation_outcome():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={
                "resourceType": "OperationOutcome",
                "issue": [{"severity": "error", "code": "not-found",
                           "diagnostics": "Patient/zzz is not known"}],
            },
        )

    async def run():
        async with FhirClient(open_settings(), transport=httpx.MockTransport(handler)) as client:
            await client.read("Patient", "zzz")

    with pytest.raises(FhirError, match="Patient/zzz is not known") as exc:
        asyncio.run(run())
    assert exc.value.status_code == 404


def test_smart_mode_sends_bearer_and_retries_once_on_401(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    settings = open_settings(
        auth_mode="smart_backend",
        client_id="cid",
        private_key_pem=pem,
        token_url="http://auth.test/token",
    )

    state = {"tokens": 0, "resource_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "http://auth.test/token":
            state["tokens"] += 1
            return httpx.Response(200, json={"access_token": f"tok-{state['tokens']}", "expires_in": 3600})
        state["resource_calls"] += 1
        auth_header = request.headers.get("authorization")
        if state["resource_calls"] == 1:
            assert auth_header == "Bearer tok-1"
            return httpx.Response(401)  # token revoked server-side
        assert auth_header == "Bearer tok-2"
        return httpx.Response(200, json={"resourceType": "Patient", "id": "p1"})

    async def run():
        async with FhirClient(settings, transport=httpx.MockTransport(handler)) as client:
            return await client.read("Patient", "p1")

    patient = asyncio.run(run())
    assert patient["id"] == "p1"
    assert state["tokens"] == 2  # initial + refresh after the 401


def test_read_binary_returns_raw_bytes():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept"] == "*/*"
        return httpx.Response(200, content=b"%PDF-1.4 fake")

    async def run():
        async with FhirClient(open_settings(), transport=httpx.MockTransport(handler)) as client:
            return await client.read_binary("Binary/123")

    assert asyncio.run(run()) == b"%PDF-1.4 fake"
