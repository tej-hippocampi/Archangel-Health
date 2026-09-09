# PRD — The digest as an editorial card, and a community that feels like colleagues are in the room

Verified against `Archangel-Health-main (38)` and re-audited against the tree at
build time — see §9 for every citation that had drifted and every decision the
code forced. Builds on the structured digest contract already landed
(`payload_json`, `community/store.py:606`; compose returns
`{items:[{headline, why_it_matters, source, section, url}]}`, validated by
`community/digest_contract.py`, whose prompt is `_COMPOSE_SYSTEM`,
`community/digest.py:116`).
Design decisions below were approved by Tej on 9 Sep: **Top story** lead ·
**icon + tint per tag** · **greeting + presence bar, member profile cards, warmer
empty states** (no "Today" rail this round) · **channel post + pinned home card**.

---

## §0 What's wrong with today's card (screenshot, 9 Sep)

It reads as a list: a mono eyebrow, a section label, a headline, a gray sentence, a
source link that says "STAT ↗". Nothing is promoted, nothing is tinted, the headline
and the description say the same thing twice, and the whole thing has the visual
weight of a settings page. The structure underneath is now right; the presentation
is not.

---

## §0.5 The governing rule: fewer words than feels safe

Tej's instruction, verbatim in spirit: *design it like Rick Rubin would.* Every element
must earn its place by being removed and missed. Word budgets are hard limits, not
targets — the post-processor and the render tests enforce them:

| Element | Budget |
|---|---|
| Digest title | 3 words ("Medical AI Digest") · no deck under it |
| Lead headline | ≤ 10 words |
| Lead deck | ≤ 25 words, one sentence |
| Why it matters | ≤ 14 words, no label — the lime wash *is* the label |
| Compact item | headline + why-it-matters only; no deck |
| Tag | one word |
| Source | `Full article →` and the publisher name. Nothing else |
| Greeting | one line, ≤ 9 words |
| Empty state | one sentence ≤ 12 words + one button of ≤ 3 words |
| Profile card | 3 stats, 1 action |

If a line can be deleted and the card still reads, delete it.

---

## §1 The digest card

### 1.1 Anatomy

```
┌────────────────────────────────────────────────────────────────────────────┐
│  Medical AI Digest                                          Tuesday · 4 stories│  ← serif, 22px. No deck.
│                                                                                │
│  ┃ TOP STORY                          ⚖ REGULATION                             │  ← lead: 3px amber rule on the left,
│  ┃ Federal regulators open closed-door talks on clinical AI policy             │     26px headline, 2-line deck
│  ┃ The rules for "approved" clinical AI are being written now, without doctors.│  ← deck, one sentence
│  ┃ You will practise under them.                                               │  ← lime wash, no label

│  ┃                                                   [ Full article → ]  STAT   │
│                                                                                │
│  ─────────────────────────────────────────────────────────────────────────    │
│  🏥 DEPLOYMENT                                                                  │  ← compact: tag, headline, one line
│  Drug-diversion AI works only when staff use it                                │
│  A monitoring tool is a policy, not a product.              [ Full article → ] │
│                                                                                │
│  📏 EVALS                                                                       │
│  ED trial: AI didn't beat clinicians                                            │
│  Loudest claims, thinnest evidence.                          [ Full article → ] │
│                                                                                │
│  Discuss →                                                                      │
└────────────────────────────────────────────────────────────────────────────┘
```

- **Title** — "Medical AI Digest" in the same serif stack the welcome letter uses
  (`Georgia, 'Iowan Old Style', 'Times New Roman', serif`), declared on
  `.cm-digest-title` **inside `community.css`** — never by importing `asclepius.css`
  or reusing `.asc-fr-letter-title`; 22px; right-aligned mono `{weekday} · {n} stories`.
  Nothing under it.
