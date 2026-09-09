# PRD D — The legacy applicant lockout

§3 of Tej's running "Product Edits and Improvements" document. §1 and §2 of that
document are `prds/PRD_A_APPLICANT_SCREEN_AND_EXAM_BLOCKER.md`, which shipped as
Onboarding Master Phases 0 and 2 — read it there rather than here.

> **Why this file holds only §3.** The running document was briefly checked in
> whole, which put a second copy of §1 and §2 in the tree beside PRD_A's. Two
> copies of the same section diverge, and PRD_A carries an **AS IMPLEMENTED**
> preamble explaining that its `asclepius.js` anchors describe the tree BEFORE
> that work — deliberately left as the historical diagnosis, since every
> function they name was deleted and there is no current line to repoint them
> at. A copy without that preamble reads as an open work list against code that
> no longer exists, which is exactly the "two notes, no way to tell which wins"
> failure F3 below is about. So: one section, one home.

> **AS IMPLEMENTED.** §3 shipped on `claude/charming-hypatia-r27gj9`. The build
> log is §3.5. Citations below are current against that branch and are checked
> by `prd_audit.py`.

---

## §3 Audit of the onboarding flow (zip 38) — what is correct, what is broken, and the exact fixes

### 3.0 Verified correct — do not touch
| Step | Evidence |
|---|---|
| Screen 1 captures a **required** password for physicians | `landing/src/app/components/onboarding/steps.tsx:617` `needsPassword` gates Continue; `routers/onboarding.py:783` `hash_team_password` stores only the hash |
| Wizard path identity → verify → cv → review → attestations → submitted | `OnboardingWizard.tsx:139-176` |
| Applicant home: two boxes, video + guide rows, exam card, `mailto:tejpatel@berkeley.edu`, no interstitials | `asclepius.js:3206` `renderApplicantHome`. The three interstitial renderers are deleted, and `founderStripEl` (`asclepius.js:3156`) has no callers |
| Stage machine is exam-only | `credentialingStage()` returns `exam_not_started / in_progress / submitted` |
| `#examination` deep link opens the exam card | `asclepius.js:3152-3157` |
| Demo video ticket allowed for provisional | `asclepius_media.py:167` uses `get_current_account` |
| Exam workspace gates (§2) | `require_task_access` + `is_users_exam_task` in `auth.py:542-556`, `exam_case.py:153`, tested in `test_exam_task_access.py` |
| Admin decision reads the examination, computes no verdict, requires an explicit tier | `asclepius_verify.py:260 _examination_block`, `:705 approve` |
| Approval mints a temp password **only** for `NO_PASSWORD_HASH` accounts, never overwriting a chosen one | `_needs_credentials` `:686-691` |

### 3.1 What is broken (four findings)

**F1 — Legacy passwordless applicants are locked out of their own examination.**
Accounts created during the window when the wizard had no password step (Tej's test
account is one) carry `NO_PASSWORD_HASH`. `POST /auth/login` (`routers/asclepius.py:376-383`)
answers them **403 "Your application is in review, we'll email you within 24–48
hours"** whether or not the examination is done, and `renderAwaitingVerification`
(`asclepius.js:2374`) offers only **Check again** (re-boots into the same 403) and
**Sign in with a different account**. The sign-in page's own comment (`:2263-2267`)
says the intended repair is *"Forgot your password?"* — which mints a reset for any
active user and clears `must_change_password` — but the 403 fires before the applicant
ever sees that page, and the gate card never mentions it.

**F2 — The "submitted" email and screen promise a way back in that does not exist.**
The screen says *"we've emailed you a link back in"* (`landing/src/app/components/onboarding/steps.tsx:3982`); the email's
CTA is the bare `portal_url` — **no sign-in link is minted at finish** (no
`create_signin_link` call in `onboarding.py`). The email also says *"There is no
password to remember yet: signing in is a single-use link we email you"*
(`onboarding_emails.py:1355`) — false since screen 1 took a password — and its CTA
reads **"Open my practice case"** (`:1372`, and again at `:1192`) when the required
step is the examination.

**F3 — Contradictory source comments will mislead the next agent.** `orderFor`
(`OnboardingWizard.tsx:161-181`) says *"NO PASSWORD STEP on this path… credentials are
minted and mailed when a human approves"*; the sign-in page (`asclepius.js:2263-2265`)
says *"The wizard now takes a password on screen 1."* The second is true.

