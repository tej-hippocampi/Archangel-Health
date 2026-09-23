# Security reliability corrections — 22 September 2026

Scope: reviewed from `origin/main` at `a688e94ae9e81abaf6c35c919dcc08230c8ad353`. This record covers export publication, ledger approval/void record gates, and durable submission/upload generation recovery. Other audit changes have separate evidence. No production writes, real payments, or external messages were performed.

## Data and invariants

The realm-selected Asclepius SQLite database and local export tree are affected. Export catalog, license membership, records, submissions, earnings and audit events now commit together where one operation requires them to agree. Export files and directory entries are flushed before the catalog makes a bundle downloadable. Exclusivity and both submission/record approval are rechecked under the SQLite writer reservation. A failed new bundle stays as an unpublished recovery artifact; accepted originals are never removed.

Two additive tables, `background_work` and `auto_generation_cases`, retain submission/upload work, leases, progress and stable child generation identities. Accepted submission payloads, existing record IDs/payloads, original ingest charts, immutable financial amounts and accepted artifacts remain unchanged. A package and its completion event commit together. Legacy partial packages retain their existing records and add only missing record types. Submission and record pipeline phases commit together, preserving terminal human decisions. Model-failure retries reuse the original evidence row.

Upload claims and parent jobs commit together. Parents resume the same existing durable generation jobs and deterministic task IDs. A restart does not reinterpret changed upload mode or replace a partially generated walk. Storage faults leave work retryable. A legacy interrupted upload with existing tasks but no durable plan is reported for review rather than regenerated automatically. No new UI is introduced.

Production workers use the existing realm-bound store. Independent store instances are only constructed by the synthetic test harness. Team/community databases, signature evidence, upload blobs, encryption keys and external provider configuration are outside this change; existing best-effort notification hooks remain in their current realm.

## Frozen inventories and restore

Evidence is retained under the project workspace `output/security-audit/agent-reliability/` outside the checkout. These are synthetic local checks, not evidence of production backups.

Before source edits, four export/payment fixtures held 15, 11, 4 and 5 rows across 80 tables each. `*-before.json` inventories include the complete export file tree. SQLite `Connection.backup` retained committed WAL; originals and export files were restored separately and compared field-by-field and by file hash. Backups remain in `synthetic-backup/`; exact fixture/restore locations are in `preservation-results.json`.

After recovery on those same frozen fixtures, CLI inventory diffs passed with no missing IDs, payload changes or lost/altered original files. The accepted concurrent-export and failed-license fixture rows stayed unchanged. Explicit permitted transformations for failed-metadata recovery were `records.export_id` and `submissions.updated_at`; historical approval repair permitted `records.status`, `submissions.qa_reason`, and `submissions.updated_at`. Historic conflicting licenses were retained for review.

A separate frozen job fixture had 5 existing rows across 80 tables before the recovery edits. `jobs-before.json`, `jobs-after.json`, and `jobs-preservation-results.json` record the same-fixture check. Backup `synthetic-backup/jobs.db` was made with SQLite's backup API and restored to `/var/folders/yq/llwsy6gj12l482bt66x88htw0000gn/T/archangel-jobs-restore-fgxa51st/restored.db`; the restore CLI diff passed with no allowances. Recovery retained all 5 original rows and added 4 derived rows. Its only allowed field changes were `tasks.status`, `submissions.status`, `submissions.qa_reason`, `submissions.updated_at`, `submissions.progress_json`, `ingest_uploads.auto_generate_report_json`, and `ingest_uploads.updated_at`. Both adopted jobs completed; the deliberately incomplete submission visibly reached QA. No source forms or chart fields changed. This job fixture had no blob references; export blob preservation is covered by the earlier nonempty fixtures.

## Verification and review

`test_export_transactions.py` covers concurrent exclusive exports, failed file flushes, failed catalog/license/event writes, packaging-time rejection (including submission-only rejection), manual/automatic/reviewer/void gate failures, historic repair, and real subprocess termination before/after commit. All 17 cases pass.

`test_background_job_recovery.py` covers durable 202 receipts before memory scheduling, concurrent claims, expired-worker fencing, `os._exit` during/after package commits and after terminal commit, legacy partial package retention, transactional phase-write failure, concurrent human rejection, atomic upload claims, dropped scheduling, frozen mode, stable child identity after interruption, legacy partial-generation review, model-failure idempotency, and storage faults after a real generated task insert or child checkpoint. All 16 cases pass; external model calls use synthetic fixtures.

