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

## Chunked upload — how a multi-GB bundle arrives (PRD-I §1)

The platform closes a request body that has not finished uploading within **five
minutes**. That is an edge property, not a tunable: at a realistic hospital 20 Mbps
that buys ~750 MB, at 5 Mbps ~180 MB. **A multi-GB single-request upload is
physically impossible**, and raising a byte cap does nothing about it. So anything
above `8 MB` goes through a three-phase handshake, where the five minutes applies
*per chunk* and stops mattering.

```
POST   /api/asclepius/hs/uploads/sessions                    declare {filename, size, sha256}
                                                             → server mints the session + chunk plan
GET    /api/asclepius/hs/uploads/sessions/{id}               → which parts are already stored (resume)
PUT    /api/asclepius/hs/uploads/sessions/{id}/parts/{n}     one chunk + X-Chunk-SHA256
POST   /api/asclepius/hs/uploads/sessions/{id}/complete      assemble, re-verify, create the upload
DELETE /api/asclepius/hs/uploads/sessions/{id}               abandon and release the parts
```

- **Nothing is an upload until it verifies.** No `ingest_uploads` row exists until
  `complete` has recomputed the sha256 over the assembled bytes and matched it
  against what was declared. An assembled-but-unverified file is invisible to the
  application by construction, not by convention.
- **`(sha256, byte_size, verified_at)` is the chain-of-custody record.** Per-chunk
  digests catch a corrupt chunk but cannot catch a missing or reordered one, and
  byte counts miss silent truncation — a short read that reports its own short
  length is internally consistent all the way down. The whole-file digest is the
  only thing that proves nothing was lost, and it is what you show a partner who
  asks *"did you get everything."*
- **Resumable and idempotent.** Re-declaring the same `{sha256, size}` returns the
  **existing** session, so a refresh at 3.2 GB resumes rather than restarts. The
  session id is the idempotency key. Unverified parts are reaped after
  `ASCLEPIUS_UPLOAD_SESSION_TTL_HOURS` (24).
- **Scale path.** Presigned direct-to-object-storage would scale further and is the
  right answer *once a BAA with the storage vendor exists*. That is a contract
  question, not an engineering one, and is deliberately not in this release.

New env vars: `ASCLEPIUS_UPLOAD_CHUNK_BYTES` (16 MB) ·
`ASCLEPIUS_INGEST_MAX_BUNDLE_BYTES` (8 GB) · `ASCLEPIUS_UPLOAD_MAX_PARTS` (4096) ·
`ASCLEPIUS_UPLOAD_SESSION_TTL_HOURS` (24) · `ASCLEPIUS_INGEST_MAX_ENTRY_BYTES`
(64 MB) · `ASCLEPIUS_INGEST_MAX_RATIO` (100) · `ASCLEPIUS_INGEST_TOTAL_RATIO` (10) ·
`ASCLEPIUS_PORTAL_BUDGET_MS` (120).

### Archive safety at size

Header-declared entry sizes are attacker-controlled and are used for nothing. Every
ceiling is enforced against bytes **actually produced**, mid-write, so a bomb costs
the chunk in flight rather than the whole expansion: a per-entry byte cap, a
per-entry compression-ratio cap, and a whole-archive output budget that scales with
the bytes the partner really uploaded. Entry bytes are spilled to disk rather than
all retained, so bundle memory is one entry rather than the sum of them. Nested
archives are rejected rather than opened, which is the nesting-depth cap stated as
a rule. **Antivirus is bounded, and 'we could not tell' is not 'this is malware'.**
ClamAV's own scan-size limits fight multi-GB files and its "unlimited" config OOMs a
small container, so above `ASCLEPIUS_MALWARE_SCAN_MAX_BYTES` (512 MB) inline scanning
is skipped and the object is left to a post-`verified` worker — the answer is
*later*, not *no*. Below it the timeout scales with the bytes being scanned rather
than sitting at a flat 120 s that is generous for a 4 MB CSV and impossible for a
500 MB bundle. A **detection** is a hard rejection. A **timeout or a broken scanner**
is inconclusive: the upload is held for review instead of being rejected with copy
that tells a hospital their clean bundle was malware.

## Purpose — task creation vs brokering (PRD-I §2-§4)

An upload is for one of two things, and **the health system must never be able to
tell which**. Not from the URL, the page, an API response, an email, an error
message, a header, a timing difference, or a response size. Only the admin knows.

This is a business-confidentiality requirement rather than a legal one — brokering
is permitted; the concern is that a partner who knows their data goes to brokering
will go direct to the buyer. It is built to the standard of a security control
anyway, because that is the only standard that actually holds.

- **The admin mints one of two links.** Same form, same endpoint, same code path,
  one different value: *Send link — task creation* / *Send link — brokering*.
  Everything downstream of the mint is identical.
- **`purpose` is a column, not a branch.** It lives on the row that authorizes an
  upload (the portal account, or the magic link) and is copied server-side onto the
  upload and then the case by a JOIN. It is never sent by a client, never accepted
  from a request body, and never inferred from anything the provider controls. The
  upload doors are not even given a way to *name* it — they pass the authorizing
  account and the store resolves the rest.
