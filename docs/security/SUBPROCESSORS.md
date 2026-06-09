# Subprocessor Register (PRD-4)

The third-party services that may create, receive, maintain, or transmit data on
our behalf. HIPAA requires a signed **Business Associate Agreement (BAA)** with
every subprocessor that touches PHI. The application enforces this with a gate
(`backend/compliance/subprocessors.py`): until a vendor's BAA flag is set, PHI is
**de-identified** before it leaves our infrastructure (voice/avatar) or **stripped**
(email). Live status is visible at `GET /admin/compliance/subprocessors`.

| Vendor | Purpose | PHI sent? | HIPAA-eligible | BAA on file | Enforcement when no BAA | Enable flag |
|---|---|---|---|---|---|---|
| **Anthropic — Claude API** | LLM generation / classification | Yes (clinical text) | Yes (first-party API only) | **Required — sign it** | n/a (must have BAA) | `ANTHROPIC_BAA_SIGNED` |
| **Twilio — SMS** | Text delivery | Minimal (name + link + codes) | Yes (SMS) | **Required — sign addendum** | n/a | `TWILIO_BAA_SIGNED` |
| **Twilio SendGrid — email** | Transactional email | Name only (codes/links are not PHI) | **No** | **Will not sign** | Patient name stripped from email body; warned at startup | `SENDGRID_BAA_SIGNED` |
| **ElevenLabs — TTS** | Voice synthesis | Yes (voice script) | Unconfirmed | **Pending** | Script de-identified (names, dates, MRN/MBI, contact info) | `ELEVENLABS_BAA_SIGNED` |
| **Tavus — AI video avatar** | Conversational avatar | Yes (EHR summary + script) | Unconfirmed | **Pending** | System prompt + context de-identified | `TAVUS_BAA_SIGNED` |

> Cloud hosting (Railway/Render) and any object storage are also subprocessors and
> need BAAs; track them here too once finalized.

## Human actions (cannot be done in code)

1. **Execute BAAs** with Anthropic (first-party Claude API only — not
   Console/Workbench/consumer tiers), Twilio (SMS addendum), and confirm/execute
   with **ElevenLabs** and **Tavus**. Set the matching `*_BAA_SIGNED=1` flag only
   after signing.
2. **Move PHI email off SendGrid.** SendGrid is not HIPAA-eligible and Twilio will
   not sign a BAA for it. Either route PHI-bearing email through a BAA-backed
   provider (e.g. Paubox, LuxSci) over SMTP, or keep email limited to non-PHI
   (the app already strips the patient name and never puts clinical content in the
   body). Use SendGrid only for non-PHI transactional mail.
3. **Self-hosted SMTP** to a BAA-backed relay is treated as PHI-eligible by the
   app (`email_phi_allowed()`), so configuring `SMTP_*` instead of SendGrid
   restores name personalization in emails.

## How the gate works

- `phi_allowed(vendor)` → True only if the vendor is HIPAA-eligible **and** its BAA
  flag is set. Unknown vendors are treated as not covered.
- `deidentify_for_vendor(text, patient_name=…)` → best-effort Safe-Harbor scrub
  (names, dates, email, phone, SSN, MBI/MRN, long numeric ids, ZIPs). It is
  defense-in-depth, **not** a substitute for a BAA where one is required.
- Voice (`ElevenLabsClient.synthesize`) and avatar (`TavusClient.create_conversation`)
  de-identify automatically when the vendor lacks a BAA.
- Email (`send_to_patient`) drops the patient name from the body when the active
  transport isn't PHI-eligible.
