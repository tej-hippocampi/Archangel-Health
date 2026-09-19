# Agreement enforcement — data-safety change record

Date: 2026-09-18. Initial audit: origin/main 321994b; final comparison base: d964fdb. Branch: fix/agreement-enforcement.
Related specification: [Agreement enforcement](PRD_AGREEMENT_ENFORCEMENT.md).

## Scope and preserved data

Application changes affect the realm-scoped Asclepius and team SQLite stores.
The agreement requirement is read-only and runs before covered route bodies.
The existing signature endpoint continues writing append-only physician_agreements
rows with document version, hash, signature and signup-attestation snapshot.
No signature table, trigger, legal source document, endpoint, or stored ID changes.
No migration or bulk data rewrite is introduced.

Intentional writes: team.asclepius_people.attestations_json merges caller-provided
keys over existing keys; updated_at advances on a successful save. Asclepius
users.attestations_json retains omitted keys on re-onboarding and changes only
explicitly supplied values. False remains false. Existing unrelated provision_user
updates are unchanged. Corrupt stored objects fail rather than being discarded.
Both merge paths acquire the SQLite write transaction before reading.

The community database, chart/source stores, upload sessions, accepted originals,
blob trees, object buckets, encryption keys, earnings and export files are outside
the write scope. The code uses the existing realm-scoped stores. Sandbox signing
links preserve the sandbox URL and its token namespace.

## Local inventory and restore evidence

Evidence: [preservation summary](evidence/agreement-enforcement/preservation-summary.json).
The full before/after inventories, SQLite backups and restored copies remain in
the task's local output/agreement-enforcement directory; they contain synthetic
fixture data only. No live database or account was used.

- Asclepius: 74 application tables, 1 fixture row before/after, zero field/ID loss.
- Team: 44 application tables, 3 fixture rows before/after, zero field/ID loss.
- SQLite backup API copies were restored to separate files and compared with the
  same frozen inventories. Both restore comparisons had zero differences.
- v1 document SHA-256 remains
  282306f1b15f15a4a3b3d99d8a95a6afa2d0e67d7ed04278cb37fd1b9b5c9666.
  CURRENT_VERSION remains v1; unset ASCLEPIUS_AGREEMENT_GATE remains false.

The first synthetic snapshot was taken directly after provisioning. Reopening it
exercised existing main's organization/tier metadata backfills. That initial
failed comparison and its original snapshots were retained. The fixture was then
restored from its pre-change backup and initialized using store.py loaded from
origin/main. This normalized dataset was frozen, inventoried, reopened using the
changed code, and compared without allow-change exceptions. No production data
was rebaselined or modified.

These small local fixtures are regression evidence, **not** proof of production
backup coverage, available capacity, encryption-key recovery, or historical
unsigned-record provenance. Production release must provide those operational
checks required by docs/data-safety/POLICY.md.

## Failure, retry and authorization checks

- Serialized concurrent partial team saves retain every omitted key.
- Explicit false values are persisted and then rejected by signup completion.
- An aborted database write leaves prior evidence intact; retry and duplicate
  retry merge successfully without erasing keys.
- Invalid persisted JSON/object shapes fail visibly without overwriting evidence.
- Both signup completion paths reject every missing/non-boolean consent before
  creating a portal account or sending completion mail.
- All 16 covered work routes reject unsigned and superseded accounts with the
  existing structured response. Signed, disarmed, admin and mock cases pass the
  agreement layer, while reviewer-role restrictions remain.
- Three explicitly retained task continuation routes stay usable after supersession.
- Browser tests exercise signing/resuming and preservation of unsaved annotations,
  review notes and step-level judgments, plus both realm link destinations.

## Independent review

A separate auditor reviewed the backend, frontend, new tests, and revised PRD
with fresh context. The auditor found additional QA work routes; these now carry
the same agreement check while retaining QA authorization. A second pass found
wrong-realm signing links and loss of unsaved review step judgments; both were
fixed and received regression coverage. The final re-review found no remaining
code blockers. Test execution results are the builder's responsibility.

Dedicated browser checks of QA-note and longitudinal-score refusal/retry were
considered useful follow-ups rather than blockers: the reviewer inspected those
catches and confirmed they leave their controls and in-memory input mounted and
reenable retry. The shared refusal classifier and realm link are browser tested.

## Validation results

- Final focused agreement and attestation suite: **164 passed**.
- Full backend run on the rebased implementation: **7,577 passed, 4 skipped,
  3 failed** in 15m12s. Two failures were corrected: the applicant-home DOM
  fixture now provides the location used by agreement deep links, and eight
  shifted citations in existing PRDs were refreshed. All **74** applicant-home
  and harness checks passed on the corrected tree.
- The remaining failure, `test_release_library_contains_all_86_reviewed_cases`,
  also fails on a separately archived, clean `d964fdb` main tree. Required
  specialty case artifacts are missing from that upstream release. This branch
  changes neither the library nor its material or acceptance test. The test is
  retained and the PR stays draft; the repository-wide suite is not fully green.
- The six agreement browser cases pass after relocation into the existing
  physician-onboarding browser module. This puts them in the Chromium CI job
  without changing shard selection or permitting silent browser skips.
- All 64 landing component tests pass, and the production landing build passes.
- Route baseline is unchanged at 676 routes; dangling-import and JavaScript
  syntax checks, destructive-SQL guard, all shipped PRD citation checks, and
  merge readiness pass.
- The independent auditor approved the browser-test relocation and the narrow
  fixture/citation corrections. Production code was unchanged by these fixes.

The full-run and focused logs, clean-main reproduction, and CI logs are retained
in the task's local output/agreement-enforcement directory. PR #166 carries the
latest CI status and final focused agreement/attestation result.

## Release decision and recovery

This is a staged code change, shipping with agreement enforcement disabled by
default. No production deployment, Railway setting change, roster email, or live
backup operation has occurred in this task. Contributor notice copy and the
activation sequence are in the PRD. Historical export provenance is a separate
owner scope decision; it is not silently inferred from a later signature.

Before release, collect scoped live/sandbox inventories and off-volume backups,
restore them in isolation and compare identities/hashes, and verify intended
email delivery/acceptance. Before arming, complete the contributor notice/signing
window and verify the real roster state instead of assuming all accounts remain
unsigned. Unset the flag to roll back signature enforcement. Revert code only if
rolling back the added eligibility chains or finish validation. Retain signature
and attestation evidence; disabling enforcement requires no destructive migration
and no restoration over production.
