"""The digest's shape contract (Community News PRD §2.2).

The compose pass used to return "markdown-lite" prose and the product rendered
it twice, badly. The web renders a subset of markdown, the email renders none,
and the notification snippet is a whitespace-collapsed copy of the raw body, so
a post the model wrote as ``**Medical AI Digest** **Clinical Practice** -
[Opinion: ...`` arrived in physicians' inboxes exactly like that. Nothing about
the post was designed, and nothing constrained the model's prose habits.

So the compose pass returns STRUCTURE and the product renders it: a title and
3 to 5 items, each with a headline, a why-it-matters, a source, a url and a
section from a fixed vocabulary. Web draws a card, email draws a list, and the
body becomes a plain-text fallback for search and for clients that predate the
card.

**Strip or fail, and which is which.** A rule is enforced by STRIPPING when the
repair is unambiguous and loses nothing — a stray ``**``, an em dash between two
clauses, a trailing period on a headline. A rule is enforced by FAILING when
repairing it would mean inventing or discarding meaning: too few items, a
headline over the word cap, a hype adjective load-bearing in its sentence, a
section the vocabulary does not have. A failed run posts NOTHING, which is what
today's parse failure already does — an empty or malformed digest is worse than
no digest, and the ledger records the reason.

Nothing here reaches a model, a store or the network. It is a pure function over
one dict, so every rule below has a test that is a call and an assertion.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

#: The fixed section vocabulary, IN RENDER ORDER. The order is part of the
#: contract, not a display detail: a digest whose sections reshuffle daily reads
#: as a different product each morning, and the reader loses the ability to skip
#: to the part they care about.
SECTIONS: Tuple[str, ...] = ("Research", "Regulation", "Deployment", "Evals", "Opinion")

#: Lowercased, for tolerant matching of what the model returns.
_SECTION_BY_LOWER = {s.lower(): s for s in SECTIONS}

#: Bumped from 1 when the compose pass started clearing urgency off non-lead
#: items (Digest Design PRD §9.6). It is the ONE fact that separates a payload
#: whose stray badges have been dealt with from one written before that rule
#: existed, and the admin override needs to tell them apart: in a v2 payload an
#: `urgent` flag on a non-lead item is a badge that was earned and then demoted
#: by a promotion, and must survive so promoting it back restores it; in a v1
#: payload it may be a flag nothing ever earned.
PAYLOAD_VERSION = 2

MIN_ITEMS = 3
MAX_ITEMS = 5

#: The word budgets (Digest Design PRD §0.5). Tighter than the numbers this
#: module shipped with (12 and 25), and tightened deliberately: a headline that
#: runs to twelve words and a why-it-matters that runs to twenty-five say the
#: same thing twice, which is exactly what the redesign is removing. The cap is
#: the LINE, not the target — the compose prompt asks for two words under each
#: one, because a hard cap with no headroom is a daily outage waiting for the
#: morning a model lands one word over.
HEADLINE_MAX_WORDS = 10
WHY_MAX_WORDS = 14
#: The lead's one-sentence deck. Every item is written one and only the lead's
#: is DRAWN -- see ``mark_lead`` for why they are all kept.
DECK_MAX_WORDS = 25

DEFAULT_TITLE = "Medical AI Digest"

#: Titles per digest kind. Small, closed, and here rather than in the prompt:
#: the title is product copy, and a model that renames the digest every morning
#: is a model deciding branding.
TITLE_BY_KIND = {
    "news": "Medical AI Digest",
    "papers": "Papers of the week",
}

#: The four kinds of same-day event that may carry a BREAKING badge (§2.2).
#: Closed, and checked rather than inferred: "is this urgent" is a judgement the
#: model makes, and "did the model name one of the four kinds we agreed on" is a
#: fact this module can verify. Without the second, ``urgent: true`` is a
#: free-form claim and the badge fires on whatever the model found exciting.
URGENT_KINDS: Tuple[str, ...] = ("regulatory", "safety", "trial", "release")

#: Hype the post-processor refuses rather than repairs. Cutting the adjective
#: leaves a sentence that no longer says what the model meant it to say, and
#: rewriting it here would be this module inventing editorial content.
_HYPE = (
    "game-changer", "game changer", "gamechanger", "game-changing",
    "revolutionary", "revolutionise", "revolutionize", "exciting",
    "groundbreaking", "ground-breaking", "breakthrough", "unprecedented",
)

#: Month names, for the calendar-date rule. A full date in a digest line
#: false-trips the clinical PHI gate (``exact_date``), which silently drops the
#: whole post — so it is caught HERE, where the ledger can say why.
_MONTHS = ("january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december",
           "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept",
           "oct", "nov", "dec")
_DATE_PATTERNS = (
    # "March 14", "14 March", "Mar 3, 2026"
    re.compile(r"\b(?:%s)\.?\s+\d{1,2}\b" % "|".join(_MONTHS), re.I),
    re.compile(r"\b\d{1,2}\s+(?:%s)\b" % "|".join(_MONTHS), re.I),
    # 2026-03-14, 03/14/2026, 14.03.2026
    re.compile(r"\b\d{4}-\d{1,2}-\d{1,2}\b"),
    re.compile(r"\b\d{1,2}[/.]\d{1,2}[/.]\d{2,4}\b"),
)

#: Emoji, and deliberately NOT arrows.
#:
#: U+2190-21FF (arrows) belongs on no ban list here: "Discuss in thread →" and
#: "Open the community →" are the product's own copy, the source link on a
#: digest card ends in "↗", and a rule that called those emoji would fail the
#: designed post it was written to protect. The ban is on decoration a model
#: reaches for — 🎉, ✨, ❗ — not on the typographic vocabulary the product
#: already uses.
_EMOJI = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # pictographs, emoticons, transport, symbols
    "\U00002300-\U000023FF"   # misc technical
    "\U00002460-\U000024FF"   # enclosed alphanumerics
    "\U000025A0-\U000027BF"   # geometric shapes, dingbats
    "\U00002B00-\U00002BFF"   # misc symbols and arrows
    "\U0000FE00-\U0000FE0F"   # variation selectors
    "\U0001F1E6-\U0001F1FF"   # regional indicators
    "]+",
    flags=re.UNICODE,
)

#: A digit range joined by an en dash means "to", not a clause break. Handled
#: before the general dash rule, or "2020-2024" becomes "2020, 2024" and says
#: something the source did not.
_DASH_BETWEEN_DIGITS = re.compile(r"(?<=\d)\s*[–—]\s*(?=\d)")
_DASH_CLAUSE = re.compile(r"\s*[–—]\s*")
_MULTI_SPACE = re.compile(r"\s+")
#: A candidate sentence break: a terminator, whitespace, then a capital.
#: Lowercase after a period is a decimal or an abbreviation, never a new
#: sentence, so the capital does most of the filtering.
_SENTENCE_BREAK = re.compile(r"[.?!]\s+[A-Z]")

#: Words whose trailing period is part of the word. Without these, "U.S. FDA
#: clears it" and "Dr. Smith reports" both read as two sentences and a
#: perfectly good digest fails the contract for a rule it did not break.
_ABBREVIATIONS = frozenset((
    "dr", "mr", "mrs", "ms", "prof", "st", "vs", "etc", "inc", "ltd", "co",
    "no", "fig", "al", "approx", "est", "jr", "sr",
))


def _has_second_sentence(text: str) -> bool:
    """True when ``text`` really is more than one sentence.

    An abbreviation ends in a period and so does a sentence, and a rule that
    cannot tell them apart rejects "U.S. regulators cleared it" — correct
    English, one sentence, and exactly the register this field is written in.

    "Approved in the U.S. Adoption is slow." is genuinely undecidable without
    parsing, and this reads it as one sentence. That is the direction to be
    wrong in: a false failure costs the whole day's digest, and a false pass
    costs a slightly run-on line that the 25-word cap still bounds.
    """
    for match in _SENTENCE_BREAK.finditer(text):
        head = text[: match.start()]
        # A single letter before the period is an initialism (U.S., F.D.A.).
        letters = re.findall(r"[A-Za-z]+", head)
        last = (letters[-1] if letters else "").lower()
        if len(last) == 1 or last in _ABBREVIATIONS:
            continue
        return True
    return False


class DigestContractError(ValueError):
    """The compose pass returned something the product will not publish.

    Raised, not repaired, and never swallowed: the caller records the run as
    failed and posts nothing, which is the same outcome a JSON parse failure has
    always had. A digest that half-renders is worse than a missing one, because
    nobody goes looking for the missing one in the database.
    """


# ─── Cleaning (the "strip" half) ─────────────────────────────────────────────

def clean_text(raw: Any) -> str:
    """Normalise one human-visible string to the house rules.

    Everything removed here is either markup the renderers do not use (``**``,
    ``*``, ``#``, backticks) or a character the house style bans (dashes, emoji,
    exclamation marks). None of it changes what a sentence says, which is the
    test for belonging in this function rather than in the validator.
    """
    text = str(raw or "")
    text = _EMOJI.sub("", text)
    # Markup first: a "**word**" would otherwise leave stranded asterisks after
    # the character rules run.
    text = text.replace("**", "").replace("`", "")
    text = text.replace("*", "").replace("#", "")   # bullets, bold, hashtags
    text = _DASH_BETWEEN_DIGITS.sub(" to ", text)
    text = _DASH_CLAUSE.sub(", ", text)
    # An exclamation is a period wearing a hat. Collapse the doubled stop the
    # substitution can create ("Big news!." → "Big news.").
    text = text.replace("!", ".")
    text = re.sub(r"\.{2,}", ".", text)
    text = re.sub(r",\s*,", ",", text)
    text = _MULTI_SPACE.sub(" ", text).strip()
    # A comma the dash rule created at the very start or end of the string is
    # punctuation with nothing on one side of it.
    return text.strip(" ,")


#: The §2.2 character bans, as a detector rather than a scrubber. ``clean_text``
#: is the repair; this is the question "does this string still break the rules",
#: which is what a gate at the write path needs to ask.
_BANNED_PATTERNS = (
    ("dash", re.compile(r"[—–]")),
    ("asterisk", re.compile(r"\*")),
    ("hashtag", re.compile(r"#")),
    ("exclamation", re.compile(r"!")),
    ("emoji", _EMOJI),
)


def banned_patterns(text: str) -> List[str]:
    """The names of every §2.2 rule ``text`` breaks, or an empty list.

    Characters and hype only. The word caps, the item count and the section
    vocabulary are properties of the STRUCTURE and are checked by
    ``validate_payload``; they cannot be re-derived from a rendered string
    without guessing where one item ends and the next begins.

    Callers must mask URLs first. A fragment identifier is a ``#`` and a path
    segment can hold a ``!``, and neither is a hashtag or an exclamation mark
    in any sense a reader would recognise.
    """
    found = [name for name, pattern in _BANNED_PATTERNS if pattern.search(text or "")]
    hype = _has_hype(text or "")
    if hype:
        found.append("hype")
    return found


def _word_count(text: str) -> int:
    return len([w for w in text.split() if w.strip()])


def _has_hype(text: str) -> Optional[str]:
    low = text.lower()
    return next((w for w in _HYPE if w in low), None)


def _has_calendar_date(text: str) -> bool:
    return any(p.search(text) for p in _DATE_PATTERNS)


def _looks_like_a_host(source: str) -> bool:
    """True when the "publisher" is really a URL host.

    ``statnews.com`` is not how a person says STAT, and letting it through means
    the card's one piece of provenance is the thing the link already carries.
    """
    s = source.strip()
    if " " in s:
        return False
    return bool(re.match(r"^(?:https?://)?(?:www\.)?[\w-]+\.[a-z]{2,}", s, re.I))


# ─── Validation (the "fail" half) ────────────────────────────────────────────

def _validate_item(idx: int, raw: Any) -> Dict[str, Any]:
    where = f"item {idx + 1}"
    if not isinstance(raw, dict):
        raise DigestContractError(f"{where} is not an object")

    section_raw = clean_text(raw.get("section"))
    section = _SECTION_BY_LOWER.get(section_raw.lower())
    if section is None:
        raise DigestContractError(
            f"{where} has section {section_raw!r}, which is not one of "
            + " / ".join(SECTIONS))

    headline = clean_text(raw.get("headline")).rstrip(".").strip()
    if not headline:
        raise DigestContractError(f"{where} has no headline")
    if _word_count(headline) > HEADLINE_MAX_WORDS:
        raise DigestContractError(
            f"{where} headline is {_word_count(headline)} words, "
            f"the cap is {HEADLINE_MAX_WORDS}")
    if headline.isupper():
        raise DigestContractError(f"{where} headline is in capitals")
    hype = _has_hype(headline)
    if hype:
        raise DigestContractError(f"{where} headline uses hype ({hype!r})")
    if _has_calendar_date(headline):
        raise DigestContractError(
            f"{where} headline carries a calendar date; the platform timestamps "
            "the post and a full date false-trips the PHI filter")

    why = clean_text(raw.get("why_it_matters"))
    if not why:
        raise DigestContractError(f"{where} has no why_it_matters")
    if _word_count(why) > WHY_MAX_WORDS:
        raise DigestContractError(
            f"{where} why_it_matters is {_word_count(why)} words, "
            f"the cap is {WHY_MAX_WORDS}")
    if _has_second_sentence(why):
        raise DigestContractError(f"{where} why_it_matters is more than one sentence")
    hype = _has_hype(why)
    if hype:
        raise DigestContractError(f"{where} why_it_matters uses hype ({hype!r})")
    if _has_calendar_date(why):
        raise DigestContractError(f"{where} why_it_matters carries a calendar date")
    if not why.endswith("."):
        why += "."

    url = str(raw.get("url") or "").strip()
    if not re.match(r"^https?://\S+$", url, re.I):
        raise DigestContractError(f"{where} has no usable http(s) url")

    source = clean_text(raw.get("source"))
    if not source:
        raise DigestContractError(f"{where} has no source")
    if _looks_like_a_host(source):
        raise DigestContractError(
            f"{where} source {source!r} is a URL host, not a publisher name")

    # The deck. Every item is asked for one and only the lead's is DRAWN,
    # because the compose pass does not know which item will lead — the lead is
    # chosen from the SELECT pass's relevance after this returns. Asking for one
    # deck and guessing which item needs it would mean a second model call or a
    # lead with nothing under its headline.
    deck = clean_text(raw.get("deck"))
    if deck:
        if _word_count(deck) > DECK_MAX_WORDS:
            raise DigestContractError(
                f"{where} deck is {_word_count(deck)} words, "
                f"the cap is {DECK_MAX_WORDS}")
        if _has_second_sentence(deck):
            raise DigestContractError(f"{where} deck is more than one sentence")
        hype = _has_hype(deck)
        if hype:
            raise DigestContractError(f"{where} deck uses hype ({hype!r})")
        if _has_calendar_date(deck):
            raise DigestContractError(f"{where} deck carries a calendar date")
        if not deck.endswith("."):
            deck += "."

    # BREAKING, and what earns it. ``urgent`` alone is the model asserting
    # importance; ``urgent_kind`` is the model naming which of the four agreed
    # events happened, and that is the part this module can check. A claim with
    # no kind, or a kind outside the four, fails the run rather than posting a
    # badge nobody agreed to.
    urgent = bool(raw.get("urgent"))
    urgent_kind = clean_text(raw.get("urgent_kind")).lower() or None
    if urgent:
        if urgent_kind not in URGENT_KINDS:
            raise DigestContractError(
                f"{where} claims urgent with kind {urgent_kind!r}, which is not "
                "one of " + " / ".join(URGENT_KINDS))
    elif urgent_kind is not None:
        # A kind on a non-urgent item is the model hedging. Dropped rather than
        # failed: it is invisible either way, and nothing downstream reads it.
        urgent_kind = None

    return {"headline": headline, "why_it_matters": why, "source": source,
            "url": url, "section": section, "deck": deck,
            "urgent": urgent, "urgent_kind": urgent_kind}


def validate_payload(raw: Any, *, kind: str) -> Dict[str, Any]:
    """The §2.2 object, cleaned and checked, or ``DigestContractError``.

    ``kind`` is the digest kind (``news`` / ``papers``) and decides the title:
    the model is not asked for one, because the title is the product's name for
    this post and it must read the same every morning.

    Items keep the model's ORDER within a section and the sections render in
    ``SECTIONS`` order; sorting items by anything else here would silently
    override the selection pass's relevance ranking.
    """
    if not isinstance(raw, dict):
        raise DigestContractError("compose pass did not return a JSON object")
    items_raw = raw.get("items")
    if not isinstance(items_raw, list):
        raise DigestContractError("compose pass returned no items list")
    if len(items_raw) < MIN_ITEMS:
        raise DigestContractError(
            f"{len(items_raw)} item(s), the floor is {MIN_ITEMS}; "
            "a thin digest is not worth a post")
    if len(items_raw) > MAX_ITEMS:
        raise DigestContractError(
            f"{len(items_raw)} items, the cap is {MAX_ITEMS}")

    items: List[Dict[str, Any]] = []
    seen_urls = set()
    for idx, row in enumerate(items_raw):
        item = _validate_item(idx, row)
        # The same story twice is the one duplicate the select pass can miss
        # (two feeds, one press release), and it reads as padding.
        if item["url"] in seen_urls:
            raise DigestContractError(f"item {idx + 1} repeats an earlier url")
        seen_urls.add(item["url"])
        items.append(item)

    return {
        "version": PAYLOAD_VERSION,
        "kind": kind,
        "title": TITLE_BY_KIND.get(kind, DEFAULT_TITLE),
        "items": items,
    }


def mark_lead(payload: Dict[str, Any], lead_url: Optional[str] = None) -> Dict[str, Any]:
    """Choose the TOP STORY and make every other item a compact one (§2.2).

    Mutates and returns ``payload``. Three rules, all here rather than in the
    renderers, because a card that promotes one item and an email that promotes
    another is one digest read two ways:

    * The lead is the item at ``lead_url`` (the highest-``relevance`` story the
      select pass kept) and, when that url is not in the payload — the compose
      pass is allowed to drop items — the first item the model returned.
    * The lead moves to index 0 and carries ``lead: True``. Nothing else in the
      product has to re-derive it.
    * ``deck`` and ``urgent`` are KEPT on every item, and only the LEAD'S are
      rendered. That is the renderers' job, and ``lead_and_rest`` is how they
      agree on which item that is.

      Both used to be cleared here, and both clearings were the same bug. A
      story promoted on Tuesday afternoon came up as a 26px headline with
      nothing under it, because the deck it needed was deleted at 6am; and
      promoting a different story destroyed a BREAKING badge the compose pass
      had earned on one of the four same-day events, with no way back through
      an endpoint that refuses to grant one. An override whose cost is
      irreversible is an override nobody dares press.

      So neither is positional data. A compact item carrying ``urgent: true``
      draws no badge — ``digestItemEl`` and the email's compact row never read
      the field — and gets its badge back if it is promoted again.

    Passing no ``lead_url`` is the honest fallback, not a shortcut: the select
    pass has already sorted by relevance, so the first item is the best guess
    available when the join fails.
    """
    items: List[Dict[str, Any]] = list(payload.get("items") or [])
    if not items:
        return payload
    idx = 0
    if lead_url:
        for i, item in enumerate(items):
            if item.get("url") == lead_url:
                idx = i
                break
    lead = items.pop(idx)
    lead["lead"] = True
    for item in items:
        item["lead"] = False
    payload["items"] = [lead] + items
    return payload


def lead_and_rest(payload: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """``(lead, compact_items)`` — the shape the card, the pinned card and the
    email all draw.

    One definition, and the only one: three surfaces that each decide for
    themselves which story leads will disagree on the day it matters, and the
    reader will be the one who notices.
    """
    # A headline-less item is dropped HERE, once, rather than by each surface
    # deciding for itself. The web card already skipped them (`digestOf`
    # filters on `headline`) while the email drew an empty link and the
    # plain-text body omitted them entirely -- so one post said two different
    # things about how many stories it had. The PHI gate is unaffected: it
    # walks the payload's own items, not this view.
    items = [i for i in (payload.get("items") or [])
             if isinstance(i, dict) and (i.get("headline") or "").strip()]
    if not items:
        return None, []
    for i, item in enumerate(items):
        if item.get("lead"):
            return item, items[:i] + items[i + 1:]
    return items[0], items[1:]


def plain_text_body(payload: Dict[str, Any]) -> str:
    """The message body: a plain-text rendering of the structure.

    Not the post. The post is the card (web) and the item list (email), both
    built from ``payload``. This exists so the row is searchable, so a client
    that predates the card still shows something true, and so the PHI gate has
    the human-visible text in one string to scan.

    Deliberately markdown-free: the whole reason the structure exists is that a
    body carrying ``**`` and ``[title](url)`` leaked into an inbox verbatim.

    THE LEAD GOES FIRST, and the section is a label on each item rather than a
    heading over a group. Not a formatting preference: this used to group by
    ``SECTIONS`` order, which meant the top story appeared wherever its section
    happened to fall, so a digest led by a Regulation story showed a Research
    story first in every notification snippet. One post, two hierarchies -- the
    card and the email promoting one story and the inbox preview promoting
    another -- which is exactly the split the structured payload exists to
    close.
    """
    lead, rest = lead_and_rest(payload)
    lines: List[str] = [payload.get("title") or DEFAULT_TITLE]
    for item in ([lead] if lead else []) + rest:
        # ``.get`` throughout, not subscripting. This used to render only
        # payloads ``validate_payload`` had just built, where every key is
        # guaranteed; it now also renders payloads read back off disk for the
        # admin override, where a row written by an older build or repaired by
        # hand is missing one and a KeyError is a 500 on an admin's click.
        # A field that is not there renders as nothing, which is what the card
        # and the email already do with it.
        headline = item.get("headline") or ""
        if not headline:
            continue
        lines.append("")
        if item.get("section"):
            lines.append(item["section"])
        lines.append(headline)
        # Only the LEAD'S deck, because only the lead's is drawn anywhere else,
        # and a body that carried three decks would not be a view of the card.
        # Every deck is still scanned by the gates: they read
        # ``system_posts._payload_text``, which walks the payload's own strings
        # precisely so that a derivation like this one cannot become the hole.
        if item is lead and item.get("deck"):
            lines.append(item["deck"])
        tail = " ".join(p for p in (
            item.get("why_it_matters") or "",
            f"({item['source']})" if item.get("source") else "",
            item.get("url") or "",
        ) if p)
        if tail:
            lines.append(tail)
    return "\n".join(lines).strip()