**F4 — No self-serve door for a passwordless account.** `/auth/signin-link`
(`routers/asclepius.py:571`) exists and works but nothing in the UI calls it.

### 3.2 Execution steps for Claude Code (in order, one commit each)

1. **Split the login gate.** In `POST /auth/login` (`routers/asclepius.py:376`), inside the
   passwordless+pending branch, read `tutorial.exam.state` for that user. If it is not
   `"submitted"`, raise 403 with header `X-Asclepius-Auth-Gate: pending_examination`
   and detail *"One step left: your examination. Set a password to get back into your
   account."* Otherwise keep today's `pending` response verbatim. Test both, plus the
   unchanged 401 for accounts that have a password.
2. **Give the gate card a door.** In `verificationGate()` accept the new header value;
   in `renderGated` route it to a new `renderExaminationOwed(gate, email)` card:
   title *"One step left: your examination"*, one line *"Choose a password and you'll
   land straight on it."*, primary **Set my password** → the existing forgot-password
   request for that email (the sign-in page comment `:2263` documents that this
   converts a passwordless applicant with no admin and no new code), confirmation
   inline, secondary *Sign in with a different account*. **No "Check again"** on this
   card. `pending` and `rejected` cards unchanged.
3. **After the reset lands, route to the exam.** `set_user_password` already clears
   `must_change_password`; on the next boot `enterApp()` → `sessionIsProvisional()` →
   `renderApplicantHome()` (`asclepius.js:1804` `enterApp`) — verify with a test that a converted
   legacy account lands on the applicant home, not the tutorial.
4. **Fix the submitted email + screen copy** (`onboarding_emails.py:1347-1360`,
   `:1192`; `landing/src/app/components/onboarding/steps.tsx:3975-3983`): CTA **"Open my examination"** linking to
   `portal_url#examination`; replace the "no password to remember yet" paragraph with
   *"Sign in with the email and password you chose. If you forget it, use Forgot your
   password on the sign-in page."*; screen 1 footer becomes *"Your answers save. Sign
   in any time with the password you chose."* Delete the "link back in" sentence.
5. **Correct the stale comments** in `orderFor` (`OnboardingWizard.tsx:161-181`) to
   state that the password is captured on screen 1 (`Step1NameEmail`) and that
   `/auth/signin-link` is a recovery path for legacy passwordless accounts only.
6. **Admin resend.** On the verification-queue row for any `NO_PASSWORD_HASH`
   applicant, add **Send password-setup link** (calls the forgot-password mint). Covers
   the applicant who emails support instead of clicking.
7. **Guard the wire.** `step1-identity`'s `password` is `Optional` server-side
   (`onboarding.py:248`) because the client enforces it. Add a server check: for
   `product == "asclepius"` and `kind == "physician"`, a missing password is a 400 —
   so no new passwordless physician account can be created by any client.

### 3.3 Tests
```
- login: passwordless+pending+exam not submitted → 403 pending_examination, detail names the examination
- login: passwordless+pending+exam submitted → 403 pending, copy unchanged
- login: account with password → unchanged (401/200)
- gate card: pending_examination renders Set my password, no Check again; pending/rejected unchanged
- forgot-password on a NO_PASSWORD_HASH applicant → reset works, must_change cleared, next boot → renderApplicantHome
- step1-identity without password for asclepius+physician → 400; member/advisor paths unchanged
- submitted email: no "no password to remember" text; CTA "Open my examination"; link ends #examination
- orderFor comment no longer contains "NO PASSWORD STEP" (grep test)
- e2e: new applicant → wizard with password → submitted → close tab → sign in with password → applicant home → examination → submit → sign in again → "in review" card
```

### 3.35 Invariants — the design this rests on

Four properties hold before and after §3. Every one of them is a thing a
plausible-looking change here would break quietly.

1. **The generic 401 is not an enumeration oracle, and the split does not make
   it one.** An account that HAS a password gets `Invalid email or password`
   and no gate header, always. The split in §3.2 step 1 reads the examination
   only INSIDE the branch already reachable solely by passwordless accounts —
   accounts with no credential to guess, and whose existence that branch
   already disclosed before this change.

   Stated precisely, because the loose version ("it distinguishes nothing new")
   is not true: the split **does** add one bit. For an address already known to
   be a passwordless applicant, the 403 detail now discloses whether their
   examination is submitted — unauthenticated, at the login limiter's
   10/min/IP. What it adds nothing to is *account existence* and *credential
   strength*, which are the properties this door was hardened for. Application
   progress about an account with no credential was judged an acceptable
   disclosure against stranding the applicant permanently; it is a design
   decision, not an absence.