- **A brokering case can never become a task.** Both promote endpoints refuse, and
  the refusal reads the case's purpose *or its upload's*, so a failed propagation
  cannot present as NULL and resolve to task creation. A mixed batch promotes the
  task-creation cases and reports the skipped count rather than failing wholesale.
- **`NULL` means a link minted before this existed.** It resolves to task creation
  in the promotion gate **and nowhere else** — everywhere the admin can see it
  renders as *"Purpose not set — legacy link"*, in lime, with buttons to resolve it.
- **Brokering has its own admin bucket**, with no Promote button in it. The server
  refusing is the enforcement; the missing button is the affordance; a design that
  relies on only one of the two eventually promotes one by accident.

Three tests are deliverables, not extras — `test_purpose_isolation.py` (static:
provider-reachable code may not name the distinction), `test_link_indistinguishability.py`
(two partners provisioned identically except for purpose; every observable
compared, including an error-differential fuzz over every failure mode), and
`test_promotion_gate.py`.

**Both doors carry it.** `POST /admin/upload-links` requires `purpose` exactly as
the health-system form does, and a link-door upload joins its purpose from the link
row through the same `attach_upload_provenance` call the other two doors use. All
three doors resolve through that one function, which is what makes "the same bytes
are recorded the same way whichever door they came in" true by construction rather
than by three implementations agreeing.

**Purpose is resolved LIVE, never snapshotted.** A chunked session stores only
`actor` and joins through it at completion. A session lives 24 h and resumes across
that window, so a copy taken at declare is stale for every byte that arrives after
an admin corrects the mint — and the single-request door, which resolves live, would
record the same bytes differently.

**Corrections have a direction.** `brokering → task_creation` is refused on an
upload, and on an account that has already sent anything: that transition *is* "a
brokering case becomes promotable", spelled differently. `task_creation → brokering`
stays open — it removes a promotion path and never adds one. Resolving an unset
purpose is allowed either way.

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
- **Sender failure notifications.** A link can carry an optional **contact
  email** (set on the Mint form). If an upload through it ends up **rejected** or
  its raw blob is **lost**, we automatically email that address a reassuring,
  **PHI-free** note — *your file didn't come through; nothing was leaked and there
  was no data breach; please re-send* — fired once per upload. The admin can also
  hit **"Notify sender"** on any non-ingested row to send it manually (repeatable).
  Uses the shared email transport (`EMAIL_DEV_MODE=1` prints instead of sending);
  quarantine (held for review) does **not** auto-email.
- **Partner Uploads list** paginates over full history (`GET /ingestion/uploads?
  limit=&offset=`, returns `total`) — nothing scrolls off at 50 anymore.
- **Storage durability is a boot gate, not a warning** (PRD I-0). All three stores
  — the **database**, the raw ingest dir, and the asset store — are checked at
  startup. In production a non-durable store **refuses to boot**, and the log names
  which one. A container that will not start is a five-minute incident; a container
  that quietly eats uploads is a partnership. In development it warns and starts.
  - Set all of `ASCLEPIUS_DATA_DIR`, `ASCLEPIUS_DB_PATH`, `ASCLEPIUS_ASSET_STORE`
    and `ASCLEPIUS_INGEST_DIR` to paths on the mounted volume, plus `ENV=production`.
    `ASCLEPIUS_DATA_DIR` alone would resolve the others by convention — set them
    explicitly so a future reader does not need the resolution order to know where
    the data is.
  - **The database check is the one that matters most and was the last to exist.**
    Losing image blobs degrades cases; losing `asclepius.db` destroys every user,
    task, submission, review and payout row at once. It is probed for writability,
    not merely non-ephemerality: a read-only or failed mount presents as a perfectly
    healthy directory and fails on the first write.
  - Pointing `ASCLEPIUS_ASSET_STORE` at `/tmp` now **triggers** the ephemeral
    warning instead of silencing it. The old predicate answered "did anyone set the
    variable", so setting it to an ephemeral path quieted the alarm without
    touching the fire.
- **`GET /api/asclepius/admin/storage/reconcile`** (admin) reports every asset
  reference whose blob is gone and every blob with no reference, and the live
  durability verdict for all three stores. It is **read-only**: an orphan blob costs
  disk, a wrongly-deleted blob costs a case whose partner bundle has already aged
  out of retention. A non-zero `missing_count` is an **incident, not a metric** —
  the admin Health Systems page renders it in pink, and states the healthy case
  explicitly ("All N asset references resolve") so an empty panel and a healthy
  panel never look identical.
- `ASCLEPIUS_MALWARE_SCAN_CMD` — plug a real AV (e.g. `clamscan --no-summary`);
  fail-closed. Without it only structural zip checks run. The scanner is handed
  **plaintext**: it was previously given the encrypted blob, so with
  `DATA_ENCRYPTION_KEY` configured a real engine scanned ciphertext and reported
  clean every time.
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
