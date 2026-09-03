# Harness report

Execution of `PRD_FAKE_LLM_PROVIDER.md` then `PRD_ENGINEERING_HARNESS.md`
(H1 → H5), one commit per section. Both PRDs are committed under `docs/prd/` and
both audit clean with `/prd-audit`.

---

## The inventory that was asked for first

Read before any edit, to confirm or correct the harness PRD's §0 finding.

| Claim in H1 §0 | Verdict |
|---|---|
| `AGENTS.md` is 151 lines | Confirmed — exactly 151 |
| It says "CareGuide" | Confirmed — twice, lines 6 and 13 |
| It says "No database" | Confirmed — line 13, while `ASCLEPIUS_DB_PATH`, `COMMUNITY_DB_PATH` and `TEAM_DB_PATH` all exist |
| It gives Cursor Cloud instructions | Confirmed — a whole section at line 10 |
| It points at the peri-op demo | Confirmed — `/patient/maria_001` at line 26 |
| The three skills are all peri-op | Confirmed — `surgical-risk-triage`, `team-eligibility-review`, `ehr-extraction` |

**Two corrections.**

1. The H1 test asks for "no `triage` outside the landmines section". `triage`
   appeared **zero** times in the old `AGENTS.md`, so that clause was already
   satisfied. It is implemented anyway — the risk it guards is real.
2. **`.claude/settings.json` was not stale.** It enables the `healthcare` and
   `superpowers` plugin marketplaces and names no peri-op skill. The harness PRD
   implies the whole agent configuration describes the wrong product; the skills
   did, this file did not. It was left alone apart from the hooks question below.

---

## What shipped

### `PRD_FAKE_LLM_PROVIDER.md`

| § | Shipped | Commit |
|---|---|---|
| §1 | `ai/fake_llm.py`, the switch in `model_config` | `41e2ccc` |
| §2 | Both seams wired, conftest default, production guard | `5264e5d`, `ab600e0` |
| §3 | `llm-smoke` workflow, `workflow_dispatch` only | `dcaa992` |
| §4 | 24 acceptance tests + the `keyless` CI job | `0d856ec` |
| §5 | Do-not-touch respected — see below | — |

**Three deviations, each because the PRD's sketch was wrong about this tree.**

1. **The switch is not inside `resolve_provider`.** The PRD puts it there. Three
   callers need the *real* vendor even when the transport is faked, and the worst
   is `constants.baseline_pairing_ok`, which requires the two baseline models to
   resolve to two *different* vendors — the pairing is the product. A global
   override collapses both to `"fake"` and fails startup validation in the exact
   sandbox this feature exists to enable. `resolve_provider` stays pure;
   `active_provider`/`fake_llm_enabled` carry the override at the two seams.

2. **Fixtures key on `purpose`, falling back to `role`.** §1.1 assumes every call
   site passes a purpose and says to fail loudly otherwise. An AST scan found
   **11 non-test call sites that pass none** — `main.py:1790`, `main.py:4999`,
   `intake_section_chat.py` ×3, `eligibility/extract.py:202`,
   `routers/internal.py` ×2, `pipeline/extract.py:75`, `pipeline/generate.py:136`,
   `triage/intraop/extractor_llm.py:208`. Failing loudly there would break core
   paths and contradict §0's own goal. Every one of them declares a literal
   `role`, so role is the fallback and **no product call site was edited**.

3. **Tool-use calls are answered from the tool's own `input_schema`.** The PRD's
   table is all text fixtures, but `eligibility/extract.py` and
   `triage/intraop/extractor_llm.py` force a tool and read
   `.content[].input`; a text block makes both raise "returned no tool_use
   block". Synthesizing from the schema covers all five tool-using roles and
   stays correct as those schemas change. Verified: the eligibility path returns
   real TEAM fields (`partA`, `partB`, `esrdBasis`, `umwa`).

Smaller: §1.1 says prelabel returns `confidence: "low"`; `critic.run_prelabel`
parses `confidence` as a **float** in `[0,1]`, so the fixture returns `0.2`.
And the PRD's "27 distinct `purpose=` strings" counts three upload/link purposes
from test files (`brokering`, `nonsense`, `task_creation`) that are not LLM
purposes; the real figure is 27 literal LLM purposes, a different set, across
26 modules rather than 14.

### `PRD_ENGINEERING_HARNESS.md`

