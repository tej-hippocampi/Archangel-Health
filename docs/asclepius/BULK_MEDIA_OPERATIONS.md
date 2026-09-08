# Bulk media deployment and acceptance

The bulk path is disabled by default. Merging this code does not configure AWS,
create PostgreSQL, start a worker, or establish production terabyte capacity.
Use synthetic nonclinical files for rehearsal.

## Configuration

Deploy `deploy/media-storage.yaml` in the chosen AWS account/region. Supply the
exact HTTPS portal origin, including a port only if used. The template retains
the bucket and KMS key on stack deletion, enables versioning, blocks public
access, requires TLS, configures KMS encryption and browser CORS. No automatic
original expiration or age-based multipart expiration is installed.

Create a dedicated PostgreSQL database with encrypted transport and backups.
In Railway, add these variables to the web service and a separate worker using
the same image. Store credentials in Railway's secret variables, never GitHub
source or provider forms:

| Variable | Value |
| --- | --- |
| ASCLEPIUS_MEDIA_ENABLED | Leave unset/0 until staging acceptance; then 1 |
| ASCLEPIUS_MEDIA_DATABASE_URL | PostgreSQL connection URL, with required TLS |
| ASCLEPIUS_MEDIA_BUCKET | Bucket output from the template |
| ASCLEPIUS_MEDIA_KMS_KEY | KMS ARN output from the template |
| AWS_DEFAULT_REGION | Bucket region |
| AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY | Scoped service identity, or supported role credentials |
| ASCLEPIUS_MEDIA_ORG_BYTES | Default 100,000,000,000,000 bytes; committed plus reserved |
| ASCLEPIUS_MEDIA_COLLECTION_BYTES | Default 100,000,000,000,000 bytes |
| ASCLEPIUS_MEDIA_SOURCES | Optional operator-managed JSON allowlist below |

Keep existing clinical stores on their persistent Railway volume. Do not set
the legacy asset path to an S3 URL; that legacy adapter remains unsupported.

The service identity needs bucket-scoped `s3:GetBucketVersioning`,
`s3:GetBucketPublicAccessBlock`, `s3:ListBucket`,
`s3:ListBucketMultipartUploads`; object-scoped `s3:PutObject`, `s3:GetObject`,
`s3:GetObjectVersion`, `s3:ListMultipartUploadParts`,
`s3:AbortMultipartUpload`; and key-scoped `kms:GenerateDataKey`, `kms:Decrypt`.
There is intentionally no original delete permission. Approved import sources
add versioned read access and their KMS decrypt permission, scoped to that source.

From the backend working directory, run `python -m scripts.media_worker --migrate`
once. It only creates additive media tables and indexes. After staging is ready,
set the separate worker start command to `python -m scripts.media_worker`.
Schedule `python -m scripts.media_worker --reap-orphans` daily. Orphan cleanup
only aborts unreferenced multipart fragments older than seven days, never
completed originals or actively tracked multipart uploads. The regular worker
expires tracked uploads after seven days of inactivity. Do not add an S3
abort-incomplete lifecycle based on initiation age: it ignores activity renewals.

Start with one worker. Each verifies one file at a time in 8 MiB blocks; browser
and CLI send one part at a time (64 MiB to approximately 105 MiB at 1 TiB).
Scale workers only after measuring CPU, memory, database connections, throughput,
KMS request rates and physician-portal latency. PostgreSQL transactions serialize
quota changes within an organization; no web-service replica holds media bytes.

## Provider transfer

When enabled, the provider upload page shows a separate **Large files and video**
section. Files and folders use direct multipart storage. The clinical drop zone
keeps its existing clinical limits. After closing the browser, select the same
files again to resume; received parts are compared against newly computed hashes.
IndexedDB stores metadata per file, not the footage. Use a new collection for an
intentionally different file with the same identity. Keep the source files stable.

For local disks or mounted NAS, install `httpx` in the provider's Python
environment and run from the backend directory:

```sh
python -m scripts.media_upload --url https://YOUR-PORTAL --username USERNAME /path/to/footage
```

