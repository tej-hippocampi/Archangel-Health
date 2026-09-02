# PRD: Applicant gate (pre-approval funnel, vetting integration, promotion)

PR-1, group A. Companion to `prd-physician-profile.md` (group B, same PR).

## Problem

From the Sep 1 product meeting: an applicant should be able to log in the moment
they apply, see a view-only dashboard, and do exactly one practice case. No real
cases, no community, nothing else. The practice case is one of the two completion
requirements before founder review (the other is credential import), and its
result should feed the vetting dossier. If either requirement is still missing
24 hours after application, we send one nudge. The internal vetting score is
never shown to the physician, and it should be able to change a physician's role
over time; today the tier is set once at approval and never again.

Today none of that pre-approval experience exists, because Onboarding v2
deliberately removed the password step: `/asclepius/finish` creates the account
`pending` with `NO_PASSWORD_HASH` and explicitly mints no session
(`backend/routers/onboarding.py`, lines 1723-1733 and 1802-1815, verified). The
practice case therefore runs post-approval as the first-run tutorial, its result
reaches nobody, and there is a standing endpoint that leaks the internal score
to the physician's own session.

## Decisions

Locked in planning (do not relitigate):

- The internal score (tiering proposal and contributor score) is never shown to
  a physician, anywhere, in any form.
- Never-collect credential fields stay never-collect
  (`backend/asclepius/tiering.py` FORBIDDEN_CREDENTIAL_KEYS, lines 120-127).
- Pre-approval access is exactly the TUTORIAL and BROWSE surfaces.
- Staged serial PRs; this PRD is implemented inside PR-1.

Decided here, with reasons:

**D1. Pre-approval sessions use magic links, not a password at application.**
The applicant gets a session token at submit, and return visits use an emailed
single-use sign-in link. We weighed the alternative (restore a password step to
the wizard) and rejected it:

- v2 §2 deliberately deleted the password screen; name, email and specialty are
  the whole requirement (`routers/onboarding.py` line 1698). Re-adding a step
  taxes every applicant to serve the minority who return before a decision.
- The security reasoning at approval (`routers/asclepius_verify.py` lines
  628-663, verified) is that a durable credential should not exist before it is
  needed and should never sit in an inbox as plaintext. A magic link is
  short-lived and single-use, so an inbox breach months later yields nothing.
  A password chosen at application is a durable credential for an account we
  may reject.
- The `NO_PASSWORD_HASH` sentinel (`asclepius/store.py` lines 124-129) is what
  the resume logic and `provision_user` use to mean "v2 applicant". A password
  at application would overload that meaning across three call sites.
- The code already concedes the identity argument: the session-minting comment
  at `routers/onboarding.py` line 1799 notes the onboarding token and the
  mailbox OTP have proved who this is.

Mechanics: submit returns a session token exactly as the non-deferred path does
today. For return visits, the sign-in page offers "email me a sign-in link"
whenever the account matches `password_is_unset()`. Tokens are single-use,
15-minute expiry, stored as a SHA-256 hash (the `ingest_upload_links` pattern,
`store.py` line 806), and the response is identical whether or not the address
has an account, so the door does not enumerate. Approval keeps minting the
temporary password exactly as it does now; nothing in that flow changes.

**D2. PROVISIONAL narrows to {TUTORIAL, BROWSE}.** `capabilities._BY_ACCESS`
currently grants PROVISIONAL six surfaces including community write, earnings
and referral (`asclepius/capabilities.py` lines 152-157). That was reasoned for
a world where signup included a chosen password and no meeting decision existed.
The meeting decision is explicit: view-only dashboard plus one practice case,
nothing else. Community write from unvetted accounts is also the exact exposure
the vetting queue exists to prevent. The change-of-mind comment above
`_BY_ACCESS` gets rewritten to record this reversal and why.

**D3. The practice case becomes a pre-review signal, not a hard wall in front
of the founders.** The queue badges it and the nudge chases it, and the queue's
default filter is "ready for review" (credentials evidence present and practice
case done) with an explicit toggle to see everyone. We do not hard-block the
decision buttons: the grading ledger is client-declared (a pedagogy gate, not
an authz boundary, per `tutorial_case.py` lines 54-57), and founders must stay
able to reject an obviously bad application, or approve a known colleague,
without waiting on it.

