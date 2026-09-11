# Bulk surgical footage: upload, storage, and brokering

Status: first implementation built behind `ASCLEPIUS_MEDIA_ENABLED=1`; disabled by default. Production provisioning and real-cloud scale validation remain release gates. See [deployment and acceptance runbook](../asclepius/BULK_MEDIA_OPERATIONS.md).

Inspected checkout: `5c585be3856a7d21de878b5c1cb7eef23f5cd93f`. Date: 7 September 2026.

## Outcome

The current product is **not ready to reliably receive and retain 100–1,000 hours of surgical footage for brokering**. It has useful resumable-upload and authorization foundations, but its defaults and processing model target small clinical bundles. Increasing environment limits alone is insufficient.

The founder confirmed that this footage is for brokering. File sizes, codecs, bitrates, and transfer origins are unknown. Build receipt and preservation independently of clinical parsing, with browser and cloud-transfer paths. Brokering media must never automatically become physician tasks or reach model APIs.

## Baseline evidence and corrections

The following evidence describes the original clinical upload path, which remains available with its clinical size limits. Railway was subsequently inspected read-only; cloud accounts and real throughput have not been verified. Corrections made by this implementation are identified below.

1. Daily organization allowance is **5 GiB per rolling 24 hours**. `_hs_upload_quota_bytes` is defined at backend/routers/asclepius_provider.py:607; the resumable endpoint applies the remaining allowance through `_hs_quota_remaining` before declaring a session at backend/routers/asclepius_provider.py:1277. Thus a fresh organization cannot actually send an 8 GiB file under default settings.
2. Resumable files have an **8 GiB declaration ceiling**, 16 MiB default chunks, and 4,096-part ceiling. `max_bundle_bytes` is at backend/asclepius/uploads.py:105. `chunk_size_bytes` is at backend/asclepius/uploads.py:94. These are configurable, not demonstrated terabyte capacity.
3. The regular multipart request is capped at **100 MiB**. `max_zip_bytes` is at backend/asclepius/ingestion.py:52. Selecting more than one file uses that regular path even when the files are large; the `CHUNKED_MIN_BYTES` branch is at frontend/provider/provider.js:760.
4. The browser pre-hashes the entire file before transfer and sends parts sequentially, stopping on the first failed part. The existing flow can resume after reselecting a file, but has no persistent multi-file queue or automatic part retry: frontend/provider/provider.js:686 and frontend/provider/provider.js:721. The picker in frontend/provider/index.html excludes video extensions.
5. A ZIP member has a **64 MiB extraction cap**. `max_entry_bytes` is at backend/asclepius/ingestion.py:81. `_classify` at backend/asclepius/ingestion.py:851 returns unsupported for MP4, MOV, MKV, and AVI. The clinical pipeline rejects a bundle without parseable clinical content through `_fail` at backend/asclepius/ingestion.py:1779. An upload receipt therefore does not establish that video was accepted as a usable asset.
6. Raw storage is local and normally placed beside the database. `_default_ingest_dir` is at backend/asclepius/ingestion.py:124. Upload admission budgets **4× file size** for working copies; `disk_amplification_factor` is at backend/asclepius/uploads.py:141. Concurrent organizations can compete for that same disk; a free-space check is not a global capacity reservation.
7. Object storage is not implemented in the existing asset backend: backend/asclepius/assets.py:68 raises an error for an S3 location. Its durability check now correctly rejects that setting at backend/asclepius/assets.py:218; the new media adapter is separate. A configuration string is not a working storage adapter.
8. The September 10 preservation audit removes age-only raw-original deletion. `purge_expired_raw` at backend/asclepius/ingestion.py:538 now cleans only disposable scratch; accepted originals and unacknowledged recovery blobs require a separate authorized disposition. Capacity, backups and restore evidence remain operational release gates.
9. Admin download previously loaded the complete raw object into memory. It now streams encrypted frames through the reader at backend/routers/asclepius.py:6778.
10. Completion queues clinical ingestion in the web process at backend/routers/asclepius_provider.py:1453. `process_upload` at backend/asclepius/ingestion.py:1759 scans, unpacks, and builds clinical cases. There is no dedicated media catalog/transfer worker in this path.
11. Configured malware scanning returns a successful result with a deferred message above the default 512 MiB ceiling at backend/asclepius/ingestion.py:808. Repository search found no implementation of the referenced post-verification scanner. Deferred must become a real pending state and durable job, not an implied clearance.
12. Existing purpose restrictions are valuable: `blocks_promotion` at backend/asclepius/ingestion.py:233 only permits task creation for explicitly cleared data. Preserve that boundary and the existing account/organization/realm checks.

