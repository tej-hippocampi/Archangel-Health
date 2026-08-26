# Turning the morning routine on

The routine posts one brief per channel per day at 7am local and emails each
doctor what landed in their rooms. It ships **off**.

## 1. Environment (Railway)

```
COMMUNITY_MORNING_ENABLED=1
ANTHROPIC_API_KEY=...            # sourcing needs it; without it nothing is searched
PUBLIC_BASE_URL=https://app.archangelhealth.ai   # unsubscribe links die without it
INTERNAL_TOOL_SECRET=...         # already set if the other /internal endpoints work
```

Optional, with sensible defaults: `COMMUNITY_MORNING_HOUR_LOCAL=7`,
`ARCHANGEL_HOME_TZ=America/New_York`, `COMMUNITY_COUNTRY_MIN_MEMBERS=3`,
`COMMUNITY_EVENTS_MAX=3`, `COMMUNITY_DISCUSSION_DOW=2`,
`COMMUNITY_WEBSEARCH_MAX_USES=5`.

Turning this on also stands the older news-digest **email** down, because the
morning email carries the digest and two automated emails on the same morning
is one too many. The in-app digest post is unaffected.

## 2. The clock

With `COMMUNITY_MORNING_ENABLED=1` the app drives itself: an hourly in-process
tick calls the same code the endpoint does. That is enough, and it is why this
feature does not *depend* on anything outside Railway.

The more reliable path is an external trigger, because it survives a restart at
the wrong moment and leaves a log a person can read. `community-morning.workflow.yml`
in this directory is ready to use:

```bash
mkdir -p .github/workflows
cp docs/asclepius/community-morning.workflow.yml .github/workflows/community-morning.yml
git add .github/workflows/community-morning.yml && git commit && git push
```

It could not be committed from the PR that added it: pushing a file under
`.github/workflows/` needs a token with GitHub's `workflow` scope, which the
agent's did not have. Adding it by hand, or through the GitHub web UI, works
fine.

Then two repository secrets (Settings → Secrets and variables → Actions):

| Secret | Value |
|---|---|
| `MORNING_BASE_URL` | `https://app.archangelhealth.ai` |
| `INTERNAL_TOOL_SECRET` | the same value the backend has |

Both triggers share the run ledger, so running both cannot double-post:
whichever arrives first marks the run and the other finds nothing due.

## 3. Check it

```bash
# Force one channel's brief regardless of the clock
curl -X POST -H "Authorization: Bearer $INTERNAL_TOOL_SECRET" \
  "$BASE/internal/community/run-morning?only=morning:events&force=1"

# Everything that is actually due
curl -X POST -H "Authorization: Bearer $INTERNAL_TOOL_SECRET" \
  "$BASE/internal/community/run-morning"

# The emails
curl -X POST -H "Authorization: Bearer $INTERNAL_TOOL_SECRET" \
  "$BASE/internal/community/run-newsletter"
```

The response names every scope that posted, was quiet, was skipped as not due,
or failed. Run the first command twice: the second should report the scope as
skipped, which is the idempotence you are relying on.

## What "quiet" means

No sources returned anything usable, so nothing was posted, and the run still
counts. That is deliberate: a channel that greets its members with three stale
conferences every morning teaches them to stop looking, and a routine that
retried all day against empty sources would fill the log instead of the
channel.

## What "failed" means

Either a source raised, or the PHI gate blocked the post. The gate skips system
posts silently by design, which is right until the thing being skipped is the
whole morning, so a blocked morning is recorded as a failure with
`error="post_blocked"` rather than passing as a quiet day.
