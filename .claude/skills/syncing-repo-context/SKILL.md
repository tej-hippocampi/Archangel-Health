---
name: syncing-repo-context
description: Keeps docs/context/ current as the repo changes. Sweeps git history since the last-synced commit, updates repo-state.md (and the README map when new top-level modules appear), and advances the watermark. Use when the user says "sync repo context", "update the context folder", "catch up the repo context", when docs/context/repo-state.md is stale, or after landing a substantive change (new module, endpoint, or PRD).
---

# Syncing repo context

`docs/context/` is a code-free map of this repo for a human or AI coworker who
needs the structure without reading the whole backend and frontend. This skill
keeps it current **incrementally** - it only looks at what changed since the
last sync, so it stays cheap and low-noise.

Read `docs/context/README.md` once for the layout it maintains.

## Sole-writer rule

This skill is the **only** writer of:

- `docs/context/repo-state.md` (the volatile "what changed / what's live" file)
- `docs/context/ingestion-log.md` (the append-only ledger + watermark)

It **curates** but does not silently rewrite:

- `docs/context/README.md` and `docs/context/architecture.md` - additive facts
  (a new top-level directory, a new backend domain package) may be folded into
  the layout / module-roots tables directly; anything that reinterprets the
  architecture is **proposed to the user**, not written on your own authority.

Never touch code, tests, or any file outside `docs/context/` (except an
approved README/architecture map update as above).

## The watermark

The watermark lives in `docs/context/ingestion-log.md`:

```
last-synced-commit: <full 40-char SHA>
```

It is the commit the last sweep stopped at. The sweep is
`git log <last-synced-commit>..HEAD` - everything merged since then, nothing
before. Advancing the watermark to `HEAD` only after a successful, fully
applied sweep is what keeps runs incremental.

**Correctness check:** immediately after a sync, a second run must find
`<last-synced-commit>..HEAD` empty and report "nothing to ingest."

## Run loop

1. **Read state.** Read the `last-synced-commit` line from
   `docs/context/ingestion-log.md` and the current `docs/context/repo-state.md`.
   Capture `HEAD`: `git rev-parse HEAD`.

2. **Sweep what changed** (only the new range):
   - `git log --oneline <watermark>..HEAD` - the commits and their messages.
   - `git diff --name-status <watermark>..HEAD` - files added (A), modified (M),
     deleted (D), renamed (R).
   - `git diff --dirstat=files,0 <watermark>..HEAD` - which directories moved
     the most (a cheap "where did the work happen" signal).
   - Merged PRs are visible as merge-commit messages in the log; note PR
     numbers where present.
   If the range is empty, go to step 6 (no-op path).

3. **Detect structural change.** From the `A`/`D` lines, look for:
   - a **new top-level directory** or a **new `backend/<domain>/` package** ->
     fold it into the `README.md` layout table and module-roots, and into
     `architecture.md`'s domain table if it is a runtime domain.
   - a removed/renamed module -> supersede the old line (annotate, do not just
     delete) and note it.
   - a change to the runtime shape (a database appears, the frontend gains a
     build step, a deploy target moves) -> **do not rewrite architecture.md
     yourself**; surface it to the user as a proposed edit.

4. **Update `repo-state.md`:**
   - Refresh the freshness stamp (date + new `HEAD` short SHA).
   - Replace `## Changed since last sync` with this range's deltas, grouped by
     domain, one bullet each, provenance-tagged (see below). If nothing
     substantive changed, write "(no substantive changes; N commits, mostly
     <area>)".
   - Update `## Current focus / in-flight branches` from `git worktree list`
     and any obvious in-flight themes.
   - Move anything now fully merged out of "in-flight" into "Recently landed".

5. **Append to the ledger and advance the watermark:**
   - Append one `YYYY-MM-DD | source | what was learned | wrote-to` line per
     fact to `## Entries` in `ingestion-log.md`. Never edit past lines.
   - Update the `last-synced-commit` (and `last-synced-date`) to the `HEAD` you
     captured in step 1, and add a parenthetical `(watermark advanced <old> ->
     <new>, <date>)` line.

6. **No-op path.** If the range was empty: still refresh the freshness stamp's
   date, and append one ledger line `... | git | no changes since <watermark> |
   none`. This makes a silent run distinguishable from a broken one.

7. **Report.** Tell the user, in a few lines: the commit range swept, the
   headline deltas, any README/architecture map updates you made, and any
   architecture change you are **proposing** for their approval.

## Provenance tags (same contract as repo-state.md)

- `[confirmed <date> <source>]` - stated in a commit message, a merged PR, a
  doc in the repo, or by the user. Example source tokens: `commit:<sha>`,
  `pr:#64`, `doc:docs/prd/README.md`, `user`.
- `[inferred <date> <source>]` - deduced from a diff or the shape of the change.
  Inferred lines are context only, never cite them as fact.

When a new fact contradicts an existing line, **supersede**: replace the line,
keep a short note that it changed, and log it. When it only updates scale
(counts, versions), update in place with the new date.

## What this skill does NOT do

- It does not run the code, run tests, or verify behavior - it reports what the
  history says changed, not whether it works.
- It does not write specs or PRDs - those live in `docs/prd/` and
  `docs/security/prd/` and are human-owned.
- It does not push or open PRs. Delivery of the updated context folder follows
  the repo rule (PR to `main`, never a direct push) and is the user's call.

## When to run

- On demand: "sync repo context" / "update the context folder".
- After landing a substantive change in this session (new module, endpoint, or
  PRD), so the folder reflects it.
- When `repo-state.md`'s freshness stamp is more than 14 days old.
