# Ingestion log

Append-only record of what the `syncing-repo-context` skill has ingested, plus the watermark that tells it where the last sweep stopped.

**Entry format** (one line per fact learned):

```
YYYY-MM-DD | source | what was learned | wrote-to
```

- `source` is one of: `commit`, `pr`, `doc`, `user`, `git` (for repo-structure facts like worktrees/branches).
- `wrote-to` names the file (and section) that changed, or `none` if the fact was noted but nothing needed changing.
- **Never edit or delete past entries.** Corrections are new lines that supersede old ones.
- Watermark advances are recorded as parenthetical lines interleaved with entries.

## Watermark

```
last-synced-commit: 14eb23f23f554e564d0cc7f20748c03337d0b600
last-synced-date: 2026-08-06
```

The skill sweeps `git log <last-synced-commit>..HEAD`. Advance this to `HEAD` only after a full sweep succeeds. Correctness check: a second run immediately after a sync must find nothing to ingest.

## Entries

```
2026-08-06 | user   | Context folder created to give a human/AI coworker a code-free map of the repo, kept current by an on-demand skill (not CI). | docs/context/README.md, architecture.md, repo-state.md
2026-08-06 | git    | Repo has backend/ (FastAPI, in-memory), frontend/ (static), landing/ (React/Vite on Vercel); no database, no frontend build step. | docs/context/architecture.md
2026-08-06 | commit | Seed reading of the last ~20 commits on main: PRD-P (payments), PRD-M (manuals), PRD-R (paired review), PRD-I (ingestion), PRD-C (credentialing), in-case UX, doctor email verification + task notifications. | docs/context/repo-state.md (Recently landed)
2026-08-06 | git    | In-flight local worktree branches noted as inferred current focus (claude/asclepius-incase-ux, claude/team-ai-spotlight, task-notifications). | docs/context/repo-state.md (Current focus)
```
(watermark set to 14eb23f at folder creation, 2026-08-06)
