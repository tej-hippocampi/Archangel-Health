# Physician join-link reliability — 13 September 2026

## Root cause and intended behavior

The deployed backend at `f2ea264c2b818711752ad126e6aad18b6d772b07` returned HTTP 200 with a randomly generated, unpersisted onboarding token whenever the legacy `company_website` hidden field was populated. The following wizard session request necessarily returned `Invalid or expired onboarding link.`

Read-only Railway logs from deployment `0bc101c6-f8d0-4376-bbaa-9059141e30b6` confirm this exact branch for a masked `aiimsjodhpur.edu.in` address at 2026-09-12 02:58 and 03:00 UTC. The log search returned 16 such events across several domains, including US health systems. The logs prove that the hidden field was populated; browser autofill is the likely source but the logs do not identify which browser feature populated it. Email nationality/domain validation was not the failing branch.

Every successful self-serve response now refers to an invite created by the existing store method. Older cached clients may still send the hidden field, but it no longer rejects a signup. Both current physician signup forms omit the field. Contact-form protection remains. Existing request/email limits, expiry, returning-account handling, referral attribution, and inbox-verification gates are unchanged. Invalid-link screens offer same-token retry and a physician-specific route to `/join`; member invitations do not offer unrelated physician signup.

## Data scope and invariants

- No schema, migration, storage function, existing-row repair, accepted form, clinical record, agreement, signature, upload, asset, encryption-key, or payment logic changes.
- Self-serve requests use the existing realm-scoped team store: a new `health_systems` invite and optional identity/flavor/provenance fields. The existing Asclepius account lookup and optional referral-attribution paths remain unchanged. All existing row identities, field values, and relationships must survive.
- No live or sandbox production data was written during investigation. All test requests and email calls use isolated local stores/stub transport. No emails were sent to doctors.
- No new object-store/blob/key dependency. No backfill or token rotation. Old fake tokens cannot be repaired because they were never stored; users must restart at `/join` after deployment. Genuine expired/completed-link handling is unchanged.

## Validation and preservation evidence

- API regression matrix includes `aiimsjodhpur.edu.in`, `dpu.edu.in`, `atriushealth.org`, and Gmail with empty, URL, and saved-profile hidden-field values. Each success resolves to a persisted session; password creation remains blocked before inbox verification.
- Actual rate limiting enabled: sixth request from one IP and 61st request across distinct IPs return 429 without creating rows. Three recent invites per email (24 hours) remains enforced for autofilled requests.
- Injected storage failure raises instead of returning a successful link. The independent auditor additionally verified HTTP 500 without a URL/new row.
- A nonempty frozen local team database is inventoried using `scripts.data_inventory.snapshot` before and after the new signup. `compare` finds no lost rows or changed existing fields. An independent SQLite backup is made using SQLite's backup API, reopened, inventoried, and used to resolve the original invite. This proves preservation/restore for that synthetic fixture only.
- Built-site Chromium regression on 1440px and 390px starts at an old fake link, follows recovery to `/join`, enters the international email, submits, resolves the stored token, sets a password, reaches email verification, and reloads correctly. Mobile case injects the populated legacy field on the request to cover cached clients. All browser requests are intercepted and routed locally.
- Frontend component tests exercise invalid-link recovery, same-token retry after a 503, and member-invitation isolation. Existing international credentialing/application tests are included in the targeted backend run.
- Destructive-SQL gate and merge-readiness check pass. The browser checks run in the existing visual CI job; backend/component regressions run in their existing jobs.

Final local results: 373 affected backend/browser tests passed; 62 frontend component tests passed; production landing build passed. Independent auditor: no remaining code findings, 672 routes unchanged, 708 files scanned with no dangling imports; request-throttling and storage-failure probes passed. Auditor confirmed the final preservation tests and this scoped record.

## Operational release status and rollback

Code is ready for review after the checks reported in the pull request. Production deployment has not been performed by this task. The live backend's mounted Railway volume was confirmed read-only. Current off-volume backup/restore, key-recovery and provider acceptance/bounce checks required by `docs/data-safety/POLICY.md` have not been established by these local tests; do not call this a completed production-preservation audit or a deployed fix. Existing audit documents also leave those operational checks unverified.

Before a normal production release, complete the applicable live policy gates or obtain an explicit owner exception for this scoped hotfix. Deployment must update both backend and Vercel frontend. No migration is needed. A code rollback must preserve all rows created after deployment; reverting the backend would reintroduce the fake-link bug for older clients. Never restore an old database over new physician applications as a code rollback.

Disk-full, process-death and external-provider delivery drills are not newly implemented by this change: the underlying storage and email implementations are unchanged. The injected store failure and local SQLite restore are the available bounded failure evidence, not substitutes for the broader operational gates.
