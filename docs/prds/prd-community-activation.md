# PRD: Community activation (group E)

The community subsystem is substantial and dormant. Eight core channels,
per-specialty and per-country rooms, a morning routine with local-7am timing,
citation-verified web search, a specialty-personalized newsletter, events,
polls, pins: all built, none of it running in production, because every gate
that would turn it on ships off and every way it switches itself off is
silent. This PRD turns it on, and adds the pieces the Sep 1 meeting asked for
that do not exist yet: subspecialty and city rooms, a real poll for the weekly
discussion, an admin way to post as the Archangel persona, and the daily
staff-only AI spotlight from closed PR #60.

## Problem (from the meeting)

Doctors should land in rooms where their colleagues actually are: same
specialty, same subspecialty, same city, same country. The bot should make the
place feel alive daily (events, research, news, a discussion prompt people can
actually vote on), with pinned explainers, personal-feeling posts from the
Archangel account, and a morning email personalized by specialty. The bar
named in the meeting is Reddit-thread liveliness. The decision recorded during
planning: the in-app community is the platform for all of it. Nothing here is
Slack.

## Decisions

Locked with the founders:

* **In-app for everything.** No Slack API, no external community surface.
* **Per-case rooms are group DMs, not channels** (locked during planning);
  they belong to the task-pipeline PR (group D), not here. This PRD leaves
  `get_or_create_dm` (`community/store.py:837`) untouched.

Made here, with rationale:

* **Subspecialty and city rooms reuse the country mechanism wholesale**: same
  member-count threshold (default 3), same global visibility, same stickiness
  (a room with history never vanishes: `channel_has_messages`,
  `community/store.py:480`; `visible_channels`, `community/router.py:483`).
  A room of one is a directory entry, not a community, and the country code
  already learned that lesson.
* **Subspecialty rooms come from a curated alias map, not free text.**
  Subspecialties live in `credentials_json` as a free-text array
  (`ship.subspecialties`, surfaced in `community/router.py` member
  serialization and `asclepius/constants.py:1270`). Slugifying free text
  yields `#ckd`, `#chronic-kidney-disease` and `#c.k.d.` as three empty
  rooms. A config-only `community/subspecialties.py` (mirroring
  `countries.py`: no DB access, plane isolation holds) maps known aliases to
  one slug per subspecialty; an unmapped subspecialty simply has no room
  until someone adds it, exactly the countries rule.
* **City rooms key on an optional self-reported practice city.** No reliable
  city field exists in the Tier A ship today, and the buyer-facing credential
  block deliberately never ships city (`asclepius/credentials.py:94`), which
  is about buyers, not colleagues: members already see each other's real
  names. The profile work in PR-1 (group B) adds `practice_city`; the channel
  mechanism lands here and rooms appear as the field fills in. Until then
  city rooms exist in code and never cross threshold, which costs nothing.
* **The weekly discussion becomes a poll at the store layer, not through the
  HTTP polls API.** `POST /community/polls` requires a member and deliberately
  authors polls as their creator, never the bot
  (`community/social.py:204-240`). The morning composer instead calls the
  store primitives directly (`create_poll`, a system message of kind `poll`,
  `link_poll_message`) authored by `SYSTEM_USER_ID`
  (`community/system_posts.py:33`), so member polls stay member-authored and
  the weekly prompt becomes votable without loosening the API.
* **The admin composer posts as the bot, from the community frontend, not
  from asclepius.js admin regions.** Tej's admin redesign (PR-4 territory)
  owns those regions; a composer buried there would be rewritten in a week.
  `community.js` already renders for staff; a staff-gated composer there,
  backed by one admin endpoint, keeps this PR out of the collision zone.
* **PR #60 re-lands rather than reopens.** The branch
  (`claude/team-ai-spotlight`, e20b34c) predates Community v2.1 merges; its
  ideas survive (staff-only channel, one story a day picked from the digest
  pool including `skipped` rows, WS and message-access scoping for
  `staff_only`), its diff does not. Re-implement against current
  `digest.py` / `router.py` / `store.py`.

## Requirements

**A. Enabling the routine in production**

1. Railway env (the deploy checklist, not code): `COMMUNITY_MORNING_ENABLED=1`
   (gate: `community/morning.py:52`), `COMMUNITY_NEWS_ENABLED=1`
   (`community/digest.py:343`), and `COMMUNITY_DB_PATH` on the mounted
   volume (`/data/community.db`). The newsletter has no flag of its own: it
   rides the morning gate (`newsletter.py:45`, `enabled()` returns
   `cmorning.enabled()`). The DB path is the named silent failure: the
   default path (`community/store.py:127-129`) is inside the container, so
   every deploy resets the dedup ledger and the run history.
2. `GET /internal/community/status` reports gate, loop, last-run and email
   state after deploy. It exists on the unmerged `claude/community-routine-on`
   branch, not in this working tree (verified: `main.py` has only the
   `run-*` internal endpoints, lines 6698-6759). Merging that branch is a
   prerequisite of this PRD and is already first in the PR-train plan.
