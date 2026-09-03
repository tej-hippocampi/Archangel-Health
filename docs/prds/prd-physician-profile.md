# PRD: Physician profile and verified card

PR-1, group B. Companion to `prd-applicant-funnel.md` (group A, same PR).

## Problem

From the Sep 1 product meeting: a physician's profile should be extensive and
standardized, with a shareable "verified card" (checkmark, photo, name,
specialty, years of practice) that other doctors see in the community and that
a physician can share outward, the way a verified social profile is shared.
Below the card, the full profile and a history of labeled cases and
performance. The internal score stays internal. Skipped fields get one-question
nudge emails, because a richer profile powers task routing and pay. The
referral page works but is too wordy; cut it down without losing the
physician and health-system columns or any functionality.

Today we already collect most of the profile (languages, subspecialties,
residency, fellowships, board certifications, practice settings, years in
active practice) into `users.credentials_json` during onboarding, and then
never show any of it back: `GET /me/profile` surfaces only qualification and
degree from that blob. There is no card, no case history on the profile, and
no completeness loop.

## Decisions

Locked in planning (do not relitigate):

- The internal score is never shown to the physician and never on the card.
- The never-collect list stays never-collect: medical school, graduation year,
  date of birth, sex, IMG status, practice region, self-rated expertise
  (`backend/asclepius/tiering.py` FORBIDDEN_CREDENTIAL_KEYS lines 120-127, and
  the type-level comment in `landing/src/app/components/onboarding/steps.tsx`
  lines 171-183). Education is deliberately not collected, for fairness and
  legal reasons, so the card shows practice facts, not pedigree. The meeting's
  "where they studied" ask is overridden by this standing position.
- No em dashes in any user-facing copy.

Decided here, with reasons:

**D1. The card carries exactly: verified checkmark, avatar, name, primary
specialty, years in practice, country of practice.** Practice facts only.
Nothing from the never-collect list, no organization requirement (optional
display), no email, no NPI or registration number (identifiers invite scraping
and impersonation on a public URL), and never a score or band.

**D2. Sharing works through a tokenized public card URL whose page carries a
server-rendered share image.** We considered image-only sharing and rejected
it as the primary mechanism: a bare image cannot be verified (anyone can edit
a PNG), while a URL on our domain is self-authenticating, revocable, and
gives crawlers something to unfurl. The URL is minted by the physician
(opt-in), unguessable, and revocable; the token is stored as a SHA-256 hash,
never raw, following the `ingest_upload_links` convention
(`backend/asclepius/store.py` line 806). The page serves OG tags whose
`og:image` points at a server-rendered PNG of the card (Pillow is already a
dependency, `backend/requirements.txt` line 36, and `asclepius/avatar.py`
already does image work). OG implications we accept and note: link previews
are cached by third parties, so revoking a token stops the page but not
copies of the preview image; that is tolerable precisely because of D1, since
the card contains only what the physician chose to publish about their
practice. Approval-gated: only `verification_status == approved` accounts can
mint, because the checkmark asserts we verified them.

**D3. Community member views render the same card block from the same
serializer.** One card shape, one place that decides its fields, so the
community view and the public page cannot drift into showing different facts.
The existing community helpers (`_initials`, `specialty_accent` in
`community/router.py`, already reused by `/me/profile` for the avatar block)
stay the single source for avatar rendering.

**D4. The performance panel shows counts and streaks only.** Total cases,
reviews, last-7-days, a monthly history, and a day streak, all derivable from
`submissions` timestamps. No kappa, no acceptance rate, no quality signal:
those feed the internal score, which is internal. The `/me/stats` docstring
today says streaks do not exist in the schema; we compute the streak from
`submissions.created_at` at read time rather than adding state.

**D5. Completeness nudges are one question, rarely.** One email asks exactly
one missing field, at most one profile nudge per 30 days, and each field is
asked at most once ever (stamped, claim-first), inside the meeting's 1-6 month
cadence. We reuse the `onboarding_nudge` idempotency discipline rather than
building a second scheduler.

**D6. Referral terms stay at $50 referrer and $25 referred.** *(Amended during
implementation. This paragraph originally pinned the reverse, $25 referrer and
$50 referred, per one line of the Sep 1 meeting. The flip was built and then
reverted by founder decision; what follows is the settled position.)*

The code pays the referrer $50 (`ASCLEPIUS_REFERRAL_BOUNTY_CENTS` default 5000,
`backend/asclepius/payments.py` line 230) and the referred physician $25
(`ASCLEPIUS_REFEREE_BONUS_CENTS` default 2500, line 241), and it keeps doing so.

The transcript is ambiguous rather than clear. It says "the people who refer get
a free $50", which matches the code, and it closes by settling the signing bonus
at "$25 for completing the case", which also matches the code. One line in the
middle says the reverse. Two readings out of three, plus the amounts already
live on a referral page physicians have read, break the tie toward not silently
restating what people have been promised.

The product argument runs the same way. The referrer is the scarce input: a
well-connected physician who will spend their reputation introducing colleagues
is worth more than the marginal signup, and the ask has to be worth making. The
referred physician's $25 is the activation half, paid at the first accepted case
because that is where somebody stays or is never seen again.

