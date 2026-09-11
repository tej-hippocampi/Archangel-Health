# Physician acceptance welcome — 11 September 2026

## Design and invariants

An active physician accepted to the platform receives the approved personal
welcome, whether approved for labeling or reviewing. Manual approval, automatic
verification and physician restoration share the acceptance queue. The message
uses the first name, approved logo, highlighted “That’s you.”, 120 × 44 Sign in
button, conversation link and Tej & Aryaa signature artwork. The physician's
first-run page loses only the gray OUR MISSION label.

- `backend/physician_welcome_email.py:33` renders the email.
- `backend/notifications.py:285` defines recipient eligibility.
- `backend/notifications.py:316` queues the common welcome.
- `backend/main.py:782` checks current eligibility and claim ownership before sending.
- `backend/routers/asclepius_verify.py:738` preserves approval when queue confirmation fails.
- `frontend/asclepius/first_run.js:361` contains the one-line welcome-page edit.

The source of truth remains the realm's Asclepius users table: active account,
physician role (`evaluator` or `qa_reviewer`) and approved verification. Tier is
not an email gate. Pending, rejected, inactive and nonphysician accounts do not
receive this welcome. Delivery also checks the recipient's user ID against the
existing idempotency key, preventing an email address reassignment from routing
another account's queued approval to it.

The existing unique `physician_approved` key is retained. Repeated approvals do
not append another welcome. Rejection cancels pending approval mail. Reapproval
can revive a never-sent row created by this template, preserving an in-flight
sender's lease. Completion cannot overwrite a replacement lease. Legacy void
rows remain untouched because the old inline sender voided a separate notice
after success without recording sent_at on that notice. The admin receives an
explicit uncertainty message for that historical state, not a delivery claim.
Old pending notices render the current approved design at delivery.

Existing passwords remain unchanged. Legacy accounts with an unset or temporary
password receive directions to the existing Forgot your password flow. No new
temporary secret is minted or persisted in the outbox during acceptance.

## Data scope and intentional writes

No schema migration, backfill or production data operation is included.

| Store / integration | Scope |
| --- | --- |
| Realm-scoped Asclepius SQLite | Existing users/verification operations; existing admin_notify_outbox queue and delivery state |
| Team and community SQLite | No new writes or schema changes; existing approval community behavior is retained |
| Sandbox | Same code through the existing realm-scoped store and delivery isolation |
| Blob roots, upload sessions, object buckets, source documents | No changes |
| Encryption keys | No new dependency; existing outbox field decryption is retained |
| Email transport | Existing configured transport and importance headers; shared queue replaces competing inline acceptance sends |
| Public assets | Two versioned PNGs in backend/assets, served by the existing anonymous /email-assets mount |

Existing user IDs, emails, password hashes and relationships are retained.
Existing verification/tier decisions use the existing store API. Intentional
outbox mutations are status, subject/body on safe revival, send_after,
claimed_at, send_attempts, last_error and sent_at. No rows are deleted, replaced
or truncated. A successful provider response is recorded as sent; this means
provider acceptance, not confirmed inbox delivery.

## Tests and preservation evidence

- **171 targeted tests passed**, including 47 acceptance tests covering all
  three acceptance paths, both tiers, chosen/unset/temporary password states,
  qa_reviewer role, name escaping, anonymous asset loading, password reset and
  actual sign-in, rejection/reapproval, concurrent drains, retry, stale claims,
  legacy rows and outbox lookup/revival failures.
- **370 related regression tests passed** across founder/task notifications,
  health-system onboarding and recovery, admin approval/password setup,
  password reset/signup, verification-to-review, tiers, promotion, welcome
  packages and CI sharding.
- Actual backend email rendered in Chromium at 700, 390 and 320 pixels: no
  overflow; both assets load; the button measures 120 × 44; long names fit;
  essential actions remain available with images blocked. Desktop and mobile
  screenshots were visually inspected. Real Outlook/Gmail/Apple Mail inbox
  rendering has not been exercised.
- Route baseline unchanged: **671 routes**. Dangling import scan: **708 files,
  none dangling**. New tests are automatically included by the total CI shard
  assignment.
- Local preservation drill froze one existing user and one existing outbox row
  across **71 tables**, then exercised new labeler/reviewer approval, rejection,
  revival and failed-send state. After: **8 rows**, all original identities and
  field hashes unchanged, zero inventory problems. This is a small synthetic
  fixture, not evidence about production clinical records or backups.
- The initial fixture revealed existing boot normalization of four empty user
  fields (organization, tier, tier_assigned_at, tier_assigned_by). The initializer
  is unchanged in this patch. The successful drill froze its baseline after
  that existing normalization, then compared the SAME dataset before/after.
- SQLite backup API snapshot at **2026-09-11 05:38:48 UTC**, with no fixture
  workers running; restored to a separate database and compared all baseline
  identities, fields and table counts successfully. Local artifacts are in
  `output/welcome-email-design/` in the parent workspace: the
  `inventory-normalized-{before,after,restored}.json` files,
  `preservation-normalized-{fixture,backup,restored}.db`,
  `preservation-report.json`, `check_preservation.py`, render checks and test logs.

Outbox insertion failure, metadata failure, provider rejection, concurrent
requests and stale ownership are tested. No new file ingestion or clinical
parsing path exists in this change. Disk-full/process-death and real provider
bounces were not injected. Approval and enqueue retain the existing separate
commit boundary: a crash between them requires an admin approval retry. A crash
after provider acceptance but before its database receipt retains the existing
at-least-once retry limitation; this is not an exactly-once delivery claim.

## Independent review

Fresh-context reviewer `final_acceptance_review` reproduced and verified fixes
for in-flight reapproval duplication, canceled-row retry, historical void rows
and outbox failures after approval. Final report:

> All audit findings are resolved; no remaining actionable findings in the
> reviewed changes. Independently confirmed 47 acceptance tests passed in 5.37
> seconds, covering concurrent reapproval, canceled-message recovery, stale
> claims, legacy rows, outbox failures, recipient eligibility, and password-reset
> sign-in. git diff --check also passed. The audit used isolated databases and
> mocked delivery. Production provider behavior, real inbox rendering, and live
> data-preservation gates remain outside this audit.

## Rollback and release decision

The change is ready for code review after local checks. Production release
remains subject to CI and the live operational gates in
`docs/data-safety/POLICY.md`: mounted-store/capacity checks, recoverable backups
and keys, restore verification, and authorized provider acceptance/failure
checks. None of these live checks is claimed by the fixture drill. No real
test emails or bulk welcome backfill were sent.

Rollback is a code revert without a data restore or schema downgrade. Retain the
two versioned public artwork assets so previously sent messages keep rendering.
Pause notification workers before changing sender versions; inspect pending,
void and sent acceptance records before resuming. Do not blindly restore a
pre-release database over new approval activity or run old/new senders together.

## Out of scope / do not touch

Clinical originals, uploads, contracts/signatures, annotations, submissions,
tasks, records, assignments, exports, payments, source provenance, tier policy,
authentication policy, other email designs and production credentials remain
outside this change. No promotion or health-system welcome is routed through
the physician acceptance template. No retroactive mass mailing is introduced.
