"""FHIR R4 integration (SMART on FHIR Backend Services).

Feature-flagged via ``FHIR_ENABLED`` — when off, nothing in this package is
exercised at runtime. See ``docs/FHIR_INTEGRATION.md`` for the rollout plan
(local HAPI sandbox → Epic sandbox → real-site pilot).
"""

from integrations.fhir.config import FhirSettings, get_settings
from integrations.fhir.client import FhirClient, FhirError, FhirAuthError

__all__ = ["FhirSettings", "get_settings", "FhirClient", "FhirError", "FhirAuthError"]
