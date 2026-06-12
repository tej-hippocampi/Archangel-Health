"""SMART on FHIR Backend Services authorization (system-to-system).

Implements the client-credentials flow from the SMART Backend Services spec
(HL7 smart-app-launch / RFC 7523): a short-lived JWT assertion signed with
our RS384 private key is exchanged at the server's token endpoint for a
bearer access token. The matching public key is published as a JWKS that we
register with the EHR (Epic: "Apps on FHIR" → non-production client).

The token endpoint is discovered from ``<base>/.well-known/smart-configuration``
unless ``FHIR_TOKEN_URL`` pins it explicitly.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Optional

import httpx
import jwt

from integrations.fhir.config import FhirSettings

log = logging.getLogger("fhir.smart_auth")

ASSERTION_LIFETIME_SEC = 300  # spec maximum is 5 minutes
TOKEN_REFRESH_SKEW_SEC = 60   # refresh when less than this remains
CLIENT_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"


class FhirAuthError(Exception):
    """Token endpoint discovery or exchange failed."""


class SmartBackendAuth:
    """Caches one access token per instance; refreshes ahead of expiry."""

    def __init__(self, settings: FhirSettings) -> None:
        self._settings = settings
        self._token: Optional[str] = None
        self._expires_at: float = 0.0
        self._token_url: str = settings.token_url

    def build_assertion(self, token_url: str, *, now: Optional[int] = None) -> str:
        """Signed RS384 JWT: iss = sub = client_id, aud = token endpoint."""
        ts = int(now if now is not None else time.time())
        claims = {
            "iss": self._settings.client_id,
            "sub": self._settings.client_id,
            "aud": token_url,
            "exp": ts + ASSERTION_LIFETIME_SEC,
            "jti": uuid.uuid4().hex,
        }
        headers = {"kid": self._settings.key_id} if self._settings.key_id else None
        return jwt.encode(claims, self._settings.private_key_pem, algorithm="RS384", headers=headers)

    async def _discover_token_url(self, http: httpx.AsyncClient) -> str:
        if self._token_url:
            return self._token_url
        url = f"{self._settings.base_url}/.well-known/smart-configuration"
        try:
            resp = await http.get(url, headers={"Accept": "application/json"})
            resp.raise_for_status()
            token_url = (resp.json() or {}).get("token_endpoint") or ""
        except (httpx.HTTPError, ValueError) as e:
            raise FhirAuthError(f"SMART discovery failed at {url}: {e}") from e
        if not token_url:
            raise FhirAuthError(f"SMART discovery at {url} returned no token_endpoint")
        self._token_url = token_url
        return token_url

    async def get_token(self, http: httpx.AsyncClient) -> str:
        """Return a valid bearer token, exchanging a fresh assertion if needed."""
        if self._token and time.time() < self._expires_at - TOKEN_REFRESH_SKEW_SEC:
            return self._token

        token_url = await self._discover_token_url(http)
        assertion = self.build_assertion(token_url)
        try:
            resp = await http.post(
                token_url,
                data={
                    "grant_type": "client_credentials",
                    "scope": self._settings.scopes,
                    "client_assertion_type": CLIENT_ASSERTION_TYPE,
                    "client_assertion": assertion,
                },
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as e:
            raise FhirAuthError(f"Token request to {token_url} failed: {e}") from e
        if resp.status_code != 200:
            # Never log the assertion or response body verbatim at error level —
            # bodies can echo identifiers. Status + error code is enough to debug.
            err_code = ""
            try:
                err_code = (resp.json() or {}).get("error", "")
            except ValueError:
                pass
            raise FhirAuthError(f"Token exchange failed: HTTP {resp.status_code} {err_code}".strip())

        payload = resp.json()
        token = payload.get("access_token")
        if not token:
            raise FhirAuthError("Token response missing access_token")
        self._token = token
        self._expires_at = time.time() + float(payload.get("expires_in") or 300)
        log.info("FHIR token acquired (expires_in=%ss)", payload.get("expires_in"))
        return token

    def invalidate(self) -> None:
        self._token = None
        self._expires_at = 0.0