**D4. The tiering signal is one new principled feature, not a bonus.** See R7.
We add `practice_first_pass` to `tiering.FEATURES` as a ninth scored feature: a
capped binary (1.0 only when the first attempt passed), prior (0.4, 4.00), a
weight row in `tiering_weights`, updated by the same learning batch under the
same guardrails as every other feature (MAX_DELTA_M, Q_MAX), and unable to open
a hard gate. Not continuous in attempts: retry count measures interruption and
UI familiarity as much as judgment, and the gate forces an eventual pass anyway,
so only the first attempt carries information. We acknowledge the cost: FEATURES
says eight is a deliberate events-per-variable budget (lines 83-97); we spend
one slot on the only work sample most applicants will have, which is the same
argument that admitted `calibration_z`.

**D5. The score-leak endpoint is removed, not re-gated.** `GET
/api/asclepius/score` is session-scoped ("my score"), so an admin-only version
of the same path is meaningless. The admin surface already exists
(`GET /api/asclepius/admin/scores/{user_id}`). The physician route's stated
reason to exist (the dashboard renders the in-review state from it) is stale:
`/me/profile` already dropped score and band deliberately
(`routers/asclepius.py` lines 680-690) and no frontend file references the
route (verified by grep; only tests do).

**D6. Re-tiering is an admin decision assisted by score bands, never
automatic.** The endpoint records who, why, and the score at the time; a band
crossing surfaces a candidate, it does not move anyone. Promotion to reviewer
still re-checks the tiering hard gates, because the score cannot open a gate.

## Requirements

R1. A clinical v2 applicant receives a session at `/asclepius/finish` and lands
    on the dashboard in its applicant state. Non-clinical account kinds
    (advisor, referrer) keep today's behavior.
R2. An applicant with `password_is_unset()` can request a sign-in link by
    email; the link signs them in exactly once, expires after 15 minutes, and
    the request response does not reveal whether the account exists. Accounts
    with a password are directed to the password door (no link offered).
R3. A PROVISIONAL session reaches only TUTORIAL and BROWSE surfaces. Every
    other surface returns the existing denial. The dashboard renders review
    status, the practice case entry, and the profile; community, earnings,
    referral and real-work chrome do not render.
R4. The practice case is playable pre-approval end to end, grading exactly as
    today (`grade_tutorial_submission`), writing `tutorial_json` including the
    gate sub-object, and never touching the `tasks` table.
R5. The admin queue row and the dossier each carry a `practice_case` block:
    state (locked, passed, grandfathered), attempts, matched count of 4,
    planted-finding hit, first-attempt pass, passed version, and timestamp.
    The queue exposes the "ready for review" filter from D3.
R6. No response reachable by a physician session contains the tiering score,
    contributor score, band, components, or the practice-case matched count
    (the result screen keeps explaining findings without a numeric score, as
    `tutorial_case._headline` already mandates).
R7. `practice_first_pass` exists as a FEATURES row per D4, appears in
    ALL_WEIGHT_NAMES, moves under the learning update within MAX_DELTA_M and
    Q_MAX, is encoded 1.0 only on a first-attempt pass, and every PINNED_ZERO
    row still moves by exactly 0.0 in the same batch.
R8. `GET /api/asclepius/score` no longer exists; `/admin/scores/{user_id}`
    is unchanged.
R9. 24 hours after submission, an applicant missing credential evidence (no CV
    sha and no NPI or registration number) gets one credentials nudge, ever; an
    applicant whose practice gate is not passed gets one practice-case nudge,
    ever. Both use claim-first stamps (R10) and send nothing when no mail
    transport is configured, without stamping.
R10. Each new nudge kind has its own stamp, claimed by a conditional UPDATE
    before sending, so a restart or a racing worker cannot double-send
    (mirrors `onboarding_nudge.py` and `stamp_onboarding_nudge`).
R11. `POST /api/asclepius/admin/retier/{user_id}` (admin only) takes
    `{tier, note}`; validates tier against `capabilities.TIERS`; requires a
    non-empty note; refuses promotion to reviewer when tiering hard gates
    fail; writes `users.tier`; logs a `tier_changed` event with actor, old and
    new tier, note, and the contributor score and case count at that moment.
