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
   is an alias of `require_full_access` (`auth.py:526`); `POST /assist/prelabel`
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
