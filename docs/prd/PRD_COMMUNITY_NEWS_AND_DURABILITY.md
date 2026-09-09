# PRD — Community: the vanishing news posts, a redesigned digest, and a full audit

Verified against `Archangel-Health-main (37)`.

---

## §1 Why the news is in the email but not in the channel

**It is not a community bug. The community database is being erased on every deploy.**

The trace, end to end:
- The digest loop posts into `#medical-ai-news` (`community/digest.py:354`) via
  `post_system_message` (`community/system_posts.py:292`), which inserts into
  `community_messages` and queues the email fan-out. The read path
  (`router.py channel_messages` → `store.list_messages` → `_serialize_messages`,
  which explicitly handles the `u-system` author at `community/router.py:923`) is correct. Realm
  scoping of the loop, the notify flush, and the email transport is correct.
- The community store resolves its file as `COMMUNITY_DB_PATH` **or
  `backend/community.db` inside the container** (`realm.py:127-128
  live_community_db`). Your Railway variable list (38 vars) contains
  `ASCLEPIUS_DB_PATH`, `TEAM_DB_PATH`, `ASCLEPIUS_DATA_DIR` — **and no
  `COMMUNITY_DB_PATH`.**
- The code already knows: `main.py:6669-6710` — *"community.db holds the entire
  community (channels, posts, events, the digest dedup ledger) and its default path
  is inside the container, so on RAILPACK it is ephemeral unless someone sets
  COMMUNITY_DB_PATH. Neither variable was in .env.example, which is how a deploy
  loses both without anyone choosing to."* The check is **WARN-only** (CRITICAL log +
  `/healthz`), deliberately not fail-closed.
- So: 13:00 UTC the digest posts and emails everyone → you deploy (you deploy many
  times a day) → the container is replaced → `community.db` is gone →
  `ensure_default_channels()` (`community/store.py:711`, `main.py:6937`) re-creates the seven
  empty channels at boot → Kalpesh opens `#medical-ai-news` and sees the empty-state
  hero. The email is the only surviving evidence the post existed.

Consequences you should know: **every post, DM, reaction, read marker, event, and
the digest's own dedup ledger have been wiped on every deploy since launch.** The
footer *"Messages are retained indefinitely unless an admin removes them"* is false in
production today. And because the dedup ledger resets, a same-day restart can email
the roster twice.

### 1.1 Fix — three layers, so it cannot recur

1. **Railway (do this today, before the code):** add `COMMUNITY_DB_PATH` pointing at
   the persistent volume beside the other stores (same directory as
   `ASCLEPIUS_DB_PATH`, e.g. `/data/community.db`). Redeploy. History from before this
   is unrecoverable; from this deploy on it persists.
2. **Code default:** `realm.live_community_db()` falls back to
   `os.path.join(dirname(ASCLEPIUS_DB_PATH or ASCLEPIUS_DATA_DIR), "community.db")`
   **before** the beside-the-code path — the durable directory is already known to the
   process, and the sandbox realm already derives its paths this way. A deploy that
   forgets the variable then still lands on the volume.
3. **Fail closed in production:** extend the `main.py:6630` production gate to the
   community and tenant stores. The comment's reason for WARN-only ("would brick every
   legitimate local run") is handled by the gate already keying on `ENV=production`;
   the reason "would take down a running deployment" is exactly the point — a
   deployment that silently erases its community on restart *should* refuse to start
   until the variable is set. Keep `STORAGE_GATE_ALLOW_EPHEMERAL` as the escape hatch.
   Surface all five stores on `/healthz` with `durable: true/false` and the resolved
   path, and show a red banner on the admin Community tab when any is false.

Test: boot with `ENV=production` and no `COMMUNITY_DB_PATH` on ephemeral disk → refuse
with the variable named; with `ASCLEPIUS_DB_PATH` on a volume and no
`COMMUNITY_DB_PATH` → community lands beside it; `/healthz` reports all five.

---

## §2 The digest itself — from markdown blob to a designed post