3. The scheduler is the GitHub Actions hourly cron. The workflow file cannot
   be committed from this session (token lacks the workflow scope), so
   `docs/asclepius/community-morning.workflow.yml` is the source of truth; a
   person copies it to `.github/workflows/` and sets the two repository
   secrets it names (`MORNING_BASE_URL`, `INTERNAL_TOOL_SECRET`). The
   in-process loop remains the fallback for a deploy without the cron; both
   share the run ledger and cannot double-post (`main.py:6584-6590`).
4. Acceptance for "live": status endpoint shows both gates true and loops
   running; one manual `POST /internal/community/run-morning` posts briefs;
   the next scheduled hour posts nothing extra (ledger holds).

**B. Subspecialty and city channels**

5. `ensure_default_channels` (`community/store.py:403`) seeds subspecialty
   rooms from the alias map and city rooms from the distinct normalized
   `practice_city` values passed by the caller, alongside the existing core,
   specialty, and country sets. main.py's startup
   (`startup_community`, `main.py:6553`) stays the only place that reads the
   roster and passes cohort inputs in; the community store still never
   queries users.
6. Visibility: a subspecialty room shows at
   `COMMUNITY_SUBSPECIALTY_MIN_MEMBERS` (default 3, floor 1) members, a city
   room at `COMMUNITY_CITY_MIN_MEMBERS` (same defaults), both sticky once
   they have history. Counting happens in `visible_channels`
   (`router.py:483`) beside the specialty and country branches, from
   `member_map` data only.
7. Deactivation mirrors countries: a subspecialty or city room whose cohort
   input disappears from the seed call is `is_active = 0`, never deleted, and
   a `None` cohort input leaves existing rooms alone
   (`store.py:455-468` behavior, extended).
8. Room descriptions follow the existing voice: what the room is for, in one
   sentence, no onboarding essay (pinned explainers already cover that).

**C. The weekly discussion post becomes a poll**

9. `_compose_discussion` (`community/morning.py:305`) keeps its topic search
   and dedup, and its output becomes: one system message in
   `#future-of-medical-ai` of kind `poll`, linked to a `community_polls` row
   authored by `SYSTEM_USER_ID`, with 2 to 4 options taken from the
   websearch prompt payload plus a standing "Something else, in the thread"
   option. Cadence unchanged: `COMMUNITY_DISCUSSION_DOW` (default 2,
   `.env.example:358`; `discussion_dow`, `morning.py:74`).
10. Voting, results, and the `poll.updated` broadcast work identically to a
    member poll: same store rows, same serializer, same WS event
    (`social.py:243` onward). The bot never votes.
11. If the topic search returns no options, fall back to the current
    prose-only prompt rather than posting a one-option poll. A poll with
    nothing to choose is worse than a question.

**D. Admin persona posting**

12. `POST /api/asclepius/admin/community/post` (admin auth): body
    `{channel_slug, body, announce?}`, delegating to `post_system_message`
    (`system_posts.py:64`), which already runs the PHI gate, masks URLs, and
    renders as the Archangel bot. `announce` is honored only for
    `#task-announcements`, matching the fan-out rule the function documents
    (`system_posts.py:87-93`).
13. The composer UI lives in the community frontend (`community.js`), visible
    to staff only: channel picker over admin-post-policy channels
    (`task-announcements`, `events`, `medical-ai-news`,
    `research-and-opportunities`) plus the staff channel from requirement 15,
    a textarea, and a post button. Nothing is added to `asclepius.js` admin
    regions.
14. Every persona post is audit-logged with the acting admin's id (the
    endpoint knows the admin; `post_system_message` logs the system actor,
    the route logs who pressed the button).

