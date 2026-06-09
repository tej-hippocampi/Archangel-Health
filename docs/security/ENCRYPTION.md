# Encryption (PRD-6)

HIPAA §164.312(a)(2)(iv) (encryption/decryption) and §164.312(e)(2)(ii) (encryption
in transit). Encryption to a NIST standard (AES-256) also provides **breach
safe-harbor**: encrypted lost data is not a reportable breach.

We use a **layered** model.

## 1. In transit
- **TLS 1.2 / 1.3** terminated at the platform edge; HSTS is set in production
  (PRD-2) so browsers refuse plaintext after first contact. Reference: NIST SP
  800-52.

## 2. At rest — volume encryption (primary)
The database (`team.db` / `TEAM_DB_PATH`), the audit log, generated audio, and any
JSON snapshots live on the host volume. Railway/Render (and the cloud block storage
beneath them — AWS EBS / GCP PD) **encrypt volumes at rest by default (AES-256)**.
This is the primary, accepted at-rest control for the database and is inherited
from the platform's BAA-backed infrastructure. Reference: NIST SP 800-111.

> Confirm volume encryption is enabled with your host and record it in the vendor
> register; it covers `team.db` (episodes, surveys, escalations, intake, messages,
> the audit trail) at the storage layer.

## 3. At rest — application-layer field encryption (defense-in-depth)
`backend/field_crypto.py` provides **AES-256-GCM** authenticated field encryption
on top of volume encryption, for PHI we write to disk as files:

- The persisted **patient-store snapshot** encrypts its PHI fields — `name`,
  `phone`, `email`, `voice_script`, `battlecard_html`, and the JSON `structured_data`
  / `resources` blobs — so identifiers and clinical content are ciphertext on disk.
- Token format `enc:v<version>:<nonce>:<ciphertext>`. Tampering is detected by the
  GCM tag (decryption fails). Legacy plaintext values pass through and are upgraded
  to ciphertext on their next write (incremental rollout — no big-bang migration).

### Keys
- `DATA_ENCRYPTION_KEY` — base64 32-byte active key; inject from a KMS / Secrets
  Manager. Generate: `python3 -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"`.
- `DATA_ENCRYPTION_KEY_VERSION` — label for the active key (default "1").
- `DATA_ENCRYPTION_KEY_RING` — `ver:b64,ver:b64` of retired keys kept for
  decryption during rotation. To rotate: add the new key as the active version,
  move the old key into the ring (decrypt-only), then re-write data to re-encrypt.
- If `DATA_ENCRYPTION_KEY` is unset, field encryption is inactive and the app
  relies on volume encryption only; in production this logs a startup warning.

There is a one-time helper to encrypt an existing plaintext snapshot:
`python3 backend/scripts/encrypt_existing_phi.py`.

## Roadmap (next increment)
Extend field encryption to selected free-text PHI columns in `team.db`
(e.g. `care_team_messages.body`, `escalations.conversation_snapshot`,
`survey_responses.answers_json`) via the TeamStore accessors, or adopt SQLCipher
for transparent whole-database encryption. Deferred here to avoid destabilizing the
data layer; volume encryption covers the DB in the meantime.
