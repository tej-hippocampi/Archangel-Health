# Community clinical specialty correction — 22 September 2026

An approved physician's saved onboarding declaration was Dermatology, while
the legacy profile specialty was NULL. Community ignored the declaration and
showed “Not shared.” Specialty rooms were also seeded from the four-specialty
paid-generation catalog, excluding dermatology entirely.

## Scope and invariants

- Community member projections read the confirmed clinical specialty first,
  then the legacy Tier A ship/profile values. A contradictory generated blurb
  is regenerated in the response. The original credentials, CV, profile and
  vault rows are not rewritten. Credential fields never enter public payloads.
- Both live and sandbox community databases receive additive specialty rooms
  from the 54-specialty clinical vocabulary. Paid-generation availability,
  verification and access permissions do not change.
- The default specialty activation threshold becomes one approved nonstaff
  member. The explicit environment override remains supported. Empty rooms
  stay hidden; existing messages keep a room visible. Regional thresholds are
  unchanged, and regional seeding uses the same normalized member identities.
- Existing channel IDs and messages remain. Existing subspecialty slugs retain
  their meaning; colliding primary specialties use a `specialty-` prefix.
  Seeding preflights historical group conflicts and fails before any writes.
- No tasks, submissions, records, earnings, uploads, assignments, exports,
  agreements, blobs, encryption keys, email settings or external integration
  writes are involved. Community news may now include newly visible rooms via
  the existing scheduler; this change sends no test mail or authored messages.

## Frozen inventory, restore, failures and retry

The validation script at local `output/community-specialty-fix/preserve.py`
loads the exact origin/main community store to create a nonempty old fixture,
including core, specialty, country, subspecialty, city and crossed rooms, plus
messages in four existing rooms. The updated store is then opened on that same
dataset in each realm.

In both realms: 18 original channels become 68; every original ID and message
is retained. The repository data-inventory comparator finds no unexpected
field changes; only `community_channels.position` is allowed to change as new
rooms enter the ordering. Repeated seeding is identical. SQLite backup API
copies restored into separate files match the complete original inventory.
This is synthetic migration/restore evidence, not a production restore claim.

Regression tests cover absent/stale specialties, contradictory saved blurbs,
malformed declarations, public privacy, approved/pending/staff populations,
configured thresholds, actual profile/directory/channel APIs, clinical catalog
collisions, multiword/regional seeding and atomic rejection of legacy city
conflicts. The auditor separately retried a baseline-shaped conflict twice and
compared the full SQLite dump: no change. Existing transaction boundaries are
unchanged; no new filesystem ingestion or cross-store write boundary exists.

## Operational evidence and rollback

Read-only production inspection confirmed the reported physician's original
Dermatology declaration and legacy NULL specialty. The specialty threshold is
not overridden in production. Channel identities and message-body hashes were
captured before deployment without exporting message content. Complete live
and sandbox catalogs are checked against the new specialty/region definitions
before release; any conflict blocks release.

The production `/data` volume is Ready with 273.27 MB used of 5,000 MB.
Daily and weekly backups are configured. The retained September 22 daily
snapshot `9e356121-ba80-43ac-b68f-a5195ddf0f8e` was created at 16:57 UTC,
references 273 MB and expires September 28. The September 20 manual snapshot
also remains retained. Current metadata is in local
`output/production-recovery/current-backup-coverage.json`.

The prior September 14 off-volume encrypted recovery drill restored six
live/sandbox databases, 5,439 rows and 1,943 files, compared inventories, and
recovered the encryption key. That historical drill is recorded in local
`output/production-recovery/RECOVERY_RESULT.md`; it does not establish a restore
of the new September 22 Railway snapshot. This patch changes no key dependency.

Rollback is a code revert with the expanded channel catalog retained: never
delete the new rooms or restore an older database over intervening posts. A
full revert of the catalog would deactivate new rooms, preserving their rows
but hiding new discussion; retain catalog definitions if that history exists.

## Review and validation

Fresh-context auditor `audit_community_specialty` reports no remaining
actionable code findings. Regional seeding and stale-blurb findings were fixed
and rechecked. Latest independent checks: 129 targeted tests plus application
startup passed; collision rejection and normal bootstrap/retry were separately
verified against full SQLite dumps. Privacy, data-change guard and whitespace
checks passed.

Builder checks: 226 initial community tests and 218 additional community tests
passed; 103 affected specialty/cohort tests passed after audit corrections.
The live route table remains unchanged at 687 routes and the dangling-import
scan is clear. Browser and CI outcomes are recorded in the pull request.


## Integration with security release and full CI

Rebased onto main 761a9de. The community directory now mirrors the security
release's normalized rejection rule: a later rejected status supersedes an
older vault approval in directory results, notifications and specialty counts.
The auditor verified this integration with 26 specialty/WebSocket security
checks and a separate mixed-case/whitespace rejection reproduction. All 240
integrated community checks had passed before that additional correction.

The first full CI run passed 17 checks but shard 4 identified stale PRD line
citations and contamination from the new synthetic collision test's global
store. The collision test now owns an independent temporary database; its
preservation assertions remain. The 35 specialty/task-notification tests pass
in sequence. PRD citations are updated to actual symbols, without changing
production code to satisfy documentation.

Live/sandbox preflight found zero conflicts among 24/13 existing channels.
The real Chrome check passed all three user-facing surfaces with no JavaScript
errors. Final CI is required on the corrected head before deployment.


The next CI run resolved all shard-4 failures, but installed newly released
Starlette 1.7.0 (the previous run used 1.6.0). Its CORS middleware adds a fixed
`Vary: Origin` header even without a request Origin. Provider equivalence tests
now permit absent or exactly that header, while continuing to compare all
header names, order and values between both variants and freezing every other
header. No application response or CORS policy changes are included. The
change was reproduced against 1.7.0 locally and independently reviewed.
