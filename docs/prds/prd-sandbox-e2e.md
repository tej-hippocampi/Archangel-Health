# PRD — Sandbox: testing the whole product end to end with mock identities

**Status:** proposed, nothing built
**Repo:** `tej-hippocampi/Archangel-Health`
**Written against:** `claude/revert-103-104` @ `d6a58eb`

---

## Context

Archangel is three auth planes plus an ops console, and no single test crosses them:

| Plane | Token | Store | Doors |
|---|---|---|---|
| **Asclepius** (the physician data/labeling marketplace) | own JWT, `ASCLEPIUS_AUTH_SECRET` | `asclepius.db` | `/asclepius`, `/community`, `/provider`, `/workspace`, `/partner/upload` |
| **Clinical / TEAM tenant** | `AUTH_SECRET` + tenant staff JWT | `team.db` | `/doctor/sign-in`, `/t/{slug}/sign-in` |
| **Community** | rides the Asclepius session | `community.db` | `/community` |
| **Ops console** | env creds, no DB row | — | `/admin` |

There are exactly three ways this product gets exercised today, and none is end-to-end in a real environment:

1. **253 pytest files** against a FastAPI `TestClient`. `backend/tests/conftest.py:41-48` forces `EMAIL_DEV_MODE=1` and `RATE_LIMIT_ENABLED=0` process-wide, so no test has ever seen a delivered email or a live throttle.
2. **The local walkthrough** (`WALKTHROUGH-LOCAL.md`) with `@demo.local` accounts and `EMAIL_DEV_MODE=1` — OTPs print to the uvicorn terminal. Fine for clicking through; it never proves an email arrives, renders, or that its link resolves.
3. **Signing up on production with a real address.** This is what is actually happening. The founder mailbox holds 17 threads from `noreply@archangelhealth.ai` — live verification codes, live `https://archangelhealth.ai/onboard/<token>` links, "Your Asclepius workspace is ready" for organisations named `your organization` and `northrisghe nephrolgy`. Those are test signups sitting in the production databases.

What is missing: **a real deployed environment, real email delivery, real browsers, a full cast of personas, and a repeatable script.**

Two failure classes are invisible locally and have already bitten: the provider-portal cookie is unconditionally `Secure` (`asclepius_provider.py:667`), so over plain HTTP login returns 200, the browser drops the cookie and the next call 401s; and CORS / `X-Forwarded-Proto` handling behind the TLS terminator (`http_security.py`). Only a real host tests either.

---

## Decision 1 — one mailbox, many addresses. Not ten Google accounts.

1. **Nothing authenticates against Google.** No SSO, no OAuth, no `GOOGLE_CLIENT_ID` in `backend/`, `landing/src/`, or `frontend/`. Every door is email-or-username plus password. Email is an identifier and a delivery channel. Ten Google accounts buy zero coverage.
2. **Plus-addressing is not collapsed.** Every identity key in every plane is `email.lower().strip()` and nothing more — `auth.py:196`, `team_store.py:1126`, `asclepius_admin.py:1446`. The per-address invite cap keys on the same exact string (`count_recent_pending_invites_for_email`, `onboarding.py:458`), so the addresses do not even share a quota.
3. **Google fights bulk creation** — phone verification per account, recovery-number reuse limits, a real chance the batch locks mid-run, and N browser profiles to check mail with.
4. **The mail is already readable programmatically**, which is what makes an email-gated flow scriptable at all.

## Decision 2 — a second address lane, on a catch-all domain

`asclepius/credentialing.py:547-561` classifies a signup domain `academic | business | consumer` and stamps `email_domain_class` on the user (`onboarding.py:1090`), feeding the tier score. `gmail.com` is in `CONSUMER_DOMAINS`. An all-Gmail cast never takes the business branch, and the health-system personas would be unrealistic besides.

So health-system identities get addresses on a **catch-all sandbox domain** forwarded to the same inbox. DNS is already on Cloudflare; Cloudflare Email Routing does free catch-all forwarding.

`.edu` cannot be minted and the constant must not be edited to fake it — `classify_email_domain` is documented as "one weight in a score, never a gate". Cover the academic branch with a unit assertion; run the sandbox on the business branch.