- **Lead ("Top story")** — the item with the highest `relevance`; 26px headline, one
  `deck` sentence (new field, ≤ 25 words), the `why_it_matters` on a lime wash with
  **no label**, a 3px left rule in the tag's tint. Badge `TOP STORY` in mono. **Breaking** replaces "Top story" only when the
  compose pass sets `urgent: true` (definition in §2.2) — never by default.
- **Compact cards** — remaining 2–4 items: tag chip, 17px headline, one line,
  `Full article →`. Hairline dividers, no boxes, no deck.
- **Tag chips** — icon + label + tint (Tabler outline icons, design-system washes):
  Regulation `scale` amber · Research `flask` teal · Deployment `building-hospital`
  green · Evals `ruler-measure` purple · Opinion `quote` gray. Fixed vocabulary;
  anything else fails the run.
- **Source** — the button is **`Full article →`** (primary-quiet style); the
  publisher name sits beside it in mono muted. Never a bare host string.
- **Footer** — `Discuss →`. Nothing else.
- Motion: none on render. Hover on a card lifts the headline color one step; that's all.

### 1.2 Copy rules (enforced by the compose post-processor, already in place)
Headline ≤ 10 words, a verb in every one ("open," "beat," "works"). Deck ≤ 25 words,
one sentence, one fact. Why-it-matters ≤ 14 words, second person allowed, said the way
Tej and Aryaa would say it to a physician friend — then cut in half. Banned: em/en dashes, hashtags, asterisks, emoji, "game-changer",
exclamation marks, calendar dates. Rejection = no post that day.

### 1.3 Placement
- Canonical: the `#medical-ai-news` message (`kind: digest_news`), rendered by
  `digestCardEl` (`community.js:1738`) whenever `digestOf` (`community.js:1595`)
  finds a payload on the message.
- The stream reaches it through `renderMessages` (`community.js:1357`), which
  builds each row with `messageEl`.
- The `cm-msg-digest` branch (`community.js:1795`) styles only the legacy body,
  and `cardsEl` (`community.js:1489`) is not used for digests.
- **Pinned home card**: the community landing (the view before a channel is opened)
  shows the latest digest in the same card component, collapsed to title + lead +
  `Read all {n} →` which opens the channel post. One component, two contexts.
- Email (`build_community_digest_post_email`, `onboarding_emails.py:2057`): same hierarchy — Top story with deck
  and why-it-matters, then compact items — rendered from `payload_json`, never from
  the body string.

---

## §2 The compose contract, extended (backend)

### 2.1 New fields per item
`deck` (string, ≤ 25 words, lead only; empty for others), `urgent` (bool, default
false). Existing: `headline`, `why_it_matters`, `section`, `source`, `url`,
`relevance` (from the select pass, `digest.py:100`, `:191`).

### 2.2 Lead selection and "Breaking"
- Lead = highest `relevance` from the select pass (`community/digest.py:100`),
  joined into the accepted payload on the normalised url and marked by
  `mark_lead` (`community/digest_contract.py:414`), which the pipeline calls at
  `community/digest.py:267`. Ties → earliest published.
- One definition of "which story leads" is read by the card, the pinned card and
  the email: `lead_and_rest` (`community/digest_contract.py:516`).
- `urgent: true` is allowed only when the select pass's `one_liner` contains a
  same-day event of one of four kinds — a regulatory decision/approval, a recall or
  safety notice, a major clinical trial readout, or a lab/model release with a
  medical claim — **and** the item is the lead. The compose prompt lists these four;
  the post-processor rejects `urgent` on anything else. Expected frequency: a few
  times a month. If it fires more than twice in a week, the prompt is wrong, not
  the news.
