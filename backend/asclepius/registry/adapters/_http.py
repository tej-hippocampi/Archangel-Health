"""Shared HTTP plumbing for the registry adapters.

Every adapter talks to somebody else's website, and websites change without
telling us. The contract here is that a surprise — a timeout, a 500, a page
that no longer parses — comes back as ``unavailable``, never as ``not_found``.
``unavailable`` is a retry; ``not_found`` is a verdict, and only a registry
gets to hand us one of those.
"""

from __future__ import annotations

import logging
import os
import ssl
from pathlib import Path
from typing import Any, Dict, Optional

import certifi
import httpx

log = logging.getLogger("asclepius.registry.http")

DEFAULT_TIMEOUT = 10.0

#: Intermediate CAs that a registry's own server fails to send.
#:
#: Some of these sites serve only their leaf certificate and rely on the client
#: chasing the AIA extension to find the issuer — browsers and curl do that,
#: Python's OpenSSL does not, so the handshake fails against an otherwise
#: perfectly valid certificate (India's NMC is the live example). Supplying the
#: missing intermediate keeps full verification on: the alternative people
#: reach for is verify=False, which trades a server's packaging mistake for our
#: own security hole. If one of these expires or rotates, the handshake fails,
#: the check reports unavailable, and the doctor goes to document review.
_CA_DIR = Path(__file__).resolve().parent / "ca"

_ssl_context: Optional[ssl.SSLContext] = None

#: Identify ourselves. A registry that wants to rate-limit or block us should
#: be able to, and should be able to reach a human about it.
DEFAULT_USER_AGENT = (
    "ArchangelHealth-CredentialCheck/1.0 (+https://archangelhealth.ai; "
    "verification@archangelhealth.ai)"
)


def user_agent() -> str:
    return (os.getenv("REGISTRY_SCRAPER_UA") or DEFAULT_USER_AGENT).strip()


def timeout_budget(timeout: float = DEFAULT_TIMEOUT) -> httpx.Timeout:
    """Per-phase budget, for the reason ``credentialing._timeout_for`` gives:
    a bare float applies to each phase separately, so one hung request can
    hold a worker for four times as long as it looks like it can."""
    try:
        scale = max(0.05, float(timeout) / DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        scale = 1.0
    return httpx.Timeout(
        connect=3.0 * scale, read=6.0 * scale, write=3.0 * scale, pool=2.0 * scale
    )


def unavailable(reason: str) -> Dict[str, Any]:
    return {"status": "unavailable", "record": None, "reason": reason}


def not_found() -> Dict[str, Any]:
    return {"status": "not_found", "record": None, "reason": None}


def found(record: Dict[str, Any]) -> Dict[str, Any]:
    return {"status": "found", "record": record, "reason": None}


def ssl_context() -> ssl.SSLContext:
    """certifi's roots plus any intermediates the registries fail to send.

    Built once and reused; a certificate we cannot load is logged and skipped
    rather than taking the whole context down with it.
    """
    global _ssl_context
    if _ssl_context is not None:
        return _ssl_context
    ctx = ssl.create_default_context(cafile=certifi.where())
    if _CA_DIR.is_dir():
        for pem in sorted(_CA_DIR.glob("*.pem")):
            try:
                ctx.load_verify_locations(cafile=str(pem))
            except Exception as exc:  # noqa: BLE001
                log.warning("[registry] could not load supplemental CA %s: %s", pem.name, exc)
    _ssl_context = ctx
    return ctx


def request(
    method: str,
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    headers: Optional[Dict[str, str]] = None,
    **kwargs: Any,
) -> Any:
    """One HTTP call with our UA and timeout budget. Raises on transport
    errors; callers wrap and translate to ``unavailable``."""
    hdrs = {"User-Agent": user_agent(), "Accept-Language": "en"}
    hdrs.update(headers or {})
    with httpx.Client(
        timeout=timeout_budget(timeout), follow_redirects=True, headers=hdrs,
        verify=ssl_context(),
    ) as client:
        return client.request(method, url, **kwargs)


def text_of(node: Any) -> str:
    """Collapsed text of a parsed HTML node, whitespace normalized."""
    if node is None:
        return ""
    return " ".join(node.get_text(" ", strip=True).split())
