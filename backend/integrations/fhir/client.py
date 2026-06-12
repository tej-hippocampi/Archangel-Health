"""Thin async FHIR R4 REST client (read / search with pagination).

Plain ``dict`` resources in and out — we deliberately avoid a heavyweight
FHIR model dependency. Responses are validated only as far as we consume
them; the eligibility extractor downstream is already built for messy input.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from integrations.fhir.config import FhirSettings, get_settings
from integrations.fhir.smart_auth import FhirAuthError, SmartBackendAuth

log = logging.getLogger("fhir.client")

FHIR_JSON = "application/fhir+json"


class FhirError(Exception):
    """A FHIR interaction failed. Carries status code + OperationOutcome diagnostics."""

    def __init__(self, message: str, *, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _outcome_diagnostics(body: Any) -> str:
    """Pull human-readable diagnostics out of an OperationOutcome, if present."""
    if not isinstance(body, dict) or body.get("resourceType") != "OperationOutcome":
        return ""
    parts = []
    for issue in body.get("issue") or []:
        d = issue.get("diagnostics") or issue.get("code") or ""
        if d:
            parts.append(str(d))
    return "; ".join(parts)


class FhirClient:
    """One instance per request/import; owns its httpx client. Use as an
    async context manager so connections are always released."""

    def __init__(
        self,
        settings: Optional[FhirSettings] = None,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,  # tests: MockTransport
    ) -> None:
        self.settings = settings or get_settings()
        self._http = httpx.AsyncClient(timeout=self.settings.timeout_seconds, transport=transport)
        self._auth = (
            SmartBackendAuth(self.settings)
            if self.settings.auth_mode == "smart_backend"
            else None
        )

    async def __aenter__(self) -> "FhirClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()

    # ── Low-level request ────────────────────────────────────────────────
    async def _headers(self) -> Dict[str, str]:
        headers = {"Accept": FHIR_JSON}
        if self._auth:
            headers["Authorization"] = f"Bearer {await self._auth.get_token(self._http)}"
        return headers

    async def _get(self, url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        try:
            resp = await self._http.get(url, params=params, headers=await self._headers())
        except httpx.HTTPError as e:
            raise FhirError(f"FHIR request failed: {e}") from e

        if resp.status_code == 401 and self._auth:
            # Token may have been revoked server-side ahead of its expiry;
            # retry exactly once with a fresh token.
            self._auth.invalidate()
            try:
                resp = await self._http.get(url, params=params, headers=await self._headers())
            except httpx.HTTPError as e:
                raise FhirError(f"FHIR request failed after token refresh: {e}") from e

        if resp.status_code >= 400:
            diag = ""
            try:
                diag = _outcome_diagnostics(resp.json())
            except ValueError:
                pass
            raise FhirError(
                f"FHIR server returned HTTP {resp.status_code}" + (f": {diag}" if diag else ""),
                status_code=resp.status_code,
            )
        try:
            return resp.json()
        except ValueError as e:
            raise FhirError("FHIR server returned non-JSON response") from e

    # ── Public API ───────────────────────────────────────────────────────
    async def capability(self) -> Dict[str, Any]:
        """GET /metadata — used by the status probe; cheap connectivity check."""
        return await self._get(f"{self.settings.base_url}/metadata", params={"_summary": "true"})

    async def read(self, resource_type: str, resource_id: str) -> Dict[str, Any]:
        return await self._get(f"{self.settings.base_url}/{resource_type}/{resource_id}")

    async def search(
        self,
        resource_type: str,
        params: Dict[str, Any],
        *,
        max_pages: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Search and follow ``link[rel=next]`` up to ``max_pages`` pages.

        Returns the flattened list of ``entry[].resource`` dicts.
        """
        pages = max_pages or self.settings.max_search_pages
        resources: List[Dict[str, Any]] = []
        bundle = await self._get(f"{self.settings.base_url}/{resource_type}", params=params)
        for _ in range(pages):
            for entry in bundle.get("entry") or []:
                res = entry.get("resource")
                # Servers may interleave OperationOutcome entries (search warnings)
                if isinstance(res, dict) and res.get("resourceType") != "OperationOutcome":
                    resources.append(res)
            next_url = next(
                (l.get("url") for l in bundle.get("link") or [] if l.get("relation") == "next"),
                None,
            )
            if not next_url:
                break
            bundle = await self._get(next_url)
        else:
            log.warning(
                "FHIR search %s hit the %d-page cap; results may be truncated",
                resource_type,
                pages,
            )
        return resources

    async def read_binary(self, binary_ref: str) -> bytes:
        """Fetch a Binary resource's raw content (e.g. ``Binary/123``).

        Asks for the native content type; FHIR servers return the raw bytes
        when Accept is not application/fhir+json.
        """
        url = binary_ref if binary_ref.startswith("http") else f"{self.settings.base_url}/{binary_ref}"
        headers = await self._headers()
        headers["Accept"] = "*/*"
        try:
            resp = await self._http.get(url, headers=headers)
        except httpx.HTTPError as e:
            raise FhirError(f"Binary fetch failed: {e}") from e
        if resp.status_code >= 400:
            raise FhirError(f"Binary fetch returned HTTP {resp.status_code}", status_code=resp.status_code)
        return resp.content


__all__ = ["FhirClient", "FhirError", "FhirAuthError"]