The tool prompts for the password and stores only progress metadata in a local
SQLite file with owner-only permissions. Re-run the same command to resume.
Use `--state` for another collection, and `--realm sandbox` for that realm.

For S3-to-S3 transfer, operators configure an allowlist such as:

```json
[{"id":"hospital-export","realm":"live","org":"HS_ID","bucket":"provider-bucket","prefix":"approved/"}]
```

An authenticated provider submits `POST /api/asclepius/hs/media/imports` with
`collection`, `token`, `path`, `source_id`, `source_key`, `source_version`.
Source versions must be immutable (no missing or null version). The worker copies
bounded ranges and uses stored multipart listings as restart checkpoints. It then
streams the destination version to calculate full SHA-256. Arbitrary URLs and
provider-supplied bucket credentials are not accepted. Azure/GCS require additional
adapters; currently use a provider-side mounted/synced folder or approved S3 export.

## Review and delivery

Receipts distinguish uploading/importing, completing, verifying, stored,
integrity failure and cancelled states. Stored does not mean cleared for release.
No original is passed to clinical ingestion, an LLM, or an automatic transcode.
No automatic malware, video de-identification or preview support is claimed.

An authenticated admin can list a collection using
`GET /api/asclepius/admin/media/{org}/collections/{cid}?after=CURSOR` (100 rows per
page). Record externally completed malware, privacy and licensing review through
`POST /api/asclepius/admin/media/{org}/files/{fid}/review` with `approved` and
`malware_evidence`, `privacy_evidence`, `rights_evidence`. Evidence must point to
the actual completed review. A negative review holds the file and blocks new links.

Create an immutable manifest using
`POST /api/asclepius/admin/media/{org}/deliveries` with `files` (up to 1,000 IDs)
and an existing `buyer` account ID. Every file must have verified storage,
approved inspection/release and server-controlled brokering classification.
Large deliveries can use several manifests. Buyer authentication gates
`GET /api/asclepius/buyer/media/{did}` and
`POST /api/asclepius/buyer/media/{did}/files/{fid}/download`.
Downloads reference the exact stored object version and support Range requests.
Links expire after five minutes; existing links may remain usable until expiry
after an account or release revocation. Audit records say `download_link_issued`,
not delivered. Use storage access telemetry to reconcile actual receipt.

## Release checklist and recovery

1. Run PostgreSQL integration and browser CI. Verify migrations preserve IDs.
2. In staging, perform create/sign/PUT/list/complete/HEAD/versioned GET/abort with
   actual credentials; validate CORS from the real portal and encryption/version.
3. Upload a synthetic file, interrupt the browser and worker, resume, and compare
   independent full-file hashes after download. Exercise expired links and
   revocation. Check worker errors and verification backlog.
4. Test 100 GiB and 1 TiB actual transfers before advertising those capacities;
   run multi-day batches and 20-provider load. Metadata admission is not throughput
   evidence. Test source copies and KMS permissions separately.
5. Back up PostgreSQL with PITR, preserve bucket versions and KMS keys, and restore
   all three together into staging. Reconcile each media row's key/version/size/hash
   against storage. A missing version is an incident, never permission to erase a row.
6. Complete a provider rehearsal before enabling production acceptance. Confirm
   the byte budget for the collection; hours alone do not determine capacity.

After a worker crash, the 120-second lease expires and another worker resumes.
Uncertain completion checks HEAD before completing again. Full-file hashing starts
again after a crash; it does not re-upload completed bytes. If metadata is lost,
restore it before resuming workers or running orphan cleanup. Never reconstruct
commercial permissions merely from object names. After ten failed attempts the transfer becomes `attention_required`, retaining
the original/reservation. Correct the credential, source or KMS configuration, then
an admin can POST `/api/asclepius/admin/media/{org}/files/{fid}/retry`. Retries are
audited and resume the prior durable stage. If the database itself is unavailable,
restore database access first; failed attempts cannot be persisted without it.

Original disposition/deletion is not automated in this release. Any future
disposition must be explicit, audited and version-specific. Keep media disabled
if the organization cannot operate this retention and review workflow.
