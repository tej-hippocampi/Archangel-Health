"""Subprocessor BAA registry + PHI de-identification (PRD-4).

HIPAA requires a signed Business Associate Agreement (BAA) with every vendor that
creates/receives/maintains/transmits PHI on our behalf. Until a BAA is on file for
a given vendor, we must not send it PHI — so this module gives us:

  1. A single source of truth for each subprocessor's BAA status (with env
     overrides so ops can flip a vendor on the moment its BAA is signed).
  2. ``assert_phi_allowed(vendor)`` — a hard gate callers can use before sending
     identifiable data.
  3. ``deidentify_for_vendor(text, ...)`` — best-effort Safe-Harbor-style scrubbing
     of free text (names, dates, MRN/MBI, phone, email, addresses) so we can still
     use a non-BAA vendor with de-identified content.

This is intentionally conservative: when a vendor's BAA status is unknown we treat
it as NOT covered. De-identification is best-effort and is a *defense-in-depth*
control layered on top of the BAA gate — it is not a substitute for a signed BAA
where one is required.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Subprocessor:
    key: str
    name: str
    purpose: str
    # Whether a BAA is on file. May be overridden by an env flag (see baa_signed()).
    baa_default: bool
    # Env var an operator sets to "1" the moment a BAA is signed for this vendor.
    baa_env: Optional[str] = None
    # If False, PHI must never be sent to this vendor regardless of BAA (e.g. a
    # product line the vendor explicitly excludes from HIPAA coverage).
    phi_eligible: bool = True
    notes: str = ""

    def baa_signed(self) -> bool:
        if self.baa_env:
            return _env_flag(self.baa_env, self.baa_default)
        return self.baa_default

    def phi_allowed(self) -> bool:
        return self.phi_eligible and self.baa_signed()


# ─── Registry ─────────────────────────────────────────────────────────────────
# Defaults reflect reality as of this writing; ops flip the env flags when BAAs
# are actually executed. Anthropic's BAA covers the first-party Claude API (which
# is what ai/llm_client uses). Twilio signs a BAA for its HIPAA-eligible SMS.
# SendGrid is NOT HIPAA-eligible (Twilio will not sign a BAA for it). ElevenLabs
# and Tavus BAA status is treated as unsigned until confirmed via env.
SUBPROCESSORS: Dict[str, Subprocessor] = {
    "anthropic_api": Subprocessor(
        key="anthropic_api", name="Anthropic (Claude API)", purpose="LLM generation/classification",
        baa_default=True, baa_env="ANTHROPIC_BAA_SIGNED",
        notes="BAA covers the first-party Claude API only (not Console/Workbench/consumer tiers).",
    ),
    "openai_api": Subprocessor(
        key="openai_api", name="OpenAI (API)",
        purpose="Two-frontier A/B baseline generation + V4 vision A/B (Asclepius)",
        baa_default=False, baa_env="OPENAI_BAA_SIGNED",
        notes="OpenAI is the OpenAI side of the two-frontier A/B pair and reads the "
              "de-identified V4 image in the vision A/B (V4 Image Embedding PRD §5.7). "
              "Enabling ASCLEPIUS_TWO_FRONTIER_V4 sends real de-identified partner "
              "images here — a logged founder/compliance decision. PHI-ineligible until "
              "a BAA is on file (set OPENAI_BAA_SIGNED); V4 relies on partner "
              "de-identification, not a BAA, for the image content.",
    ),
    "twilio_sms": Subprocessor(
        key="twilio_sms", name="Twilio (SMS)", purpose="SMS delivery",
        baa_default=True, baa_env="TWILIO_BAA_SIGNED",
        notes="HIPAA-eligible SMS under Twilio's BA addendum.",
    ),
    "sendgrid": Subprocessor(
        key="sendgrid", name="Twilio SendGrid (email)", purpose="Transactional email",
        baa_default=False, baa_env="SENDGRID_BAA_SIGNED", phi_eligible=False,
        notes="NOT HIPAA-eligible; Twilio will not sign a BAA for SendGrid. Send no PHI.",
    ),
    "elevenlabs": Subprocessor(
        key="elevenlabs", name="ElevenLabs (TTS voice)", purpose="Text-to-speech synthesis",
        baa_default=False, baa_env="ELEVENLABS_BAA_SIGNED",
        notes="BAA status unconfirmed — send de-identified scripts until a BAA is on file.",
    ),
    "tavus": Subprocessor(
        key="tavus", name="Tavus (AI video avatar)", purpose="Conversational video avatar",
        baa_default=False, baa_env="TAVUS_BAA_SIGNED",
        notes="BAA status unconfirmed — send de-identified context until a BAA is on file.",
    ),
}


class SubprocessorPHIError(RuntimeError):
    """Raised when PHI would be sent to a vendor without a BAA / PHI eligibility."""


def get_subprocessor(vendor: str) -> Optional[Subprocessor]:
    return SUBPROCESSORS.get(vendor)


def phi_allowed(vendor: str) -> bool:
    """True only if the vendor is known AND PHI-eligible AND has a signed BAA."""
    sp = SUBPROCESSORS.get(vendor)
    return bool(sp and sp.phi_allowed())


def assert_phi_allowed(vendor: str) -> None:
    if not phi_allowed(vendor):
        sp = SUBPROCESSORS.get(vendor)
        label = sp.name if sp else vendor
        raise SubprocessorPHIError(
            f"Refusing to send PHI to {label}: no signed BAA / not HIPAA-eligible. "
            f"De-identify the payload or execute a BAA first."
        )


def registry_snapshot() -> List[Dict[str, object]]:
    """Serializable view for the admin compliance endpoint / review packet."""
    out: List[Dict[str, object]] = []
    for sp in SUBPROCESSORS.values():
        out.append(
            {
                "key": sp.key,
                "name": sp.name,
                "purpose": sp.purpose,
                "baa_signed": sp.baa_signed(),
                "phi_eligible": sp.phi_eligible,
                "phi_allowed": sp.phi_allowed(),
                "baa_env": sp.baa_env,
                "notes": sp.notes,
            }
        )
    return out


# ─── De-identification (best-effort Safe Harbor scrubbing) ──────────────────────
_MONTHS = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
_DATE_PATTERNS = [
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),                        # 2026-06-01
    re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"),                  # 6/1/2026
    re.compile(rf"\b{_MONTHS}\.?\s+\d{{1,2}}(?:,?\s+\d{{4}})?\b", re.I),  # June 1, 2026
    re.compile(rf"\b\d{{1,2}}\s+{_MONTHS}\.?\s+\d{{4}}\b", re.I),         # 1 June 2026
]
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PHONE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_MBI = re.compile(r"\b[0-9][A-Z][A-Z0-9]\d[A-Z][A-Z0-9]\d[A-Z]{2}\d{2}\b")  # Medicare MBI
_MRN = re.compile(r"\b(?:MRN|MBI|Medical Record(?:\s*Number)?)\s*[:#]?\s*[A-Za-z0-9-]+\b", re.I)
_ZIP = re.compile(r"\b\d{5}(?:-\d{4})?\b")
_LONG_NUM = re.compile(r"\b\d{7,}\b")  # account/record-like long digit runs


# Speakable placeholders so the de-identified text still reads naturally aloud
# (TTS / avatar). The non-speakable variants are kept for any text-only callers.
_SPEAKABLE = {
    "date": "the scheduled date",
    "phone": "the number on file",
    "email": "the email on file",
    "generic": "the information on file",
}
_TEXT = {"date": "[date]", "phone": "[redacted]", "email": "[redacted]", "generic": "[redacted]"}


def deidentify_for_vendor(
    text: Optional[str],
    *,
    patient_name: Optional[str] = None,
    extra_terms: Optional[List[str]] = None,
    speakable: bool = True,
) -> str:
    """Best-effort Safe-Harbor scrub of free text before sending to a non-BAA
    vendor. Removes the high-risk direct identifiers: names (when provided),
    dates, email, phone, SSN, MBI/MRN, long numeric ids, and ZIPs.

    With ``speakable=True`` (default) each identifier is replaced by a natural
    phrase ("the scheduled date", "the number on file", "the information on
    file") and names become "you"/"your", so the result still flows when read by
    a TTS / avatar voice — no bracketed tokens are ever spoken. Set
    ``speakable=False`` for text-only contexts that prefer "[redacted]" markers."""
    if not text:
        return ""
    out = text
    ph = _SPEAKABLE if speakable else _TEXT

    # ── Names ── Replace possessive ("Maria's" -> "your") then plain ("Maria" ->
    # "you"). Case-SENSITIVE on purpose: patient names in generated scripts are
    # capitalized, and matching case-insensitively would clobber ordinary words
    # that share a spelling with common names ("Grace", "May", "Mark", "Hope").
    terms: List[str] = []
    if patient_name:
        terms.append(patient_name)
        terms.extend([p for p in re.split(r"\s+", patient_name.strip()) if len(p) > 1])
    if extra_terms:
        terms.extend([t for t in extra_terms if t and len(t) > 1])
    for term in sorted(set(terms), key=len, reverse=True):
        esc = re.escape(term)
        out = re.sub(rf"\b{esc}[’']s\b", "your", out)   # possessive
        out = re.sub(rf"\b{esc}\b", "you", out)         # plain (case-sensitive)

    # ── Direct identifiers ──
    out = _SSN.sub(ph["generic"], out)
    out = _MBI.sub(ph["generic"], out)
    out = _MRN.sub(ph["generic"], out)
    out = _EMAIL.sub(ph["email"], out)
    out = _PHONE.sub(ph["phone"], out)
    for pat in _DATE_PATTERNS:
        out = pat.sub(ph["date"], out)
    out = _ZIP.sub(ph["generic"], out)
    out = _LONG_NUM.sub(ph["generic"], out)

    # ── Tidy so the audio flows ── collapse repeated "you" from direct address
    # ("Maria, you did great, Maria" -> "you, you did great, you" -> "you ...")
    # and squeeze whitespace left by replacements.
    out = re.sub(r"\byou(?:\s*,?\s+you)+\b", "you", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\s+([,.;:!?])", r"\1", out)  # no space before punctuation
    return out.strip()
