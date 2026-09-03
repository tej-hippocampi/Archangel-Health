# AGENTS.md — the map

Read this first. It is a map, not a tutorial: where things live, what will bite
you, and what the words mean. Every claim here is checkable against the file
named next to it.

## What the company sells

Archangel Health sells physician-verified medical data: evaluation sets, graded
reasoning traces, and RL environments, bought by frontier AI labs. The raw
material is real de-identified patient records contributed by health systems,
plus synthetic cases authored to be genuinely hard. Practising physicians grade
and verify every item, and that verification is the product.

## The three planes

| Plane | Lives in | What it is |
|---|---|---|
| Product | `backend/asclepius/` | The evaluation portal: task store, routing, export, payments |
| Community | `backend/community/` | The physicians' forum — recruiting and retention, not a product surface |
| Next products | `backend/gold/`, `backend/asclepius/environments/` | Conversation capture and RL environments |

`backend/routers/asclepius*.py` is the live HTTP surface. `backend/main.py` is
the app plus the shared auth and patient-facing legacy routes.

## The landmines

Five things that look dead and are not. Check before you delete.

- `/api/auth/*` in `backend/main.py:2202+` (register, login, verify-email, MFA)
  is the live auth surface and reads `team_store` — ~170 references in that file.
- `backend/routers/tenant_portal.py:36` serves live tenant sign-in
  (`/{slug}/auth/login`), separate from `/api/auth`.
- `/api/demo/sign-in-routes` at `backend/main.py:3140` is live and covered by
  `tests/test_demo_and_patient_update.py:29`.
- `backend/prompts/registry.py` is a hub: six non-test modules import it, and
  `ai/llm_client.py:_record` calls `prompt_meta` on every single LLM call.
- `backend/scripts/ci_shard.py` is load-bearing for CI. `.github/workflows/
  tests.yml` references it eight times; every test file must land in exactly one
  shard or it silently stops running.

The peri-op clinical code is flag-gated for deletion. Everything removed is kept
at `git tag legacy-periop-final`. Do not build on it.

## Status vocabularies

Three vocabularies must move together — a row whose statuses disagree is a bug,
not a state.

- **Submission** (`asclepius/constants.py:418` `SUBMISSION_STATUSES`):
  `submitted → auto_validated → needs_qa → qa_checked → export_ready →
  exported`, plus terminal side-branches `rejected`, `prompt_flagged`,
  `not_hard`. A side-branch is captured for audit and never packaged.
- **Ledger / earnings**: what a physician is owed. It must agree with the
  submission's export state — an earning that says paid while its submission
  never reached `exported` is the payout bug that has already happened here.
- **Records**: what actually shipped in a bundle. `records.jsonl` is the buyer's
  copy; the contributor table must list only annotators present in it.

Other words that carry meaning:

- **Portal version** (`asclepius/constants.py:141`): `v3` synthetic (the
  default), `v4` real static charts, `v5` real longitudinal. Environments are a
  separate track under `asclepius/environments/`.
- **`distribution`** — `open` (any eligible physician may claim) vs assigned.
- **`walk_mode`** — `solo` or `relay`, how a longitudinal case is routed across
  physicians (`asclepius/route_notify.py:459`).

## Sandbox facts

- **No secrets here, by design.** The sandbox holds no API key and never should.
- `ASCLEPIUS_LLM_PROVIDER=fake` is set for the suite (`tests/conftest.py`).
  Every LLM path runs against `ai/fake_llm.py`: deterministic, schema-valid
  fixtures keyed by the call's `purpose` (falling back to `role`), tool-use calls
  answered from the tool's own `input_schema`. `FAKE_LLM_VERDICT=fail` flips
  every judge to reject; `FAKE_LLM_LATENCY_MS` exercises timeouts.
- A fake in production refuses to boot (`ai/model_config.py`
  `assert_fake_llm_not_in_production`).
- Real-model checks run in CI only: the `llm-smoke` workflow, `workflow_dispatch`
  only, key from GitHub Secrets. `scripts/smoke_multimodal.py` never runs here.
- Email prints to stdout (`EMAIL_DEV_MODE=1`); nothing is delivered.
- Four SQLite stores, not zero: `ASCLEPIUS_DB_PATH`, `COMMUNITY_DB_PATH`,
  `TEAM_DB_PATH`, and the export dir `ASCLEPIUS_EXPORT_DIR`.

## The Sandbox *realm* (production) — not this sandbox

`docs/asclepius/SANDBOX_REALM.md`. Different thing from the facts above: a
second realm of the deployed product — `/sandbox/admin`, ten fake doctors, a
fake admin — routed to `<name>_sandbox.db` / `<root>/sandbox/` files beside the
live ones. Dark until `ASCLEPIUS_SANDBOX_ADMIN_PASSWORD` is set.

- **Every store call is realm-scoped via `realm.current()`. Never instantiate a
  store or open a DB path directly** — `asclepius.store.get_store()`,
  `community.store.get_community_store()`, `team_store.get_team_store()` (or the
  `app.state.*` proxies) return the CURRENT realm's instance.
  `tests/test_sandbox_realm_plumbing.py` fails the build on any module-level
  `TeamStore()` / `AsclepiusStore()` / `CommunityStore()`.
- Sandbox paths are derived from the live ones; never add a `*_SANDBOX_*` path
  variable (the only two are the passwords).
- Money never leaves it (`mark_paid`, buyer deliveries → 403); email never
  leaves it (the Outbox tab). `tests/test_sandbox_leak.py` asserts the live
  admin can see none of it — run it after any change to an admin endpoint.

## How to work here

- PRDs cite `file:line`. Citations drift — run `/prd-audit` before declaring any
  PRD done, and fix the **PRD**, never the code, when a citation is stale.
- Run `/merge-readiness` before `git push` or opening a PR. More than 20 commits
  behind `origin/main` means rebase first.
- Any change touching `tasks`, `submissions`, `records`, `earnings`, `uploads`,
  `assignments` or `exports` runs `/data-inventory` before and after. No id may
  disappear.
- Never `DELETE` data in a migration. Add a column, backfill, flip a flag.
- Builder writes, auditor checks with fresh context, builder fixes, auditor
  confirms, then PR. Paste the auditor's report into the PR description.

## Commands

```bash
# boot check — imports the app and exits non-zero on any import error
cd backend && python3 -c "import main; print('boot ok')"

# the loop: only the tests your change can break (<90s), first failure only
cd backend && python3 scripts/affected_tests.py | xargs python3 -m pytest -x --lf -q

# full suite (~14 min; keyless by design)
cd backend && python3 -m pytest tests/ -q

# one CI shard, exactly as CI runs it
cd backend && python3 -m pytest -q $(python3 scripts/ci_shard.py 1 4)

# route baseline — diff the live route table against the committed snapshot
cd backend && python3 scripts/route_baseline.py --diff

# dangling-import scan — imports that name a module that no longer exists
cd backend && python3 scripts/check_dangling_imports.py
```