- Admin override: on the digest post's admin menu, `Set as top story` / `Clear
  breaking` rewrite `payload_json` and re-render. No re-run.

### 2.3 Storage and compatibility
`payload_json` gains `deck` and `urgent`; `title` stays "Medical AI Digest" (the
serif title is styling, not data). Old digests without `payload_json` keep the
legacy `renderBody` path; nothing is migrated.

---

## §3 The community feels like colleagues are here

### 3.1 Greeting + presence bar (top of the community, every view)

```
Good morning, Dr. Patel.        ● 3 online · 1 new digest
```
- One line. Time-of-day from the browser clock; last name from the profile; two live
  numbers from data the client already holds (members poll, latest `digest_news`).
  No new endpoint, no avatar row, no unread count (the channel list already shows it).
- Nobody online → the dot goes hollow and the number reads `0 online`. No sentence.

### 3.2 Member profile cards
Click a name anywhere (member list, message author, presence bar) → a card
(`cm-profile`, `community.js:3038`; upgraded in place): avatar with the specialty
tint, name and one credential line — "Nephrology · 15–19 yrs", built by
`credentialLine` (`community.js:2926`) — then three stats from `profileStats`
(`community.js:2958`), and one action: **Message**. Founders' cards add **Book 20 minutes**. No blurb, no
earnings, no tier.

The three stats are **Specialty · In practice · Country**, not cases · since ·
last active — see §9.2.

### 3.3 Warmer empty states, founders' voice
Every channel's empty state (`EMPTY_COPY`, `community.js:1200`; rendered through
`cm-empty-title` at `community.js:1281` and `community.js:1386`): one sentence,
one button. Copy, verbatim:
- `#general` — "The kitchen table. Say hello." · [Say hello]
- `#introductions` — "Who are you, and what do you see most in clinic?" · [Introduce yourself]
- `#questions-help` — "Ask. One of us answers today." · [Ask]
- `#future-of-medical-ai` — "Where is this going? Contrarian welcome." · [Start a thread]
- `#medical-ai-news` — "First digest at 6am PT." (no button)
- `#task-announcements`, `#events` — "We post here when there's work." (no button)
The `COLLEAGUE DISCUSSION ONLY. DO NOT POST PATIENT-IDENTIFIABLE INFORMATION.` footer
stays exactly as is — it is a rule, and it should not be warm.

### 3.4 What is deliberately not changing
Channel list, DMs, composer, PHI gate, read-only rooms, the retention footer, the
design tokens. "Homey" comes from people and fewer words, not from more of either.

---

## §4 Design system notes (so it stays Asclepius)
- No new hues. Tag tints are the existing washes (`--lime-wash`, and the amber/teal/
  purple/green washes already in `asclepius.css`); if one is missing, add it as a
  wash of an existing token, not a new brand color.
- Serif only on the digest title and the welcome letter. Nowhere else.
- One primary action per card. `Full article →` on items; `Read all →` on the pinned
  card; `Message` on a profile.
- Tag icons are **five inline SVG paths** defined once in `community.js` (Tabler
  outline shapes, 16px). The community page loads no icon font and must not start
  loading one for five glyphs.
- Everything through `h()`, no `innerHTML`; classes registered with the orphan guard:
  `cm-digest`, `cm-digest-title`, `cm-digest-lead`, `cm-digest-item`, `cm-tag`,
  `cm-tag-{regulation|research|deployment|evals|opinion}`, `cm-why`, `cm-greet`,
  `cm-presence`, `cm-profile-*` (extend existing).
- CSS in its own block, outside any foreign `@media` (the walkthrough lesson).

---

## §5 Execution order for Claude Code
1. Backend: extend compose contract (`deck`, `urgent`, four-kind urgency rule,
   post-processor checks); admin override endpoint; tests for rejection cases.
2. `digestCardEl` + tag chips + `Full article →`; wire into `renderMessages` for
   `payload_json` posts; legacy path untouched.