| § | Shipped | Commit |
|---|---|---|
| H1 | `AGENTS.md` rewritten (118 lines), `CLAUDE.md` include, 3 skills retired, `check_agents_md.py` + CI job | `64cdfec`, `d71ecc6` |
| H2 | 6 sensor scripts, tested both ways and timed | `0be514e` |
| H3 | 4 skills + `prd_audit.py`, `data_inventory.py`, `export_audit.py` | `a8f3c72` |
| H4 | `builder.md`, `auditor.md` | `665e5b9` |
| H5 | `affected_tests.py`, `lint_test_names.py` | `665e5b9` |

---

## Hooks: installed

`.claude/settings.json` carries the `hooks` key below. The first attempt to write
it was blocked by this session's permission layer, which is the right default —
hooks run shell commands automatically on every edit and every Bash call, so
installing them is the user's decision, not the agent's. It was installed on the
user's explicit instruction.

```json
"hooks": {
  "PostToolUse": [
    { "matcher": "Edit|Write",
      "hooks": [{ "type": "command",
                  "command": "python3 \"$CLAUDE_PROJECT_DIR/backend/scripts/hook_post_edit.py\"",
                  "timeout": 15 }] }
  ],
  "PreToolUse": [
    { "matcher": "Bash",
      "hooks": [{ "type": "command",
                  "command": "python3 \"$CLAUDE_PROJECT_DIR/backend/scripts/hook_pre_push.py\"",
                  "timeout": 60 }] }
  ]
}
```

One dispatcher per event rather than H2's six matchers: each matcher pays a
Python startup, and `hook_post_edit.py` routes by file type internally.

Verified by invoking each exactly as Claude Code does, JSON on stdin:

| Input | Result |
|---|---|
| clean Python (`ai/fake_llm.py`) | `exit 0` — allow |
| unbalanced CSS (`@media` never closed) | `exit 2` — **block** |
| malformed JS (unclosed function) | `exit 2` — **block** |
| `ls -la` | `exit 0` — allow |
| `git push` on a clean branch | `exit 0` — allow |

`tests/test_harness_scripts.py` now asserts the wiring itself — that both hooks
are present, that every command names a script that exists, that the matcher
covers `Write` and not just `Edit`, and that no timeout is tight enough to kill a
check mid-run. A later `settings.json` edit cannot quietly unhook the sensors.

---

## What I did not do, and why

**1. H2's `Stop` hook is not installed.** The other two are (see below). The
`Stop` → `route_baseline.py --diff` hook boots the app (2.8s) on every turn end,
and unlike the edit and push hooks it cannot block anything actionable at that
point — a route has already changed by then. It runs in `/merge-readiness` and CI
instead, where a route diff can still stop something.

**2. `pytest -x --lf` is not a global default.** H5.3 asks for it "in the hook
config". As a global `addopts` it would be actively harmful: `-x` stops at the
first failure, and CI shards the suite expecting to see *every* failure in one
run. It is the documented agent loop command in `AGENTS.md` instead.

**3. The per-file `call_llm` stubs stay — all 27 of them.** Fake LLM §2 asks for
the stubs the fake now satisfies to be deleted, expecting "most of the 40-odd" to
go. Audited with `scripts/stub_audit.py`, which strips each stub and runs the test
against the fake: **none is redundant.**

17 fail outright without their stub. The other 10 pass, but reading each shows the
pass is VACUOUS — the stub IS the mechanism under test:

| Test | What the stub supplies | Without it |
|---|---|---|
| `test_no_results_means_no_model_call_at_all` | a spy list | `assert called == []` is free |
| `test_a_url_the_search_never_returned_is_dropped` | an invented URL | nothing is invented; the rule is never exercised |
| `test_a_non_http_url_never_survives` | `javascript:alert(1)` | nothing malicious present; passes for the wrong reason |
| `test_a_failing_search_never_raises` | a raise | nothing fails; "never raises" is trivially true |
| `test_wrong_judge_keys_would_drop_documented` | deliberately wrong keys | the fake returns correct ones |
| `test_ab_slot_randomization_is_balanced` | a constant answer | couples a position-bias test to the fake's fixture choice |

The remaining three candidates are fixtures (`_install_llm`, `searcher`,
`_mock_llm`) consumed by tests in the load-bearing set.

So the PRD's expectation does not hold for this repo, and the reason is
structural: before the fake, a call with no key **failed**, so a test either
stubbed a specific answer or asserted the degraded path. The "generic stub that
only stops the call erroring" — the kind the fake would replace — was never worth
writing here, so almost none exist. `scripts/stub_audit.py` is committed so the
question can be re-answered cheaply as the fake's fixtures grow.

**4. No product code was touched beyond the four allowed files.** `main.py` was
the natural home for the production boot guard; instead it is invoked at import
time in `ai/model_config.py`, which every entry point imports before serving
anything, so the guard covers workers and scripts too.

