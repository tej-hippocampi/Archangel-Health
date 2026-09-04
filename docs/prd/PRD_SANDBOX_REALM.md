> **Provenance.** This is the PRD as delivered before the build (its `file:line`
> citations describe the tree it was written against, `Archangel-Health-main (32)`,
> and are not maintained). What was actually built, with current locations, is
> `docs/asclepius/SANDBOX_REALM.md`; the commit messages cite this document's
> section numbers.

# PRD — The Sandbox realm: a full copy of the product, on production, that real users can never see

**One sentence:** every request carries a `realm` — `live` or `sandbox` — and the
sandbox realm routes to physically separate databases, asset directories, and an
email outbox, so ten fake doctors, a fake admin, and fake-onboarded organizations
exercise the *exact* production code and environment while being unable, by
construction, to appear anywhere a real user or the real admin looks.

Verified against `Archangel-Health-main (32)`.

---

## §0 Why isolation must be a boundary, not a filter

The existing mock concept (`auth.py:590-653`, `asclepius/store.py:6366 mock_annotator_id_hashes`)
works by *filtering mock hashes out* — in 30 call sites across 7 files. That is an
allow-list: every new query that forgets the filter leaks. The sandbox uses the
opposite mechanism: **sandbox rows live in different files.** The real admin opens
`asclepius.db`; sandbox data is in `asclepius_sandbox.db`; there is no query that can
cross because there is no shared table. The existing mock filtering stays as-is for
the legacy `mockadmin` account, untouched.

The four stores already select their file by env var — `ASCLEPIUS_DB_PATH`,
`COMMUNITY_DB_PATH`, `TEAM_DB_PATH`, `ASCLEPIUS_ASSET_STORE`. This PRD makes that
selection **request-scoped** instead of process-wide. That is the entire plumbing
change; everything else is seeding and UI.

---

## §1 The realm — plumbing

### 1.1 Context variable
`backend/realm.py` (new, ~40 lines):
```python
REALMS = ("live", "sandbox")
_current: ContextVar[str] = ContextVar("realm", default="live")
def current() -> str
def set_for_request(realm: str) -> Token          # middleware only
def paths(realm) -> dict                         # db + asset paths per realm
```
Sandbox paths derive from the live ones: `<name>_sandbox.db` beside each live DB,
`<asset_root>/sandbox/` for assets. Never configurable separately — one env, two
realms — so production and sandbox can't drift in config, which is the point.

### 1.2 Store selection becomes per-realm — four stores, one pattern, one trap

`asclepius/store.py:15727 get_store()` (29 call sites, all through this function or the routers'
`_store()` wrappers at `asclepius.py:189`, `asclepius_admin.py:48`):
```python
_STORES: Dict[str, AsclepiusStore] = {}
def get_store():
    r = realm.current()
    if r not in _STORES: _STORES[r] = AsclepiusStore(realm.paths(r)["asclepius"])
    return _STORES[r]
```
Same change for `community/store.py:1769 get_community_store()` and
`assets.py:58 _store_root()`. Migrations run on first open per realm, so the sandbox
DB always has the live schema.

**The trap — the team store is pinned at import, not accessed through a function.**
`main.py:238` is `_team_store = TeamStore()`, a module-level instance referenced
**137 times** in `main.py` (including the live `/api/auth/login` and `/register`
handlers) and exposed to routers as `request.app.state.team_store`
(`onboarding.py:125-126`, `asclepius_admin.py:1930`). Two more import-time pins:
`ai/llm_client.py:69 _event_store = TeamStore()` (AI-call telemetry) and
`eligibility/pipeline.py:37` (legacy, flag-gated — leave it).

Do NOT rewrite 137 call sites. Replace the pinned instance with a **realm proxy**:
```python
class _RealmTeamStore:
    """Forwards every attribute to the current realm's TeamStore."""
    def __getattr__(self, name):
        return getattr(team_store.get_team_store(realm.current()), name)
_team_store = _RealmTeamStore()
app.state.team_store = _team_store
```
with `team_store.get_team_store(realm)` keeping one `TeamStore` per realm
(`team_store.py:5195` keys `_STORE`S per realm; it was a single pin before this PRD). Every
existing `_team_store.foo()` call resolves the realm at call time. The same proxy
pattern replaces `llm_client._event_store`, so sandbox AI-call telemetry lands in
the sandbox team DB. A test asserts no module in `backend/` (outside `_retired`
and flag-gated legacy) instantiates `TeamStore()`, `AsclepiusStore()`, or
`CommunityStore()` at module level.

