# Handoff — Asclepius in-case UX + simplified sign-up/dashboard

**Branch:** `claude/asclepius-incase-ux` (cut from `main`, **not yet committed**)
**Status:** built + tested in this session; not pushed; not committed. Ready to review, then commit.
**Full plan file used for this work:** `/Users/aryaabhatia/.claude/plans/okay-i-m-going-to-groovy-robin.md`
(that plan file gets overwritten by future planning sessions — this handout is the durable record.)

---

## Read this first if you're picking this up in a new chat

1. This branch has **two kinds of changes mixed in the working tree**:
   - **Mine** (this session): everything described below.
   - **Pre-existing, NOT mine**: `backend/routers/onboarding.py` already had an uncommitted
     dev-only OTP-bypass change (`_is_production()` / `dev_bypass` in `request_otp`, plus a
     `logging` import) sitting in the working tree *before* this branch was created from `main`.
     `landing/package-lock.json` also has a small pre-existing diff, unrelated to this work.
     **Check `git diff main -- backend/routers/onboarding.py` before committing** and decide
     whether that dev-bypass snippet should be included, split into its own commit, or dropped —
     it was not requested or reviewed as part of this session.
2. The **in-case evaluation workflow (the staged flow, the questions physicians answer) was
   explicitly NOT changed** — the user was firm on this. Layout/UI only. See "Reverted" below for
   one place this rule was actively enforced mid-session.

---

## What was built (chronological)

### 1. In-case UX pass (kept)
- **Split-screen persistent case (V3/V4 only).** `renderTaskWorkspace()` in
  `frontend/asclepius/asclepius.js` now renders a two-column grid (`.asc-case-cols`) for V3/V4:
  a sticky left rail (clinical question + `renderCasePanel()`, its own scroll) beside the
  step-by-step workflow. Collapse toggle + narrow-screen fallback (rail hides, a sticky
  "View case" bar + overlay takes over below ~900px). **V1/V2 are untouched** — they keep the
  original single-column `.asc-wrap` layout, exact byte-for-byte.