2. **A password is never overwritten, only ever set.** `_needs_credentials`
   (`asclepius_verify.py:692`) gates every credential mint on
   `NO_PASSWORD_HASH`, and the admin control in step 6 refuses any row that
   fails it. A "reset" for a legacy applicant SETS a first password; for
   everybody else it replaces one they asked to replace.
3. **One mint, one ceiling.** `mint_password_reset`
   (`routers/asclepius.py:463`) is the single place a reset token is created.
   The applicant's forgot door and the admin's setup link both call it, so
   `MAX_LIVE_RESETS`, the expiry and the provenance row cannot diverge between
   them.
4. **Helping somebody sign in is not a decision about them.** Nothing in §3
   writes `verification_status` or `tier`. The examination gate, the gate card,
   the admin control and the wire guard all move an applicant toward being ABLE
   to sit their examination; who is approved stays exactly where it was, in
   the approve endpoint.

### 3.4 Do not touch
`_needs_credentials` (never overwrite a chosen password) · `require_task_access` ·
the exam endpoints · `_examination_block` (no verdict, every attempt) · the generic 401
(enumeration) · `/auth/signin-link` backend (keep for legacy recovery).

---

*§4+ — appended from Tej's further comments.*


---

## §3.5 What shipped (build log)

Five code commits covering the seven execution steps, plus this PRD and a
follow-up commit closing the fresh-context audit, on
`claude/charming-hypatia-r27gj9`.

One landing site per row, so each citation carries its own symbol and
`prd_audit.py` can actually verify it.

| Step | Symbol | Where |
|---|---|---|
| 1 | `login` — the split branch | `routers/asclepius.py:358` |
| 1 | `exam_state` — the new reader | `asclepius/exam_case.py:207` |
| 2 | `renderExaminationOwed` — the card | `asclepius.js:2467` |
| 2 | `renderGated` — routes to it | `asclepius.js:2544` |
| 2 | `verificationGate` — accepts the header | `asclepius.js:1593` |
| 2 | `renderLogin` — the path that actually mattered | `asclepius.js:2151` |
| 3 | `enterApp` — no new code; the ordering in it is what makes step 3 true | `asclepius.js:1804` |
| 4 | `build_application_submitted_email` | `onboarding_emails.py:1331` |
| 4 | `build_practice_case_nudge_email` | `onboarding_emails.py:1192` |
| 4 | `_exam_url` — the new link helper | `onboarding_emails.py:173` |
| 4 | `StepApplicationSubmitted` — the screen | `landing/src/app/components/onboarding/steps.tsx:3875` |
| 5 | `orderFor` — the corrected comment | `landing/src/app/components/OnboardingWizard.tsx:161` |
| 6 | `send_password_setup_link` — the endpoint | `routers/asclepius_verify.py:1079` |
| 6 | `mint_password_reset` — the shared mint it calls | `routers/asclepius.py:463` |
| 6 | `sendPasswordSetupLink` — the console control | `onboarding.js:191` |
| 7 | `step1_identity` — the wire guard | `routers/onboarding.py:729` |

**Two things the build discovered that the PRD did not predict.**

*`renderLogin`'s catch, not just `renderGated`.* §3.2 step 2 named
`verificationGate` and `renderGated`. Both were necessary and neither was
sufficient: this gate arrives from the `/auth/login` call itself, so the
sign-in form's own error handler decides what the applicant sees. It matched
on `'pending'` alone, so without a third edit the new copy — *"set a password
to get back into your account"* — would have rendered inline on a form with
nothing on it that sets one. A door named in three places and reachable from
none.

*`get_tutorial_state` discards a blob with no top-level `status`.*
`exam_state` reads through it, so a fixture written as a bare
`{"exam": {...}}` answers `not_started` for every account and a test built on
one would pass while asserting nothing. Live blobs always carry `status`
because `/exam/task` round-trips the existing blob rather than replacing it.
Pinned directly in `test_examination_owed_gate.py`.

**Reversed decisions.** Step 7 overturns a deliberate earlier trade: the
password field was optional on the wire so a cached SPA bundle could not 400
mid-deploy. The deploy window is hours; a passwordless physician account is
permanent and every repair for it is manual, which is what §3 is about. The
guard reads the STORED hash rather than the request body so it cannot fire on
a legitimate re-post of screen 1.

