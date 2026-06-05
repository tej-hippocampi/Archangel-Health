# PRD-4: BAA-Aware Subprocessor Gate & PHI De-identification

## Context
PHI currently flows to vendors without confirmed BAAs:
- **SendGrid** (`email_utils.py` / `onboarding_emails.py`) — NOT HIPAA-eligible;
  Twilio will not sign a BAA for SendGrid. PHI is in email today.
- **ElevenLabs** (`integrations/elevenlabs.py`) and **Tavus**
  (`integrations/tavus.py`) — BAA status UNCONFIRMED; patient-identifiable content
  is sent.
- **Anthropic** — BAA only covers the first-party Claude API (already used via
  `ai/llm_client`).

## Goal
Never transmit PHI to a vendor not marked BAA-covered. Where a vendor lacks a BAA,
send only de-identified content, or suppress PHI.

## Implementation
1. New `backend/compliance/subprocessors.py` with a `SUBPROCESSORS` registry:
   ```python
   { "sendgrid":      {"baa": False, "phi_allowed": False},
     "anthropic_api": {"baa": True,  "phi_allowed": True},
     "twilio_sms":    {"baa": True,  "phi_allowed": True},
     "elevenlabs":    {"baa": _env_bool("ELEVENLABS_BAA_SIGNED"), ...},
     "tavus":         {"baa": _env_bool("TAVUS_BAA_SIGNED"), ...} }
   ```
   Provide `assert_phi_allowed(vendor)` → raises if `phi_allowed` is False.
2. **Email (highest risk):** in the send path (`email_utils.py`,
   `_build_recovery_resources_email_html` in `main.py`), STRIP PHI when the active
   email transport is SendGrid. The recovery email already uses codes + a login
   link — reduce the body to: greeting WITHOUT full name (or first-name only if
   legal signs off), no procedure, no clinical detail, just "Your resources are
   ready — log in with your codes." Gate richer content behind `EMAIL_PROVIDER_IS_BAA=1`
   for when you move to Paubox/LuxSci.
3. **Voice/avatar:** add `deidentify_for_vendor(text)` that removes the 18 Safe
   Harbor identifiers (at minimum: full name → first name only or "you"; dates →
   relative; MRN/phone/email removed) before sending scripts to ElevenLabs/Tavus
   UNLESS that vendor's BAA flag is true. Call it at the entry points in
   `integrations/elevenlabs.py` and `integrations/tavus.py`.
4. Add `GET /admin/compliance/subprocessors` (admin-auth) returning the registry
   for the security-review packet.

## Acceptance criteria
- With `ELEVENLABS_BAA_SIGNED` unset, text sent to ElevenLabs contains no full name
  / no dates (assert via a unit test spying on the client call).
- SendGrid email body contains no procedure name and no full clinical content.
- `assert_phi_allowed("sendgrid")` raises.
- Tests in `backend/tests/test_subprocessor_gate.py`.
- Document the new env flags in `.env.example`; create `docs/security/SUBPROCESSORS.md`
  register (vendor, product, PHI passed, BAA signed + date, HIPAA-eligible).

## Note for the human (not code)
Sign Anthropic's BAA (first-party API only), Twilio's BA addendum (SMS), and get
written BAA confirmation from ElevenLabs + Tavus. Move PHI email to a BAA provider.