**E. The daily AI-in-medicine spotlight (PR #60, re-landed)**

15. A `staff_only` channel `#team-ai-spotlight` (new `staff_only` column on
    `community_channels` via the guarded ALTER pattern the store already
    uses, `store.py:370-401`), visible and reachable only for staff roles
    (`admin`, `qa_reviewer` per `member_map`, `router.py:266` onward).
16. One story a day, picked from the digest item pool including
    `skipped` rows so run order against the news digest does not starve it
    (the closed branch's `candidate_items_for_spotlight` reasoning), posted
    by the bot, tracked in the digest run ledger, manual trigger included.
17. The two visibility gaps the closed PR found are closed against current
    code: the WS hub must not fan staff-channel messages to non-staff
    connections, and `_require_message_access` must check channel visibility,
    not just DM membership, for every by-id path (thread, edit, react,
    attachment download).

**F. Morning email digests**

18. Nothing new to build: the newsletter already personalizes by specialty
    and cohorts by country at local 7am (`newsletter.py:180-227`), stands
    down per doctor when nothing happened (`send_for_member` returns `quiet`,
    `newsletter.py:154-156`), and respects the unsubscribe pref. What remains
    is enabling (requirement 1) and verifying standdown: one email per doctor
    per day at most, zero when the channels were silent, ledger key
    `morning:newsletter:{code}` holds across a re-run in the same local day.

## What exists today

Verified in the working tree:

* Channels: 8 core defs (`community/store.py:34-91`), specialty defs from the
  registry (`store.py:94-112`), country defs and timezones
  (`community/countries.py:41-117`), threshold-plus-sticky visibility
  (`router.py:424-509`), startup seeding from the roster
  (`main.py:6553-6592`).
* Morning routine: scopes per channel with local-7am due logic
  (`morning.py:199-263`), composers for events / news / opportunities /
  brief / discussion (`morning.py:305` for discussion), run ledger, gate off
  by default. Discussion is currently a prose post, kind `discussion_prompt`.
* Polls: full store and HTTP surface, member-authored only
  (`social.py:204-262`), poll tables (`store.py:296-320`).
* Bot posting: `post_system_message` with PHI gate and announce fan-out
  (`system_posts.py`), used by `task_notify.post_community_announcement`; no
  admin-facing route reaches it, the bot is only callable from code.
* Newsletter and news digest as described above; internal trigger endpoints
  (`main.py:6698-6759`); workflow doc template
  (`docs/asclepius/community-morning.workflow.yml`).
* Not in this tree: `/internal/community/status` and loop visibility
  (unmerged `claude/community-routine-on`); everything from PR #60.

## Gaps and changes per file

* `backend/community/subspecialties.py` (new): config-only alias map,
  `channel_defs(names)` mirroring `countries.channel_defs`.
* `backend/community/store.py`: guarded ALTERs adding `subspecialty`,
  `city`, `staff_only` columns to `community_channels` (pattern at
  `store.py:370-401`: `if "col" not in ch_cols: conn.execute("ALTER TABLE
  community_channels ADD COLUMN col TEXT")`); `ensure_default_channels`
  accepts the two new cohort inputs with the None-means-leave-alone rule;
  spotlight candidate query.
* `backend/community/router.py`: subspecialty and city threshold functions
  and `visible_channels` branches; `member_map` serializes normalized
  subspecialty slugs and city; staff-only filtering in `visible_channels`,
  `_require_message_access`, and the WS broadcast path.
* `backend/community/morning.py`: poll-emitting `_compose_discussion` with
  prose fallback.
* `backend/community/digest.py`: `run_spotlight_digest` (one story, staff
  channel, own ledger kind).
* `backend/community/system_posts.py`: a small `post_system_poll` helper so
  poll authorship stays in one reviewed place.
* `backend/main.py`: admin persona endpoint; spotlight trigger; startup
  passes the new cohort inputs; (via the routine-on merge) the status
  endpoint.
* `frontend/asclepius/community.js` + `community.css`: staff composer; poll
  rendering already exists.
* `.env.example`: the two new threshold vars beside
  `COMMUNITY_COUNTRY_MIN_MEMBERS` (line 354); spotlight flag if gated.
* No change to: `websearch.py` (search surface is sufficient),
  `newsletter.py`, `events.py`, DM code.

## Email and notification touchpoints

* No new email. The morning newsletter and the news digest email are the
  existing surfaces; enabling them is requirement 1.
* New in-app: the weekly poll post, spotlight posts (staff-visible only),
  admin persona posts. All bot-authored, all through the PHI gate.
* The spotlight channel must not leak into the newsletter: `staff_only`
  channels are excluded from `_member_channels` for non-staff recipients
  (`newsletter.py:60` area), asserted by test.

## Test plan

Plain pytest functions with WHY docstrings, outside-in where a surface
exists. New `tests/test_community_activation.py` plus additions:

* Subspecialty and city rooms: below threshold hidden, at threshold visible,
  sticky after one message despite the cohort shrinking (the country rule,
  proven for the new groups); alias map collapses variants to one slug;
  unmapped subspecialty creates nothing.
* Seeding: `None` cohort inputs deactivate nothing (the safety rule that
  keeps a roster hiccup from retiring every room).
* Discussion poll: Wednesday run produces one poll authored by `u-system`
  with linked message and votable options; empty search falls back to prose;
  a member vote updates results; second run same day posts nothing (ledger).
* Admin persona endpoint: staff-gated, posts as the bot into an admin-policy
  channel, refused for a plain member, audit row carries the admin id,
  announce fans out only for `#task-announcements`.
* Spotlight: staff sees the channel and the post, a non-staff member gets
  404 on the channel, on the message by id, and no WS frame (the two
  visibility gaps, held closed by test); one story per day across both jobs
  regardless of run order.
* Newsletter standdown: quiet channels send zero mails; a second same-day
  run skips the cohort; unsubscribe honored; staff channel content absent
  from a member's email.
* Status endpoint (after the routine-on merge): gates and loops reported
  separately, no secret values in the body.

## Out of scope

* Group-DM case rooms and routing announcements into them (group D).
* Webinars, conferences, podcast (post-seed, not product work).
* Per-case channels as public channels (decided against;
  `docs/asclepius/CASE_BATCHES_AND_ROUTING.md` records why).
* User-created channels; the channel list stays code.
* Committing the workflow file itself (token scope; a person does it).
* Any admin-tab redesign work; the composer deliberately avoids
  `asclepius.js` (group F and Tej's branch own those regions).
* Railway dashboard changes themselves (Phase 0.5 checklist; this PRD names
  the values, a person sets them).
