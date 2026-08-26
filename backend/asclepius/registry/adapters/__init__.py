"""Per-country registry adapters.

An adapter is a module exporting::

    fetch(identifier, *, extras, timeout) -> {"status", "record", "reason"}

with ``status`` in ``found`` | ``not_found`` | ``unavailable``, mirroring
``credentialing.fetch_npi_record``. It may also export ``match`` — a
``(record, family_name, extras) -> (RegistryResult value, reason)`` callable —
when family-name corroboration alone is not the right test for that registry
(India's is: its register writes names in no fixed order).

Adapters do NOT decide what a miss means. They report what the registry said;
``dispatch`` translates that into a verdict, because whether "no rows" counts
as evidence depends on whether the source is authoritative, and that decision
belongs in exactly one place.

Registered lazily by country code so a broken adapter cannot take down signup
at import time.
"""

from __future__ import annotations

import importlib
import logging
from types import ModuleType
from typing import Dict, Optional

log = logging.getLogger("asclepius.registry.adapters")

#: country code -> module name in this package
_ADAPTER_MODULES: Dict[str, str] = {
    "IN": "nmc_in",
    "PK": "pmdc_pk",
}

_cache: Dict[str, Optional[ModuleType]] = {}


def get(country: str) -> Optional[ModuleType]:
    """The adapter module for a country, or None when there is no adapter.

    The module itself rather than its ``fetch``, so ``match`` is looked up at
    call time — an adapter stays patchable and readable, and there is no
    second place for the two halves to drift apart.
    """
    code = (country or "").upper()
    if code in _cache:
        return _cache[code]
    module_name = _ADAPTER_MODULES.get(code)
    if not module_name:
        _cache[code] = None
        return None
    try:
        module: Optional[ModuleType] = importlib.import_module(f"{__name__}.{module_name}")
        getattr(module, "fetch")  # fail loudly here, not mid-verification
    except Exception as exc:  # noqa: BLE001 - a bad adapter must not break signup
        log.warning("[registry] could not load the %s adapter: %s", code, exc)
        module = None
    _cache[code] = module
    return module