## Decision 3 — a separate Railway service, over HTTPS, with `ENV=production`

Not localhost (misses the `Secure`-cookie and CORS classes; emailed links must point at a real `BASE_URL`). Not production. A second Railway service off a pinned branch, own volume, own databases, own subdomain — plus a Vercel project for `landing/`.

Set `ENV=production` deliberately: six call sites compare `ENV` against that literal (`http_security.py:18`, `patient_session.py:42`, `onboarding.py:65`, `asclepius_provider.py:434,592`, `asclepius.py:5215`) and anything else silently relaxes. The point of the sandbox is to meet production behaviour, so take it — and satisfy the durability gate (`main.py:6340-6440`) with a real volume rather than dodging it.

---

## The cast

Twelve identities, not ten — ten undercounts. `asclepius/constants.py:27` alone holds five `ROLES`, and identity is **five orthogonal axes** on one row (`role`, `tier`, `account_kind`, `verification_status`, and the practice-case gate in `tutorial_json`). Twelve is the smallest set that puts a real signup behind every value the code can hold.

**Gmail lane** (`<inbox>+ax..@gmail.com`, all `email_domain_class=consumer`):

| # | Handle | Row state | Door | What only this one proves |
|---|---|---|---|---|
| 1 | `ax01.applicant` | `evaluator`, tier NULL, `pending` | `/join` | The waiting room. `POST /auth/login` answers 403 with header `X-Asclepius-Auth-Gate: pending`; PROVISIONAL surfaces (tutorial, browse, community read/write, earnings, referral) minus `real_work`; Tasks shown **locked**. Left pending most of the run, approved at the end to prove the transition. |
| 2 | `ax02.labeler` | tier `labeler`, `approved` | admin approve | Practice-case gate → draw a task → the **server-side blind commit** → grade candidates → `$75` accrues |
| 3 | `ax03.labeler2` | tier `labeler`, `approved` | admin approve | A second independent label completes the pair |
| 4 | `ax04.reviewer` | tier `reviewer`, `approved` | approve with `tier=reviewer`, or `POST /verify/tiering/{id}/decide` | Adjudication; the calibration exam; and the **billable review session** — `POST /sessions` → repeated `/heartbeat` → `/close`. There is no server stopwatch: credit is recomputed from durable heartbeat rows, so the journey must actually beat for the duration or `$100` never qualifies |
| 5 | `ax05.referred` | `evaluator`, `approved` | `/join?ref=<ax02 code>` | Attribution via `attach_link_signup` → `claim_referral_for_signup`; `$50`/`$25` settle **only after** verified *and* first case accepted |
| 6 | `ax06.advisor` | `account_kind='advisor'` | `/join?flavor=advisor` | The view-only ceiling — browse, tutorial, community **read**, earnings, refer. Critically: it **survives approval**; an admin clicking Approve changes nothing |
| 7 | `ax07.referrer` | `account_kind='referrer'` | `/join?flavor=referrer` | The narrowest ceiling: browse + referral only |
| 8 | `ax08.general` | `signup_flavor='general'`, `account_kind` NULL | `/join?flavor=general` | Relaxed credential screens but a **deliberately uncapped** account — in neither ceiling table |
| 9 | `ax09.buyer` | `role='buyer'` | auto-provisioned by an admin delivery | `/workspace`, `/buyer/deliveries`, authenticated download — and denial of the entire main API (`auth.py:388`) |
| 10 | `ax10.partner` | `role='data_partner'` | admin invite + temp password | `/provider` upload-only; denied the main API (`auth.py:381`) |
| 11 | `ax11.qa` | `role='qa_reviewer'` | operator-provisioned, no self-serve door | `require_qa`; the de-blind capability (`agreement.py:181`) |

**Catch-all lane** (`*@<sandbox-domain>`, `email_domain_class=business`):

| # | Handle | Door | What only this one proves |
|---|---|---|---|
| 12 | `director@…` | `/provider` → `POST /hs/signup` → 6-digit code | Health system end to end: username **derived from org name** (not email), intake → application → clickwrap DLA → org `active`. Upload needs **both** gates: `hs_access.can_surface(UPLOAD)` (account approved) **and** `hs_states.can_upload` (DLA signed) |
| 13 | `seat@…` | `POST /hs/members` by #12 | A colleague seat with its own credential, org fixed by session (cap `_HS_MAX_MEMBERS = 25`) |

