# PRD — Product Edits and Improvements

A running PRD. §1 is the applicant (pre-approval) experience. Further sections are
appended as Tej adds comments. Verified against `Archangel-Health-main (36)`.

---

## §1 The applicant experience: one screen, two boxes, one job

### 1.0 What is causing the confusion (traced)

The pre-approval portal is a **six-stage hidden state machine**
(`asclepius.js:2865 credentialingStage()`): `welcome → choice → info → resources →
practice_in_progress → exam_ready → exam_in_progress → exam_submitted`. The first
three stages each render a **full-screen interstitial** that is "read once and then
never again" (`renderProvisionalWelcome` :3104, `renderOnboardingChoice` :3124 —
"There is one thing you can do that speeds this up", `renderOnboardingInfo` :3155 —
"Archangel in five paragraphs"). Only after all three does the applicant reach
`renderCredentialingDashboard` (:3225), whose **single button changes label by stage**
(`Start` / `Continue practice case` / `Take my examination` / `Resume my examination`).

So an applicant's second visit lands on a *different* screen than their first,
depending on state they can't see; the actual goal — the examination — is behind
three interstitials and a practice case; and the copy on every screen explains the
philosophy instead of the next step. The founders block (`founderStripEl` :3076, with
`FOUNDER_CALENDLY` :3067 — note: **Aryaa's** Calendly) renders on two of these
screens for someone who hasn't been accepted yet.

Everything the applicant needs exists: the exam (`GET /exam/task` `asclepius.py:1870`,
`POST /exam/submit` :1910, stage stamps), the demo video with Range streaming
(`asclepius_media.py:174 /assets/onboarding-demo`), the guide (`state.panel='guide'`,
`manual-content.js`), the practice case (`startTutorial`). The problem is
choreography, not capability.

### 1.1 The rule

**Pre-approval, the portal has exactly one screen and it says one thing: the last step
of your application is the examination.** No interstitials. No stage-dependent button
labels. No hidden state deciding what the applicant sees.

Delete from the pre-approval path: `renderProvisionalWelcome`, `renderOnboardingChoice`,
`renderOnboardingInfo`, and the `welcome / choice / info / resources / waiting` stages.
Keep the `tutorial.*` server fields (read-only history; no migration) but stop routing
on them. The only state that changes the screen is the exam: `not started · in
progress · submitted`.

### 1.2 The screen

```
YOUR APPLICATION

We are checking your credentials — and there is one thing left for you to do.

Credential review takes one to two business days and needs nothing from you; we
email you the moment it is decided. The last step of your application is the
examination below: one real case in your specialty, in the interface you would
work in. We read it when we decide.

┌ 1 · HOW TO LABEL A CASE ─────────────────┐ ┌ 2 · TAKE THE EXAMINATION ───────────────┐
│                                           │ │                                          │
│  ▶  Watch a labeled case         3 min    │ │  One real {specialty} case. About        │
│     A physician walks through one real    │ │  15 minutes. You can stop and come back; │
│     case start to finish.                 │ │  your answers are saved.                 │
│                                           │ │                                          │
│  ≡  Read the labeling guide               │ │        [ Start the examination → ]       │
│     What we look for, what a strong       │ │                                          │
│     answer contains, common mistakes.     │ │  (in progress → Resume the examination;  │
│                                           │ │   submitted → ✓ Your examination is with │
│  Optional: try a practice case first →    │ │   us. Nothing more to do.)               │
└───────────────────────────────────────────┘ └──────────────────────────────────────────┘

Any questions: tejpatel@berkeley.edu
```

Rules for the build:
- Two cards, side by side ≥900px, stacked below. Card 2 carries the **only primary
  button on the page.** Card 1's items are quiet rows with an icon, a title, one
  line of description, and an arrow — no buttons.
- **Video** opens in place (same expanding player as the first-run walkthrough,
  `/assets/onboarding-demo` with the ticket), ✕/Esc closes. **Guide** opens the
  existing guide panel in an overlay and returns here on close. **Practice case** is
  a single quiet text link at the bottom of card 1 — optional, never required, never
  a stage. *(Decision: kept as a link because the tutorial exists and some applicants
  want it; strike the line if you want strictly two items.)*
- Card 2 state: `not started` → "Start the examination →"; `in progress` → "Resume
  the examination →" + "Your answers are saved."; `submitted` → check mark, "Your
  examination is with us. Nothing more to do — we'll email you either way.", button
  gone. Nothing else on the page changes with state.
- **No founders block, no Calendly, no mission paragraph** pre-approval. Replace with
  the one line `Any questions: tejpatel@berkeley.edu` (mailto). The founders strip
  and `Book 20 minutes with us` move to the **post-approval** welcome (Onboarding v2
  §6 stop 1 / the welcome email) — that is where "meet the founders" belongs.
- Community / Referrals / Earnings remain view-only in the rail exactly as today;
  the sentence about them moves into the rail's own empty states rather than the
  application copy.

### 1.3 Copy (verbatim)

**Landing success screen** (`landing/src/app/components/onboarding/steps.tsx:3760-3830`)
replaces everything between the check mark and the sign-in link with:

> **Thank you, Dr. {last_name}.**
>
> Your application is with us. One of us will review it personally within 24–48
> hours and email {email} either way.
>
> **One step left: open your account and take the examination.** It's one real case in
> your specialty, about 15 minutes, and it's what we read when we decide.
>
> [ Open my account and take the examination → ]
>
> You can stop part way — your answers save, and we've emailed you a link back in.

Delete: "We keep review human on purpose…", "We're only confirming that you are who
you say you are…", "look around", "Or read our mission". The CTA label carries the
instruction; the paragraph carries the why, once.

**Portal heading + lede** — as drawn in §1.2. **Card titles** — `1 · How to label a
case`, `2 · Take the examination`. **Card 1 rows** — as drawn. **Card 2 body** — "One
real {specialty} case. About 15 minutes. You can stop and come back; your answers
are saved."

**"We have your examination" email** (`asclepius.py:241`) — keep; add one line: "We'll
email you our decision within 24–48 hours."

### 1.4 Code changes (ordered)

1. `asclepius.js`: replace `renderCredentialingDashboard` (:3225) with
   `renderApplicantHome()` implementing §1.2. `credentialingStage()` (:2865) reduces
   to three exam states; delete the five pre-exam branches and the three interstitial
   renderers (:3104, :3124, :3155) after grepping that nothing else calls them.
   `startCredentialing` (:2893) becomes `startExamination()` — exam only; the
   practice link calls `startTutorial({replay:false})` directly.
2. `founderStripEl` (:3076): remove both pre-approval call sites (:3116, :3273); keep
   the function for post-approval use. `FOUNDER_CALENDLY` (:3067) currently points at
   Aryaa's Calendly — confirm which one should be canonical when it returns post-
   approval (Tej's is `calendly.com/tejpatel-berkeley/intro-with-tej-patel`).
3. `steps.tsx` success block: copy per §1.3; CTA deep-links to the portal with the
   exam card focused (`/asclepius#examination`).
4. New CSS: `.asc-applicant-grid`, `.asc-applicant-card`, `.asc-applicant-row`,
   `.asc-applicant-help` — registered with the orphan-class guard; design tokens only;
   **outside any `@media` block except its own** (the walkthrough CSS lesson).
5. Server: no schema change. `tutorial.welcome_seen_at / onboarding_choice /
   info_seen_at / resources_seen_at` are no longer read by the client; leave the
   fields and the stamping endpoints in place (history), mark them deprecated in a
   comment.

### 1.5 Tests

```
- applicant with empty tutorial blob lands on renderApplicantHome, never an interstitial
- applicant with legacy stamps (welcome_seen_at etc.) lands on the SAME screen (migration by ignoring)
- exam states: not started / in progress / submitted render the three card-2 variants; nothing else on the page differs
- only one .asc-btn-primary on the page
- no Calendly href, no founders strip, no mission paragraph pre-approval; mailto tejpatel@berkeley.edu present
- video opens in place and closes on Esc; guide opens as overlay and returns
- practice link starts the tutorial and returns here on exit
- success screen: CTA text matches §1.3; deleted sentences absent (grep)
- node --check; CSS brace balance
```

### 1.6 Do not touch
`GET /exam/task` / `POST /exam/submit` and their stage stamps · the tutorial internals ·
the admin verification queue (it reads the exam) · post-approval first-run walkthrough ·
the demo-video ticket/Range endpoint.

---

## §2 BLOCKER — the examination refuses "Reveal AI answers" for the very people it exists for

### 2.0 The bug, traced

An applicant taking the credentialing examination clicks **Reveal AI answers →** and
gets *"Could not reveal the AI answers: We are still verifying your credentials…"* —
the exam cannot be completed by anyone whose credentials are pending, which is
everyone who takes it.

Cause, exactly:

1. The examination is served through a **provisional-safe door**: `GET /exam/task`
   (`asclepius.py:1870`) and `POST /exam/submit` (`:1910`) depend on
   `require_surface(TUTORIAL)`, and `TUTORIAL` is in the provisional set
   (`capabilities.py:167 _BY_ACCESS[PROVISIONAL] = {TUTORIAL, BROWSE, REFERRAL, EARNINGS}`).
   So the case *loads*. It is a real task row (`exam_case.exam_task_for`), not a
   virtual case like the practice tutorial.
2. The workspace's `revealAnswers()` (`asclepius.js:6082-6098`) special-cases only
   `tutorialActive()` → `POST /tutorial/reveal`. **For the examination
   (`examActive()`, `:3005`) it falls through to `POST /tasks/{task_id}/reveal`**
   (`:4039`), whose dependency is `require_practice_case` → `require_label` →
   full access. `auth.py:471-487 require_full_access` refuses every PROVISIONAL user
   with the message in the screenshot (`AUTH_GATE_HEADER: pending`).
3. The same wall stands in front of three more things the exam workspace calls:
   `POST /citations/search` (`:5286`, "Search the library" in the screenshot) and
   `POST /transcribe` (`:5306`, mic dictation) depend on `get_current_user`, which
   is an alias of `require_full_access` (`asclepius/auth.py:562`); `POST /assist/prelabel`
   (`:5201`) and `GET /tasks/{task_id}` (`:4017`) depend on `require_practice_case`.
   Draft autosave is `localStorage` only (`:3872`) — unaffected.

In one sentence: **the exam enters through the provisional door and then uses the
real-task workspace, whose every server call is gated on full access.** Nobody who
needs the exam can finish it; anyone who *can* finish it doesn't need it.

### 2.1 The fix — one dependency, applied at the seam, not per endpoint

Add a capability-aware dependency in `auth.py`:

```python
def require_task_access(task_id: str, user = Depends(get_current_account)):
    """Full access, OR a provisional applicant acting on THEIR OWN examination task."""
    if _caps.access_level(user) != _caps.PROVISIONAL or user.get("role") == "admin":
        return require_full_access(user)          # existing behaviour, unchanged
    if _caps.can_surface(user, _caps.TUTORIAL) and exam_case.is_users_exam_task(user, task_id):
        return user                                # the one carve-out
    raise <the existing 403 with AUTH_GATE_HEADER: pending>
```

`exam_case.is_users_exam_task(user, task_id)` compares against the task id stamped
in `tutorial.exam` when `/exam/task` served it (`:1896-1898` writes the attempt; add
`task_id` to that stamp if it isn't there). This is the **only** widening: a
provisional applicant gains access to exactly one task — theirs — and nothing else.
`_BY_ACCESS[PROVISIONAL]` is not changed; real cases, the queue, and every other
task remain unreachable.

Apply it:

| Endpoint | Today | After |
|---|---|---|
| `POST /tasks/{task_id}/reveal` (:4039) | `require_practice_case` | `require_task_access` **then** the practice-gate check for non-exam tasks (the exam is exempt from "have you done the practice case" — the practice case is optional pre-approval per §1) |
| `GET /tasks/{task_id}` (:4017) | `require_practice_case` | same pattern |
| `POST /assist/prelabel` (:5201) | `require_practice_case` | same pattern; body carries `task_id` |
| `POST /citations/search` (:5286) | `get_current_user` | `require_surface(TUTORIAL)` — searching the citation library reveals nothing patient-specific and an applicant needs it to cite |
| `POST /transcribe` (:5306) | `get_current_user` | `require_surface(TUTORIAL)` — dictation is a UI convenience, not data access |

Do **not** route the exam through `/tutorial/reveal`: that path writes no
`independent_commits` row by design ("the practice case leaves no data behind"), and
the exam's whole point is that the committed independent answer is what an admin
reads before approving (`:2055`). The exam must persist exactly like a real task.

Client: no change to `revealAnswers()` is required once the server accepts the
call — but add `examActive()` to the two places that currently only check
`tutorialActive()` for *messaging* (the "practice case" wording on the reveal
button's error toast), so an applicant never sees a sentence about a practice case
while sitting in the examination.

### 2.2 Why this shape and not "give provisional users the LABEL surface"

Widening `_BY_ACCESS[PROVISIONAL]` to include labeling would let an unverified
account draw real de-identified patient cases from the queue — the exact thing the
provisional tier exists to prevent (V4 wall, `real_data_approved`). The carve-out
above is task-scoped and identity-checked; it cannot leak a second case.

### 2.3 Tests

```
- provisional user + own exam task: GET /tasks/{id}, POST reveal, POST assist/prelabel → 200
- provisional user + ANY other task id (real V4, synthetic, another applicant's exam) → 403 pending
- provisional user: POST /citations/search → 200; POST /transcribe → 200
- full-access user: every endpoint above behaves exactly as before (regression on require_practice_case for non-exam tasks)
- exam reveal writes independent_commits (persists), unlike /tutorial/reveal
- admin verification queue shows the applicant's committed independent answer after reveal + submit
- end-to-end: fresh provisional account → /exam/task → cite → search library → reveal → evaluate → /exam/submit → stage exam_submitted, no 403 anywhere (playwright)
- error copy: in examActive(), no toast mentions "practice case"
```

### 2.4 Do not touch
`_BY_ACCESS` table · `require_full_access` semantics for everyone else · `/tutorial/reveal`'s no-persistence rule · the V4 real-data wall · the verification queue's read of the exam.

---

*§3+ — appended from Tej's further comments.*

## §3 Audit of the onboarding flow (zip 38) — what is correct, what is broken, and the exact fixes

### 3.0 Verified correct — do not touch
| Step | Evidence |
|---|---|
| Screen 1 captures a **required** password for physicians | `landing/src/app/components/onboarding/steps.tsx:617` `needsPassword` gates Continue; `routers/onboarding.py:783` `hash_team_password` stores only the hash |
| Wizard path identity → verify → cv → review → attestations → submitted | `OnboardingWizard.tsx:139-176` |
| Applicant home: two boxes, video + guide rows, exam card, `mailto:tejpatel@berkeley.edu`, no interstitials | `asclepius.js:3184` `renderApplicantHome`. The three interstitial renderers are deleted, and `founderStripEl` (`asclepius.js:3134`) has no callers |
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

1. **The generic 401 is not an enumeration oracle.** An account that HAS a
   password gets `Invalid email or password` and no gate header, always. The
   split in §3.2 step 1 reads the examination only INSIDE the branch that was
   already reachable solely by passwordless accounts — accounts with no
   credential to guess — so it distinguishes nothing new.
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

Seven commits, one per execution step group, on `claude/charming-hypatia-r27gj9`.

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
