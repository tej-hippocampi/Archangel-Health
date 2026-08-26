"""Check a medical registration number against its national registry.

The country-agnostic twin of ``credentialing.verify_npi``, and deliberately
the same shape: ``{"result", "registry", "identifier", "reason", "record",
"from_cache"}``. US signups delegate to ``verify_npi`` unchanged — NPPES was
here first and nothing about it should move.

The rule this module exists to enforce: **a scraper may confirm a doctor, it
may never disprove one.** Only registries marked ``authoritative`` in
``config`` (real APIs) can return NOT_FOUND. Everything else turns a miss into
INCONCLUSIVE, which routes to document review and a human — because India's
register is openly stale, because a page can move, and because the cost of
wrongly rejecting a real physician is one we do not get to see and they never
forget. That translation happens HERE, once, so no future adapter can get it
wrong on its own.

Adapters split fetch from judgment for the same reason ``fetch_npi_record``
and ``verify_npi`` are separate: I/O is mockable, matching is testable, and
neither has to pretend to be the other.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Dict, Optional

from asclepius import credentialing
from asclepius.registry import config as registry_config

log = logging.getLogger("asclepius.registry")

DEFAULT_TIMEOUT = 10.0


class RegistryResult(str, Enum):
    VERIFIED = "verified"          # found, and it corroborates this signup
    MISMATCH = "mismatch"          # found, but it does not corroborate
    NOT_FOUND = "not_found"        # authoritative registries only: definitively absent
    INCONCLUSIVE = "inconclusive"  # searched, no match, and the source is not trusted to say no
    UNAVAILABLE = "unavailable"    # could not check: network, timeout, layout change
    DOCUMENT_ONLY = "document_only"  # no lookup exists; the certificate is the evidence
    QUEUED = "queued"              # persisted at signup, the agent has not run yet


#: Results that settle the question. Anything else should be re-checked.
DEFINITIVE = frozenset({
    RegistryResult.VERIFIED.value,
    RegistryResult.MISMATCH.value,
    RegistryResult.NOT_FOUND.value,
})


def _adapter_for(country: str) -> Optional[Any]:
    """Import a country's adapter module lazily.

    Lazy so a broken or dependency-heavy adapter can never take down signup at
    import time, and so adding one is a file plus a config entry.
    """
    from asclepius.registry import adapters

    return adapters.get(country)


def _result(
    result: str,
    *,
    registry: str,
    identifier: str,
    reason: Optional[str] = None,
    record: Optional[Dict[str, Any]] = None,
    from_cache: bool = False,
) -> Dict[str, Any]:
    return {
        "result": result,
        "registry": registry,
        "identifier": identifier,
        "reason": reason,
        "record": record,
        "from_cache": from_cache,
    }


def verify_credential(
    country: Optional[str],
    identifier: str,
    family_name: str,
    *,
    extras: Optional[Dict[str, Any]] = None,
    timeout: float = DEFAULT_TIMEOUT,
    cached: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Verify a registration number against the registry of ``country``.

    ``extras`` carries whatever else that registry demands (India's state
    council, the Philippines' date of birth) — see ``config.FieldSpec``.

    Never raises. Every failure mode an adapter can hit — a timeout, a moved
    page, a parser that no longer matches — comes back as UNAVAILABLE, which
    is a retry, not a verdict.
    """
    # A blank country means a row written before the form asked, and everyone
    # who signed up then was a US physician with an NPI. Defaulting anywhere
    # else would quietly stop verifying every account we already have.
    cfg = registry_config.for_country(registry_config.normalize_country(country) or "US")
    ident = (identifier or "").strip()
    extras = extras or {}

    # NPPES is the US path and stays exactly as it was.
    if cfg.country == "US":
        npi = credentialing.verify_npi(
            ident, family_name, timeout=timeout, cached=cached
        )
        return _result(
            npi["result"],
            registry=cfg.registry_name,
            identifier=npi.get("npi") or ident,
            reason=npi.get("reason"),
            record=npi.get("record"),
            from_cache=bool(npi.get("from_cache")),
        )

    if not ident:
        return _result(
            RegistryResult.DOCUMENT_ONLY.value if cfg.method == registry_config.METHOD_DOCUMENT
            else RegistryResult.INCONCLUSIVE.value,
            registry=cfg.registry_name,
            identifier="",
            reason="no_registration_number_provided",
        )

    if cfg.method == registry_config.METHOD_DOCUMENT:
        # Nothing to call. Not a failure — the certificate is the evidence and
        # an admin reads it.
        return _result(
            RegistryResult.DOCUMENT_ONLY.value,
            registry=cfg.registry_name,
            identifier=ident,
            reason="no_public_lookup_for_country",
        )

    module = _adapter_for(cfg.country)
    if module is None:
        log.warning("[registry] %s is configured %s but has no adapter", cfg.country, cfg.method)
        return _result(
            RegistryResult.DOCUMENT_ONLY.value,
            registry=cfg.registry_name,
            identifier=ident,
            reason="no_adapter_for_country",
        )

    if cached is not None:
        fetched = cached
        from_cache = True
    else:
        from_cache = False
        try:
            fetched = module.fetch(ident, extras=extras, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - an adapter must never break signup
            log.warning("[registry] %s adapter raised: %s", cfg.country, exc)
            return _result(
                RegistryResult.UNAVAILABLE.value,
                registry=cfg.registry_name,
                identifier=ident,
                reason="adapter_error",
            )

    status = (fetched or {}).get("status")

    if status == "unavailable":
        return _result(
            RegistryResult.UNAVAILABLE.value,
            registry=cfg.registry_name,
            identifier=ident,
            reason=(fetched or {}).get("reason") or "unavailable",
            from_cache=from_cache,
        )

    if status == "not_found":
        # The choke point. A non-authoritative source saying "no rows" is not
        # the same fact as a registry saying "this person is not registered".
        if cfg.authoritative:
            return _result(
                RegistryResult.NOT_FOUND.value,
                registry=cfg.registry_name,
                identifier=ident,
                reason="no_registry_record",
                from_cache=from_cache,
            )
        return _result(
            RegistryResult.INCONCLUSIVE.value,
            registry=cfg.registry_name,
            identifier=ident,
            reason="not_found_in_a_register_that_may_be_incomplete",
            from_cache=from_cache,
        )

    record = (fetched or {}).get("record") or {}
    matcher = getattr(module, "match", None)
    if matcher is None:
        verdict, reason = _default_match(record, family_name)
    else:
        try:
            verdict, reason = matcher(record, family_name, extras)
        except Exception as exc:  # noqa: BLE001
            log.warning("[registry] %s matcher raised: %s", cfg.country, exc)
            return _result(
                RegistryResult.UNAVAILABLE.value,
                registry=cfg.registry_name,
                identifier=ident,
                reason="matcher_error",
                record=record,
                from_cache=from_cache,
            )

    return _result(
        verdict,
        registry=cfg.registry_name,
        identifier=ident,
        reason=reason,
        record=record,
        from_cache=from_cache,
    )


def _default_match(record: Dict[str, Any], family_name: str):
    """Family-name corroboration, reusing the NPPES name logic.

    Transliteration is the reason a mismatch here is a review flag and never a
    rejection: one register writes Mohammed, the next writes Mohamed, and a
    doctor whose name crossed a script boundary should not pay for it.
    """
    registry_name = " ".join(
        str(record.get(k) or "") for k in ("family_name", "full_name", "name")
    ).strip()
    if not credentialing.has_comparable_family_name(family_name):
        return RegistryResult.MISMATCH.value, "no_family_name_to_compare"
    if not registry_name:
        return RegistryResult.MISMATCH.value, "registry_record_has_no_name"

    if credentialing.family_names_match(family_name, registry_name):
        status = str(record.get("status") or "").strip().lower()
        if status and status in {"suspended", "cancelled", "canceled", "struck off", "inactive"}:
            return RegistryResult.MISMATCH.value, f"registration_{status.replace(' ', '_')}"
        return RegistryResult.VERIFIED.value, None
    return RegistryResult.MISMATCH.value, "family_name_mismatch_possible_transliteration"