Plus the **env admin** (`ASCLEPIUS_ADMIN_EMAIL`/`_PASSWORD`, re-applied every boot by `ensure_admin_from_env`), which is a seat, not a mock identity.

Mapping to the personas as originally described: *health systems* = #12/#13; *new users* = #1; *general users* = #8; *physicians* = #2–#4; *advisors* = #6; *referrals* = #5 + #7. **"Viewers" has no role of its own** — the read-only seats are the advisor ceiling (#6, covered by `tests/test_view_only.py`) and the buyer (#9). If a distinct viewer role is intended, it does not exist yet and is a product decision before it is a test.

Two constraints on how the cast is filled in, both easy to miss and both fatal to a section of the walkthrough:

- **Give at least three physicians the same specialty and the same country.** Community specialty and country rooms are threshold-gated at three members (`COMMUNITY_SPECIALTY_MIN_MEMBERS`, `COMMUNITY_COUNTRY_MIN_MEMBERS`, both 3). Scatter the cast across specialties and those rooms never appear, so the community half of the product looks broken when it is working exactly as designed. Make #1–#5 all nephrology, all US.
- **Names and identifiers must be alpha-only.** `tests/_asclepius.py:uniq()` is deliberately alpha-only because a run of digits trips the PHI scanner. `ax01`-style handles are fine as file labels but must not become the in-product name or organisation string.

