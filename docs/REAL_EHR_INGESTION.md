# Real EHR Ingestion — Partner Secure Upload → V4 Cases

How a data partner's **already de-identified, date-shifted** clinical data becomes
gradable **V4 real-case** tasks. (Implements `Asclepius_Real_EHR_Ingestion_PRD`.)

## The flow

1. **Admin mints a secure link** — Asclepius admin → 🏥 Ingestion → *Mint secure
   upload link* (partner id, specialty, expiry, single-use). **Copy the URL
   immediately: the token is shown once and only its SHA-256 is stored.**
2. **Partner uploads a `.zip`** through `/partner/upload?t=…` — no app account.
   Accepted content: FHIR R4 Bundle JSON, HL7 v2 (ORU) messages, lab CSV/TSV,
   plain-text/markdown notes, optional `manifest.json`. **No imaging** (DICOM
   entries are rejected; an imaging-only bundle rejects outright).
3. **Pipeline** (all server-side, background): virus-scan hook → zip-bomb-safe
   unpack → per-entry classification → format adapters → one case per patient →
   **timeline normalization** (their shifted dates → our relative day offsets;
   note dates rewritten to `[day −5]` form; the calendar never enters the model)
   → **de-id verification** (pluggable: baseline / Presidio / Comprehend Medical)
   → the `deidentify()` hard guard → `ingest_cases` row.
4. **Outcomes** — `ingested` (clean) or `quarantined` (masked findings; admin can
   *scrub the flagged spans*, *reject*, or *override with a documented reason* —
   the hard guard can never be overridden). Nothing partial, nothing silent.
5. **Promote** — admin attaches the clinical question; candidates are generated
   *on the real case*; hardness judge + the **real-case judge** (coherence,
   multimodal necessity, reasoning divergence — **no ground-truth dimension: the
   specialist is the answer key**) gate it; the task enters the **V4 queue**.

## The V4 wall (never violated)

A `case_source="real_deid"` task is a V4 task and only a V4 task, enforced
server-side in three layers: **routing** (v4 serves only real; v1–v3 exclude
real), **derivation** (the stamped portal version derives from the task; a
mislabel claim is a 400), **packaging** (a mismatch routes to `needs_qa`; no
record ships mislabeled). V4 is served **only** to contributors with
`real_data_approved` (admin: `POST /users/{id}/real-data-approval`); everyone
else sees the V4 box locked. V4 never autofills — real data can't be fabricated.
The real-case value premium (`ASCLEPIUS_VALUE_REAL_CASE_MULT`, default 2.0) keys
off `case_source`, not the label.

## The partner manifest (optional but recommended)

```json
{
  "patient_key": "p1",            // authoritative grouping: one case per key
  "index_event": "2031-03-19",    // day-0 anchor (else: latest observation)
  "specialty": "nephrology",
  "note_type": "Consult",         // for bare-text notes
  "column_map": {"analyte": "TestName", "value": "Result"}   // CSV overrides
}
```

## Ops

- Raw zips: AES-GCM-encrypted at rest (`DATA_ENCRYPTION_KEY`), auto-purged after
  `ASCLEPIUS_RAW_RETENTION_DAYS` (30). The derived case is what we keep.
- **Raw storage must be durable.** The encrypted blobs default to a dir *beside
  the DB* (`ASCLEPIUS_INGEST_DIR`, defaulting next to `ASCLEPIUS_DB_PATH`) so they
  share the DB's persistent volume. Do **not** point `ASCLEPIUS_INGEST_DIR` at
  `/tmp` on Railway/Render: that dir is wiped on every redeploy, which would leave
  ingested uploads whose admin "Download file" fails with **410** ("raw blob was
  lost") even though the derived cases survive. Blobs are only ever recoverable
  from the partner re-uploading. Guardrails enforcing this:
  - **Fail-closed:** in production the upload endpoints return **503** (refusing
    the file) if the ingest dir resolves to ephemeral storage — we never accept a
    bundle we cannot durably keep. Startup also logs a loud warning if the ingest
    dir is ephemeral or on a different volume than the DB.
  - **Store-before-claim ordering:** the encrypted bytes are written to durable
    disk *before* the one-time link is consumed and *before* the DB row is
    inserted. A storage failure leaves the link valid (partner just retries) and
    strands no row; the row always carries a reachable `raw_path`.
  - **Redeploy recovery:** on startup, uploads left mid-pipeline
    (`received`/`scanning`/`parsing`) are re-processed from their durable blob
    (idempotently — prior un-promoted cases are cleared first). If a blob is
    genuinely gone, the upload is marked `rejected` with a re-upload prompt rather
    than left stuck forever.
- `ASCLEPIUS_MALWARE_SCAN_CMD` — plug a real AV (e.g. `clamscan --no-summary`);
  fail-closed. Without it only structural zip checks run.
- `ASCLEPIUS_DEID_VERIFIER=baseline|presidio|comprehend_medical`.
- Chain of custody: every step logs an audit event (upload checksum, scan,
  per-file outcome, transforms, gates, promote).
- **BAA with the partner is a precondition** — this pipeline verifies de-id, it
  does not replace the agreement.

## The partner conversation (copy-paste)

"Send us a `.zip` through a secure, expiring link — FHIR export, HL7 results,
lab CSVs, and clinical notes are all fine (no imaging). You keep doing the
de-identification and date-shifting exactly as you do today; we independently
verify it, convert your shifted timeline into relative day offsets so the
clinical intervals survive but no calendar date ever enters our system, and map
each modality into the right place in the case. Anything that doesn't pass our
verification goes to a quarantine queue with a reason — nothing silently drops,
and nothing partial gets used."