Isolated execution of the unchanged limit and classification functions confirmed the defaults and unsupported video classifications. Machine-readable evidence: [bulk-media-inspection.json](bulk-media-inspection.json). This was not an end-to-end or load test.

## Capacity design

Hours are a catalog metric; admission and storage capacity must use bytes. Decimal GB = bitrate in Mbps × hours × 0.45. These scenarios are assumptions, not measured footage specifications:

- 5 Mbps: 100 hours = 225 GB; 1,000 hours = 2.25 TB.
- 25 Mbps: 100 hours = 1.125 TB; 1,000 hours = 11.25 TB.
- 100 Mbps: 100 hours = 4.5 TB; 1,000 hours = 45 TB.
- 400 Mbps: 100 hours = 18 TB; 1,000 hours = 180 TB.

Multiple camera tracks, sidecars, versions, derivatives, and backup copies add storage. Unknown or uncompressed formats can exceed these scenarios. Never promise unlimited bytes based on an hours allowance.

Initial product target: **1 TiB per file, 100 TB per collection, 100,000 files per collection**, with explicit per-organization and collection reservations. Larger collections, such as the 180 TB scenario, must support raising the configured allowance or grouping collections under one dataset without redesigning the storage layer. These are proposed limits to validate, not existing capabilities or an automatic permission to incur storage costs.

At an ideal sustained 100 Mbps uplink, 11.25 TB takes about 10.4 days and 45 TB takes about 41.7 days. At 1 Gbps, about 1.04 and 4.17 days. Protocol overhead and interruptions increase those times. Support long-running work and show realistic remaining-time estimates; software cannot remove the sender's bandwidth constraint.

## Architecture and invariants

Browser or provider-side uploader → private object storage for file bytes. Archangel API → authorization, reservations, collection metadata, short-lived upload access, and receipts. A durable queue → separate verification/catalog/import workers. Authorized buyer → direct, resumable object download or cloud delivery.