**Pre-existing tests changed, and why each was legitimate.**

| Test | Old assertion | Why it had to change |
|---|---|---|
| `test_onboarding_v2.py` pending gate | passwordless+pending → `pending` | Now split by exam state; the submitted case keeps this assertion verbatim, a new test covers the other branch |
| `test_signup_ceiling_and_applicant_door.py` door test | submitted mail names the practice case | §3.2 step 4 changed which case the mail names; the property it guards (the mail carries a link at all) is kept |
| `test_signup_password_and_returning.py` no-password signup | screen 1 works without a password | Step 7 reverses exactly this; rewritten to assert the refusal, plus three new tests for the paths that must still pass |
| `test_onboarding_v2.py` wizard order | greps the asclepius branch for `"password"` | Was reading the PROSE above the step array; the corrected comment necessarily discusses passwords. Now strips comments and asserts what it meant |
| `test_admin_signups.py` `_walk` helper | posted screen 1 with no password | A helper standing in for a real applicant, not a test of the rule; it now sends what a real applicant sends |

**Audits run.** `check_dangling_imports.py` clean · `route_baseline.py --diff`
clean after snapshotting the one new route · `node --check` on both changed JS
files · full backend suite · `prd_audit.py` on this file. `data-inventory` was
NOT run and did not apply: §3 touches `users` (password hash, `tutorial_json`)
and `password_resets`, none of which are in the set that skill guards
(`tasks`, `submissions`, `records`, `earnings`, `uploads`, `assignments`,
`exports`). No migration, no `DELETE`, no schema change.


### The fresh-context audit, and what it caught

Run per AGENTS.md after the seven steps were built. It confirmed the five
do-not-touch items untouched, the `mint_password_reset` extraction
behaviour-identical (ceiling, expiry, provenance row, background send, uniform
answer and latency parity all preserved), and the step-7 guard incapable of
walling a real physician. It also caught five things worth recording, because
four of them were mine and one was a real production bug.

**The bug.** `renderExaminationOwed`'s door hand-rolled its `fetch`. `fetch`
does not reject on an HTTP error, so every 4xx and 5xx fell through to the
success path: the card said "we've emailed you a link" and left its only
button dead on "Sent ✓". Three reachable paths — the per-IP rate limiter's 429
(a hospital behind one NAT), the live-reset ceiling answering 200 while mailing
nothing, and an unwell server. Strictly worse than the "Check again" the card
replaced, which at least stayed clickable, and precisely the failure the card
was written to remove. Fixed by routing through `api()`, which raises on
`!res.ok`. The sign-in form's own forgot button had the identical bug and, on a
screen opened without an error, wrote its message into a `hidden` div.

**A vacuous test.** `test_physician_order_is_cv_review_and_has_no_password_step`
was changed during step 5 to strip comments before grepping the wizard branch.
After the strip the slice was `if (product === "asclepius") {` and whitespace —
an assertion that could not fail for any possible content. It now parses the
step array, which is the contract. Two of the audit-fix tests failed the same
way while being written (a comment satisfying a grep), so the JS assertions in
`test_examination_owed_gate.py` strip comments in both directions.

**A partial repair.** Step 1's copy reached two of three sign-in surfaces.
`SignInDialog` swallows a failed Asclepius attempt and falls through to the
landing plane — correct for a 401, which is ambiguous on both planes by design,
and wrong for a gated 403, which is not. `asclepiusLogin` now carries the gate
rather than only a message; the fall-through is narrowed, not removed.

**A bound that named a column, not a person.** The admin control's only check
was `password_is_unset`. That set coincides with "legacy applicant" today only
because of how the SSO, buyer and data-partner provisioners happen to behave —
a fact about provisioning, not a rule. Bounded to evaluator, not-approved,
active.

**An assumption pinned instead of driven.** Every `exam_state` test hand-built
the blob, encoding a belief about what the server writes. §3.3's last line
asked for the end-to-end; it now exists and drives `/exam/task` and
`/exam/submit`.

Two audit findings were about this document rather than the code and are fixed
above: invariant 1 overclaimed (it now says which property it protects and
which bit it concedes), and a briefly-checked-in whole copy of the running
document duplicated §1 and §2 without PRD_A's **AS IMPLEMENTED** preamble. That
copy is gone.