R12. `GET /api/asclepius/admin/retier-candidates` lists labelers with a stored
    contributor score at or above `REVIEWER_BAND_MIN` (70) and at least
    `MEASURED_QUALITY_MIN_TASKS` (20) cases.
R13. A labeler-to-reviewer promotion sends one email naming the new role and
    what changes; demotion sends nothing automated (that is a conversation).
R14. No em dashes in any new user-facing copy.

## What exists today (verified against the working tree)

- `backend/routers/onboarding.py`: `asclepius_finish` (line 1670) creates the
  account with `NO_PASSWORD_HASH` (lines 1723-1733) and skips the session when
  credentials are deferred (lines 1802-1815). OTP, resume, and the magic-link
  machinery for the HS door already live in this file (module docstring line 1).
- `backend/routers/asclepius_verify.py`: approval mints the temporary password
  with the audit-logged, never-in-payload discipline (lines 628-663);
  `_queue_row` (line 230) and `verification_dossier` (line 345) are where the
  practice-case block lands.
- `backend/asclepius/capabilities.py`: PROVISIONAL access level (line 117),
  `_BY_ACCESS` (line 152), surfaces TUTORIAL and BROWSE (lines 125-126), and
  the practice gate as a third axis with GATE_LOCKED, GATE_PASSED,
  GATE_GRANDFATHERED (lines 254-344).
- `backend/asclepius/tutorial_case.py`: deterministic grading, TUTORIAL_VERSION
  and TUTORIAL_PASS_MIN_VERSION (lines 32-43), PASS_MIN_MATCHED 3 of 4 plus the
  required sound-answer finding (lines 70-71), no-score result headline.
- `backend/asclepius/tiering.py`: FEATURES with priors and the eight-feature
  budget rationale (lines 86-97), `calibration_z` (1.1, 4.00), PINNED_ZERO with
  PINNED_PRECISION 1e6 (lines 112-116), FORBIDDEN_CREDENTIAL_KEYS (120-127),
  MAX_DELTA_M 0.25 and Q_MAX 400 (lines 152-160), hard gates the score cannot
  open (from line 163).
- `backend/asclepius/onboarding_nudge.py`: the 24h and day-6 pre-submit nudges,
  stamp-then-send idempotency, batch cap 50, 900 second sweep interval, riding
  the verification agent loop.
- `backend/routers/asclepius_score.py`: `my_score` (line 30, BROWSE-gated, the
  leak) and `admin_score` (line 67).
- `backend/asclepius/contributor_score.py`: REVIEWER_BAND_MIN 70,
  LABELER_BAND_MIN 30 (lines 81-82), `band_word`, `prior_for`, `compute`.
- `backend/onboarding_emails.py`: the approved email deliberately carries no
  promotion sentence and names itself as where one goes if promotion is ever
  built (lines 1113-1116).
- `backend/asclepius/store.py`: `NO_PASSWORD_HASH` (line 124),
  `password_is_unset` (line 127), `Store._migrate` (line 794).

## Gaps and changes, per file

- `backend/routers/onboarding.py`: mint the session for clinical v2 applicants
  at finish (drop the `credentials_deferred` guard on session creation only;
  the deferred-credentials email flow stays). New `POST
  /api/asclepius/signin-link` and `POST /api/asclepius/signin-link/exchange`
  (or GET landing that posts the exchange), rate-limited like the other
  onboarding doors.
- `backend/asclepius/store.py`: schema changes follow the `_migrate` pattern
  exactly: inside `Store._migrate()`, read columns with the local
  `cols("table")` PRAGMA helper, then guard each additive migration as
  `if "col" not in cols("users"): conn.execute("ALTER TABLE users ADD COLUMN
  ...")`, idempotent at boot, additive only (see lines 1088-1098 for the
  canonical shape). New columns: `users.nudge_credentials_sent_at TEXT`,
  `users.nudge_practice_sent_at TEXT`; new table `signin_links` (token_hash
  unique, user_id, expires_at, used_at) following `ingest_upload_links`. New
  store methods: `stamp_applicant_nudge(user_id, kind)` (conditional UPDATE,
  returns whether this call claimed it),
  `list_applicants_needing_nudge(kind, older_than_hours, limit)`,
  `create_signin_link`, `consume_signin_link`, `set_user_tier(user_id, tier)`.