Fresh reviewer `/root/data_security_auditor` confirmed export/ledger fixes, identified the submission-only rejection race and child storage-retry gap, and confirmed their corrections. Reviewer independently passed the transaction tests and recovery/real-case/auto-generation tests, including both final child-fault regressions. Independent review notes are in `output/security-audit/agent-data/independent-recovery-review.md`.

The destructive-SQL guard and `git diff --check` passed. Both new test files are discovered exactly once by four-way CI sharding. Existing export/payment, pipeline/router, agreement, packaging, validation, baseline, generation and recovery suites were exercised; the overall audit owns full-suite and deployment checks.

## Release and rollback

Code rollback requires no schema reversal: retain the additive job tables, original source records, generated tasks, licensed exports and financial evidence. Pause workers before any separately authorized restore and use complete database backups with the corresponding file tree; never replace an active SQLite database with a `.db` copy that omits WAL. Restoring an older database must reconcile any artifacts and provider-side actions accepted after that backup.

Production release remains conditional on the project's operational requirements: live persistent storage and capacity, off-volume backup and isolated restore, recoverable keys, alerting and deployment checks. Synthetic recovery tests cannot establish those facts. No production readiness claim follows from this record alone.

Final recovery validation: 295 distinct tests across background recovery, submission router, baseline capture, agreement, packaging, validation, generation, export approval, real-case jobs/model recovery, auto-generation and transaction suites passed (141 in the first successful group and 154 in the final group). An AST writer-location assertion was updated to reflect the same pipeline transition moving into its atomic store helper; the companion PRD now names that helper. No ledger prerequisite or UI path changed. Final destructive-SQL and whitespace checks passed.

Final compatibility follow-up: the combined suite found an existing export path for individually ready records whose submission metadata still held a transient phase. Publication now accepts that legacy phase only if it matches the packaging snapshot; QA holds/rejections and changed transient phases remain forbidden under the same transaction. Six additional unchanged/changed-phase regressions bring transaction coverage to 23. All 69 transaction/admin-launch tests pass without changing the original failing test. Fresh reviewer confirmed no reopened rejection race and independently passed all 23 transaction tests in a 107-test run.

Mock authentication follow-up: disabling the existing mock now rejects its password login, bearer sessions and onboarding media tickets at authentication gates, without altering users, annotations or other stored evidence. Defaults remain enabled. Seven new regression cases cover disabled aliases, default/explicit enabling and ordinary users; each disabled case verifies an unchanged SQLite inventory. The targeted26 tests and independent39-test run passed. This source fix does not change production configuration.

## Consent, intake, and notification authorization follow-up

The later route audit extends this change to the realm-selected team SQLite store
and its patient hot cache. Consent, form edits, final submission, form/history
reads, notification listing and mark-read now check the bound patient or scoped
staff before returning clinical content or mutating state. Staff-only surgical
field restrictions and patient-only final submission remain in place. Recipient
IDs are bound to the authenticated clinician and each notification's form is
patient-scoped, including when the same email appears in different tenants.
There is no schema migration, cleanup, or source-record rewrite.

Before these route edits, a separate frozen synthetic team database was captured
using data_inventory.py: 44 tables and 9 original rows, including an accepted form,
its prior edit, escalation, unread reply and notification. Its SQLite backup was
made through Connection.backup. After all seven anonymous attempts were denied,
the SAME source database passed the CLI inventory diff without any allowed column
changes; restoring the backup to a separate file passed the same comparison.
Evidence is in output/security-audit/agent-reliability/intake-auth-before.json,
intake-auth-after.json and intake-auth-preservation.json (outside the repository).
This fixture has no blob references. No clinical data, external messaging or
production datastore was used. Synthetic preservation does not verify a live
backup or restore. Rollback does not require data changes.

The new test_intake_patient_authorization.py exercises 26 synthetic cases:
anonymous/wrong-patient/foreign-tenant denial, orphan form read denial, legitimate
cookie and bearer flows, preserved staff/patient restrictions, recipient-ID
impersonation, same-email cross-tenant notification isolation, and complete
before/after SQLite inventory equality for denied requests. The actual anonymous
consent mutation was reproduced before the fix (HTTP 200). Independent root
review and the final combined test result are recorded in the audit evidence.

## Patient-only signals and secondary clinical IDs

The final dynamic scope audit found that auth_roles.require_patient_session only
rejected staff, allowing anonymous patient actions. Its target patient_id is now
mandatory and the resolved patient session must match it. All 11 callers in
preop_retier, teachback and postop pass their path or body episode ID before model,
clinical cache or storage work. Staff continue to receive 403. Existing patient
cookies support the same UI; role tests now supply those cookies instead of
assuming anonymous callers are patients.

