# Welcome-package reliability — 22 September 2026

Accepted physicians could move past checklist steps whose save had failed, and
late demo responses could repaint onboarding after navigation. Downloading a
signed contributor agreement rebuilt it with the current profile name instead
of serving the original signed document. The signature timestamp was also
generated separately for the PDF and its database record.

## Scope and invariants

- The portal waits for checklist saves, offers retry on failure, bounds stalled
  requests, and invalidates callbacks after navigation or session changes.
  Existing `users.first_run_json` actions and server authorization remain the
  source of truth. Payment/banking logic is outside this change.
- New `physician_agreements` rows use the same signature timestamp as their PDF.
  No schema migration or existing-row update is introduced. The store method's
  new timestamp argument is optional for compatibility with existing callers.
- Agreement downloads authenticate ownership, serve the existing blob with its
  SHA-256 verified, and only rebuild a missing blob if the rebuilt bytes match
  the immutable recorded PDF checksum. A reconstruction with different content
  returns a recoverable error instead of a changed signed document.
- No tasks, submissions, assignments, earnings, records, exports, original
  uploads, team records, or community records are transformed. No production
  database or configuration is modified by this review. Both realms use the
  existing realm-scoped store and asset helpers.

## Preservation and restore evidence

`test_download_preserves_signed_bytes_after_profile_name_changes` signs a real
agreement into an isolated SQLite store and asset directory. It takes a
`scripts.data_inventory.snapshot` of all tables and file hashes, backs up the
database using SQLite's backup API, copies the blob directory into a separate
restore location, and verifies the restored inventory. Downloading after a
profile-name correction returns the original PDF bytes and leaves every
inventoried field and file unchanged.

The fixture is synthetic evidence for this code path, not a production backup
or restore claim. Production rollout must retain the existing database and
asset backups; reverting these code changes requires no data rollback. Existing
signature rows and original PDFs must remain append-only and retained.

## Failure and retry coverage

- Failed checklist save, retry, double-action prevention, and reload persistence.
- Pending saves during Tasks/Community navigation; late responses after a new
  account or screen; stalled demo metadata and departure during a media probe.
- Existing practice-skip/resume, agreement gating, review return, media ticket,
  welcome outbox retry/deduplication, and no-work-on-approval regressions.
- Signed-copy retrieval after a name change, missing-blob recovery, refusal of
  a mismatching reconstruction, exact signature time, and disk-write failure
  leaving no recorded signature. No new write/commit boundary is introduced.
- Desktop and mobile flows opening the actual community and labeling manual.

## Independent review and launch notes

An independent auditor reviewed the nonpayment diff and found two navigation
seams, both corrected: no-op rail destinations must not invalidate a visible
walkthrough, and direct tutorial replay must invalidate pending onboarding
callbacks. The auditor then reported no remaining blocking findings, with 128
independent tests passing, 686 routes unchanged, and no dangling imports across
741 files. The final PR records the broader validation results.

The reviewed production commit is `08a1e185ba2ea34f8f72afcf93aa9c5edb967edc`.
Its sign-in page, welcome logo/signature assets, and founder booking link
returned HTTP 200. Railway lists no `ASCLEPIUS_AGREEMENT_GATE` variable; the
code defaults to disabled, and the current agreement is labeled interim.
Whether to require signatures and which final text to use remains a product
decision. This change does not enable the gate or change agreement wording.

Railway reports SendGrid configuration, but OAuth hides variable values and
the read-only production queue/video inspection could not run because local
Railway SSH credentials are absent. Actual inbox delivery and the production
demo blob are therefore not certified by these local checks. No real test
email or community message was sent.
