# PRD: Admin surface separation and redesign (group F, PR-4)

Built LAST in the train, deliberately: it rewires what PR-1 through PR-3
touched and moves final code once, and it builds ON the admin-tasks redesign
that just landed on main (`docs/asclepius/ADMIN_TASKS_REDESIGN.md`), not
against it.

## Problem (from the meeting)

The admin panel must become separate from the product: its own surface, its
own admin credentials, currently intertwined. The current admin UI is
described as rage-inducing and gets a full redesign, framed as "the same as
the product but flipped". The hard constraint stated in the meeting: the
backend must not break.

Concretely today: the physician app ships the entire admin console to every
visitor. `frontend/asclepius/index.html:50-53` loads `admin_physicians.js`,
`admin_health.js`, `admin_export.js` and `admin_earnings.js` unconditionally,
and `asclepius.js` carries the admin shell (`renderAdminView` and the section
glue, around line 8776) plus the role branch that decides who sees the Admin
console button (line 261: `role === 'admin' || role === 'qa_reviewer'`). Every
physician downloads roughly 3,600 lines of admin code they can never run, and
every admin works inside a physician app wearing a different hat.

## Decisions

**Locked (founder meeting + planning session):**

- Separate admin entry with admin credentials; physician app stops shipping
  admin code; backend endpoints unchanged.
- Claude-in-admin resolved: **no embedded chat.** Admin workflows stay
  scriptable via clean endpoints, driven by Claude Code and skills from
  outside the product. An embedded assistant would be a second admin surface
  to secure and redesign; the endpoints already are the automation surface.
- Redesign follows the meeting's "product flipped" model, detailed per tab
  below.

**Made here, with rationale:**

- **F1. Same-origin second page, not a second app.** New route
  `GET /asclepius/admin` in `backend/main.py`, serving
  `frontend/asclepius/admin.html`, exactly the pattern of the existing
  `/asclepius` handler (`main.py:2744-2752`). No build system exists in this
  repo and none is introduced: admin.html is plain script tags in dependency
  order, sharing `_tokens.css`, `_base.css`, `clinical-fonts.css` and
  `admin.css` with the product so the two surfaces stay one design system.
- **F2. The shell moves, the sections stay.** The four admin bundles already
  have clean seams (`window.AdminPhysiciansSection` etc., mounted by
  asclepius.js). What moves out of `asclepius.js` into a new
  `admin_shell.js` is the shell only: tab state, `ADMIN_TAB_ALIASES`, the
  admin context object (`openBatchesFor`, `openPipeline`,
  `openPhysiciansSub`), `renderAdminView`, `adminSubnav`, and the admin-only
  section renderers. The bundles keep their contracts, so the redesign
  iterates inside sections without re-plumbing the shell.
- **F3. Security stays server-side; separation is hygiene, not a wall.**
  `admin.html` is static and public like `index.html`; every admin API call
  is already gated by `require_admin` (or the payments router's admin
  dependencies), and that does not move an inch. The page boots by calling
  `/me`: no session or a non-admin session renders the sign-in gate (same
  auth endpoints as the product). Removing admin JS from the physician bundle
  is bundle hygiene and surface clarity; the authorization boundary was and
  remains the server.
- **F4. State keys and deep links survive.** `ADMIN_TASKS_REDESIGN.md` is
  explicit that the `tasks`/`assign` state keys and the `work`/`money` keys
  are load-bearing across aliases, subnav lookups and the physician-row
  route-in, and that renaming them is silent breakage for zero benefit. The
  moved shell preserves every key and alias byte-for-byte.