- **Error-tag popover fix.** In the "critique the rejected answer" step, tapping an error tag
  opened a popover that could get stuck open (no outside-click dismiss, "Done" disabled until a
  severity was picked). Fixed: outside-click closes it, added an explicit × button, "Done" is
  now always enabled (severity is still required to *advance* the step — that gate didn't change).
- **Em-dash purge.** Removed every `—` from user-facing copy in `asclepius.js`, `asclepius.css`,
  `_tokens.css`, `_base.css`, and backend user-facing strings (HTTPException details, the NDA
  banner, taxonomy export copy). Added a regression guard test:
  `backend/tests/test_asclepius_no_em_dash.py` (checks the 4 frontend files + the taxonomy
  vocab constants exposed via `/taxonomy`).

### 2. Reverted mid-session (do not re-add without asking)
An earlier version of this pass also redesigned the final "confidence" step into three questions
(anchored confidence + case difficulty 1–5 + familiarity) plus an optional per-case feedback
block, with backend support via `SubmissionIn.model_config = ConfigDict(extra="allow")`.

**The user said this went beyond "UI only" and asked to revert it.** It was fully reverted:
the confidence step is back to the original single low/medium/high question, the extra draft
fields (`case_difficulty`, `familiarity`, `case_realism`, `case_note`, `case_flagged`) are gone,
and `ConfigDict(extra="allow")` was removed from `backend/asclepius/schemas.py`.
**Rule going forward: do not change the in-case questions/workflow — only layout/chrome.**

### 3. Simplified sign-up + dashboard landing (kept)
Triggered by the user's complaint: sign-up asked for specialty up to 4 times, asked for a
practice name/phone nobody needs, then emailed a password and forced a re-login into a bare
task queue with no home screen.

- **Trimmed sign-up** (`landing/src/app/components/OnboardingWizard.tsx`,
  `landing/src/app/components/onboarding/steps.tsx`): dropped the team-invite step from the
  linear chain (invites move to "later, from your dashboard" — not yet built, see Deferred);
  Step 4 (institution) dropped the redundant specialty field and phone, org name is now optional
  (defaults to the physician's name); Step 5 (credentials) dropped fellowship/residency/medical
  school/subspecialty-chips/practice-setting/languages/years — kept only name, NPI, degree,
  primary specialty, active-practice toggle, and board certifications (the actually-required
  core, per the backend's own validation gate). ~4 steps end to end.
- **Auto-login** (`backend/routers/onboarding.py` `asclepius_finish`,
  `landing/src/lib/auth-api.ts` new `storeAsclepiusSession`,
  `OnboardingWizard.tsx` `submitAttestations`): finishing sign-up now mints a real Asclepius
  session token (`asc_auth.authenticate` + `asc_auth.create_token`, same mechanism `/auth/login`
  uses) and returns it to the wizard, which stores it under the console's own localStorage key
  (`asclepius_token` — same origin, so it carries over). The doctor lands **already signed in**;
  no more "check your email, log back in." Credentials are still emailed for future logins.
- **Dashboard landing** (`frontend/asclepius/asclepius.js`, `frontend/asclepius/asclepius.css`,
  new backend endpoint in `backend/routers/asclepius.py`): login now lands on a dashboard
  (`renderDashboardView()`) instead of straight into a case. Shows available-case cards
  (specialty, difficulty, modality) with a live count and a "Start next case" CTA, or — if there
  are no cases — an empty state that reassures / explains / gives an action (NN/g's three jobs
  for a first-use empty screen), never a blank screen. A new `GET /tasks/available` endpoint
  wraps the *already-existing* `store.eligible_tasks_for_evaluator` query (it just wasn't
  exposed over HTTP before) with the same filters `/tasks/next` uses, so the dashboard list
  matches what the queue would actually serve. Clicking a card opens that specific task via the
  existing `GET /tasks/{task_id}` + the unchanged case workflow; "Start next case" calls the
  existing `renderEvalView()`. A "Dashboard" nav tab was added for every user (physicians
  previously got no nav at all). New test: `backend/tests/test_asclepius_available.py`.

---

## Deferred / explicitly not done this pass
- **Email-to-task deep link** ("open a case from an email"): net-new plumbing (a task-scoped
  link + the app reading a `?task=` param on load). Not built — agreed as a fast follow-on.
- **Team invites from the dashboard**: sign-up no longer forces the team step, but there is no
  dashboard UI yet to invite teammates later. That surface doesn't exist.
- **Profile completion for the deferred credential fields** (fellowship, residency, med school,
  focus areas, practice setting, languages, years-in-practice): sign-up stopped collecting these
  and says "add later from your dashboard," but no such profile-edit UI exists yet.
- **First-run paid practice/gold case + qualification gate**, **real teams schema + team-scoped
  admin role + reviewer quality dashboard**: designed in earlier planning (see prior plan
  revisions) but not built.

---

## Files touched (this session's own changes only)

Frontend (console SPA):
- `frontend/asclepius/asclepius.js` — split-screen, popover fix, dashboard view + endpoint call,
  nav, `enterApp`/`switchView` routing. Confidence-step reverted to original.
- `frontend/asclepius/asclepius.css` — `.asc-case-cols`/`.asc-case-rail`/`.asc-work-col` (split
  screen), `.asc-tag-pop-x` (popover close), `.asc-dash-*` (dashboard), em-dash cleanup.
- `frontend/asclepius/_tokens.css`, `frontend/asclepius/_base.css` — em-dash cleanup only.

Backend:
- `backend/routers/asclepius.py` — new `GET /tasks/available`; em-dash cleanup in
  HTTPException details.
- `backend/routers/onboarding.py` — `asclepius_finish` mints + returns a session token.
  **⚠️ also carries the pre-existing unrelated dev-OTP-bypass diff — see note at top.**
- `backend/asclepius/schemas.py` — no net diff (multi-signal confidence added then reverted).
- `backend/asclepius/constants.py`, `backend/asclepius/failure_taxonomy.py` — em-dash cleanup.
- `backend/tests/test_asclepius_no_em_dash.py` (new) — em-dash regression guard.
- `backend/tests/test_asclepius_available.py` (new) — dashboard endpoint tests.

Onboarding wizard (React landing app):
- `landing/src/app/components/OnboardingWizard.tsx` — shorter step order, finish chained into
  attestations, auto-login wiring.
- `landing/src/app/components/onboarding/steps.tsx` — trimmed Step 4/5 fields, updated eyebrows,
  updated success-screen copy (no more "re-login" instructions).
- `landing/src/lib/auth-api.ts` — new `storeAsclepiusSession()` helper.

---

## Verification already done
- `cd backend && source .venv/bin/activate && python -m pytest tests/ -k asclepius -q`
  → **518 passed**, 1 failed (`test_ingest_storage_durability_flags_ephemeral` — confirmed
  pre-existing on a clean `main` checkout, unrelated to this work, an environment/`/tmp` check).
- `node --check frontend/asclepius/asclepius.js` → clean.
- The three edited TS/TSX files parse cleanly via esbuild (no local `tsc` in this repo).
- Em-dash guard test: 5/5 (4 frontend files + taxonomy vocab).
- Dashboard (populated + empty state) and split-screen (desktop/collapsed/narrow) rendered and
  screenshotted using the real CSS via a Playwright harness — visuals confirmed on-brand.

## Not yet verified (do this before/while reviewing)
- **No live end-to-end walkthrough**: haven't run the actual `landing` dev server + backend
  together to click through real sign-up → auto-login → dashboard → open a case. Everything above
  was verified via unit tests + static rendering harnesses, not a live browser session against
  running servers. Worth doing once, especially for the auto-login handoff (token storage across
  the two apps depends on both being served from the same origin/proxy setup in dev).
- Email sending during `asclepius_finish` still requires `_email_configured()` — dev environments
  without SendGrid/SMTP configured will 503 on finish regardless of the auto-login work.

## How to run it locally
```bash
# Backend
cd backend && source .venv/bin/activate
python3 -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# Landing / onboarding wizard (separate terminal)
cd landing && npm run dev
```
Console lives at `http://localhost:8000/asclepius` once the backend is up.

## Suggested commit approach
Given the mixed working tree, consider:
1. First decide what to do with the pre-existing dev-OTP-bypass diff in `onboarding.py` and the
   `package-lock.json` diff (commit separately, drop, or fold in — ask the user).
2. Then commit this session's work, likely as one commit or a small stack:
   split-screen + popover fix + em-dash purge → sign-up trim + auto-login + dashboard.
3. Run the full suite once more right before committing in the new session, since state may have
   drifted if anyone else touched the repo.