### 1.3 How a request gets its realm
- **Login** (`/auth/login`, `/hs/login`, provider/buyer logins, and every onboarding
  entry): reads header `X-Asclepius-Realm` (default `live`).
- **Serving the sandbox UI:** add `/sandbox/asclepius`, `/sandbox/admin`,
  `/sandbox/provider`, `/sandbox/buyer` aliases beside `main.py:2745`, `:7033`,
  `:2825` (and the buyer route) that serve the identical HTML with
  `<script>window.__REALM='sandbox'</script>` injected. The SPA's `api()` helper
  sends the header from `window.__REALM` and keys `localStorage` tokens as
  `asclepius_token` / `asclepius_token_sandbox`, so a live and a sandbox session
  coexist in one browser.
- **Landing site:** `auth-api.ts` has **22 `fetch(` sites with hand-built headers**
  (`:239`, `:254`, `:291`…; 26 across `landing/src`). Introduce one `apiHeaders()`
  helper that injects `X-Asclepius-Realm` from a `?realm=sandbox` query param
  (persisted in `sessionStorage` for the wizard's multi-page flow) and route every
  fetch through it. A lint test asserts no bare `headers: {` object remains in
  `landing/src/lib`.
- **Sign-in semantics, stated so nobody debugs them later:** sandbox accounts exist
  only in the sandbox DB. `archangelhealth.ai/?realm=sandbox` → Sign in →
  `sb-labeler-1@…` works; the same credentials without the param are a plain 401,
  because the live DB has no such user. That asymmetry is the design: a sandbox
  account cannot land in live, and a real doctor cannot stumble into the sandbox.
  The sandbox admin's Accounts tab links carry the param so it is never typed.
- **Token**: `auth.create_token` (`asclepius/auth.py:85`, the `realm` stamp) adds claim `realm`. Middleware sets
  the context var from the token claim; the header is consulted only on unauthenticated
  entry points. **A token's realm always wins over the header** — a sandbox token can
  never touch live stores, and vice versa; mismatch is a 401 `realm_mismatch`.
- **Background loops** (`main.py:6567-6570` and the verify agent, digest, notify
  sweeps): each loop iterates `for r in REALMS` and sets the context var per pass,
  so fake onboarding gets verified and fake community gets digests. Loops that only
  make sense live (payout auto-approve → `mark_paid`, buyer deliveries) skip
  `sandbox` explicitly, with a comment naming why.

### 1.4 Realm-aware side effects (the four seams)
| Seam | Live | Sandbox |
|---|---|---|
| Email `send_html_email` (`email_utils.py:192`) | SendGrid | write to `sandbox_outbox` table (to, subject, html, extracted OTP/links); never sends |
| Payments `mark_paid` / Stripe (future) | real | 403 `sandbox_no_disbursement`; ledger accrues/approves normally so payout *logic* is testable |
| Buyer deliveries / `Export + send` | real | export builds; delivery creation 403; bundle filename and datasheet stamped `SANDBOX — not a deliverable` |
| Community `u-system` posts, digests | real | run, into the sandbox community DB |

---

## §2 Seeding the sandbox

`POST /api/asclepius/sandbox/seed` (sandbox realm, sandbox-admin only; idempotent by
stable ids). Also runnable as `scripts/sandbox_seed.py`. Creates:

**Sandbox admin** — `sandbox-admin@archangelhealth.ai`, password from
`ASCLEPIUS_SANDBOX_ADMIN_PASSWORD` (required to enable the realm at all; unset →
`/sandbox/*` 404s). Has every admin capability *within the sandbox realm only*.

**Ten physicians** — deterministic, obviously fake, specialty-spread so routing is
testable:

| # | Name | Email | Tier | Specialty |
|---|---|---|---|---|
| 1 | Dr. Ada Test | `sb-labeler-1@…` | labeler | nephrology |
| 2 | Dr. Ben Test | `sb-labeler-2@…` | labeler | nephrology |
| 3 | Dr. Cy Test | `sb-labeler-3@…` | labeler | cardiology |
| 4 | Dr. Dee Test | `sb-labeler-4@…` | labeler | cardiology |
| 5 | Dr. Eli Test | `sb-labeler-5@…` | labeler | oncology |
| 6 | Dr. Fay Test | `sb-labeler-6@…` | labeler | hepatology |
| 7 | Dr. Gus Test | `sb-labeler-7@…` | labeler | nephrology |
| 8 | Dr. Hal Review | `sb-reviewer-1@…` | reviewer | nephrology |
| 9 | Dr. Ivy Review | `sb-reviewer-2@…` | reviewer | cardiology |
| 10 | Dr. Jo Review | `sb-reviewer-3@…` | reviewer | oncology |

All: `real_data_approved=1`, verified, `first_run` complete (so they land on the
dashboard; a separate seed flag `--fresh` leaves one un-onboarded to test the
walkthrough), community-welcomed, referral codes issued. One shared password from
`ASCLEPIUS_SANDBOX_DOCTOR_PASSWORD`. Credentials render on the sandbox admin's
**Accounts** tab with copy buttons — never in the repo, never in logs.

**Sandbox community** — the standard channels, `u-system`, all ten as members.

---

## §3 The sandbox admin console

Same admin code, sandbox realm, with three additions:

1. **A persistent realm banner** on every page in the sandbox realm, top of viewport,
   lime background: `SANDBOX · nothing here reaches real users · <admin email>`. Not
   dismissible. The doctor portal shows a thinner version. This is the single most
   important UI element in the PRD: nobody should ever be unsure which realm they're in.
2. **Accounts tab** — the ten doctors + credentials, `Reset sandbox` (drops and
   reseeds the three sandbox DBs and the sandbox asset dir — sandbox only, guarded by
   realm check + typed confirmation), and `Seed fresh doctor` for onboarding tests.
3. **Outbox tab** — every email the sandbox "sent": to, subject, rendered HTML,
   and the OTP code / magic link / DLA link extracted and clickable in place. Walking
   fake physician onboarding or fake HS onboarding end to end never leaves the
   product.
4. **Data → Systems** gains an `origin` chip on every health system:
   `production copy` or `sandbox onboarded`, with the copy timestamp.

---

## §4 Real data in the sandbox — snapshot copy

`POST /api/asclepius/sandbox/copy-health-system/{hs_id}` (sandbox admin; reads the
**live** store read-only via an explicit `realm.read_live()` context, the only place
in the codebase permitted to open both stores in one request — asserted by a test
that greps for the call).

Copies: the health-system row, its uploads (+ asset files into the sandbox asset
dir), ingest cases, and purpose resolutions. Stamps `origin='production'`,
`copied_at`, `source_hs_id`. Does **not** copy tasks, submissions, or physicians —
the point is to re-run task creation and routing from raw data. Re-copy is
idempotent (replaces the sandbox copy). Live rows are never written. The uploaded
files are already de-identified; the copy inherits the same PHI-gate flags.

The four committed patient bundles are available through the same button as the
`Archangel (fixture)` provider, so the longitudinal pipeline is testable in the
sandbox on day one.

---

## §5 Fake onboarding

No new flows. The physician wizard and the HS signup/intake/DLA flows run
**unchanged** in the sandbox realm: the landing dialog gains a hidden `?realm=sandbox`
entry (and the sandbox admin's Accounts tab has `Start a fake physician onboarding →`
/ `Start a fake org onboarding →` links that open it). OTPs, welcome emails, DLA
sign requests all land in the Outbox. Verification agent runs in the sandbox realm.
Approval on the sandbox admin mints sandbox credentials. The DLA e-sign writes a
sandbox `signed_agreements` row and PDF into the sandbox asset dir, stamped
`SANDBOX` in the document header so a test signature can never be mistaken for a
real one if a file is ever moved.

---

## §6 Design invariants and the leak test

1. **No shared table.** A test opens both realms' DBs and asserts disjoint file
   paths for all four stores.
2. **Token realm wins.** Sandbox token + live header → 401; live token on
   `/sandbox/*` → 401.
3. **The leak test** (the one that matters): seed the sandbox, have all ten doctors
   submit, onboard a fake org, export, post in community. Then, as the **live**
   admin, hit every admin list endpoint (`/admin/*`, `/contributors`, `/tasks`,
   `/ingestion/uploads`, `/health-systems`, `/exports`, community `/channels`,
   payments `/admin/earnings`) and assert **zero** rows whose ids appear in the
   sandbox DB. Enumerate the endpoints from the route table so a new admin endpoint
   is automatically included.
4. **No disbursement.** `mark_paid` and buyer delivery return 403 in sandbox; the
   ledger still moves accrued→approved.
5. **Live never written by sandbox.** The copy endpoint's live connection is opened
   `?mode=ro`; a test asserts the URI.
6. **Reset is sandbox-only.** `Reset sandbox` with a live token, or with the context
   var somehow `live`, is a 403 before any file is touched.
7. **Realm banner always renders** in sandbox (playwright: every route).
8. **No import-time store pins.** The module-level scan in §1.2 passes.
9. **Config parity.** No `*_SANDBOX_*` env var except the two passwords; a test
   asserts `realm.paths("sandbox")` is derived, not read.

---

## §7 Ops — Railway variables, exactly

**Add (two, both secrets):**

| Variable | Why |
|---|---|
| `ASCLEPIUS_SANDBOX_ADMIN_PASSWORD` | Enables the realm. Unset → every `/sandbox/*` route and the realm header 404/ignored, so the feature is dark until you turn it on, and can be turned off instantly by deleting the var. |
| `ASCLEPIUS_SANDBOX_DOCTOR_PASSWORD` | The shared password for the ten seeded physicians. Separate from the admin password so it can be rotated (or handed to a tester) without exposing admin. |

**Do not add** any `*_SANDBOX_DB_PATH` / `*_SANDBOX_ASSET_STORE` variable. Sandbox
paths are derived from the live ones (`/data/asclepius.db` → `/data/asclepius_sandbox.db`,
`ASCLEPIUS_ASSET_STORE/sandbox/`), so they land on the same persistent volume
automatically and cannot drift from production config. A test asserts the derivation.

**Edit nothing else.** `ENV`, `ASCLEPIUS_DB_PATH`, `COMMUNITY_DB_PATH`, `TEAM_DB_PATH`,
`ASCLEPIUS_ASSET_STORE`, `DEMO_MODE`, and the mock-account vars are all read
unchanged; the sandbox realm derives from them. Existing `mockadmin`
(`ASCLEPIUS_MOCK_PASSWORD`) keeps working in the live realm exactly as today.

**Backups:** there is no backup script in the repo today. When one is written, it
must skip `*_sandbox.db` and the `/sandbox/` asset directory; until then, treat the
sandbox DBs as disposable — `Reset sandbox` is the recovery path.

**`AGENTS.md`:** "Every store call is realm-scoped via `realm.current()`. Never
instantiate a store or open a DB path directly. To test against production
conditions, use the sandbox realm — `/sandbox/admin`, credentials on the Accounts tab."

## §8 Do not touch
The existing `mockadmin` account and its hash filtering · any live query · the
migration order · payments rate stamping · the export bundle format (only the
`SANDBOX` stamp is added).

## §9 Execution order for Claude Code

1. `realm.py` + the four accessor changes (§1.1–1.2), **including the team-store
   proxy**. Run the import-pin scan test. Nothing user-visible yet; full suite must
   stay green in the live realm.
2. Middleware + token claim + `realm_mismatch` 401 (§1.3). Test both directions.
3. The four side-effect seams (§1.4): outbox table, disbursement 403, delivery 403,
   community realm loop. Background loops iterate realms.
4. Seed script + `/sandbox/seed` + the SPA aliases + landing header helper (§2, §1.3).
5. Sandbox admin additions: banner (first — it gates everything after), Accounts,
   Outbox, origin chips (§3).
6. Snapshot copy (§4) with the read-only live connection assertion.
7. The leak test (§6.3) — write it last, run it against everything above, and paste
   its output into the PR. A PR without the leak test output is not mergeable.
8. Railway: add the two variables (§7). The feature stays dark until step 8.

Each step is its own commit, each cites its section. If any step needs a change
outside `realm.py`, the four store modules, `auth.py`, `email_utils.py`,
`payments.py` (disbursement guard only), `main.py` (proxy + aliases + loops), the
landing `apiHeaders` helper, the admin/portal frontends, and the new sandbox
router — stop and say why.