Nothing about this is hard to revisit, and that is deliberate: both amounts are
env-configurable and both are stamped on the ledger row at accrual (lines
227-229), so a future decision moves future accruals and cannot restate a
payment already promised. The referral page renders amounts from the wire
(`payout_structure`), so the frontend follows whatever the backend says; the
hero's fallback constants (`frontend/asclepius/referral.js` lines 131-133) are
the only place a number is written twice, and they match the defaults.

## Requirements

R1. `GET /me/profile` returns, in the read-only credentials section, the
    already-collected fields: languages, subspecialties, specialty niche,
    residency rows, fellowship rows, board certifications, practice settings,
    years in active practice, plus everything it returns today. No key from
    FORBIDDEN_CREDENTIAL_KEYS ever appears in the response, even if a legacy
    blob carries one.
R2. The profile view (`renderProfileView`) renders those fields grouped as a
    training-and-practice panel; absent fields render as absent, not as
    placeholders that look like data.
R3. `GET /me/profile` includes `completeness`: a percent plus the ordered list
    of missing fields, computed from avatar, languages, subspecialties,
    residency or board certification evidence, practice settings, years in
    practice, LinkedIn, and specialty niche. The dashboard and profile render
    it as a quiet meter, not a demand.
R4. An approved physician can mint, re-mint, and revoke a public card URL.
    Minting returns the full URL once; re-minting invalidates the old token.
    Pending, rejected and non-physician accounts get 403.
R5. The public card page renders D1 fields only, over the account's live
    state: revoking the token or un-approving the account kills the page. It
    carries OG title, description and image tags; the image endpoint returns a
    PNG of the card rendered server-side from the same serializer.
R6. The card block appears on community member views for approved physicians,
    from the same serializer as R5.
R7. `GET /me/stats` returns counts and streaks only: totals, last-7-days,
    reviews completed, last submission time, current day streak, and a
    12-month count series. No field of the response derives from grading,
    agreement, or the contributor score.
R8. The own-profile page shows the labeled-case history panel built from R7.
R9. Profile-completeness nudge emails ask exactly one question, name exactly
    one field, link to the profile, respect the 30-day spacing and the
    once-ever-per-field stamp, and send nothing when the profile is complete
    or no mail transport is configured (without stamping).
R10. The referral page above the fold is: one-line hero, the two terms as
    single lines ($50 to you, $25 to them, per the amended D6), and the
    copy-link row.
    The two-column physician and health-system layout, invite composer,
    funnel list, share targets, and the health-system note card all keep
    working. Roughly six prose blocks go: the hero subtitle paragraph, the
    "No ceiling" term block, the physician column pitch paragraph, the
    health-system column pitch collapses to one line, the "a founder reads
    every one" footer, and the equity footnote collapses to one line (it must
    survive in some form; it prevents the terms reading as a promise to
    equity-compensated accounts).
R11. Payout defaults stay as production already promises them, per the amended
    D6: $50 referrer and $25 referred, in `payments.py` and in the
    `referral.js` fallbacks alike. Ledger history is untouched, as it would
    have been either way, because both amounts are stamped at accrual.
    Asserted in `backend/tests/test_referral_payout.py`: the two defaults, both
    halves accruing at those amounts through the real trigger, and a rate
    change moving future accruals only.
R12. No response reachable by a physician session, and nothing on the card or
    its image, contains the internal score, band, or components.

## What exists today (verified against the working tree)

- `backend/routers/asclepius.py`: `GET /me/profile` (line 631) with editable,
  credentials, standing and avatar blocks; it parses `credentials_json` but
  surfaces only qualification and degree (line 676); the deliberate no-score
  comment (lines 680-690); `PATCH /me/profile` (line 718); `_avatar_block`
  (line 705); `GET /me/stats` (line 2687).
- `backend/asclepius/store.py`: `evaluator_self_stats` (line 6984) returns
  totals, this-week, last-at; its docstring notes no streak data exists in the
  schema. `Store._migrate` (line 794) with the `cols()` guard pattern.
- `landing/src/app/components/onboarding/steps.tsx`: the Credentials type
  collects qualification, degree, boardCertifications, fellowship, residency,
  primarySpecialty, specialtyNiche, subspecialties, practiceSettings,
  yearsInActivePractice, languages (lines 118-140); the never-collect comment
  block (lines 171-183) and the form-level note at line 2430.
- `backend/asclepius/tiering.py`: FORBIDDEN_CREDENTIAL_KEYS (lines 120-127).
- `backend/asclepius/avatar.py`: EXIF-stripped, content-addressed avatar
  pipeline using Pillow.
- `frontend/asclepius/asclepius.js`: `renderProfileView` (line 8103),
  `meIdentityCard` (line 8141), `meStanding` (line 8176), the me-grid panels.
- `frontend/asclepius/referral.js`: hero with subtitle and three term blocks
  (lines 130-160), `physicianCol` pitch (line 177), `systemCol` pitch and
  founder-reads footer (lines 463-492), wire-driven `payout_structure` with
  hardcoded fallbacks (lines 131-133), zero-innerHTML contract enforced by
  `test_no_innerhtml_and_no_long_dashes_in_the_copy`.