Two features do not exist at HEAD and must not be planned around: the magic-link **applicant gate** (PR #104) and **health-system referral introductions** (PR #103) were both reverted — `653484c`, `d6a58eb`. Covering them is a merge decision first.

---

## The harness

```
sandbox/
  README.md
  personas.yaml       # the cast: address, lane, flavor, expected role/tier/account_kind
  mailbox.py          # IMAP: wait_for(to=, subject_contains=, since=, timeout=) + extract(regex)
  api.py              # httpx client for doors with no UI (admin approve, tier, provision, deliver)
  journeys/*.py       # one module per identity, Playwright
  run.py              # orchestrator, explicit dependency order + pacing
  reset.py            # rm the three DBs and reboot (there is no migration runner)
  report.py           # runs/<ts>/report.md + per-step screenshots
```

**Reuse what exists — do not write a seeder from scratch.** `backend/tests/_asclepius.py` already holds exactly the primitives this needs, and they are public for this reason:

- `make_user(store, role="evaluator", **kw)` — defaults `tier="labeler"` and auto-passes the practice case; `tier=None` gives the signed-up-not-approved state and `practice_case=False` gives the gated state; refuses to tier `data_partner`/`buyer`
- `pass_practice_case(store, user_id)` — public precisely so a test that builds a physician through the real approval flow can still open the gate
- `token_for(user)` / `headers_for(user)` — real Asclepius JWTs via `asclepius.auth.create_token`
- `fresh_store()`, `uniq()`

`api.py` wraps these for the admin-side steps that have no UI. And `tests/test_self_serve_end_to_end.py` already walks the wizard in the wizard's own order — it is the blueprint for journey #1, not something to reinvent.

**Mail transport is IMAP.** Playwright cannot call an MCP tool, so the automated lane uses Gmail IMAP with an app password (`SANDBOX_IMAP_USER` / `SANDBOX_IMAP_APP_PASSWORD`).

**Playwright is already installed** in `backend/.venv` with chromium; `tests/test_asclepius_visual.py` already uses it, and CI already runs a Chromium job. No new dependency.

**Persist `storage_state` per identity, and save every onboarding link.** This is not a nicety: an applicant has **no password** — `finish` stores `NO_PASSWORD_HASH` (`store.py:124`) and hands back a session token. Close the tab before approval and the only way back in is the onboarding link itself (or `POST /admin/signups/resend`). The password arrives in the approval email with `must_change_password=1`.

**Leave the honeypot empty and assert the token works.** A non-empty `company_website` on `/self-serve` or `/hs/signup` returns a **decoy 200 with a garbage token** that 404s later (`onboarding.py:441-461`). Browser autofill has tripped this before. Every journey asserts it got a *working* token, so a future honeypot change fails loudly instead of looking like a pass.

**Pacing is part of the design.** Keep `RATE_LIMIT_ENABLED=1` — production fidelity is the point — and pace the run against the real binding limit, `/api/onboarding/self-serve` at **5 per 10 min per IP** (plus 60/h global). Thirteen identities ≈ 25–30 minutes. One journey deliberately fires a sixth mint inside the window and asserts the 429. Documented escape hatch: `RATE_LIMIT_ENABLED=0`, needed only if the shared bucket makes the run non-deterministic — `client_ip()` takes the **last** `X-Forwarded-For` hop (`ratelimit.py:44-58`), which behind Cloudflare and Railway is an edge IP shared with other traffic.

---

## Phases

### Phase 0 — reserve the addresses (no code)

Gmail app password for IMAP. Cloudflare Email Routing catch-all on a sandbox domain forwarding to the inbox. A Gmail filter labelling everything from the sandbox sender — **this matters beyond testing**: the GTM reply-check and daily-brief routines sweep this same mailbox, and unlabeled sandbox mail would enter the outreach reply-check as if a prospect had written back.

### Phase 1 — stand up the service

New Railway service off a pinned branch, volume at `/data`. Note `backend/Procfile` says Railway builds with RAILPACK and uses the Procfile, not the Dockerfile.

Must differ from production:

| Group | Vars |
|---|---|
| URLs | `BASE_URL`, `LANDING_URL`, `PUBLIC_BASE_URL`, `ASCLEPIUS_PORTAL_URL`, `ALLOWED_ORIGINS`, `ALLOWED_HOSTS` |
| Storage | `TEAM_DB_PATH=/data/team.db`, `ASCLEPIUS_DB_PATH=/data/asclepius.db`, `COMMUNITY_DB_PATH=/data/community.db`, `ASCLEPIUS_DATA_DIR=/data` |
| Secrets | fresh `AUTH_SECRET`, `ASCLEPIUS_AUTH_SECRET`, `INTERNAL_TOOL_SECRET`, `ADMIN_PASSWORD`, `ASCLEPIUS_ADMIN_EMAIL`/`_PASSWORD`, `ASCLEPIUS_MOCK_PASSWORD` |
| Email | **separate SendGrid key/subuser**, `SENDGRID_FROM_EMAIL=sandbox@…` (verified sender) |
| Alert routing | `FOUNDER_NOTIFY_EMAILS`, `ASCLEPIUS_ADMIN_NOTIFY_EMAILS`, `LEAD_NOTIFY_EMAIL`, `ENTERPRISE_NOTE_EMAIL` |
| Behaviour | `EMAIL_DEV_MODE` **unset**, `RATE_LIMIT_ENABLED=1`, `ASCLEPIUS_TR_MIN_SECONDS=60`, `ENABLE_TRIAGE_DEMO=0`, `COMMUNITY_MORNING_ENABLED=0`, `COMMUNITY_NEWS_ENABLED=0`, `ASCLEPIUS_VERIFY_AGENT_ENABLED=0` |
| Vercel | `VITE_API_URL`, `VITE_DASHBOARD_URL` |

Four of those are load-bearing enough to call out:

- **`VITE_API_URL` is the single worst footgun.** Unset, a deployed landing silently falls back to `PROD_BACKEND_ORIGIN = "https://app.archangelhealth.ai"` (`landing/src/lib/auth-api.ts:20`) — the sandbox UI would sign the mock identities up against production, which is the exact failure this whole PRD exists to end.
- **Alert routing.** `FOUNDER_NOTIFY_EMAILS` falls through to a built-in founder pair (`notifications.py:39,62-88`), and nine event types fire founder alerts. Left alone, every sandbox run pages the founders.
- **`ASCLEPIUS_VERIFY_AGENT_ENABLED=0`.** The agent hits the live NPPES registry on a 30-second loop under a UA identifying `archangelhealth.ai`. Pointing a stream of synthetic NPIs at a public federal registry is not something to do casually; cover the agent with its own targeted test instead.
- **Digests.** `COMMUNITY_DIGEST_INTERVAL_SEC` defaults to 300, plus a 7am newsletter, a 24h application nudge and a day-6 expiry warning. Left on, thirteen real addresses accrue recurring mail indefinitely.

Also: `.env.example:325-330` claims nothing calls `load_dotenv()`. **That is stale** — `main.py:30-49` uses `dotenv_values` and non-empty values **overwrite** `os.environ`, so a `.env` baked into the image would beat every Railway dashboard variable. `.dockerignore` excludes `.env*` today; keep it that way. And do not point the `community-morning.yml` cron's `MORNING_BASE_URL` at the sandbox — it is a real email-sending cron.

Add one guard: a boot assertion that refuses to start when the sandbox marker is set and any DB path resolves outside `/data`. The failure to design against is a sandbox run writing into production.

Start the databases **empty**. There is no migration runner — each store's `_init_schema()` runs `CREATE TABLE IF NOT EXISTS` on first connect. Do not copy a local `team.db`; it runs to hundreds of MB.

### Phase 2 — skeleton

`mailbox.py`, `personas.yaml`, and journey #1 (`ax01.applicant`) end to end by hand, screenshots on. Read `docs/asclepius/PRODUCT_STATE.md` (shipped state) and `docs/asclepius/SIGNUP_DOORS.md` (the canonical door/ceiling reference) first; `HEALTH_SYSTEM_ONBOARDING.md` before #12.

Expect to write the browser layer from scratch: the only Playwright file in the repo is a pixel-level appearance gate, and the ~20 `*_dom*` files drive `asclepius.js` through a hand-rolled DOM shim in node. There is no user-journey E2E harness to extend. Two entry mechanics the journeys must implement rather than click past: community is reached only via `POST /community/handoff` → `/community?t=<code>` → redeem (single-use, short-lived), and its WebSocket needs `POST /api/community/ws-ticket`.

### Phase 3 — the remaining journeys

Dependency order: 1 → admin approve/tier → 2 → 3 (pair) → 4 (adjudicate) → 5 (referral off 2's code) → 6, 7, 8 (independent) → 12 → 13 (invited by 12) → 9, 10, 11 (admin-provisioned) → approve 1 last.

### Phase 4 — one command

`python3 sandbox/run.py --all` → `runs/<ts>/report.md`: per-identity PASS/FAIL, every email received with rendered subject and extracted token, a screenshot per step.

### Phase 5 (optional) — nightly

Same command on a schedule, with `reset.py` first.

---

## Verification

- `run.py --persona ax01` produces a real email in the inbox whose link opens the wizard **on the sandbox host** — check the link's domain, not just that it resolves.
- `ax06.advisor` still cannot reach `real_work` **after** an admin approves it. That assertion is the one that proves `account_kind` is a ceiling and not a starting state.
- After a full run the admin's Exports and Metrics shows one completed pair, one adjudication, non-zero earnings for #2/#3/#4 — and `κ` reads `null`, because n < 30 is the correct answer below `ASCLEPIUS_KAPPA_MIN_N`, not a bug. Do not lower the floor to make the report look fuller.
- The community specialty and country rooms for the nephrology/US cohort actually appear once the third member joins.
- **Money stops at the ledger, and that is the whole scope.** There is no Stripe: `ASCLEPIUS_STRIPE_ENABLED` describes an unbuilt PRD, `stripe` is not in `requirements.txt`, and the physician bank card is a disabled "coming soon". `mark_paid` records that something settled; it moves nothing. So the assertion is that the ledger rows are correct and idempotent on `(kind, ref_id)` — not that anyone got paid.
- The health-system upload door stays shut until **both** gates open, and opens when the DLA is signed.
- The existing suite still passes: `cd backend && python3 -m pytest tests/ -q`.
- Production is untouched: no rows for any `+ax` address, no founder alert mail from the run.

---

## Open items to settle in Phase 1

- **NPI.** Synthetic NPIs return `not_found` → `npi_verified=0` and `flagged=1`, which is never an auto-rejection and is itself a realistic path worth covering. The *verified* path needs one genuine NPI from the public registry. Decide per-identity; do not weaken the check to make the run green.
- **Which branch.** The tree is on `claude/revert-103-104` with ~20 sibling worktrees and the #103/#104 reverts on top. Pin the sandbox service to a named branch and say which, or the run tests something nobody can name.