---

## Hook timings

Best of three, against H2's 5-second ceiling. All hooks pass with two orders of
magnitude to spare.

| Hook | Input | Time |
|---|---|---|
| `hook_post_edit` | 700-line Python (`ai/llm_client.py`) | **0.172s** |
| `hook_post_edit` | 5,586-line CSS (`asclepius.css`) | **0.742s** |
| `hook_post_edit` | 14,151-line JS (`asclepius.js`) | **0.101s** |
| `hook_post_edit` | `store.py` DELETE guard | **0.697s** |
| `hook_pre_push` | non-push Bash command | **0.048s** |
| `hook_pre_push` | real `git push` (runs merge readiness) | **0.094s** |

Standalone, not hook-bound: `route_baseline --diff` 2.81s (boots the app),
`check_dangling_imports` whole-tree 2.12s (`--file` mode 0.04s),
`affected_tests` 0.88s, `data_inventory --snapshot` 0.05s.

---

## Full suite with `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` unset

Measured on the tree after `origin/main` was merged in (`1120a6f`):

**38 failed, 5675 passed, 3 skipped — 16m22s.**

**All 38 are pre-existing. None is this branch's.** Proven, not asserted, in two
groups against `git worktree` checkouts:

| Failures | Where | Evidence it is not ours |
|---:|---|---|
| 15 | `test_admin_shell_ui.py` | Identical 15 on `origin/main` alone. `OSError: [Errno 7] Argument list too long: '/usr/local/bin/node'` — an environment limit invoking node, not an assertion. These files arrived with `main`. |
| 9 | `test_telehealth_router.py` | In the pre-change baseline at `9a6f2cc` |
| 4 | `test_intervention_email.py` | ditto |
| 4 | `test_care_team_messaging.py` | ditto |
| 3 | `test_asclepius_mm_debug.py` | ditto |
| 2 | `test_triage_timeline.py` | ditto |
| 1 | `test_asclepius_router.py` | ditto |

The lower 23 are byte-identical to the baseline failure set measured before any of
this work landed; the 15 reproduce on `main` with none of this code present.

New tests added by this work: **69** (24 fake-LLM, 45 harness), all passing.

### Two CI-only failures worth recording

CI shard 1 reported two failures that do **not** reproduce locally — not in
isolation, not for the whole file, and not when running shard 1's exact 71-file
list:

```
test_v4_promotion.py::test_an_unapproved_physician_never_sees_a_v4_real_case
test_v4_promotion.py::test_the_fan_out_widens_visibility_and_nothing_else
```

Both assert `body["task"] is None`. The mechanism is understood and IS related to
the fake: those assertions encode an empty-queue precondition that held only
because, with no API key, no synthetic task could ever be generated. With the fake
on, an earlier test in the same shard can generate one into the shared store, and
the v4 endpoint then serves it.

**It is not a real-data leak, and that was checked directly rather than assumed.**
On a clean store the wall holds — an unapproved physician is served nothing while
all three `real_deid` cases sit in the store. Inserting one synthetic task and
repeating the request serves that synthetic task (`case_source: None`,
`display_bucket: synthetic`), never a real chart. The real-data wall gates real
cases; synthetic content was never behind it.

Fixing it properly means changing what those two tests assert — from "the queue is
empty" to "nothing real is served" — which is test content, outside this work's
scope. Left for the author with the diagnosis above.

## What the harness caught while being built

Not a claim about future value — these are defects it found in this branch, in
the hour it existed.

- **The post-edit hook**, on its first run, flagged two imports left unused in
  `ai/llm_client.py` by the provider switch. Removing them then broke
  `test_llm_timeout_does_not_retry`, which monkeypatches `resolve_provider` in
  that namespace — which is *why* the seam was restored rather than the test
  edited.
- **`/prd-audit`** found a genuinely drifted citation: the harness PRD cites the
  unclosed `@media` at `asclepius.css:4814`; it is at **4816**. The PRD was
  fixed, per H3's own rule.
- **Writing the tests** found two bugs in the sensors themselves, both of which
  would have made them useless in the exact case they exist for:
  `check_dangling_imports` resolved `from routers.deleted_router import thing` as
  valid, and `hook_pre_push` treated `echo git push` as a push.
- **`affected_tests`** first returned 122 of 263 files for a change to
  `critic.py`, because a test importing the `asclepius` package matched any
  change inside it. Now 12.

A sensor that has never fired is indistinguishable from no sensor. Every script
here is tested against clean input *and* against the incident it was written for.