3. Pinned home card (same component, collapsed mode).
4. Email builder renders from `payload_json` with the same hierarchy.
5. Greeting + presence bar.
6. Profile card upgrade (+ founders' Calendly).
7. Empty-state copy.
8. Screenshot every state in the sandbox realm and attach to the PR: digest with
   Top story, digest with Breaking, pinned card, greeting with 0 and 3 online, each
   empty state, a profile card.

## §6 Tests
```
compose: headline ≤10, deck ≤25 (lead only), why ≤14 words — over budget rejects; urgent rejected off-lead or without one of the
         four kinds; banned characters reject; a run with <3 items posts nothing
render:  payload_json → digestCardEl; legacy body → renderBody; tag class matches
         section; Full article label; lead has rule + badge; Breaking only when urgent
home:    pinned card shows latest digest_news, collapsed; opens the channel post
email:   HTML contains no '*', '#', '—'; Top story first; every item links to url
greeting: one line ≤9 words; counts match members/digest state; 0-online renders hollow dot
profile: card fields; Message action; founders show Calendly; no earnings/tier fields
empty:   each channel renders one sentence ≤12 words; read-only rooms have no button
node --check; css brace balance
```

## §7 Do not touch
PHI gate · digest scheduling/ledger · `COMMUNITY_DB_PATH` durability fix · realm
scoping · the composer · the retention/PHI footers.

---

## §8 Blast radius — this PRD touches the community and nothing else

Verified in zip (38): the community is its **own page** (`community.html` loads
`community.css` + `community.js`), with its **own class namespace** (`cm-*`, 297
classes; zero `asc-*` component classes). The physician portal (`asclepius.js` /
`asclepius.css`), admin, onboarding, and landing are separate bundles. Staying inside
these files is what keeps the rest of the product untouched:

| Allowed to change | Must NOT change |
|---|---|
| `frontend/asclepius/community.js`, `community.css`, `community.html` | `asclepius.js`, `asclepius.css`, `_tokens.css`, `_base.css`, `admin.css`, any landing file |
| `backend/community/digest.py` (compose contract, urgency rule), `system_posts.py` (one line, below) | any other router, `store.py` schema beyond reading `payload_json`, the PHI gate's logic |
| `onboarding_emails.py` — **only** the digest email builder (`:2030`) | `_shell()` (`:86`, shared by every email in the product), any other builder |
| `admin_community.js` — the `Set as top story / Clear breaking` action | the rest of admin |

Three cross-cutting items the agent would otherwise miss:

1. **PHI gate coverage.** `_PAYLOAD_VISIBLE_KEYS` (`community/system_posts.py:75`) lists the payload
   fields that are scanned for PHI before a bot post is inserted. The new `deck` field
   **must be added to that tuple** or it becomes the one piece of digest text the gate
   never reads. This is the only edit outside the community frontend that touches
   safety; a test asserts every key the card renders is in the tuple.
2. **Design tokens.** Tag tints are new `cm-tag-*` rules in `community.css` using the
   existing token variables from `_tokens.css` (read, never edited). If a wash the
   palette lacks is needed, derive it in `community.css` from a token; do not add a
   token.
3. **Founders' Calendly on profile cards.** `FOUNDER_CALENDLY` lives in `asclepius.js:3020`,
   which the community page does not load. Pass the URL through the shell data
   `community.html` already hands to `community.js` (a `data-` attribute or the `/me`
   payload) — do not import from the portal bundle and do not hardcode a second copy.

Definition of done for this section: `git diff --stat` on the PR lists only the files
in the left column. Any file from the right column in the diff is a stop-and-explain.

---

## §9 As built — every place the tree said no

Written after the build, against the tree, and audited with `/prd-audit`
(exit 0). A PRD whose citations are asserted on faith is worse than one with
none, so each item below records what the PRD asked for, what shipped, and why.

### 9.1 Tag tints: three land, two substitute

§1.1 asks for amber · teal · green · purple · gray. §4 and §8.2 also say a
missing tint is **derived from an existing token, never added as a new brand
colour**, and `_tokens.css` is read and never edited. The palette has
`--orange-wash`, `--green-wash`, `--pink-wash`, `--lime-wash` and no blue, so
teal and purple cannot be mixed from it without inventing a hue.

The two rules cannot both be satisfied and §4 is the harder one, so:

| Tag | Asked | Shipped | Token |
|---|---|---|---|
| Regulation | amber | amber | `--orange-wash` |
| Deployment | green | green | `--green-wash` |
| Opinion | gray | gray | `--ink` at 6%, mixed in `community.css:1117` |
| Research | teal | **lime** | `--lime-wash` |
| Evals | purple | **pink** | `--pink-wash` |

Five distinct chips, zero new hues. Adding `--teal-*` and `--purple-*` to
`_tokens.css` is a design-system decision, not a community one, and it is the
right fix if the two substitutions read wrong on screen.

### 9.2 Profile stats: the verified card's three, not cases · since · last active

§3.2 asks for cases · since · last active. None exists on this plane:
`member_map` (`community/router.py:328`) is Tier A plus the users table, case
counts live behind the asclepius gate, and there is no join date or last-seen
column. §8 forbids widening another router for it.

Shipped: **Specialty · In practice · Country** — which are exactly
`backend/asclepius/card.py::CARD_FIELDS`, the set this panel was *already*
required not to drift from. Adding the other three means a real API change and
is a follow-up, not a decoration.

### 9.3 The digest room keeps explaining itself

§3.3 gives `#medical-ai-news` the copy "First digest at 6am PT." That sentence
says the same thing on a morning the pipeline is switched off, and telling a
quiet room from a broken one is why `digestEmptyCopy` exists — a previous audit
added it after the digest posted into a database the next deploy deleted, for
months, with the room looking exactly like a quiet week.

So the PRD's sentence is the fallback and the live schedule wins when it can be
read, shortened to one sentence to stay inside the §0.5 budget.

### 9.4 The admin lever is on the post, not the console

§8's table puts `Set as top story` / `Clear breaking` in `admin_community.js`.
That console's feed carries an author, a channel and a body string — no message
id and no payload — so the action would have needed a wider admin summary
endpoint to reach what it acts on. It ships on the digest post's own menu
(`digestAdminEl`, `community.js:1678`), admin-only, and absent from the pinned
card. `admin_community.js` is unchanged.

### 9.5 Word budgets ship with headroom

§0.5's caps are enforced by rejection and a rejected run posts nothing that day.
The caps are exact (`HEADLINE_MAX_WORDS = 10`,
`community/digest_contract.py:53`; `WHY_MAX_WORDS = 14`,
`community/digest_contract.py:54`; `DECK_MAX_WORDS = 25`,
`community/digest_contract.py:56`), and the compose prompt asks for **8, 12 and
20** so a model landing one word long does not cost the morning.

### 9.6 `urgent` is checkable, because "one of four kinds" is not

§2.2 allows `urgent: true` only for a same-day regulatory decision, recall,
trial readout or model release with a medical claim. A post-processor cannot
read a one-liner and decide which of those happened. So the model must also
name the kind, and `URGENT_KINDS` (`community/digest_contract.py:83`) is the
closed set the validator checks against — a claim with no kind, or a kind
outside the four, fails the run.

A flag on an item that is **not** the lead is cleared once, at compose time
(`community/digest.py`, after `mark_lead`), and logged. That rule is newer than
the digests already in the database, so the admin override normalises a payload
below `PAYLOAD_VERSION` on the way in — otherwise `Set as top story` on a legacy
row carrying an unearned flag would publish BREAKING through the endpoint that
refuses to grant one. Gated on the version rather than applied to every row,
because in a current payload an `urgent` flag on a non-lead item means something
worth keeping: a badge that *was* earned and then demoted by an earlier
promotion. Not in `mark_lead`
itself, which the admin override also calls: clearing there would destroy a
badge the compose pass legitimately earned, every time somebody promoted a
different story — see §9.7. The distinction is which claim is being judged. A
flag on the story that led when the digest was written was earned and is kept;
a flag on item four was never earned by anything, and leaving it in
`payload_json` would let `Set as top story` publish a BREAKING badge through the
endpoint that deliberately refuses to grant one.

### 9.7 Decks and badges survive on every item; only the lead's are drawn

The first cut cleared `deck` off non-lead items at post time, which made §2.2's
admin override destructive: promoting a story in the afternoon produced a 26px
headline with nothing under it, because its deck had been deleted at 6am.

The audit found the same bug one field over, and worse. `mark_lead` also cleared
`urgent`, so one promotion permanently erased a BREAKING badge the compose pass
had earned on one of the four same-day events — with no way back, because the
endpoint refuses to *grant* urgency by design. An override whose cost is
irreversible is an override nobody dares press.

So neither field is positional. Both stay on every item and only the **lead's**
are drawn — `digestItemEl` and the email's compact row never read either — so
promoting a story back restores exactly what it arrived with. `Clear breaking`
is still one-way, and it says so on the button.

### 9.8 The body follows the lead

`plain_text_body` grouped by `SECTIONS` order, so the top story appeared wherever
its section happened to fall: a digest led by a Regulation story showed a
Research story first in every notification snippet and every pre-card client.
One post, two hierarchies — the split the structured payload exists to close.

It is lead-first now, with the section as a label per item, matching the card and
the email. The admin override rewrites it alongside `payload_json`
(`set_message_payload(..., body=...)`), so a promotion moves the inbox preview
too.

### 9.9 The presence bar counts digests, not unread messages

§3.1 asks for "1 new digest" from the latest `digest_news`. The first cut took
the channel's unread number, which counts **every** unread message in
`#medical-ai-news` — and the morning routine posts briefs into that same room
(`community/morning.py`), so three briefs and no digest rendered "3 new
digests".

Unread messages are the last *n* rows in a room, so `loadLatestDigest` counts the
digests among that tail. Same data the client already holds, and it is now the
number the PRD actually asked for. The same fetch also widened from 5 rows to 25:
five newer briefs used to push the digest out of the window entirely and leave
the landing page with no card and, by design, no explanation.

### 9.10 Two smaller copy changes, recorded rather than left in the diff

- A channel with neither an `EMPTY_COPY` entry nor a server description now
  renders `#name` and nothing under it. The old generic fallback ("Open
  discussion between contributor physicians.") is gone: §0.5's rule is that an
  element earns its place by being removed and missed, and a sentence that is
  true of every room in the product tells the reader nothing about the one they
  are looking at.
- Profile stats say **"Not shared"** where a colleague has not filled a field
  in. The first cut used an em dash, which `test_no_em_dashes` correctly refused
  — and a dash in a stat slot reads as a value the product failed to load rather
  than a fact nobody has given us.

### 9.11 Blast radius, actual

Beyond §8's left column the diff also touches:

| File | Why |
|---|---|
| `community/digest_contract.py` | The contract §2 extends. §8 predates its extraction from `digest.py`. |
| `community/router.py` | The override endpoint §5.1 asks for. Additive: one admin-gated route. |
| `community/schema.py` | Its request model, `DigestLeadIn`. |
| `community/store.py` | `set_message_payload`, which writes `payload_json` and the derived `body`. **No schema change** — both columns exist and the insert path already writes them. |
| `community/notify.py` | One comment, re-citing a helper that no longer exists. |
| `ai/fake_llm.py` | The offline fixture has to satisfy the tightened caps, or every sandbox run records a contract violation. |
| `docs/asclepius/ROUTES.json` | Re-snapshotted for the one new route, as `route_baseline.py` requires. |

`asclepius.js`, `asclepius.css`, `_tokens.css`, `_base.css`, `admin.css`, every
landing file, `admin_community.js`, `_shell()` and every other email builder are
untouched.
