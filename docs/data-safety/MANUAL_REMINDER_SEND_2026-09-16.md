# Manual reminder sending and personalized previews — 2026-09-16

## Change and operational action

The approved reminder templates were still behind the production approval flag.
The product owner explicitly approved both templates on 16 September 2026.
`ASCLEPIUS_MANUAL_REMINDERS_ENABLED=1` was applied to the Archangel-Health service
in the `easygoing-victory` production environment. Railway deployment
`bb7ca42a-404b-4909-8dc5-ac71d35d698a` completed successfully. The configuration
change alone sends no messages. No real reminders were sent during verification.

The code adds a rendered preview per recipient ID, a recipient preview selector,
selected-row highlighting, and an in-flight send guard. Approved email copy and
templates, eligibility checks, identity matching, link generation, provider
integration, and delivery ledger behavior are unchanged.

## Data scope and invariants

- Existing realm-scoped Asclepius and team SQLite stores supply the candidates.
  Preview adds rows to the existing `manual_onboarding_reminders` ledger.
- The new response field only renders HTML/text from the corresponding saved
  name. It does not store new personal data or change any source record.
- Recipient email, target ID, and member/director identity stay paired. Send
  rechecks current eligibility and renders with that same person's current name.
  Missing names use the template's neutral greeting.
- No schema migration, deletion, contract modification, blob rewrite, upload,
  encryption key change, or community-store change is introduced.
- Production still uses the existing `/data` 5 GB mounted volume. This fact is
  not evidence of an off-volume backup or a verified production restore.

## Frozen-fixture preservation and restore

The isolated validation script is retained at
`output/reminder-send-fix/preservation.py` in the parent working directory.
Its `preservation/` folder contains before/after inventories, SQLite backup API
backups, separately restored databases, and a fixture original document.

| Realm/store | Before rows | After rows | Existing IDs/fields/blobs | Backup restore |
| --- | ---: | ---: | --- | --- |
| live Asclepius fixture | 1 | 3 | unchanged | exact match |
| live team fixture | 5 | 5 | unchanged | exact match |
| sandbox Asclepius fixture | 1 | 3 | unchanged | exact match |
| sandbox team fixture | 5 | 5 | unchanged | exact match |

Only two new preview-ledger records per Asclepius fixture were added. There were
no missing IDs, changed existing values, missing files, or changed file hashes.
These nonempty synthetic fixtures establish preservation for this change; they
do not establish the state of production backups. No production database was
copied or restored, and no data recovery was performed over production.

## Verification and independent review

- 92 reminder/signup/examination tests passed, including different names for
  each recipient, member versus director, duplicate mailbox name/link pairing,
  stale previews, changed eligibility, ownership, concurrent claims, retry after
  response loss, and unknown provider acceptance. Provider calls are captured.
- Four shipped-UI/real-API browser checks passed: both reminder cohorts at
  desktop and phone widths. Preview sends nothing; changing the selector shows
  the correct greeting; only checked recipients are submitted; completed sends
  disable the button. No real provider receives test messages.
- Data SQL guard, unchanged 675-route baseline, dangling-import scan, JavaScript
  syntax check, and whitespace check passed.
- Fresh-context independent auditor `audit_reminder_send` found no blockers and
  independently reran the 92 tests, syntax and whitespace checks successfully.
- Live browser inspection was blocked by the browser's admin-policy check. It
  was not bypassed. Railway's successful deployment confirms the flag rollout;
  actual provider acceptance/delivery must be observed when the admin sends.

## Rollback

Set `ASCLEPIUS_MANUAL_REMINDERS_ENABLED=0` to return to preview-only mode. Revert
the UI/response additions if needed; older clients still receive the original
`template` field. Preserve all reminder ledger rows and provider outcomes.
Never blindly retry an unknown provider outcome; consult reminder history.