The same audit found two secondary-ID violations. Teachback answer selected a
body session_id without verifying its patient, allowing foreign results to be
returned or altered. Postop alert resolution authorized the path patient but
updated solely by flag_id. Both now constrain the selected original object to the
authorized patient. The store's optional patient_id condition is inside the UPDATE
so the ownership check and resolution are one database operation. Internal calls
without that optional parameter preserve their existing behavior.

Before editing, a separate synthetic team fixture was inventoried: 44 tables,
9 original rows, including two accepted teachback sessions and two patient alerts.
After 11 anonymous requests plus foreign-session and foreign-alert attempts were
rejected, the same fixture's CLI diff showed every original ID and field intact,
with no permitted changed columns. Only denied-request audit metadata was added
(47 tables, 20 total rows after auth/audit initialization). The original SQLite
backup was restored separately and matched the complete before inventory.
Evidence: output/security-audit/agent-reliability/patient-signals-{before,after,
preservation}.json, outside the repository. No blobs, actual patient data,
external messages or production writes were involved.

New test_patient_signal_authorization.py adds 39 cases across all 11 routes,
foreign completed/incomplete teachback IDs, foreign alert resolution, and valid
patient flows. Negative checks compare every clinical table and hot-cache value;
only the intentional append-only access audit is excluded from strict equality.
All 39 new cases and 50 existing postop/preop/teachback/role cases passed. Root
independently reviews this patch. The new file is assigned to exactly one CI shard.

Final signal validation: 142 tests passed in 29.34s across the 39 new authorization
cases, four existing router/role suites, postop apply, preop algorithm/survey
wiring and patient access. A final stronger incomplete-session regression kept all
39 new cases green (3.36s). No source edits followed those results.

### Generated HTML card presentation safety

Generated/stored cards previously reached browser HTML insertion without HTML
sanitization. `card_html.py` now creates presentation copies using nh3 0.3.7's
HTML5 parser and tinycss2 1.5.1's CSS parser. Serving boundaries cover split
resources (including pre-op), single cards, patient/doctor page JSON, pipeline
stream and terminal payloads, legacy processing, teach-back material, and the
internal prompt preview. The resources GET no longer mutates or persists accepted
HTML. Voice scripts and other clinical strings retain their original values.

The policy retains template classes, layout/color CSS, responsive media rules,
static SVG, normal links and existing teach-back anchors. It strips executable
markup, named form objects, URL-bearing/unknown CSS functions and importing
at-rules. Other IDs, their fragment links, and CSS ID selectors are namespaced to
avoid browser named-property collisions. CSS text escapes less-than characters
before the final HTML5 pass so decoded string contents cannot create a fresh
unfiltered style block. No frontend design or interaction changes were needed.

Only returned copies change. Team episode snapshots, accepted HTML file bytes,
original hot records and persisted source cards are immutable in this change.
There is no migration, deletion, new storage schema, encryption-key dependency,
external delivery or production mutation. Dependencies are pinned in
`backend/requirements.txt`; release-wide dependency checks belong to the parent
audit. Rollback reverts serving code/dependencies and requires no data rollback.

Preservation evidence lives outside the public repository in
`output/security-audit/agent-reliability/card-html-{before,after,preservation}.json`
and `card_html_preservation.py`. The same frozen synthetic fixture contains
44 team tables / 4 original rows and an accepted HTML blob. Two authorized reads
leave the original hot object and file hashes unchanged; before/after inventory
has zero problems. SQLite backup API captured committed contents before edits;
restore to a separate database reproduced every original table/row/field hash
exactly. The fixture is synthetic and does not establish production recovery.

Validation: 25 new sanitizer/serving regressions plus existing patient JSON,
teach-back, streaming and processing tests passed **86/86**. Coverage includes
script/event attributes, encoded JavaScript URLs, SVG/malformed namespace
payloads, CSS escaped functions and decoded closing tags, links/anchors, plain
clinical inequalities, exact template markup/classes/CSS declarations, and
read-only original preservation. Independent app-security review passed 61
focused tests and 26 Chromium exploit contexts with zero execution or network
requests. Actual diagnosis and treatment template screenshots were
pixel-identical before/after at 390px and 1440px. Browser evidence is
`output/security-audit/agent-app/card_browser_review.json`; review approved with
no further actionable finding. The parent audit owns the final complete suite.
