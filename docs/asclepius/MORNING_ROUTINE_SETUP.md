# Turning the morning routine on

The routine posts one brief per channel per day at 7am local and emails each
doctor what landed in their rooms. **It now ships ON**: it defaulted to off, and
an off-by-default gate is indistinguishable from a quiet community, which is
how a finished routine sat dormant in production for weeks.
`COMMUNITY_MORNING_ENABLED=0` is the kill switch and still works.

## 1. Environment (Railway)

```
ANTHROPIC_API_KEY=...            # sourcing needs it; without it nothing is searched
PUBLIC_BASE_URL=https://app.archangelhealth.ai   # unsubscribe links die without it
INTERNAL_TOOL_SECRET=...         # already set if the other /internal endpoints work
COMMUNITY_MORNING_ENABLED=0      # ONLY if you want it off
```

Optional, with sensible defaults: `COMMUNITY_MORNING_HOUR_LOCAL=7`,
`ARCHANGEL_HOME_TZ=America/New_York`, `COMMUNITY_COUNTRY_MIN_MEMBERS=3`,
`COMMUNITY_EVENTS_MAX=3`, `COMMUNITY_WEBSEARCH_MAX_USES=5`.
`COMMUNITY_DISCUSSION_DOW` is unset by default, which means the discussion
prompt fires **every** morning; set it to a weekday number (`2` = Wednesday) to
pin it back to one day.

Without a key or a provider nothing breaks and nothing posts, and the run says
so: every run records a `reason` (`no_model_key`, `no_search_provider`,
`provider_error`, `nothing_found` …), surfaced on the admin community tab under
"The daily routine". A silent morning and a working one are no longer the same
row.

### Paid retrieval (recommended)

The Anthropic search tool alone is one vendor and one index, and its failure
mode without a key is silence that looks exactly like a quiet day. Add the paid
rungs:

```
COMMUNITY_SEARCH_PROVIDERS=exa,firecrawl,anthropic
EXA_API_KEY=...
FIRECRAWL_API_KEY=...
COMMUNITY_SEARCH_DAILY_CALL_CAP=40     # calls per provider per UTC day; 0 = no cap
```

Exa and Firecrawl are **retrievers**: they return results and the model then
selects among them. This makes the citation gate stronger rather than weaker,
because the allowlist becomes a set built from a search response instead of one
parsed back out of the model's own prose. A URL the search never returned still
never reaches a doctor, on either path.

With Firecrawl configured, the weekly discussion prompt also runs an agentic
second step: it fetches the page it is about before writing the summary. It is
the one item that earns the extra call, because it runs weekly, claims to
summarize a specific source, and asks a room of physicians to argue about it.

The cap counts **calls, not dollars**. Per-provider pricing drifts, and a spend
figure the code cannot verify would read as a guarantee. The ledger is durable
(`community_search_budget`), so a restart does not hand the day a fresh budget,
and a redeploy loop cannot spend it repeatedly. If the ledger itself is
unreachable the check fails **open**: the cap exists to stop a runaway loop, not
to police a correct one, and refusing every search because SQLite hiccuped is
the more expensive failure.

Turning this on also stands the older news-digest **email** down, because the
morning email carries the digest and two automated emails on the same morning
is one too many. The in-app digest post is unaffected.

## 2. The clock

With the gate on the app drives itself: an hourly in-process tick calls the
same code the endpoint does. That is enough, and it is why this feature does
not *depend* on anything outside Railway.

The external backup is committed at `.github/workflows/community-morning.yml`.
It runs hourly at ten past; GitHub scheduling can be delayed, so the app's
15-minute news loop remains the primary clock. Do not replace the committed
workflow with the older example file in this directory.

Then two repository secrets (Settings → Secrets and variables → Actions):

| Secret | Value |
|---|---|
| `MORNING_BASE_URL` | `https://app.archangelhealth.ai` |
| `INTERNAL_TOOL_SECRET` | the same value the backend has |

The workflow drives the whole routine, not only the brief: the morning, the
per-doctor newsletter, the news and papers digests
(`run-digest?...&scheduled=true`, which applies the digest schedule rather than
posting on every call), the staff spotlight and the weekend webinar series.
Both triggers share the run ledger, so running both cannot double-post:
whichever arrives first marks the run and the other finds nothing due.

News runs in its own job before the other routines, so an email or morning
timeout cannot prevent its attempt. Missing secrets and application errors
(including HTTP 200 with `ok=false`) fail the workflow. A green setup check is
not proof that a digest was published: inspect the news result and the admin
Community run ledger.