- `backend/asclepius/payments.py`: `referral_bounty_cents` (line 230),
  `referee_bonus_cents` (line 241), ledger stamping rationale (lines 227-229).
- `backend/asclepius/onboarding_nudge.py`: the stamp-then-send idempotency
  pattern and the verification-agent ride-along this PRD reuses.
- `backend/routers/asclepius_verify.py` lines 620-626: community welcome on
  approval, the hook point for surfacing the card to colleagues.

## Gaps and changes, per file

- `backend/routers/asclepius.py`: extend `my_profile` with the R1 fields (one
  new `_credentials_detail(creds)` helper that whitelists keys, so forbidden
  keys are excluded by construction rather than by filtering), plus the
  `completeness` block; extend `my_stats` per R7.
- New `backend/routers/asclepius_card.py`: `POST /me/card` (mint or re-mint),
  `DELETE /me/card`, `GET /api/asclepius/card/{token}` (public page JSON or
  server-rendered HTML with OG tags), `GET /api/asclepius/card/{token}/image`
  (PNG). Public endpoints rate-limited like the other public doors.
- New `backend/asclepius/card.py`: the single card serializer (D3) and the
  Pillow renderer for the share image.
- `backend/asclepius/store.py`: schema via the `_migrate` ALTER pattern, using
  the local `cols(table)` PRAGMA helper and guarded additive statements of the
  exact shape `if "card_token_hash" not in cols("users"):
  conn.execute("ALTER TABLE users ADD COLUMN card_token_hash TEXT")` (see
  lines 1088-1098 for the canonical instances). New columns:
  `users.card_token_hash TEXT`, `users.card_minted_at TEXT`,
  `users.profile_nudge_json TEXT` (per-field stamps plus last-nudge
  timestamp). New methods: `set_card_token`, `revoke_card_token`,
  `get_user_by_card_token_hash`, `monthly_submission_counts`,
  `stamp_profile_nudge(user_id, field)` (claim-first conditional UPDATE),
  `list_profiles_needing_nudge(limit)`.
- `backend/asclepius/onboarding_nudge.py`: a third sweep section for profile
  nudges (complete accounts skipped, 30-day spacing, once per field), same
  claim ordering and batch cap.
- `backend/onboarding_emails.py`: `build_profile_nudge_email(field, ...)`, one
  template parameterized by the single question.
- `frontend/asclepius/asclepius.js`: training-and-practice panel, completeness
  meter, card preview with mint, copy and revoke controls, history panel on
  the profile; community member view renders the card block.
- `frontend/asclepius/referral.js`: the R10 trim; swap the fallback constants
  per R11.
- `backend/asclepius/payments.py`: default swap per D6 (env still wins).

## Email and notification touchpoints

- Profile completeness nudge: one question, one field, links to the profile;
  at most one per 30 days per physician, each field once ever.
- No email on card mint or revoke (the physician did it themselves, in-app).
- Referral emails unchanged apart from the amounts they quote, which already
  come from `payout_structure`.

## Test plan

Repo style: plain test functions, docstrings explaining WHY, asserting what a
physician, a colleague, or an anonymous visitor would observe.

- Extend `backend/tests/test_own_profile.py`: R1 fields present; a poisoned
  legacy blob carrying `medicalSchool` or `gradYear` never reaches the wire;
  completeness math for empty, partial and full profiles.
- Extend `backend/tests/test_asclepius_me_stats.py`: streak and monthly series
  from crafted submission timestamps; no score-derived field in the response.
- New `backend/tests/test_verified_card.py`: mint, re-mint invalidates the old
  token, revoke kills the page, pending and rejected accounts cannot mint,
  anonymous fetch of a valid token gets D1 fields and nothing else, the image
  endpoint returns a PNG, OG tags present, forbidden and internal fields
  absent from page and image metadata alike.
- New `backend/tests/test_profile_nudges.py`: one question per email, per-field
  once-ever under a simulated race, 30-day spacing, complete profile sends
  nothing, no transport stamps nothing.
- Extend `backend/tests/test_referral_section_dom.py`: copy-link above the
  fold, single-line terms with the pinned amounts, cut prose absent, both
  columns and the note card still functional, and the existing
  no-innerHTML-no-long-dashes assertion still passing over the new copy.
- Extend `backend/tests/test_referral_payout.py`: flipped defaults accrue $25
  referrer and $50 referred on new rows while previously stamped rows keep
  their amounts.

## Out of scope

- Collecting any new never-collect field, or any education history.
- Profile picture upload changes (the avatar pipeline already exists).
- Task-routing changes driven by the richer profile (routing already reads
  specialty; deeper profile-aware routing is PR-3 territory).
- The referral bounty mechanics, anti-abuse, and funnel logic (unchanged).
- Health-system referral intros (`hs_enrich.py` work rides its own branch).
- Community channel features (PR-2) and admin redesign (PR-4).
- Printable or wallet-format card exports; the share image covers the ask.
