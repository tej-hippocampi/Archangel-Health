# Manual onboarding reminders — review and validation

Status: implemented locally on `feat/manual-onboarding-reminders`, based on
`origin/main` at `fced9c5`. Not deployed. No real email or test email was sent.
The founder approved both templates in this task on 14 September 2026 UTC,
with the clarification that every email must use the person's actual signup
name. Both send paths reread the saved name immediately before rendering;
admin previews now also show an actual recipient instead of `[Name]`.
Missing names use `Hello,` rather than a placeholder or a guessed name.
Sending defaults off through
`ASCLEPIUS_MANUAL_REMINDERS_ENABLED=0`; preview works while sending is disabled.

## Behavior

The two Pending for Review tables have a Preview reminders action; eligible
individual rows also have Remind. An admin sees the email subject, HTML preview,
deduplicated recipient names and emails, and checkboxes before Send. Existing
wizard Resend link behavior is retained. Manual nudges use the configured
SendGrid/SMTP transport and set Reply-To to `tejpatel@berkeley.edu`.

Examination recipients must be active pending physician accounts with credentials
evidence, a password, and an unstarted/in-progress examination. Filed examination
records suppress a reminder even if the tutorial flag is stale. Prior automated
reminders and the 36-hour timer do not restrict manual nudges, and manual sends
never update automated reminder history or stamps.

Wizard recipients are the existing unfinished signup cohort, excluding addresses
already provisioned. Repeated email addresses use the signup with the most
progress, then the newest activity and a stable tie break. Personal links point
to the existing signup, not a new application or a fixed CV screen. Existing live
director links are preserved; expired links are renewed only on Send. Member
tokens are renewed on Send as in the existing resend path; this replaces the old
member link, and the preview tells the admin that consequence. Both link types
retain sandbox realm context.

Wizard startup reads current server state on click. Saved physician steps 1–5
were checked in a fresh mobile browser. The saved Review form now opens
attestations (step 5), rather than going back to Review (step 4). CV-only uploads
still open Review; identity and verification gates remain in order. Missing or
unrecognized early progress defaults to identity. Unsaved form edits cannot be
reconstructed. Invited-member startup also uses saved credentials, attestations
and mailbox verification instead of always resetting to its first screen.

## Delivery boundaries

Preview stores an immutable recipient selection and template version, never a
bearer link. Preview IDs expire after 30 minutes and are bound to the admin and
realm. Sending claims one ID transactionally, rechecks current eligibility and
email identity, resolves the link, then rechecks before provider handoff.
Concurrent clicks/retries of the same ID cannot submit a second email. Other
manual attempts to that mailbox are held while a send is unresolved, or within
10 minutes of a provider acceptance. Confirmed failure requires a new preview;
unknown outcomes require provider reconciliation and are never auto-retried.
Closing the dialog stops submitting additional recipients after the current
request. The history endpoint reports attempts, provider IDs and outcomes.

An acceptance means the email service accepted the request, not inbox delivery.
A completion after provider handoff cannot retract an email. Automated reminders
remain independent and may occur near a manual send.

## Data preservation record

- Stores: Asclepius live/sandbox gain only `manual_onboarding_reminders` and its
  lookup index. Existing users, exams, annotations, contracts and source records
  are read, not modified by this feature. Team live/sandbox are read for selection;
  sending may change existing invite token/hash/expiry fields through the same
  token APIs as Resend. No new cleanup, retention or deletion path.
- External integration: existing email provider, with recipient name, email,
  application reminder and first-party link. No new blob/object integration or
  encryption key dependency. Community stores and clinical blob stores are not
  mutated. Existing email transport may capture sandbox messages in its outbox.
- Frozen fixtures: previous-main schema with one pending physician, one unfinished
  signup, saved credential fields and a fixture original document in each realm.
  Existing startup organization normalization was run before freezing the
  baseline. Migration plus two previews added two reminder rows per Asclepius
  store. All existing table IDs, field hashes and original-file hashes matched.
- SQLite backup API copied each frozen database, including committed WAL state.
  Each copy restored into a separate database and matched the baseline inventory.
  No production database contents or backup was accessed; fixture validation is not proof
  of production backup, restore or email delivery.
- Local evidence: `output/manual-onboarding-reminders/preservation-final/` in the
  parent project contains before/after JSON inventories, SQLite backup/restore
  copies and results. `preservation.py` records the reproducible procedure.
- Rollback: disable `ASCLEPIUS_MANUAL_REMINDERS_ENABLED` and retain the additive
  delivery table for reconciliation; deploying previous application code does
  not require removing data. Restore only into an isolated environment first.

## Validation

- Related reminder, signup and onboarding suite: 192 passed. After adding
  sandbox/configuration and saved-name regression cases, the complete
  manual-reminder file passed all 27 tests. Both cohorts use each recipient's
  current full signup name in HTML and plain text, including a changed name
  after preview; missing names use the neutral greeting.
- Repository-required affected suite against `origin/main`: 2,878 passed,
  1 skipped in 402 seconds. The new test file was staged before selection.
- Five fresh-browser tests using real local session APIs: saved wizard steps
  1, 2, 3, 4 and 5 all opened correctly from the actual personal link.
- Built landing successfully. Browser checks of the shipped admin component
  with an in-memory API passed: both cohort buttons, filed-exam row exclusion,
  no send on preview, approval switch, recipient selection and wizard preview.
- Email previews at 650, 390 and 320 pixels: no horizontal overflow, readable
  text, one primary CTA and support mailto link, CTA height at least 44 pixels.
  Visually inspected desktop and mobile output. This is browser rendering;
  Outlook/Gmail inbox rendering and actual provider delivery remain untested.
- Route snapshot updated: only the three intentional manual-reminder routes
  added; 675 routes match. Data SQL guard and `git diff --check` passed.
- Independent auditor: `/root/audit_manual_reminders`; found sandbox director
  links losing their realm query, confirmed the fix for reused and renewed
  links, and reported no remaining actionable code findings. Auditor ran 68
  related tests and verified the new test file is included exactly once in CI
  sharding. Parent subsequently added permanent sandbox regression coverage.
  Incremental audit of actual-name preview and sends found no actionable
  findings; all four personalization regression cases passed.

## Production readiness inspection

Read-only Railway inspection on 14 September 2026 UTC confirmed the production
service is online at `fced9c5`, with its persistent volume mounted at `/data`.
Reported usage was about 252 MB of 5 GB. The existing email transport is
configured; only configuration names were inspected, never secret values.
The dashboard's Backups page says backups/PITR require the Pro plan; its volume
usage alert control is disabled for the same reason. The available interfaces
did not establish an off-volume backup, key recovery or a successful isolated
restore. This does not prove no separate backup exists. No infrastructure
settings, database contents or deployment were changed by this inspection.

Release remains held on the production verification requirements of
`docs/data-safety/POLICY.md`; template approval does not provide backup/restore
evidence. Keep the PR unmerged and the switch off until this gate is satisfied
or the product owner explicitly authorizes a scoped release exception.

## Rollout after template review

Deploy backend and rebuilt landing together so saved-step routing matches the
email copy. Verify live mounted-store backups/restore readiness and correct
`LANDING_URL`, `ASCLEPIUS_PORTAL_URL`, and email transport settings. Keep sending
off until deployment verification is complete, then enable the manual switch
under the founder's recorded template approval,
open a fresh recipient preview and explicitly send the reviewed selection.
Reconcile any unknown result by reminder/provider ID; do not treat a timeout as
permission to resend. Production recipient counts are not inferred from the
screenshots and will be determined from the live preview.
