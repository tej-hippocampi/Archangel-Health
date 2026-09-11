# Examination reminders

An active, pending physician who submitted credentials and has not submitted the examination receives one reminder after 36 hours. A saved examination draft qualifies. The existing verification-agent loop checks every 15 minutes and sends at most `ASCLEPIUS_NUDGE_BATCH` reminders per pass (default 50). This replaces the former 48-hour account-age examination reminder.

The approved email uses the existing light background, the headline **This is the last step!**, and one **Sign in & complete case** button. Sign-in returns to `/asclepius#examination`, where the applicant can start or resume. The link does not start an attempt automatically. Submitted or decided applications retain their current account state.

## Configure and inspect

Use the application host's usual environment and database paths. `ASCLEPIUS_PORTAL_URL` (or `BASE_URL`) must be the first-party HTTPS portal origin or its `/asclepius` page. The normal SendGrid/SMTP configuration is reused. Set `ASCLEPIUS_EXAM_REMINDER_ENABLED=0` to pause the feature. The old `ASCLEPIUS_EXAM_NUDGE_AFTER_HOURS` setting is superseded by this fixed 36-hour rule.

The operator tool is available only to someone with application-host access; no unauthenticated HTTP send endpoint is exposed. Run from the repository root:

```sh
python backend/scripts/exam_reminders.py preview
python backend/scripts/exam_reminders.py history --limit 50
python backend/scripts/exam_reminders.py render --out /tmp/examination-reminder.html
```

`preview` lists eligibility, due time, and exclusion reasons. It does not claim or send mail, change send history, or backfill user timestamps. Store initialization may perform the normal additive schema migration. `render` needs no database and writes the exact production template using an illustrative recipient. Neither command is a send.

The two explicit send actions:

```sh
python backend/scripts/exam_reminders.py test --to your-test-mailbox@example.org
python backend/scripts/exam_reminders.py run --limit 10
```

`test` prefixes the subject with `[TEST]` and never consumes an applicant reminder. `run` uses the same due-only claims and safeguards as the automatic sweep. It does not force-send to ineligible users. Add `--realm sandbox` for the isolated sandbox store and captured email outbox; sandbox messages do not leave the host. Live `EMAIL_DEV_MODE` suppresses automatic applicant sends without marking them reminded.

Before rollout, inspect `preview`, render the email, and use an explicit test mailbox to validate the configured sender and sign-in link. The existing overdue cohort drains in bounded batches. Previously reminded applicants remain excluded.

## Clock and eligibility

`users.application_completed_at` records successful credential onboarding once; later profile edits do not restart it. Historical applicants can use the recorded completion of their credential-bearing `asclepius_people` application. A preview with `completion_time_unknown` means no trustworthy completion timestamp exists. Do not backfill from account creation: an invited account may be much older than the credential submission. The first send claim persists only the proven historical completion timestamp.

Eligible users must have a usable sign-in password, credential evidence, a valid email, an active pending evaluator account, and an examination in `not_started` or `in_progress`. The system excludes submitted examinations, decided/inactive/non-physician accounts, unknown examination states, previously reminded accounts, and recorded delivery suppressions. All queries and claims use the current realm's store. Eligibility is checked again under the claim lock and immediately before provider submission.

## Delivery outcomes

`exam_reminder_deliveries` stores one row per account in each realm. A transactional claim serializes competing workers. `accepted` means the transport accepted the message, not that it reached the inbox. SendGrid's message ID is retained when returned, and the claim ID is attached as `exam_reminder_id` in SendGrid or `X-Archangel-Reminder-ID` in SMTP for provider reconciliation.

Confirmed temporary refusals (such as SendGrid 429 or temporary SMTP recipient refusal) are retried at most three attempts with 15/30-minute backoff. Each retry rechecks current eligibility. Permanent refusals stop. SendGrid's configured bounce/complaint suppression continues to apply; the tool does not override provider suppression lists or label provider acceptance as inbox delivery.

Timeouts, ambiguous exceptions, or a worker lost before recording its result become `unknown`. They are never retried automatically. Claims still `sending` after 15 minutes become `unknown` on the next enabled sweep. Reconcile them against the provider using the recorded claim ID and recipient before any operator intervention. There is deliberately no force-resend button that could bypass this boundary.

Completion after provider handoff cannot retract an email; the link still shows the up-to-date application state. No exactly-once inbox delivery guarantee is claimed.

## Validation

`tests/test_exam_reminder.py` covers the precise 36-hour boundary, historical evidence, drafts, exclusions, concurrent claims, late submission, bounded batches, retry/unknown outcomes, approved copy, escaping, and transport metadata/plain text. Existing applicant reminder and onboarding tests cover compatibility. Browser rendering verifies layout, but actual Gmail/Apple Mail/Outlook behavior must also be checked with the test-send action in the deployment environment.
