# Applicant practice scoring and skip — 15 September 2026

## Problem and result

The scoring editor described “must never” as an auto-fail, but its toggle only
changed the sign of the existing weight: the default +5 became −5. The shared
practice/examination finish gate requires a named critical negative (−8 to −10).
A physician could correctly select “must never” and still be blocked at finish.

New “must never” selections now start at Critical. Existing draft weights are
preserved on load. A blocked recap acknowledges the existing negative and offers
“Mark Critical (−9)” beside that specific criterion. The physician explicitly
chooses which criterion to promote; text, citations, axes and other criteria
remain intact. A positive-only guide offers a direct way to add the missing
critical negative. Blank text still cannot satisfy the gate. Server grading
requirements and the optional empty-rubric behavior are unchanged.

Applicants can skip the entire practice from its welcome screen or any tour
step and open their examination immediately. An existing exam is resumed.
Practice drafts are saved; skipping does not submit practice, grant a practice
pass, alter the accepted-doctor welcome package, or bypass server authorization.
Advisors and applicants with submitted exams retain their existing exit path.

Practice reveal, reasoning, submission, progress and task-load responses are
bound to their originating workspace/tutorial. A late response cannot overwrite
exam content or metadata. Resumed workspaces reset canceled request flags and
retry unfinished reasoning splits, while retaining saved physician reasoning.

## Scope and preservation

- Production changes: two static frontend files only; no database migration,
  backend route, grading policy, email or infrastructure change.
- Existing writes exercised: browser draft storage, Asclepius tutorial/exam
  progress and examination submission through the existing API routes.
- Asclepius fixture inventories compare every existing table, ID, column and
  field hash before/after eight browser flows. Only `users.tutorial_json`
  (normal practice/exam progression) and `users.first_run_json` (session count on
  refresh) may change. Additive events/exam rows are permitted; deletion is not.
- Each flow takes a consistent SQLite backup using the backup API, opens that
  separate backup, and compares it against the initial snapshot. All eight
  backups match; after-flow comparisons report no unexpected changes.
- Existing exam identity and draft, practice draft, first-run stops, rubric
  text, axes and citations receive additional direct assertions. Skipping
  preserves practice status and gate rather than claiming completion.
- The local team and community fixture databases are also snapshotted and
  restored, with no changed existing fields allowed. There is no production or
  deployed sandbox realm access, upload/blob or encryption key dependency, or
  real external-provider operation. These local synthetic fixtures do not
  establish production backup/restore health.
- Rollback: revert the frontend commit. There is no schema/backfill rollback.
  Existing saved drafts keep the physician-selected weights and remain readable.

## Verification

- 296 focused API/model/DOM tests passed across scoring, practice, first-run,
  applicant exam access, exam submission, onboarding and CI sharding.
- 32 portal browser cases passed against shipped assets and real isolated API
  routes. Seven unrelated public landing/build scenarios were excluded.
- After the final reasoning-retry fix, all 10 affected browser cases passed
  again, including mobile exam submission after pausing a delayed reasoning
  split, delayed reveal pause/resume, and practice-to-exam reveal isolation.
- Desktop/mobile manual scoring, recovery of −5 drafts, actual practice and
  examination submission, welcome/mid-practice skip, existing-exam resume,
  refresh, temporary exam unavailability and retry all passed.
- Twelve DOM race cases cover late reveal/reasoning/submission success and
  failure both while an exam loads and after it opens; old responses cannot
  clear new request flags or change exam data.
- Mobile visual review and explicit tooltip/skip-button viewport bounds passed.
- JavaScript syntax, diff whitespace and destructive SQL guard passed.
- Route baseline unchanged: 675 routes. No dangling imports: 716 files scanned.
- Merge readiness clear against `21a1d7794`; zero commits behind at review.

Reproduce targeted tests from `backend/`:

```sh
python -m pytest tests/test_asclepius_eval_ui_overhaul.py tests/test_practice_case_tour_dom.py tests/test_asclepius_rubric.py tests/test_practice_case_gate.py tests/test_first_run_dom.py tests/test_credentialing_exam.py tests/test_exam_task_access.py tests/test_examination_owed_gate.py tests/test_asclepius_onboarding.py tests/test_ci_sharding.py -q
ONBOARDING_SCREENSHOT_DIR=/tmp/practice-evidence python -m pytest tests/test_physician_onboarding_browser.py -k 'not public_join and not reminder_link_opens_saved_wizard_step' -q
```

Browser evidence includes screenshots plus per-case `before.json`, `after.json`
and `preservation.json`; the committed preservation summary records snapshot
hashes and results. The tests use development email mode and the fake LLM.

## Independent reviewer

Fresh-context auditor `audit_practice_fix`, read-only review against origin/main:

> No outstanding findings. Delayed practice responses cannot overwrite exam
> answers, reasoning, or workspace state. Paused exams can reveal answers after
> resume. Canceled reasoning requests can retry; saved physician reasoning
> remains intact. Late tutorial metadata responses cannot replace newer exam
> state. The scoring repair preserves criterion text, axes, and citations.
> Practice skip saves the draft and uses the existing exam route without
> changing server authorization or marking practice completed.

Independent verification: 96 focused tests plus executed delayed-response,
pause/resume, metadata and saved-reasoning checks passed. The builder fixed the
auditor's initial response-isolation and canceled-request findings before this
confirmation. Production deployment remains a separate release action.
