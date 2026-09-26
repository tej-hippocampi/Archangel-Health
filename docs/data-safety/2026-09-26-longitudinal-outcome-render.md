# Longitudinal outcome rendering and case transitions

## Scope and invariants

Frontend repair for the physician's saved invalid-case flag followed by an
endless “Opening what happened next” spinner. The live submission was retained
as V5 prompt_flagged; its sealed follow-up window contains 34 lab panels.
The browser called a lab renderer that had moved into a private shared-module
closure, and synchronous painting was outside the outcome error boundary.
The prior unit harness supplied a nonexistent global helper and browser fixtures
had notes-only outcomes. These gaps are now covered with the actual module and
lab-containing outcomes.

Successful invalid/incoherent flags fetch trajectory progress and open the next
assigned point directly. Ordinary evaluations retain outcome review and optional
self-scoring. Duplicate submissions still recover the authoritative original
outcome. Continuation reuses progress, avoiding a redundant metadata fetch.
Navigation GETs expire after 15 seconds and show Retry. Rendering failures use
the same recovery screen. No flag or evaluation is reposted by navigation retry.
An existing compare-stage draft's answer-reveal POST retains its existing behavior.

Both live and sandbox use the shared frontend. Browser recovery retains task IDs,
account/realm scoping and optional self-score marks, adding only a continuation
boolean for saved flags. Completed-point recovery clears after the next workspace
opens. Stale responses cannot replace a newer screen or signed-in account.

There are no backend/schema changes, migrations, source transformations,
historical rewrites, assignment changes or operational submissions. All existing
source charts, tasks, flags, annotations, records, earnings and exports remain
source-of-truth. Team/community stores, upload sessions, blob roots, buckets,
encryption keys, agreements, email and external integrations are unchanged.

## Preservation and rollback

Evidence is kept in `output/longitudinal-outcome-render/` outside the repository.
A before inventory and after diff use the same frozen backend fixture (82 tables,
27 rows), with no lost identities or changed baselined content. A fresh isolated
restore uses SQLite's backup API and is compared against that inventory.
The reported account's seven point IDs, assignments, source/case hashes and
existing submission IDs are captured with read-only production queries before
release and checked again after deployment. Production flags are never submitted
or modified by the verification scripts.

This is scoped frontend/preservation evidence, not a full production disaster
recovery certification. Rollback redeploys the prior code while retaining all
server records and browser drafts/recovery entries. Never restore over the live
database or clear a physician's storage to roll back this UI change.

## Verification

Regression browser tests load the actual shipped scripts on desktop and mobile,
show lab values/units/flags/relative dates after ordinary evaluations, recover from
synchronous render failure, preserve outcome/self-score recovery on refresh, flag
four assigned points without fetching outcomes, retain one submission per flag,
retry metadata and next-case failures after refresh, abort hung navigation reads,
and stop before unassigned points. The next queue also has a timed-read retry.
Executed JavaScript tests load the real case-panel module rather than inventing
its missing global helper. Shared reviewer chart parity tests remain in scope.

An independent reviewer (`audit_outcome_render`) checked module context, saved
flag continuation, stale navigation, deadline/abort behavior, and delayed cleanup.
Final test counts, reviewer confirmation, CI and deployed-code checks are recorded
in the PR. The runtime verification is read-only.
