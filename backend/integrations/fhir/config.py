"""FHIR integration settings, read from environment.

Two auth modes:
  - ``none``          — open servers (local HAPI sandbox). Dev only.
  - ``smart_backend`` — SMART on FHIR Backend Services (RFC 7523 client
                        credentials with a signed RS384 JWT assertion).
                        This is what Epic / Cerner production requires.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

DEFAULT_SCOPES = (
    "system/Patient.rs system/Coverage.rs "
    "system/DocumentReference.rs system/Binary.rs"
)


def _env_bool(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class FhirSettings:
    enabled: bool
    base_url: str
    auth_mode: str  # "none" | "smart_backend"
    client_id: str
    scopes: str
    token_url: str  # optional; discovered from .well-known/smart-configuration when empty
    private_key_pem: str
    key_id: str  # optional JWKS "kid" header (Epic requires one)
    timeout_seconds: float
    max_search_pages: int

    def validation_errors(self) -> List[str]:
        """Configuration problems that block use (only when enabled)."""
        errs: List[str] = []
        if not self.enabled:
            return errs
        if not self.base_url:
            errs.append("FHIR_BASE_URL is not set")
        if self.auth_mode not in ("none", "smart_backend"):
            errs.append(f"FHIR_AUTH_MODE must be 'none' or 'smart_backend' (got '{self.auth_mode}')")
        if self.auth_mode == "smart_backend":
            if not self.client_id:
                errs.append("FHIR_CLIENT_ID is required for smart_backend auth")
            if not self.private_key_pem:
                errs.append("FHIR_PRIVATE_KEY_PEM or FHIR_PRIVATE_KEY_PATH is required for smart_backend auth")
        return errs


def _load_private_key() -> str:
    pem = os.getenv("FHIR_PRIVATE_KEY_PEM", "").strip()
    if pem:
        return pem
    path = os.getenv("FHIR_PRIVATE_KEY_PATH", "").strip()
    if path:
        p = Path(path)
        if p.exists():
            return p.read_text(encoding="utf-8")
    return ""


def get_settings() -> FhirSettings:
    """Read settings fresh from env on each call (cheap; keeps tests simple)."""
    return FhirSettings(
        enabled=_env_bool("FHIR_ENABLED"),
        base_url=os.getenv("FHIR_BASE_URL", "").strip().rstrip("/"),
        auth_mode=os.getenv("FHIR_AUTH_MODE", "none").strip().lower(),
        client_id=os.getenv("FHIR_CLIENT_ID", "").strip(),
        scopes=os.getenv("FHIR_SCOPES", DEFAULT_SCOPES).strip(),
        token_url=os.getenv("FHIR_TOKEN_URL", "").strip(),
        private_key_pem=_load_private_key(),
        key_id=os.getenv("FHIR_KEY_ID", "").strip(),
        timeout_seconds=float(os.getenv("FHIR_TIMEOUT_SECONDS", "30")),
        max_search_pages=int(os.getenv("FHIR_MAX_SEARCH_PAGES", "5")),
    )