News is due daily at `COMMUNITY_DIGEST_NEWS_HOUR_UTC` (default 13:00 UTC,
06:00 Pacific during daylight saving time). A successful scheduled news day
requires a published digest. Empty or failed attempts retry after two hours,
keeping selected stories available; a restart does not erase the daily claim.
Invalid compose output receives at most two correction requests with the
validator's feedback. Every corrected draft must still pass all format, source
URL and write-path checks. If sources or the model remain unavailable, the run
fails visibly instead of filling the channel with invented or duplicated news.

## Diagnosing a missed news day

1. Open Operations → Community → The daily routine. Check the `news` row's
   timestamp, item count and reason. `contract_violation` means the writer's
   output failed validation; `no_source_items` means no source candidates.
2. Inspect the `community-morning` GitHub Actions run. Confirm both repository
   secrets above exist. Historical runs before the recovery fix returned
   success even when both were missing and no endpoint was called.
3. Read `GET /internal/community/status` with the existing internal bearer
   credential for loop state and attempt/failure timestamps. Railway runtime
   logs contain the specific validation error.
4. After resolving the cause, use
   `POST /internal/community/run-digest?kind=news&scheduled=true` and verify
   the published post and ledger. It honors backoff and today's claim. A bare
   trigger bypasses the schedule and may queue subscriber email, so do not use
   it merely as a diagnostic probe.

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

## Seeing it work with no key at all

The routine could not be run offline. Three things blocked it independently,
each on its own sufficient to produce a silent morning that looked exactly like
a quiet web, and all three are fixed:

* `search_providers.available("anthropic")` returned False without a key, and
  `_ask` short-circuits on it BEFORE the LLM client is consulted, so
  `ASCLEPIUS_LLM_PROVIDER=fake` was never reached;
* the fake transport intercepted any call carrying `tools` and answered from the
  tool's own schema, but Anthropic's hosted web search is a SERVER-SIDE tool
  with no `input_schema`, and the caller wants the model's text having searched;
* both morning fixtures returned prose, so `_parse_items` produced `[]`.

With those closed, one command posts a real brief into the local community:

```bash
cd backend && ASCLEPIUS_LLM_PROVIDER=fake COMMUNITY_FAKE_SEARCH=1 \
  COMMUNITY_MORNING_ENABLED=1 \
  python3 -c "import asyncio, main; from community import morning; \
  print(asyncio.run(morning.run_morning(force=True, only='morning:events')))"
```

`{'ran': ['morning:events'], ...}` means it posted. `quiet` means it sourced
nothing, and the run ledger records which reason.

Swap `only=` for `morning:news`, `morning:opportunities` or
`morning:discussion` to see the other three. The fixture varies its items per
call, so running all four in one session does not have the cross-channel dedupe
swallow every scope after the first.

`COMMUNITY_FAKE_SEARCH` is a SECOND switch on purpose. The test suite sets
`ASCLEPIUS_LLM_PROVIDER=fake` for every run, so keying the harness on the
transport alone would switch it on underneath the tests that verify the
citation gate and the missing-key reason.

**The gate is never skipped, only its allowlist is substituted.** Its contract
is that a URL the search never returned must never reach a physician; under the
fake there is no search, no model and no physician, and the fixture URLs are on
a reserved domain. `_keep_cited` still runs and still refuses anything that is not http(s), so a
`javascript:` URL is refused in the harness exactly as in production. What
changes is where the allowlist comes from: the fixture's own URLs, on a
reserved domain. `ai/model_config.assert_fake_llm_not_in_production`
refuses to boot a fake in production, so it cannot be reached there.

Run the same scope twice and the second says `quiet`. That is not a broken
harness, it is the cross-channel dedupe (`community_content_items`), and it is
the same ledger production relies on to stop a conference being announced twice.
The fixture is deterministic, so its URLs are already in the ledger. Point the
three DB paths at a temporary directory for a clean run:

```bash
TMP=$(mktemp -d)
cd backend && ASCLEPIUS_LLM_PROVIDER=fake COMMUNITY_FAKE_SEARCH=1 \
  COMMUNITY_MORNING_ENABLED=1 COMMUNITY_DB_PATH=$TMP/c.db \
  ASCLEPIUS_DB_PATH=$TMP/a.db TEAM_DB_PATH=$TMP/t.db \
  python3 -c "import asyncio, main; from community import morning; \
  print(asyncio.run(morning.run_morning(force=True)))"
```
