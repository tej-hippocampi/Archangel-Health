# PRD set — The engineering harness: five PRDs that compound to 10×

**Premise, proven in this repo's own history:** the expensive bugs were harness
failures, not model failures — a branch cut from a stale merge-base, a payout status
that never told export, a CSS brace that hid 69 rules, a PRD range that swallowed
the live login route. Each was *context the agent didn't have* or *a check the agent
didn't run*. These five PRDs supply the context and automate the checks. Land them in
order; each makes the next cheaper.

Verified against `Archangel-Health-main (32)`.

---

## §0 The finding that makes H1 urgent

Every Claude Code session begins by reading `AGENTS.md`. Today's `AGENTS.md` (151
lines) describes **"CareGuide"**, says the app has **"No database"** (it has four
SQLite stores), gives Cursor Cloud instructions, and points at the peri-op patient
dashboard as the demo. The three skills in `.claude/skills/` are `surgical-risk-
triage`, `team-eligibility-review`, and `ehr-extraction` — all peri-op, all
describing code that is flag-gated for deletion. **The agent's first read of the
repo is a confident description of a product you no longer build.** Nothing else in
this document matters until that is fixed.

---

# H1 — `AGENTS.md` as a map, and skill retirement

### Deliverable
Replace `AGENTS.md` (Claude Code also reads `CLAUDE.md`; make `CLAUDE.md` a one-line
include of `AGENTS.md` so both tools see one file). ≤120 lines. Sections, in order:

1. **What the company sells** — three sentences. Physician-verified data, evals, RL
   environments. Real de-identified records from health systems. Sold to labs.
