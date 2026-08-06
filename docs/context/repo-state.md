# Repo state

**Freshness:** last synced 2026-08-06, at commit `14eb23f`. *If this stamp is more than 14 days old, run the `syncing-repo-context` skill before relying on the "Changed since last sync" section below.*

> This file is written **only** by the `syncing-repo-context` skill. It records what is true about the repo **right now** and what changed recently. Every line carries a provenance tag:
> - `[confirmed <date> <source>]` - stated in a commit message, a PR, a doc, or by the user.
> - `[inferred <date> <source>]` - deduced from diffs or the commit log; treat as a hint, not authority.
> Do not hand-edit this file; run the skill instead.

## Live right now

- Product runs as a single FastAPI backend (`backend/`) serving a static frontend, plus a separate React landing site (`landing/`). No database; in-memory data resets on restart. [confirmed 2026-08-06 doc:AGENTS.md]
- Three product surfaces share the one repo: CareGuide (surgical patient platform), Asclepius (LLM eval portal), Community (physician community). [confirmed 2026-08-06 doc:AGENTS.md]

## Changed since last sync

(none - this folder was seeded at commit `14eb23f`. The next run of `syncing-repo-context` will populate this section from `git log 14eb23f..HEAD`.)

## Recently landed (from the commit log at seed time)

These are the themes visible in the ~20 commits before the seed point. Tagged inferred because they are read off commit messages, not confirmed feature by feature:

- **PRD-P (payments)** and **PRD-M (instruction manuals)** merged at the seed commit. [inferred 2026-08-06 commit-log]
- **PRD-R (paired review):** two independent labels per case, then one paired review, in the Asclepius review console. [inferred 2026-08-06 commit-log]
- **PRD-I (ingestion)** and **PRD-C / credentialing** merged into the payments and review branches. [inferred 2026-08-06 commit-log]
- **In-case UX** and **onboarding redesign** landed on main. [inferred 2026-08-06 commit-log]
- **Doctor sign-up email verification + task-assignment notification emails** (PR #59, #64). [inferred 2026-08-06 commit-log]
- An em-dash guard was added and then **removed** (`6df6d4a`); do not reintroduce a mechanical em-dash guard without checking that history. [inferred 2026-08-06 commit-log]

## Current focus / in-flight branches

Inferred from local worktree branches at seed time (these are working branches, not necessarily merged):

- `claude/asclepius-incase-ux` - in-case UX work. [inferred 2026-08-06 git:worktree]
- `claude/team-ai-spotlight` - community "team AI spotlight". [inferred 2026-08-06 git:worktree]
- `claude/asclepius-task-notifications`, `claude/asclepius-task-notify-hardening` - task-notification work. [inferred 2026-08-06 git:worktree]
- `claude/doctor-email-verification-and-task-notifications` - merged via PR #64. [inferred 2026-08-06 git:worktree]

## Active PRDs

The living PRD suites already in the repo (read these for spec-level detail):

- `docs/prd/` - the triage PRD suite (initial / intra-op / post-op / pre-op re-tier) with its own `README.md` integration map. [confirmed 2026-08-06 doc:docs/prd]
- `docs/security/prd/` - `PRD-1` through `PRD-8`, the HIPAA / compliance control PRDs. [confirmed 2026-08-06 doc:docs/security]
- `docs/asclepius/` - `PRODUCT_STATE.md` and handoff notes for the eval portal. [confirmed 2026-08-06 doc:docs/asclepius]