- **F5. qa_reviewer keeps its door.** The role branch admits `qa_reviewer` to
  the console today; the new page admits the same two roles. Capability
  differences (the Evaluate chooser's `review` check) remain enforced where
  they are enforced now.
- **F6. Evaluate stays in the product.** An admin previewing the physician
  experience is using the physician surface, which is the point of the
  chooser. The product's "Admin console" nav button becomes a plain link to
  `/asclepius/admin`; the admin page links back to the product for Evaluate.
  Duplicating the evaluation surface into admin.html would be the exact
  intertwining this PR removes, in the other direction.
- **F7. Community composer is linked, not duplicated.** PR-2 builds the admin
  persona composer as its own endpoint and surface. The admin page's
  Community tab embeds that surface by mounting the same module; it does not
  reimplement posting. One composer, one place to get it wrong.
- **F8. Redesign is executed with the `frontend-design` skill loaded** (per
  the execution plan) and lands as changes INSIDE the section bundles, on top
  of the just-landed Tasks redesign. The Tasks pages (Data & Task Creation,
  Task Routing) move across functionally unchanged: they were redesigned
  days ago with their own documented reasoning, and redesigning the redesign
  is churn, not progress.

**"The product but flipped", concretely per tab:**

| Tab | Product surface | Flipped admin meaning |
|---|---|---|
| Physicians | the physician's own verified card + profile (PR-1) | the same card as the unit of a gallery: every physician rendered as their verified card (photo, name, specialty, tier word, advisor badge), with filters over the roster counts, drill-in to the dossier, case history, roster metrics (median time, agreement, from PR-3), and their ledger |
| Tasks | the physician's queue and case player | the sender's side of the same queue: the redesigned Data & Task Creation and Task Routing pages, preview through the exact serve-path renderer, QA and Metrics beside them |
| Money and Metrics | the physician's earnings page and referral card | the payer's side of the same ledger: outstanding by physician, held rows, pay and void, plus the referrals BOOK (`/admin/referrals`: who referred whom, funnel state, fraud flags) as the flip of the physician's referral funnel |
| Data | the partner upload door | what came through the door: ingest pipeline, export, brokering, and the PR-2 lead view (every `/partner` submission, the legal audit trail of authority attestations) |
| Community | the member's channels | the same channels wearing the Archangel persona: the PR-2 composer, pinned explainers, case rooms (admin-visible per the group-D PRD) |

## Requirements

- **R1.** `GET /asclepius/admin` serves `admin.html` with the same headers and
  static-serving behavior as `/asclepius`. No new auth on the HTML itself
  (F3).
- **R2.** `admin.html` loads, in order: shared CSS, `admin.css`,
  `manual-content.js` if the shell needs shared helpers, `admin_shell.js`
  (new), then the four admin bundles, then the PR-2 composer module. It does
  NOT load `first_run.js`, `earnings.js`, `review.js`, `case_panel.js`
  (except where the Routing preview requires the case renderer; if so, that
  one shared module is loaded knowingly and documented in admin.html's own
  comments, which follow index.html's annotated-script-tag style).
- **R3.** `index.html` drops the four admin script tags (lines 50-53).
  `asclepius.js` drops `renderAdminView`, the admin section renderers, the
  admin context, and the admin tab state; the role branch keeps only the
  Evaluate chooser and replaces the console button with a link to
  `/asclepius/admin` (F6).
- **R4.** A physician session opening `/asclepius/admin` sees the sign-in
  gate or an "admin credentials required" screen after `/me`; no admin DOM
  is ever mounted for a non-admin session. (The API would 401 anyway; the
  page just says so honestly instead of rendering dead furniture.)
- **R5.** Backend contract freeze. The redesign consumes these endpoints
  UNCHANGED (verified present in the working tree; the freeze is the "backend
  must not break" instruction made testable):
  - `routers/asclepius_admin.py`: `/physicians` (776), `/physicians/{user_id}`
    (846), `/signups` (1323), `/health-systems` (1939),
    `/health-systems/{hs_id}` (1688), `/health-systems/{hs_id}/payouts`
    (2972), `/health-systems/{hs_id}/invoices` (3369),
    `/health-system-signups` (2857), `/batches` (2232), `/batches/{batch}`
    (2243), `/batches/relay/{trajectory_id}` (2363), `/batches/relay/
    {trajectory_id}/reassign` (2385), `/batches/preview/{task_id}` (2476),
    `/assignments` (2778), `/agreements/{agreement_id}/document` (3305),
    `/export/case-options` (230), `/export/case-preview` (241),
    `/storage/reconcile` (316), `/metrics/questions` (120), plus the
    ingestion review/promote surface the Tasks pages already call.
  - `routers/asclepius_payments.py`: `/admin/earnings` (438),
    `/admin/earnings/held` (756), `/admin/earnings/{id}/release` (775),
    `/admin/earnings/{id}/void` (830), `/admin/earnings/pay` (908),
    `/admin/earnings/mark-paid` (579), `/admin/referrals` (535),
    `/admin/earnings/{id}/case-export` (724).
  - Everything PR-2 and PR-3 add for leads, composer, roster metrics and case
    rooms, as landed.
  No path, verb, request shape or response shape changes in PR-4. New UI
  needs met by new endpoints go in their own PR, not smuggled here.
- **R6.** Information architecture: five tabs: Physicians, Tasks, Money and
  Metrics, Data, Community, per the flipped-product table above. Internal
  state keys stay `physicians`/`work`/`money`/`data` plus new `community`
  (F4); labels are display-only, exactly as `renderAdminView` does today.
- **R7.** Physicians tab becomes the verified-card gallery (drill-in
  unchanged: the existing dossier view). The Pending queue keeps its
  name-and-specialty austerity; the redesign spends its budget on the roster,
  where operators live.
- **R8.** The QA pending badge, `openBatchesFor`, `openPipeline` and
  `openPhysiciansSub` cross-links all survive the move and are exercised by
  tests (they are the routes operators actually navigate by).
- **R9.** Playwright visual tests updated deliberately, not incidentally:
  `backend/tests/test_asclepius_visual.py` gains the admin page as a driven
  surface, and its two general guards (off-palette paint,
  capitalize-mangling) run against it. Per that file's own warning, any new
  guard is mutation-checked against the defect it claims to catch before it
  lands; a vacuous admin screenshot test is worse than none.
- **R10.** No embedded chat, no assistant panel, no LLM call from the admin
  frontend. The scriptability story is R5's frozen endpoints.

## What exists today (verified in the working tree)

- `frontend/asclepius/index.html:50-53`: all four admin bundles ship to every
  visitor, annotated as "PRD-C admin sections, own files, mounted by
  asclepius.js".
- `frontend/asclepius/asclepius.js:261`: `isAdmin = role === 'admin' ||
  role === 'qa_reviewer'`; `:8776` `renderAdminView` with four tabs
  (Physicians / Tasks / Money and Metrics / Data), alias table, load-bearing
  `work`/`money` keys, QA badge on the Tasks tab; admin context with
  `openBatchesFor` / `openPipeline` / `openPhysiciansSub` just above it.
- Bundle sizes: `admin_physicians.js` 1719 lines, `admin_health.js` 1247,
  `admin_earnings.js` 497, `admin_export.js` 163; sections expose
  `window.Admin*Section { render, reset }`.
- `backend/main.py:2744-2752`: the `/asclepius` HTML route this PR mirrors.
- `frontend/asclepius/admin.css` exists and is admin-specific already.
- `docs/asclepius/ADMIN_TASKS_REDESIGN.md`: the just-landed Tasks redesign,
  its hard no-deletion invariant, its state-key freeze, and its test style
  (renderers EXECUTED against the DOM shim, because this repo has twice
  shipped complete, correct, invisible sections).
- `backend/tests/test_asclepius_visual.py`: pixel-reading, mutation-checked
  visual gate; skips cleanly without Playwright.
- Legacy `frontend/admin.html` is CareGuide, unrelated; it is not touched and
  the new page is `frontend/asclepius/admin.html` to keep the namespaces
  apart.

## Gaps / changes per file

| File | Change |
|---|---|
| `backend/main.py` | R1 route for `/asclepius/admin` |
| `frontend/asclepius/admin.html` (new) | R2 entry page, annotated script tags |
| `frontend/asclepius/admin_shell.js` (new) | F2: shell moved from asclepius.js; sign-in gate; five-tab nav; state keys preserved |
| `frontend/asclepius/asclepius.js` | R3 removals; console button becomes a link |
| `frontend/asclepius/index.html` | R3 drop admin script tags |
| `frontend/asclepius/admin_physicians.js` | R7 card gallery on the roster tab; drill-in unchanged |
| `frontend/asclepius/admin_earnings.js` | flipped-ledger polish; referrals book surfaced |
| `frontend/asclepius/admin_health.js`, `admin_export.js` | Data tab composition incl. PR-2 lead view |
| `frontend/asclepius/admin.css` | redesign styles; tokens shared, no fork of the palette |
| `backend/tests/test_asclepius_visual.py` | R9 admin page coverage |
| `docs/asclepius/ADMIN_TASKS_REDESIGN.md` | one-line pointer that the pages now mount from admin_shell.js (behavior unchanged) |

## Email / notification touchpoints

None. This PR moves and redesigns UI; it sends nothing new. The admin page
surfaces existing queues (QA badge, held earnings, pending physicians) that
already have their own notification stories. Any admin-notification work is
out of scope here precisely because the backend is frozen.

## Test plan (plain pytest functions with WHY docstrings, repo style)

- `test_admin_separation.py`
  - `test_admin_page_served_and_physician_page_ships_no_admin_js`: WHY: the
    separation IS the deliverable; assert `/asclepius/admin` returns the new
    page and `/asclepius` HTML no longer references any `admin_*.js`.
  - `test_admin_shell_preserves_state_keys_and_aliases`: WHY: F4; the
    redesign doc records that renaming `work`/`money`/`tasks`/`assign` is
    silent breakage; assert the moved shell's key set is identical.
  - `test_non_admin_session_gets_gate_not_console`: WHY: R4; the page must
    tell a physician the truth rather than mount furniture whose every fetch
    401s.
- `test_admin_backend_freeze.py`
  - `test_every_frozen_endpoint_still_answers`: WHY: R5 makes "the backend
    must not break" mechanical; call each frozen route as admin and assert
    status and top-level response shape, in the exact spirit of
    `test_admin_tasks_redesign_endpoints_live.py`.
- `test_admin_shell_ui.py` (DOM shim, renderers EXECUTED, per the redesign
  doc's stated lesson that source-only frontend tests are blind to unmounted
  sections)
  - `test_all_five_tabs_mount_their_section`: WHY: this repo has shipped
    complete-correct-invisible twice; run the shell, click each tab, assert
    the section rendered.
  - `test_cross_links_survive_the_move`: WHY: R8; `openBatchesFor` from a
    physician row must land on Task Routing with the doctor preselected, in
    the moved shell.
  - `test_card_gallery_renders_advisor_and_tier_words`: WHY: the roster's
    standing vocabulary rules (tier as a word, advisor as a second badge)
    must survive the gallery redesign; regressing them is the documented
    quiet-wrong bug.
- Playwright (`test_asclepius_visual.py`): admin page under both guards (R9),
  plus one gallery screenshot assertion mutation-checked against a real
  defect (e.g. the card grid collapsing to a single column).
- Full 4-shard CI + the Playwright job green before merge, per the train's
  verification rules.

## Out of scope

- Any backend change beyond the one HTML route (R5 freeze).
- Separate admin ACCOUNTS or a second credential store: admin credentials
  mean the existing admin/qa_reviewer roles and server-side gates; an
  identity-provider story is its own future decision.
- Embedded Claude, chat, or any assistant UI (locked: no).
- Redesigning the Tasks pages' behavior (F8: they just landed with their own
  reasoning).
- A build system, bundler, or framework (the no-build constraint holds).
- The physician-facing product design (PR-1's territory).
- Mobile-specific admin layouts.