- `backend/asclepius/capabilities.py`: `_BY_ACCESS[PROVISIONAL] =
  frozenset({TUTORIAL, BROWSE})` plus the rewritten rationale comment.
- `backend/asclepius/tiering.py`: add `practice_first_pass` to FEATURES;
  encoder reads it from the user row's `tutorial_json` gate sub-object, never
  from credential keys; the empty-intersection assertion against
  FORBIDDEN_CREDENTIAL_KEYS (line 723) keeps holding.
- `backend/routers/asclepius_verify.py`: `_queue_row` and the dossier gain the
  `practice_case` block (from `capabilities.practice_gate` plus
  `_tutorial_blob`); queue endpoint gains the `ready` filter param.
- `backend/routers/asclepius_score.py`: delete `my_score`; keep `admin_score`.
- `backend/asclepius/onboarding_nudge.py`: extend `sweep()` with the two
  post-submit kinds, same claim-first order, same batch cap, same
  transport-not-configured early return.
- New `backend/routers/asclepius_retier.py` (or extend `asclepius_verify.py`):
  the R11 and R12 endpoints.
- `backend/onboarding_emails.py`: `build_credentials_nudge_email`,
  `build_practice_case_nudge_email`, `build_tier_promotion_email`; the
  promotion sentence lands where lines 1113-1116 reserved it.
- `frontend/asclepius/asclepius.js`: applicant dashboard state (review status
  card, practice case entry, profile); hide non-granted rails from PROVISIONAL
  sessions; admin queue badge, filter toggle, dossier block, and a re-tier
  affordance on the physician detail view.

## Email and notification touchpoints

- Credentials nudge (24h post-submit, once ever).
- Practice-case nudge (24h post-submit, once ever).
- Sign-in link email (on request, single-use).
- Promotion email (labeler to reviewer only).
- Unchanged: submitted confirmation, approval welcome with temporary password,
  rejection notice, pre-submit 24h nudge and day-6 expiry warning.
- All sends go through `email_utils.send_html_email` and respect
  `is_email_transport_configured()`.

## Test plan

Repo style throughout: plain test functions, docstrings that explain WHY the
behavior must hold, asserting what a physician or admin would observe.

- Extend `backend/tests/test_onboarding_v2.py`: finish returns a session for a
  clinical applicant; advisor and referrer doors unchanged; the line-936
  assertion about `/api/asclepius/score` flips to expect the route gone.
- New `backend/tests/test_applicant_session.py`: magic-link request, exchange,
  single-use, expiry, non-enumeration, password-holder gets no link.
- Extend `backend/tests/test_tier_capabilities.py`: PROVISIONAL grants exactly
  TUTORIAL and BROWSE; every other surface denies.
- Extend `backend/tests/test_asclepius_tutorial.py`: pre-approval play-through
  writes the gate and never a `tasks` row; result screen still scoreless.
- Extend `backend/tests/test_tiering_score.py` and
  `backend/tests/test_tiering_learning.py`: `practice_first_pass` encodes 1.0
  only on first-attempt pass, moves within guardrails, and PINNED_ZERO rows
  move by exactly 0.0 in the same batch; varying forbidden keys still changes
  the score by exactly 0.0.
- Rewrite `backend/tests/test_score_router.py`: physician route gone for every
  session shape; admin route intact.
- New `backend/tests/test_applicant_nudges.py`: due lists, claim-first stamps
  under a simulated race, once-ever, and no stamp without a transport.
- New `backend/tests/test_retier.py`: validation, note required, hard-gate
  refusal, event payload contents, candidate banding at 70 and 20 cases,
  promotion email fired exactly once.
- Extend `backend/tests/test_verify_queue` equivalents (dossier tests): the
  `practice_case` block and the ready filter.

## Out of scope

- Contract e-sign, the Gunderson agreement document, W-9 and 1099 (PR-3).
- Stripe rails (PR-3), community activation (PR-2), admin surface separation
  (PR-4).
- The profile, verified card, completeness nudges and referral trim (group B,
  `prd-physician-profile.md`).
- Any automatic tier change; the learning loop keeps proposing, humans keep
  deciding.
- Changes to the calibration exam (`calibration.py`); it remains a separate,
  richer instrument.