2. **The three planes and where they live** — `asclepius/` (product: store, routing,
   export, payments), `community/` (the physicians' Slack), `gold/` +
   `asclepius/environments/` (next products). One line each. `routers/asclepius*.py`
   is the live HTTP surface.
3. **The landmines** — verbatim from the cleanup PRD §0: `/api/auth/*` in `main.py`
   is live and reads `team_store`; `routers/tenant_portal.py` serves live tenant
   sign-in; `/api/demo/sign-in-routes` is live; `prompts/registry.py` is a hub;
   `ci_shard.py` is load-bearing for CI. Five lines, each with the file.
4. **Status vocabularies** — the three that must move together (ledger / submission
   / records), the portal-version meanings (V3 synthetic · V4 real static · V5 real
   longitudinal · ENV environments), `distribution`, `walk_mode`.
5. **Sandbox facts** — no secrets by design; `ASCLEPIUS_LLM_PROVIDER=fake`; email
   prints to stdout; `smoke_multimodal.py` runs only in CI (`llm-smoke` workflow).
6. **How to work here** — PRDs cite `file:line`; run `/prd-audit` before declaring a
   PRD done; run `/merge-readiness` before opening a PR; never `DELETE` data in a
   migration; `git tag legacy-periop-final` holds everything deleted.
7. **Commands** — the five that matter: boot check, full suite, one shard, route
   baseline, dangling-import scan.

### Skill retirement
Move `surgical-risk-triage`, `team-eligibility-review`, `ehr-extraction` to
`.claude/skills/_retired/` with a one-line README ("peri-op; code flag-gated; see
tag"). Skills that describe dead code actively mislead — an agent that triggers
`ehr-extraction` starts editing `pipeline/`.

### Test
A CI check that `AGENTS.md` contains no occurrence of `CareGuide`, `No database`,
`Cursor`, `patient/maria`, or `triage` outside the landmines section. Cheap, and it
would have caught today's file.

---

# H2 — Hooks: sensors that run whether the agent remembers or not

Claude Code hooks (`.claude/settings.json` → `hooks`) run shell commands on events.
The agent cannot skip them. Each hook below is a sensor that turns a silent failure
into a loud one, chosen because the corresponding failure has already happened here.

| Event | Matcher | Command | Why (the incident) |
|---|---|---|---|
| `PostToolUse` | `Edit\|Write` on `*.js` | `node --check "$FILE"` | orphan-class / syntax slips in a 13k-line file |
| `PostToolUse` | `Edit\|Write` on `*.css` | `python3 scripts/css_balance.py "$FILE"` | the unclosed `@media` at `asclepius.css:4816` |
| `PostToolUse` | `Edit\|Write` on `backend/**/*.py` | `python3 -m pyflakes "$FILE"` + `python3 scripts/check_dangling_imports.py --file "$FILE"` | function-local import of a deleted router |
| `PostToolUse` | `Edit\|Write` on `backend/asclepius/store.py` | `grep -nE "DELETE FROM (tasks\|submissions\|records\|earnings\|uploads)" "$FILE" && exit 2` | the no-data-loss contract |
| `PreToolUse` | `Bash` matching `git push` | `python3 scripts/merge_readiness.py` (H3) | the longitudinal branch that didn't merge |
| `Stop` | — | `python3 scripts/route_baseline.py --diff` | flag-gated routes disappearing unnoticed |

Exit code 2 blocks the action and feeds stderr back to the agent as the reason.
Hooks must finish in <5s each; anything slower goes to H5's fast-test loop instead.

### New scripts (each ≤60 lines, each with its own test)
- `scripts/css_balance.py` — brace depth must be 0 at EOF; report the line where an
  `@media` opened and never closed.
- `scripts/route_baseline.py` — `--snapshot` writes the ordered route table to
  `docs/asclepius/ROUTES.json`; `--diff` prints added/removed routes vs the snapshot
  and exits 1 on any change not accompanied by an updated snapshot in the same diff.
- `check_dangling_imports.py` gains `--file` (single-file mode) so it's hook-fast.

---

# H3 — Skills: the checks I've been running by hand, made automatic

Skills live in `.claude/skills/<name>/SKILL.md` and trigger on name or description.
Four, each encoding a check that caught a real bug in this repo.

### `/prd-audit`
*Trigger:* any time a PRD is written or declared complete.
1. Extract every `file:line` and `` `symbol` `` citation from the PRD.
2. For each: `sed -n "${line}p" file` and assert the line contains the cited symbol
   or a stated keyword; print a table `citation · actual line · ✓/✗`.
3. Any ✗ → fix the PRD, never the code, and re-run.
4. Assert the PRD has: an invariant section, a tests section, a do-not-touch section.
*Incidents:* `ingestion.py:1533→1534`, `asclepius.js:7462→7997`, `store.py:291→503`
— every PRD in this repo has drifted at least once.

### `/merge-readiness`
*Trigger:* before `git push` or opening a PR.
1. `git merge-base HEAD origin/main` → how many commits behind; **>20 → stop and
   rebase first**.
2. `git apply --check` of the branch diff against `origin/main` (or a dry-run merge);
   list conflicting files.
3. For each conflicting file that is `store.py`, `export.py`, or `asclepius.js`:
   print both sides' hunks — these three have collided in every branch so far.
4. Run the affected-test subset (H5) on the rebased tree.
*Incident:* the longitudinal branch predated the assignments feature; naive
resolution would have silently killed one of two features.

### `/data-inventory`
*Trigger:* any PRD that touches `tasks`, `submissions`, `records`, `earnings`,
`uploads`, `assignments`, or `exports`.
Runs the five-table id+count snapshot (Export PRD §0) to
`docs/asclepius/INVENTORY_<date>.json` before the change and diffs after. Any id
missing → exit 2. *Incident:* "56 tasks, none may be lost."

### `/export-audit`
*Trigger:* before any bundle goes to a buyer.
Unzips the bundle and asserts: license ≠ NC; contributor table lists only annotators
present in `records.jsonl`; no physician name string anywhere; no `answer_key`; no
`amount_cents`/`earning_id`; scope recorded in `batch.json`; all `.jsonl` parse.
*Incident:* the Centaur sample — earning bundle, NC license, eight-row roster.

---

# H4 — Build / audit split with subagents, and parallel worktrees

### The pattern
An agent that wrote the change is anchored on it. A second agent with **fresh
context** finds what the first can't. Define two subagents in `.claude/agents/`:

- **`builder.md`** — implements a PRD. Tools: all. Instruction: "cite the PRD section
  in every commit message; run H2 hooks; do not self-declare done — hand to auditor."
- **`auditor.md`** — read-only tools + Bash for tests. Instruction: "You did not write
  this. Assume it is wrong. Run `/prd-audit`, `/merge-readiness`, the affected
  tests, and the route diff. Report findings as `file:line — what — why it matters`.
  Do not fix; report." A finding list with zero entries must say what was checked.

Workflow in `AGENTS.md`: builder → auditor → builder fixes → auditor confirms → PR.
The auditor's report is pasted into the PR description, so a human reads what a
fresh agent found, not what the author claimed.

### Parallel worktrees
Independent PRDs (e.g. Welcome Package v2 and Export) run concurrently in
`git worktree` checkouts, one builder each, one shared auditor at the end. The
`merge-readiness` skill is what makes this safe — it catches the two branches
colliding on `asclepius.js` before either pushes.

---

# H5 — The fast feedback loop

An agent iterates only as fast as it learns it was wrong. The full suite is 12
minutes; that's too slow for a loop, which is why agents skip it.

1. **Affected-test selection.** `scripts/affected_tests.py`: map changed files →
   test files by (a) import graph (AST, the same resolver used for the cleanup) and
   (b) naming convention (`x.py` → `test_x*.py`). Prints the list; `pytest` runs it.
   Target: <90s for a typical change. Full suite stays for `/merge-readiness` and CI.
2. **Fake LLM is a precondition** (`PRD_FAKE_LLM_PROVIDER.md`) — without it, any
   generation-path test is either stubbed per file or skipped. Land it first.
3. **`pytest -x --lf` by default** in the hook config: stop at the first failure,
   rerun last-failed first. The agent reads one failure, not forty.
4. **Test names are the spec.** Enforce (lint) that new test functions read as
   sentences: `test_v4_queue_never_serves_a_trajectory_point`. The failure message
   then tells the agent *what rule broke*, which is the thing it needs to fix it.

---

## Sequencing and the compounding math

| Order | PRD | What it removes | Rough effect on rework |
|---|---|---|---|
| 1 | H1 map + skill retirement | agent starts from a false model of the repo | −30% |
| 2 | Fake LLM (separate PRD) + H5 | untestable paths, 12-minute loops | −30% |
| 3 | H2 hooks | the four incident classes, permanently | −25% |
| 4 | H3 skills | drifted citations, stale merge-bases, lost data, bad bundles | −25% |
| 5 | H4 build/audit | author-anchored blind spots | −25% |

Compounded, that is roughly a 5–8× reduction in rework, and rework is where the
hours go. The 10× comes from the fact that each layer also lets you run more work in
parallel safely (H4) — throughput up while error rate goes down.

## Do not touch
Test content, prompt text, the CI shard scheme (`ci_shard.py` is load-bearing),
product code — this PRD set is harness only.
