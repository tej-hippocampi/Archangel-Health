# PRD-6: Application-layer encryption at rest for PHI (AES-256-GCM)

§164.312(a)(2)(iv); AES-256 to NIST standard also provides breach safe-harbor.

## Context
PHI persists in plaintext: `backend/auth_users.json`,
`backend/demo_patient_store.json`, and `team.db` (SQLite). No app-layer encryption.

## Goal
Encrypt sensitive PHI fields at rest with AES-256-GCM using a KMS-managed key, with
a clean key-rotation story; document disk-level encryption inheritance.

## Implementation
1. New `backend/crypto/field_crypto.py`: `encrypt_field(plaintext) -> token`,
   `decrypt_field(token) -> plaintext` using AES-256-GCM (`cryptography`). Key from
   env `DATA_ENCRYPTION_KEY` (base64, 32 bytes); in prod injected from AWS KMS /
   Secrets Manager. Token format `v1:<nonce_b64>:<ct_b64>` for key/version rotation.
   Support a `KEY_RING` of `{version: key}`.
2. Define the PHI field set to encrypt at rest in the patient store / `team.db`:
   patient name, dob/date_of_birth, mrn, phone, email, and the `structured_data`
   clinical blob. Encrypt on write, decrypt on read via a thin accessor layer so
   callers are unchanged.
3. For `team.db`, encrypt the sensitive columns (store ciphertext tokens). Provide a
   one-time migration `backend/scripts/encrypt_existing_phi.py`.
4. Keep demo working: if `DATA_ENCRYPTION_KEY` is unset AND `ENV != production`, fall
   back to plaintext with a loud warning (dev only); in production, refuse to boot
   without a key.
5. `docs/security/ENCRYPTION.md`: document AES-256-GCM at rest, TLS 1.2/1.3 in
   transit (ref NIST SP 800-52 / 800-111), cloud volume encryption inheritance, and
   key management.

## Acceptance criteria
- Round-trip encrypt/decrypt; tampered ciphertext fails GCM auth (test).
- A patient written to the store has ciphertext on disk (assert the raw JSON/db
  bytes do not contain the plaintext name/MRN).
- Key rotation: a token encrypted under v1 still decrypts after adding v2 (test).
- Prod boot without `DATA_ENCRYPTION_KEY` raises; dev falls back with warning.
- Tests in `backend/tests/test_field_crypto.py`. Add `cryptography` to
  `requirements.txt`.
