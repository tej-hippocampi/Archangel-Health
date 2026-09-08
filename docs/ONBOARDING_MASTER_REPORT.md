# Onboarding Master PRD — implementation report

Phases 0 through 3 of `docs/prd/onboarding-master/00_MASTER_ONBOARDING_PRD.md`
§1. Phase 4 is not started, by instruction.

Branch `claude/onboarding-master-prd-xllldy`, six commits on `cdf99f8`.

---

## Before anything changed: the citation audit

All 53 path-qualified `file:line` citations and all 26 bare `:NNN` continuations
across the four documents resolved **exactly** against this checkout. Nothing had
drifted, so no PRD text was edited before implementation.

The PRD B baseline was reproduced rather than taken on trust. Running the
investigation's own probe (`evidence/onboarding-ux-evidence/`) against the
unmodified tree gave **8 of 29 checks passing** — the number the PRD reports,
from the same script, on this checkout.

Implementing the phases then moved 44 of those anchors. They were repointed
afterwards; see [Citations, after](#citations-after).

---

## Phase 0 — the applicant can finish the examination

`965600a` · PRD A §2

### What shipped

An applicant clicked **Reveal AI answers →** and was told *"We are still
verifying your credentials."* So was everybody: the examination could not be
completed by anyone whose credentials were pending, which is its entire
population.

The examination enters through a provisional-safe door — `/exam/task` and
`/exam/submit` depend on `require_surface(TUTORIAL)`, which `PROVISIONAL` holds —
and is then sat in the ordinary workspace, whose every call is gated on
`require_practice_case` → `require_label` → full access. The case loaded and
nothing in it worked.

| Change | Where |
|---|---|
| `owns_this_exam_task(user, task_id)` — the carve-out as one predicate | `backend/asclepius/auth.py:523` |
| `is_users_exam_task(store, user, task_id)` — identity from server-written state | `backend/asclepius/exam_case.py:145` |
| `require_task_access` — the seam; `_full_task_gate` reproduces the old chain link for link | `backend/routers/asclepius.py:3257`, `:3272` |
| `GET /tasks/{id}`, `POST /tasks/{id}/reveal` rewired | `backend/routers/asclepius.py` |
| `/assist/prelabel` — same rule, resolved from the body (the task id is not a path parameter) | `backend/routers/asclepius.py:5243` |
| `/citations/search`, `/transcribe` → `require_surface(TUTORIAL)` | `backend/routers/asclepius.py` |
| `task_id` stamped on the exam draw **and carried through submit** | `backend/routers/asclepius.py:1901`, `:1957` |
| `goToPracticeCase()` no longer tells an applicant mid-exam to go do a practice case, and no longer starts one | `frontend/asclepius/asclepius.js:2475` |

The submit path rebuilt the exam blob from scratch and would have dropped the
stamp — closing the applicant's own case to them at the moment they filed it.

The examination is **not** routed through `/tutorial/reveal`. That path writes no
`independent_commits` row by design, and the committed independent answer is
exactly what an admin reads before approving.

### Evidence promoted

None — Phase 0 has no diagnostic in `evidence/`; the PRD's trace is a code
reading, and the tests below are written from it.

### Tests

`backend/tests/test_exam_task_access.py` — **24, all passing**. The refusals are
the point, not the 200s:

- their own examination opens (fetch, reveal, prelabel);
- **another applicant's** examination, a **different specialty's** exam case, an
  ordinary synthetic task and a **real V4 case** are all still 403 `pending`;
- `_BY_ACCESS[PROVISIONAL]` is asserted to be the same four surfaces;
- a fully-approved physician meets the identical gates in the identical order,
  including the structured `practice_case_required` error the client routes on;
- an untiered physician is still refused (`require_label` is not lost);
- the examination **persists** — it writes `independent_commits`, unlike
  `/tutorial/reveal`;
- a legacy in-flight exam (a stamp with no `task_id`) is recognised by
  recomputing the same deterministic rotation that served it.

### Deliberate widening, flagged

`/citations/search` and `/transcribe` moved to `require_surface(TUTORIAL)` as the
PRD specifies. `TUTORIAL` is also held by **advisor** accounts, so those two
endpoints are now reachable by advisors. Advisors already click through the whole
practice case, and neither endpoint reaches anything patient-specific — the
citation library is published guidance and dictation is the user's own microphone
— so this is consistent with what that account already grants. No table changed.

---

## Phase 1 — a physician can type a phone number

`450cc6c` · PRD B P0-A, P0-B

### What shipped

`Group` was a component declared **inside** `Step5Credentials`. React reconciles
by component type, so a component defined in a render body is a new type on every
render: never an update, always an unmount and remount of the whole subtree. Every
keystroke updates parent credentials, re-runs `Step5Credentials`, mints a fresh
`Group`, and throws away every field under it.

`ReviewGroup` is now at module scope
(`landing/src/app/components/onboarding/steps.tsx:2268`) taking its section and
open-state decision as ordinary props.

Not fixed by refocusing after each update, remembering a selector and re-clicking,
or freezing rerenders with stale memo dependencies. Each leaves DOM identity
broken underneath and breaks caret, selection, IME composition and screen-reader
focus with it.

**P0-B** needed no separate change and the report should say why: with the hoist
in place `OnboardingSection`'s open state is no longer reset, because `useState`
ignores later `defaultOpen` changes — so the physician's collapse survives
editing. Collapsed bodies stay mounted under `display: none`, which preserves
their drafts *and* removes them from the tab order and the accessibility tree,
which is both halves of what P0-B asks for. Both are now asserted.

### Evidence promoted

`evidence/onboarding-ux-evidence/run_form_probe.cjs` → **`landing/test/onboarding-form-stability.test.cjs`**.

Two things changed on the way in. Each check is now an independent assertion that
fails the build — the probe printed JSON and exited zero whether or not anything
passed. And it runs against production sources with **no patching**: the probe
compared the real form against a disposable copy with `Group` hoisted, and there
is nothing left to compare against.

`landing/test/build-onboarding-fixture.cjs` builds the real components; it applies
`withRowIds` exactly as `OnboardingWizard` does on hydration, so the tests
exercise the row shape the product actually renders.

### Tests

There was no JavaScript test runner in this repo. There is one now:
`npm test` in `landing/`, and a new **`onboarding-form` CI job** in
`.github/workflows/tests.yml` that fails if the suite reports no tests — a file
that fails to load otherwise exits zero and looks green.

- `landing/test/onboarding-form-stability.test.cjs` — 30 of the 37
- `landing/test/onboarding-structure.test.cjs` — §2 invariant 7 as a rule: no
  capitalised function may be defined at an indent anywhere in `onboarding/`.
  Pinned against the exact shape that caused this, and re-scanning the pre-hoist
  source shows it catching `Group` at the line the PRD cites.

Phase 1 shipped 21 passing plus 3 `todo` placeholders for the checks the hoist
does not fix. **Phase 3 turned all three into real assertions**; the suite is now
**37 passing, 0 todo**.

### Deferred deliberately

Stable per-row ids. PRD B P1-A says to coordinate them with PRD C's provenance and
row-merge schema rather than ship two conflicting row-id mechanisms, so they
landed in Phase 3 with that schema.

---

## Phase 2 — the portal is one screen that says one thing

`d0f717a` · PRD A §1

### What shipped

A six-stage hidden state machine whose first three stages were full-screen
interstitials, read once and never again — so an applicant's second visit landed
on a different screen from their first, decided by state they could not see.
Behind them, a dashboard whose single button changed label four ways. The
examination was behind all of it and behind a practice case. The founders
introduced themselves and offered a Calendly on two of those screens, to somebody
not yet accepted.

**Deleted:** `renderProvisionalWelcome`, `renderOnboardingChoice`,
`renderOnboardingInfo`, `renderCredentialingResources`,
`renderCredentialingDashboard`, `startCredentialing`, `stampCredentialing`, and
the `welcome / choice / info / resources / waiting` stages.

**Added:** `renderApplicantHome()` — two cards; card 1's items are quiet rows with
an icon, a line and an arrow; card 2 carries the only primary button on the page.
The heading, lede and card 1 are identical in all three exam states.
`openGuideOverlay()` is a real overlay built from the same manual data and the
same `guideSection` builder the full Guide uses, returning focus to whatever
opened it. The video reuses the walkthrough's existing player, which already
closes on ✕ and Esc.

**Migration by ignoring.** `welcome_seen_at`, `onboarding_choice`, `info_seen_at`
and `resources_seen_at` are still written and still returned; nothing reads them.
An applicant carrying a full set of legacy marks and one who signed up this
morning land on the same screen. No backfill, no schema change, no blob rewritten.

The landing success screen lost four paragraphs of philosophy, a founders'
signature, an invitation to sign in and browse, an outbound `/mission` link, and a
second thank-you repeating the 24–48 hours the first had given. The CTA deep-links
to `/asclepius#examination`, which the portal reads as a focus hint, never routing.

CSS: `.asc-applicant-*` at the top level of the stylesheet, outside every foreign
`@media` block, with its own single breakpoint. The four rules that styled the
deleted screens went with them.

### Evidence promoted

None — Phase 2 has no diagnostic script; PRD A §1 is a design specification.

### Tests

`backend/tests/test_applicant_home_screen.py` — **23, all passing**, covering
PRD A §1.5's list: legacy stamps land on the same screen; the three card-2
variants; exactly one primary; nothing else changes with state; no Calendly, no
founders, no mission paragraph, `mailto:tejpatel@berkeley.edu` present; the guide
overlay's Esc/✕/focus-return; the practice link; the success-screen copy and the
absence of the deleted sentences; and a brace-depth check that the applicant CSS
sits outside every foreign `@media` block.

**Six existing tests encoded the deleted design as invariants and were rewritten,
not dropped.** This is worth reading as a list, because a test rewritten to pass
is a bad smell unless the spec moved — and here it did:

| Test | Why it changed |
|---|---|
| `test_the_founders_appear_where_an_applicant_actually_waits` | **Inverted.** §1.2 removes the strip pre-approval; it now asserts the opposite and that the function is kept for post-approval |
| `test_the_new_stages_are_all_tested_after_the_old_ones` | The stages are gone; it now asserts that *nothing reads them*, which is the migration |
| `test_choosing_to_wait_is_not_a_dead_end` | The `waiting` stage no longer exists |
| `test_the_examination_is_reachable_without_the_optional_material` | Repointed from the deleted resources screen to the applicant screen |
| `test_the_success_screen_spends_the_session…` | Asserted the screen names the practice case; §1.3 makes it name the examination |
| `test_the_mission_link_opens_in_a_new_tab` | The link is deleted rather than mitigated; it now asserts no outbound link remains |

`founderStripEl` and `FOUNDER_CALENDLY` are kept with no caller, per §1.4.2 — that
strip belongs to the post-approval welcome, and `FOUNDER_CALENDLY` is bound to a
backend constant by `test_landing_config`.

### Open question, deliberately not decided

§1.4.2 asks which founder's calendar should be canonical when the strip returns
post-approval. `FOUNDER_CALENDLY` still points at **Aryaa's**. Left exactly as it
was: nothing renders it pre-approval any more, and changing it here would silently
re-point the approval email too. **This needs a human decision.**

---

## Phase 3 — unanswered stays unanswered, and an old CV stops overwriting a new one

`ee13abd` · PRD B P1-A/B/C/D + PRD C §6 A, C, E

### Tri-state board validity (C §6-E, §2 invariant 1)

The parser emits `None` for "is this certification currently valid". That `None`
became `false` in the wizard, and `YesNoToggle` renders `false` as a **selected
"No"** — so every physician who uploaded a CV was shown a negative attestation,
already made on their behalf, on every certification they hold. Manual rows had
the mirror defect: they defaulted to `true`.

**Changing the UI type alone would have been unsafe, and this is the part worth
reading.** The scorer read `_truthy(active) is not False`, so an unanswered row
scored exactly like a confirmed one. Moving the wizard to `null` on its own would
have turned *"we never asked"* into *"yes, currently certified"* for every
applicant at once.

| Signal | Axis | Change |
|---|---|---|
| `feature_vector` → `board_certified_active` | confirmed current validity | now requires an explicit `True`; the legacy `users.board_cert` text OR-fallback is **removed** |
| `domain_match` | claim | **unchanged** — an unanswered validity question does not make a nephrology subspecialty stop being one |
| `propose_tier` → `board_certified` weight | claim | **unchanged** — no physician loses credit they had |

The removed fallback is the shape §6-E names: `users.board_cert` is a text
projection of the first board row's *name*, written at signup and never consulting
`active`, so typing a board name granted "active certification" even to somebody
who had explicitly answered No.

**No stored value is rewritten.** An existing `true` keeps its credit, an existing
`false` keeps its refusal; only new rows start `null`. Migration is additive, per
§6-E and §2 invariant 3.

### CV lifecycle (C §6-A, §2 invariant 2)

New additive table `asclepius_cv_attempts`. Beginning B supersedes A in the same
transaction; every worker write is stamped with its attempt and refused once
superseded; the terminal state and its result are one statement;
`_record_cv_on_person` merges named keys inside one transaction instead of
read-modify-writing the whole credential blob. Superseded attempts are retained as
history. `PARSER_VERSION` is recorded.

Client: the poll now knows which upload it is watching — it checks at every await
boundary, trusts the server's `attempt_id`, stops on re-upload and on unmount, and
behaves exactly as before against a server that sends no attempt id.

### Field mapping (C §6-C)

Fellowship specialty is filled when the document says it, deterministically, from
a fragment that resolves against the specialty registry — never inferred from the
physician's current specialty. A single unambiguous **completed** residency
suggests its end year in the completion-year field, chipped like every other
suggestion; the attestation itself stays theirs. The licence is written as one
value: `fill` twice could pair a jurisdiction the physician typed with a number
off their CV. Additional licences are kept on the record instead of dropped.

### Form (B P1-A/B/C/D)

Stable per-row ids minted once when a row is introduced — also PRD C §6-D's merge
key, which is why they waited for this phase. Real `<label htmlFor>` with
`aria-describedby` on `TextField`, `SelectField`, `TextArea` and `ChipMultiSelect`;
a Yes/No pair is one named `role="group"`; each Remove button says what it removes.
`rowHasContent` counts typed text, not flags.

### Evidence promoted

- `evidence/cv-extraction-evidence/worker_race_result.json` — the recorded
  interleaving where A overwrote B while keeping B's filename — became
  `test_a_late_success_from_the_old_upload_is_refused` and its siblings.
- The `active: false` finding recorded in `probe_results.json` /
  `frontend_results.json` became `test_board_validity_tri_state.py` and four
  JSDOM behaviour tests.
- The three Phase 1 `todo` placeholders became real assertions.

### Tests

- `backend/tests/test_board_validity_tri_state.py` — **15**
- `backend/tests/test_cv_attempt_lifecycle.py` — **20**
- `landing/test/onboarding-form-stability.test.cjs` — now **37 passing, 0 todo**
- `backend/tests/test_cv_parse_quality.py:275` — the test PRD C identified as
  *protecting* the bug (`assert "active: false" in _APPLY`) is **replaced**.

### Two defects the tests caught in my own work

Recorded because they are the reason the tests are worth having:

1. `rowId` is a non-blank string, so adding it made `rowHasContent` count **every
   empty row as filled** — the exact defect P1-C exists to fix, reintroduced by
   the fix for P1-A. Caught by `a blank board row does not count as filled`.
2. `aria-describedby` pointed at a hint element that is not rendered while an
   error is showing, resolving to nothing silently. Caught by `a hint is
   announced with the control it belongs to`.

---

## Verification

| Check | Result |
|---|---|
| `python3 -c "import main"` | boot ok |
| `scripts/route_baseline.py --diff` | route table unchanged (651 routes) |
| `scripts/check_dangling_imports.py` | no dangling imports (663 files) |
| `scripts/merge_readiness.py` | clear — 6 ahead, **0 behind** `origin/main` |
| `scripts/prd_audit.py` × 4 documents | **exit 0, all clean** |
| `tests/test_ci_sharding.py` | 19 passed — every new test file lands in exactly one shard |
| TypeScript (`tsc --noEmit`, onboarding subtree) | no new errors |
| `landing` component suite | 37 passed, 0 failed, 0 todo |

New backend test files by CI shard: `test_applicant_home_screen` and
`test_cv_attempt_lifecycle` → shard 2; `test_board_validity_tri_state` → shard 3;
`test_exam_task_access` → shard 4. None silently drops out of CI.

### Full backend suite

Run on both trees, in a clean worktree for the base, so the comparison is real.

|  | base `cdf99f8` | this branch |
|---|---|---|
| passed | 6,293 | **6,381** |
| failed | 33 | 29 |
| skipped | 14 | 14 |

**Every one of the 29 failures on this branch is present on the untouched base**
— `git diff`-verified set comparison, not a count. They are in
`test_telehealth_router` (9), `test_intervention_email` (4),
`test_care_team_messaging` (4), `test_demo_and_patient_update` (3),
`test_asclepius_mm_debug` (3), `test_triage_timeline` (2),
`test_community_meeting_gaps` (2), `test_auth_hardening` (1) and
`test_asclepius_router` (1) — largely the peri-op and telehealth areas
`AGENTS.md` records as flag-gated for deletion. None is mine, and none is fixed
by this work. The base's other four (`test_storage_durability`) pass here; that
file is environment-sensitive and the difference is not attributable to this
change.

Two regressions were introduced and both are fixed:

1. `test_no_em_dashes` — three em dashes in new copy, which is a repo style rule
   the PRD's own prose does not follow. Caught and fixed inside Phase 2.
2. `test_harness_scripts::test_both_shipped_prds_audit_clean` — adding ~150 lines
   to `team_store.py` drifted an **unrelated, pre-existing** PRD's citation
   (`PRD_SANDBOX_REALM.md`'s anchor for `_STORES`). Fixed in the PRD, per the
   rule, not in the code. This is the check working exactly as intended.

`docs/asclepius/` data inventory: the sandbox database is **empty**, so
`data_inventory.py --diff` reporting "no ids lost" is vacuous and is not offered
as evidence. The substantive check is structural, and it holds: the diff across
all six commits adds **no** `DELETE FROM`, `DROP TABLE`, `DROP COLUMN` or
`TRUNCATE` against any table, and its only schema statements are
`CREATE TABLE IF NOT EXISTS asclepius_cv_attempts` and its index.

---

## The audit round

AGENTS.md: "Builder writes, auditor checks with fresh context, builder fixes,
auditor confirms, then PR." The auditor read the four PRDs and the whole diff
against `a25d32a` without my account of it, and reproduced its findings by
execution rather than by reading. It found one critical defect I had introduced,
three high-severity ones, and a set of smaller things. All are fixed.

### Critical — the carve-out was forgeable, and it was mine

**`POST /exam/submit` took `task_id` from the request body and stamped it into
`tutorial.exam`, which is the only input to the carve-out's identity check.**

One POST — no draw, on a fresh account — rewrote the stamp to any task id the
caller could name, and the next `GET /tasks/{id}` returned it. `reveal` then
wrote an `independent_commits` row under an unverified account, and each submit
rewrote the stamp again, so it walked the synthetic queue. The V4 wall held
throughout; everything behind it did not.

The draw's own comment says this value is "written server-side, never accepted
from a client". The submit path was quietly doing the opposite, and my Phase 0
tests submitted only the honest id, so the one client-controlled input in the
whole carve-out was untested.

`/exam/submit` now resolves the task server-side — from the stamp, or the same
deterministic rotation `is_users_exam_task` re-derives for a legacy blob — and
403s a body that names anything else. Four tests cover it, including the
no-draw forge, the walking variant, that a forged submit files no examination
record, and a positive control that an honest submit still works.

### High — the CV lifecycle races the PRD named were still open

| Defect | Fix |
|---|---|
| `merge_asclepius_credentials` was **not atomic**. `connect_team_db` leaves `isolation_level = ''`, so sqlite3 issues an implicit `BEGIN` before DML only, never before a `SELECT` — read-then-write in one connection is still two transactions, and in WAL mode a commit landing between them is lost | `BEGIN IMMEDIATE`, in this method and in `advance_cv_attempt` and `start_cv_attempt` |
| The staleness guard was **check-then-act across two transactions** | `require_attempt` folds the currency check into the merge's own transaction |
| `/asclepius/cv/status` labelled the payload with the **newest** attempt rather than the one that wrote it — and the client's guard is `served !== attemptId`, so it would have *accepted* A's extraction as B's | the response reports `cvAttemptId`, stamped by the same merge that wrote the parse |
| A failed attempt **inherited the previous attempt's `ok: true` result** under its own identity — verbatim what §6-A forbids, and no race was needed | the failure patch sets `cvParsed: None` explicitly |
| `_preserve_server_cv_fields` still read in one transaction and let its caller write in another, so a worker write landing between them was lost | `save_asclepius_credentials_preserving`, one transaction; §6-A's rule applies to whichever end of the race is second |

Seven tests, including a real interposed writer that fails without
`BEGIN IMMEDIATE`, and one proving a refused write releases the lock rather than
stranding it.

### Medium

- **`/assist/cite` still 403'd for the applicant.** PRD A §2.0 listed the
  endpoints the exam workspace calls and this was not on the list, so the
  implementation followed the list rather than the code. The client swallows the
  error, so nothing visibly broke — the one-click citation chips were simply dead
  for the only population the examination exists for, which is what §2.3 means by
  "cite a guideline … zero 403s". Now `require_surface(TUTORIAL)`, with the
  client's `tutorialActive()` guard taught to let the examination through.
- **`/transcribe` had no size bound.** It moved to a wider audience in front of a
  metered provider with an unbounded `await file.read()`. Now a running cap at
  12 MB, the same shape the CV path uses.
- **The licence fix over-corrected.** Requiring both halves empty meant a resumed
  session — where `licenseState` is prefilled from signup — never got the CV's
  licence *number* either. §6-C blocks the *conflicting* pair, not the agreeing
  one; now compared by state.

### Lower

The guide overlay declared `aria-modal` without trapping Tab or moving focus in
— a promise to assistive technology that has to be true of the focus order, as
this codebase's own dialog says. Fixed, and its scrim now uses the portal's
documented "canvas, never black" law instead of a raw black. The legacy exam
recompute no longer seeds gold cases from inside an authorization predicate.
`advance_cv_attempt` returns what the `UPDATE` actually did. The structure lint
now discovers files in `onboarding/` instead of listing them, catches a
signature wrapped over several lines, and its self-test runs the real detector
against a fixture that includes a shape that must *not* match. The
`additionalLicenses` claim is corrected: it is retained for Phase 4 and a future
migration, and nothing reads it yet.

### What the auditor confirmed

It re-ran the base comparison independently, by test id rather than by count:
the failure sets on this branch and on `cdf99f8` are **identical** — no
regressions, nothing accidentally fixed. It also verified `_full_task_gate` drops
no gate, `_BY_ACCESS` is unchanged, the V4 wall holds by execution, the tiering
split revokes nothing (`tr_eligibility` feeds an admin *proposal*, not live
routing), `ENCODER_USER_COLUMNS` still means something, `useId` has no hook-order
or SSR hazard, `withRowIds` never reassigns, and that all six rewritten Phase 2
tests were justified by the spec moving — three of them strengthened.

## The seven invariants

| # | Invariant | Held by |
|---|---|---|
| 1 | No new mandatory fields; unknown stays `null` | Board validity is tri-state end to end and the scorer requires an explicit Yes; no field became required |
| 2 | Never overwrite a user edit | Attempt-scoped writes; transactional key merge; poll identity; the licence tuple is written whole or not at all |
| 3 | No database deletion; additive migrations only | One new table, one new index, no `DELETE`/`DROP`; superseded attempts retained; no stored `active` rewritten |
| 4 | A provisional applicant reaches exactly one task | `_BY_ACCESS` unchanged and asserted; the carve-out is identity-checked against server-written state; the V4 wall stands behind it |
| 5 | The examination persists; not through `/tutorial/reveal` | Asserted: the exam writes `independent_commits`; the tutorial path is untouched |
| 6 | CV content is data, not instructions | No new code executes, fetches or follows anything from a parsed CV; extraction stayed regex/vocabulary-bound |
| 7 | One primary per screen; tokens only; no inline components; CSS outside foreign `@media` | Asserted by test, including a lint-rule test for inline components and a brace-depth check for the CSS |

---

## Do not touch / non-goals

- `_BY_ACCESS`, `require_full_access` semantics for everyone else, the
  `/tutorial/reveal` no-persistence rule, the V4 real-data wall, and the
  verification queue's read of the examination (PRD A §2.4).
- `GET /exam/task` / `POST /exam/submit`'s purpose and stage stamps, the tutorial
  internals, the post-approval first-run walkthrough, and the demo-video
  ticket/Range endpoint (PRD A §1.6). The submit path's task-id VALIDATION was
  added because it was a security defect, not a change of purpose.
- Tier weights, seniority or prestige features, retroactive revocation and
  auto-approval (PRD C §6-E). The tri-state work corrects the input semantics of
  existing computations and changes no weight.
- Any database deletion. The CV attempt table is additive and superseded
  attempts are retained (§2 invariant 3).
- Phase 4 (PRD C §6 B, D, F). Not started, by instruction.

## What remains unverified

**These require a human at a real browser in the sandbox realm. They are not
claimed, and JSDOM cannot establish any of them — it has no layout engine, no
scroll, no virtual keyboard, no IME and no accessibility runtime.**

1. **Real-browser scroll position.** PRD B's ≤2 CSS-pixel rule for typing and
   toggles, and the founder's exact reported sequence (scroll to residency, click
   Yes, type the year, continue to mobile, type without re-clicking). The
   mechanism behind the reported jump — whole-subtree replacement and accordion
   reset — is fixed and proven fixed; **that the pixel symptom is gone is not
   measured.**
2. **Mobile virtual keyboard.** Visibility, obscured active field, clipped
   controls, horizontal overflow at 390px, and a small laptop height.
3. **IME composition.** Composition events, mid-composition normalisation and
   caret behaviour. The tests dispatch `input` events; they do not compose.
4. **Screen-reader output.** The labels, `aria-describedby` relationships,
   `role="group"` naming and `aria-pressed` states are asserted as computed
   accessible names in the DOM. **What a screen reader actually announces is not
   tested.**
5. **Native tab order** across the full form, and keyboard-only completion of the
   whole signup.
6. **Zoom and viewport matrix.** 80/100/200% desktop zoom, Chromium *and* WebKit,
   390px and 1280px, large text, reduced motion.
7. **Caret and selection.** `selectionStart/End` through mid-string edits.
   Asserted only that the node and focus survive.
8. **The end-to-end journey of PRD §4** on a fresh provisional account in the
   sandbox realm: wizard → CV → unanswered board validity → two-box screen →
   video → examination → cite → library search → **reveal** → evaluate → submit →
   "Your examination is with us" → the admin queue showing the committed
   independent answer. Each leg is covered by a server or component test; **the
   whole path has not been walked by a person.**
9. **The applicant screen rendered.** Its CSS, two-column breakpoint and overlay
   are asserted as source and brace structure, never as pixels.
10. **OCR and PDF quality.** Untouched — Phase 4 (§6-B/F). `tesseract` is absent
    here, and the existing per-page/OCR defects PRD C §3 lists are unchanged.

Per the master, this acceptance runs in the **sandbox realm or a test account —
never by submitting a production physician application.**

---

## Not started

**Phase 4** — PRD C §6 B, D and F: structured extraction with evidence spans,
provenance chips and clear-review UI, PDF quality and OCR bounds. Not begun, by
instruction. The named extraction defects in PRD C §3 (accented names, negation,
reference-author degrees, single-line fellowship institution, lowercase "present",
two-line board/issuer layout) are **unfixed and remain accurate**.

Within Phase 3's own scope, two things were read narrowly and are worth naming:

- **Repeatable licences** are not a UI yet. §6-C's atomic-projection rule is
  honoured — the primary licence is written whole or not at all — and additional
  licences are preserved on the credential record rather than dropped, but the
  physician still reviews only the primary one. The full repeatable-licence
  control is §6-C's larger ask and belongs with Phase 4's review UI.
- **`_extract_training` single-line institution** still includes the role and
  dates ("Nephrology Fellowship, Cleveland Clinic, 2015-2017" as the institution).
  §6-C asks for institution to exclude role, dates and city; that is entry-boundary
  work, which is §6-B, which is Phase 4. The fellowship *specialty* extraction
  added here works on both layouts.

---

## Citations, after

Implementing the phases moved 44 of 53 anchors. `9382a20` repoints them: 32
mechanically, the rest in prose because the symbol was renamed or deleted by the
work the PRD specifies. `prd_audit.py` exits 0 on all four documents.

One correction to the master's own §0 audit, found by implementing it: **the
array-index key sites are three, not two.** It listed board and fellowship and
missed residency. PRD B P1-A names all three groups, so only the summary was short.