Recommended first backend: Amazon S3 using multipart uploads, private buckets, versioning, and managed encryption keys. Keep the Railway application as the control service. S3 currently documents 10,000 parts and 5 MiB–5 GiB part sizes; the product must calculate a valid plan and paginate part listings. Its documented object ceiling is above our proposed 1 TiB limit. [S3 multipart specifications](https://docs.aws.amazon.com/AmazonS3/latest/userguide/qfacts.html).

Railway currently documents self-service volume growth to 1 TB for Pro and above, and no replicas for services with volumes. This supports moving bulk bytes out of the app volume; it does not establish this deployment's actual plan or size. [Railway volume documentation](https://docs.railway.com/volumes/reference).

Use a dedicated PostgreSQL metadata store for the new transfer subsystem, with transactional quota reservations and job outbox. Existing app stores can remain unchanged initially. Provider authorization stays in the application; new rows reference its organization/account identifiers. Keep transfer reservation, completion, and outbox writes within the new store so workers do not require access to local SQLite files. An optional legacy upload-history link must reconcile idempotently, not pretend to be a transaction spanning both stores.

Core invariants:

- Original bytes are immutable and version-addressed. Conversion, thumbnails, or redaction produce separately tracked derivatives.
- Receipt, integrity, inspection, and release are distinct. A file can be stored without being cleared for buyer delivery or supported for preview.
- Brokering and unknown-purpose data never automatically enter task creation, evaluation exports, or model APIs. Internal commercial purpose remains server-controlled; the provider transfer flow remains neutral.
- Every collection, file, session, object key, and download grant is scoped to the organization and live/sandbox realm. Never trust a client-supplied storage key or purpose.
- The web service does not buffer, assemble, unpack, proxy, or hash whole media files. Worker resource budgets remain bounded independently of total collection size.
- Original deletion requires an explicit retention disposition and an audited workflow; the clinical 30-day sweeper cannot touch media originals. Temporary abandoned multipart parts have a separate cleanup policy.

## Implementation sequence

### 1. Durable media storage and metadata

Add a separate media subsystem, rather than increasing clinical ZIP extraction limits. Suggested new files: backend/asclepius/media_store.py, backend/asclepius/media_storage.py, backend/routers/asclepius_media_ingest.py, and deployment configuration for storage and workers. These paths are now implemented; delivery, source import, and provider-side scripts are separate modules.

Add collections, media files, transfer sessions, reservations, jobs/outbox, and delivery manifests. Store organization, realm, actor, request association, opaque object key, version, original filename, relative path, declared/actual bytes, checksums with algorithm/type, source provenance, purpose, retention policy, and timestamps. Index organization + creation time and collection + stable file cursor. List endpoints must paginate; no enormous nested JSON file lists.

Use immutable randomly assigned object keys; duplicate basenames never overwrite files. Separate file identity from content similarity. Idempotency is scoped to uploader/collection/file/session; avoid cross-organization deduplication disclosures. Capture authorized source/license references and permitted recipient/use metadata without inferring rights from possession of a file.

The new adapter must implement upload initiation, part signing, part listing, completion, abort, metadata lookup, ranged download grants, and storage readiness checks. Verify real bucket/key permissions and encryption configuration, not just a nonempty environment variable. Add backup/restore and object-to-catalog reconciliation procedures. S3 versioning alone is not a complete recovery plan for metadata or key loss.

### 2. Resumable browser and batch receipt

Add paginated collection/file declaration, session resume, bounded part-signing, completion, and abort APIs. Reserve quota transactionally before accepting bytes. An idempotent resume must reuse its reservation, even when the remaining allowance is zero; the current declaration order can charge an already-open file against remaining quota again. Reconcile reservations on completion/abort/expiry and after failures.

The browser uploads parts directly to storage. Start around 64 MiB per part, enlarge for large files to stay within provider part limits, and reduce concurrency to keep aggregate in-memory buffers within a tested budget. Sign small pages of parts on demand, enforce the planned lengths and checksums as supported by the chosen client/provider, and validate actual stored part sizes before completing. Do not issue unrestricted bucket credentials or allow clients to finalize arbitrary objects.

Support multiple files and folders, relative paths, drag/drop, a persistent queue, file-level progress, total bytes, pause/resume/cancel, bounded retries with jitter, and expired-signature refresh. A failed file must not restart successful files. Hash slices in a worker while transferring instead of requiring a complete pre-upload pass. Persist session metadata in IndexedDB; on browsers without persistent file handles, ask the user to reselect the source files and verify identity before resuming. Closing or sleeping a browser pauses it—do not claim background transfer continues.

Persist active sessions across application redeploys. Proposed policy: expire after seven days without progress, with warned resumability deadlines; active transfers renew their leases and can run for weeks. Coordinate object-store lifecycle cleanup with those leases. Reauthentication must recover the same session. Revocation prevents new signatures and completion; already-issued URLs may remain usable until their short expiry, so completion authorization remains mandatory.

### 3. Verification and media catalog

Completion is idempotent and reconciles uncertain provider responses. Verify part sequence, counts, lengths, object version, and checksums; create a durable job before acknowledging work as queued. Use durable leases, retries, dead-letter states, and restart recovery. No long processing inside an HTTP completion request or a FastAPI background task.

Use validated per-part SHA-256 and store its composite checksum separately from a full-file digest. Compute full-file SHA-256 in a streaming worker and compare against the sender's digest when available. Do not represent an ETag or multipart composite SHA-256 as the original file SHA-256. A server-only digest proves the identity of stored bytes, not comparison to a sender-provided original. [S3 checksum types](https://docs.aws.amazon.com/AmazonS3/latest/userguide/checking-object-integrity-upload.html).

Durable workflow: uploading → stored → verifying → cataloged; failures remain visible and retryable. Maintain separate inspection and release fields so stored bytes are never mislabeled as buyer-ready. Batch completion reconciles expected files/counts/bytes; unavailable duration remains unknown, not zero or an assumed hour count.

Run file identification and bounded media probing in isolated workers. Catalog MP4/MOV/MKV/AVI and other supported containers by duration, codec, resolution, frame rate, audio tracks, and camera/session grouping. Preserve unsupported formats as originals with clear preview/inspection status. Handle truncated files, variable frame rates, misleading extensions, split recordings, missing sidecars, and archives. Preserve large archives without automatically expanding them; optional extraction has separate bounded jobs and path/bomb protections.

Large-file scanning must have an actual worker and explicit inconclusive handling. Original footage can contain faces, audio, screens, and embedded metadata; textual clinical de-identification is not evidence that video is cleared. Catalog the supplied clearance status and make any required media review/redaction a separate release step. Do not automatically transcribe or send footage to model vendors.

### 4. Cloud and provider-side transfers

Support the same collection manifest and receipt regardless of transport. First implement direct browser transfer and an S3 source adapter. Then add Azure Blob and Google Cloud Storage adapters against the same contract before advertising those sources as supported. A provider-side resumable uploader covers local folders/NAS and browsers that cannot stay open for days; use scoped, expiring authorization and durable local checkpoints.

Cloud imports run as durable server-side jobs with narrowly scoped temporary access, source object versions, pagination, checkpointing, cancellation, and retry. If a source changes mid-transfer, flag it rather than silently mixing versions. Prefer cloud copy where supported; stream cross-provider data in workers without staging complete files on the app disk. Restrict destinations and source schemes/hosts; do not build an arbitrary URL fetch endpoint. Use pre-approved source connectors, enforce redirects and network boundaries, and redact credentials/URLs from logs.

### 5. Brokering catalog and buyer delivery

Admin view: provider, dataset, verified bytes/files/hours, incomplete transfers, format coverage, provenance, purpose, retention, release status, and storage usage. Provider view: understandable progress and receipts, without exposing internal commercial-purpose controls.

Build immutable delivery manifests referencing exact object versions and checksums. Downloads use authorized short-lived links with range/resume support; large deliveries use individual files or cloud transfer, not an app-generated multi-terabyte ZIP. Distinguish link issuance, bytes transferred, and buyer acknowledgement. Preserve audit history and enforce the recorded recipient/use restrictions. Never mark data delivered merely because a link was generated.

Record original/derivative/version storage, incomplete-part usage, verification reads, cloud-transfer traffic, and buyer egress. Budget estimates must include these separately; no price quote is justified until storage region, retention, and delivery pattern are selected.

## Tests and release gates

Baseline validation (before implementation): established an isolated Python 3.12.14 environment and ran 90 existing upload, storage-durability, purpose-isolation, and provider-portal tests: **90 passed**. Nine additional local PRD readiness probes produced **1 pass and 8 failures**. A real 128 MiB synthetic MP4 transferred in eight default-size parts, resumed after part three, and round-tripped through encrypted storage with matching SHA-256, but was rejected by clinical ingestion. The probes also reproduced large-file/batch admission failures, quota-exhausted resume failure, brokering-original deletion after 30 days, and the missing S3 backend. See the [implementation validation report](../asclepius/BULK_MEDIA_VALIDATION.md) for subsequent results. These results establish local baseline behavior, not production or terabyte capacity. The previously reported Python environment blocker has been resolved.

Implementation validation: 128 focused tests and four browser behavior scenarios passed. The full local run passed 6,392 tests with one existing admin-header test failure reproduced on unchanged main and two skips. PostgreSQL and full Linux CI must pass before merge.

Required gates:

1. Run existing upload, provider DOM, storage-durability, ingestion, purpose-isolation, and brokering regression tests. Follow the repository's data-inventory workflow before/after migrations; preserve every existing ID and register new tests with the CI shard inventory.
2. Unit and API tests: per-file and collection size boundaries, more than 1,000 parts, 100,000-file pagination, atomic concurrent quota claims, resume at exhausted quota, duplicate filenames, tampered keys, cross-tenant/realm access, expired authorization, corrupted/missing/reordered parts, duplicate completion, and cancellation races.
3. Fault injection: terminate browser, application, and worker; disconnect networking; expire a signed URL; revoke an account; change cloud source content; deliver duplicate/out-of-order queue events; lose a completion response. Completed bytes must not be duplicated or lost.
4. Retention and purpose tests: no automatic clinical processing, task/model use, or 30-day deletion of brokering originals; inspection failure cannot release a file; archived metadata and verified originals can be restored together.
5. Real object-storage staging tests using synthetic nonclinical data: upload and download a 100 GiB file, exercise the proposed 1 TiB file limit before claiming it supported, and run a multi-day batch with restarts. Verify full hashes end to end. Metadata-only simulations are useful but do not establish real terabyte transfer capacity.
6. Measure transfer-worker memory/disk, aggregate throughput, verification backlog, and app latency under at least 20 concurrent provider sessions. Set explicit tested concurrency and resource limits; target no more than 10% regression in baseline physician-portal p95 latency under the agreed load.
7. Before accepting the real collection, verify production bucket access, encryption, region and applicable data-handling arrangements, collection allowance, retention, recovery, and a successful provider rehearsal. Actual providers, credentials, and deployed settings remain operational dependencies, not questions blocking this design.

## Do not touch / out of scope

This plan does not redesign physician annotation, revive legacy peri-operative features, alter earnings, promote footage into clinical tasks, automatically redact/transcode every original, or migrate the entire application database. Preserve the existing small clinical-file route. Do not delete existing data in a migration or copy clinical encryption containers into object storage without a verified reader/migration strategy.

Original build order: storage/metadata foundation → browser batch receipt + verification → provider rehearsal → cloud/provider-side transfer adapters → buyer delivery and operational scale validation. The initial 100-hour receipt can open after its transport and capacity pass the gates; 1,000-hour support is claimed only for tested byte limits and deployment settings.


## Implemented scope and remaining release gates

Built: private versioned S3 multipart adapter; PostgreSQL metadata with transactional reservations; realm/organization/account scoping; 1 TiB planning; paginated catalogs; durable completion, verification, cancellation and inactivity cleanup; orphan MPU reconciliation; full-object SHA-256 distinct from multipart composite checksums; direct browser folder/file queue with IndexedDB, hashing worker, retries and resume; a local/NAS CLI; version-pinned, allowlisted S3 cloud imports; admin inspection attestations; immutable buyer-bound manifests and short-lived versioned downloads. Link issuance is audited as issuance, not delivery proof. Original retention is explicit and never handled by the clinical sweeper.

Implementation choices: the file row itself is the durable job, so reservation and job state share one PostgreSQL transaction without a second queue write. The initial browser and CLI use one part at a time to bound memory. Multipart cleanup is based on tracked inactivity and orphan reconciliation; do not attach an age-based S3 abort lifecycle to active uploads. Full hashing streams in a separate worker and restarts after a worker crash; completed multipart transfers are reconciled via HEAD before retrying completion.

Remaining product extensions: Azure/GCS adapters, automated AV/media inspection and preview extraction, cloud delivery to buyer buckets, and richer admin/buyer UI. The initial inspection workflow requires an administrator to record externally completed malware, privacy and licensing review evidence. Unsupported media remains stored and held without parsing or model calls. These are explicit extensions, not claims of implemented automation.

Release remains disabled until PostgreSQL/S3 credentials, bucket/KMS configuration, workers, recovery drills, provider rehearsal and real-cloud capacity tests pass. Metadata tests at 1 TiB and catalogs with 100,000 rows do not demonstrate a 1 TiB transfer or 1,000 hours of production capacity. Local browser tests use synthetic bytes and intercepted HTTP responses, not an AWS bucket.