### 2.1 What is wrong today
`_COMPOSE_SYSTEM` (`digest.py:105-118`) asks the model for "markdown-lite": a bold
header, 2–4 bold section lines, bullets of `[title](url): one_liner`. The web renders a
subset of markdown (`community.js:170 renderBody`); the email renders **none** — the
notification snippet is `_snippet()` (`notify.py:188`), a whitespace-collapsed raw
string, which is why the email shows literal `**Medical AI Digest** **Clinical
Practice & Ethics** - [Opinion: …`. The post is a blob in both places, the model's
prose habits (em dashes, hashtags, hype) are unconstrained, and nothing about it is
designed.

### 2.2 The new contract — structure, not prose

The compose pass returns **JSON, not markdown**, and the product renders it:

```json
{
  "title": "Medical AI digest",
  "items": [
    {
      "headline": "FDA clears autonomous AI for diabetic retinopathy screening in primary care",
      "why_it_matters": "First reimbursed autonomous diagnostic; sets the template for other screening tools.",
      "source": "STAT", "url": "https://…", "section": "Regulation"
    }
  ]
}
```

Rules enforced in the prompt **and** by a post-processor that rejects the run if
violated (a failed run posts nothing, like today's parse failure):
- 3–5 items. Never more. If fewer than 3 clear the relevance bar, skip the day.
- `headline` ≤ 12 words, plain declarative, sentence case, no trailing period.
- `why_it_matters` ≤ 25 words, one sentence, written for a practising physician: what
  changes for care or for AI evaluation. No hype adjectives.
- **Banned characters and patterns:** em dash `—`, en dash `–` (use a comma or a
  period), `#` hashtags, `**`, `*`, emoji, "game-changer/revolutionary/exciting",
  exclamation marks, calendar dates (PHI filter). The post-processor strips or fails.
- `section` from a fixed vocabulary: `Research · Regulation · Deployment · Evals ·
  Opinion`. No invented sections.
- Source name is the publisher, never the URL host string.

`_SELECT_SYSTEM` stays (it already produces the factual `one_liner`); the compose pass
becomes a **shaping** pass over kept items, which is a smaller, more reliable job.

### 2.3 The rendered post (web)

A bot digest renders as a **card**, not a message bubble (`kind` is already
`digest_news` / `digest_papers` and already gets `cm-msg-digest` at
`community.js:1266` — that class becomes the card):

```
┌──────────────────────────────────────────────────────────────────┐
│ MEDICAL AI DIGEST                                  Archangel · 2h │
│                                                                    │
│ Regulation                                                         │
│ FDA clears autonomous AI for diabetic retinopathy screening        │
│ First reimbursed autonomous diagnostic; sets the template for      │
│ other screening tools.                                  STAT ↗    │
│                                                                    │
│ Research                                                           │
│ Frontier models score under 0.3 kappa on study risk-of-bias        │
│ More reasoning did not help; the failure is judgment, not recall.  │
│                                              Synthesis Bench ↗    │
│                                                                    │
│ Discuss in thread →                                                │
└──────────────────────────────────────────────────────────────────┘
```
- Eyebrow = title in mono caps; section labels in muted small caps; headline in the
  body weight (500), one line if possible; why-it-matters in regular weight, muted;
  source as a quiet link on the right, opens new tab.
- Items grouped by section in the fixed order; sections with no items don't render.
- One action: *Discuss in thread →* (the existing thread affordance). No reactions row
  clutter on digests.
- Old markdown-bodied posts (pre-migration) keep rendering through `renderBody` — the
  card path triggers only when `cards_json`/structured payload is present.

### 2.4 The email

`notify.py` builds the digest email from the **same structure**: subject
`Medical AI digest · {n} items`, then each item as headline (link) + why-it-matters,
grouped by section, then *Open the community →*. Never `_snippet()` of the body for
digest kinds. The morning-brief and welcome kinds keep their current templates.

### 2.5 Storage
`post_system_message` already accepts `cards`; add `payload_json` (the §2.2 object)
on the message row (additive column, NULLable). Body remains a plain-text fallback
generated from the structure (headline per line) for search and for old clients.

---

## §3 Community audit — what was verified, what needs a fix, what to test

| Area | Finding | Action |
|---|---|---|
| Message read path | `list_messages` clauses correct (channel, top-level, id paging, `community/store.py:1015`); tombstones handled; the `u-system` author is serialized by `_serialize_messages` (`community/router.py:923`) | none |
| Digest loop realm scoping | `with _realm.scoped(r)` per realm (`digest.py:618`); notify flush per realm; sandbox → outbox | none |
| **Durability** | community.db ephemeral (§1) | **fix** |
| **Dedup ledger** | lives in community.db → wiped with it → duplicate digests/emails after a same-day redeploy | fixed by §1; add a test that a restart on the same day does not re-post |
| Retention footer | "retained indefinitely" is false until §1 | after §1, true; keep copy |
| PHI gate | `_phi_clear` runs on body + card text before insert (`system_posts.py`) with `exact_date` exemption for morning kinds | keep; add the §2.2 banned-pattern check beside it |
| Channel slug uniqueness | `UNIQUE` on slug (`community/store.py:311`) | none |
| `/internal/community/purge` | manual, internal-auth only (`main.py:7225`); deletes all bot posts | keep, but log an audit event with actor; never call from a scheduler |
| Read-only channels ("RO") | composer disabled with copy "Only the Archangel team posts…" | fine |
| Email markdown leak | `_snippet()` on raw body (`notify.py:188`) | fixed by §2.4 |
| Empty-state hero | shows the correct room copy; masks the durability loss | after §1, an empty `#medical-ai-news` should read "No digest yet today" and show the next run time from the ledger, so silence is explained |
| DMs | pairwise `UNIQUE(user_a,user_b)` (`community/store.py:372`); private case channels member-scoped in the store | none |
| WebSocket live updates | ticket redemption realm-aware (`realm.py:376`) | smoke: post in one tab, appears in another without reload |
| Members list | reads the users plane, so it survives a wipe (why "Members (7)" looked fine while rooms were empty) | none |

Tests to add beyond §1/§2: post → redeploy simulation (new process, same DB path) →
post still listed; digest JSON contract (each rule in §2.2 has a negative case); card
renders for structured posts and `renderBody` for legacy; email template renders
structure with no `*`/`#`/`—` anywhere (grep the HTML); duplicate-run guard.

## §5 Design invariants

The rules the implementation must not break, stated once so a later change can
be checked against them rather than against a diff.

1. **A store's durability is a property of its RESOLVED DIRECTORY, never of
   whether a variable is set.** The two came apart the moment `community.db`
   learned to derive its path, and a check that asks the second question reports
   a durable deployment as broken and trains the operator to ignore it.
2. **A derived path outranks the beside-the-code path, always.** A file inside
   the container image must never be able to relocate a live database off the
   volume, whatever else is true. A developer's stale local file is a log line,
   not a precedence rule.
3. **The digest post is a STRUCTURE; body, card and email are three renderings
   of it.** No renderer may carry a fact the payload does not have, and the body
   is derived from the payload rather than written beside it.
4. **A contract violation posts nothing.** Never a partial digest, never a
   repaired one: repairing a rule that cannot be repaired losslessly means the
   product inventing editorial content.
5. **One sender per email.** A second mailer for the same post is a duplicate
   waiting for a configuration change, however carefully it is switched off.
6. **A run retires an item only when a model judged the item.** Stories the
   selector wanted and the digest had no room for stay candidates; burning them
   starves the pool while the ledger reports a healthy quiet day.
7. **Every "why is this room empty" answer must come from the same rules the
   scheduler uses.** An explanation computed beside `_due` will contradict it.

## §4 Do not touch
The PHI gate's rejection behaviour · channel seeding · DM privacy queries · the
realm-scoped store accessors · the sandbox outbox path.
