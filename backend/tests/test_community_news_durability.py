"""Community News PRD — durability (§1), the digest contract (§2), the audit (§3).

Three properties, and each one has already failed in production or would have:

* **§1** ``community.db`` resolved to a path inside the container image, so every
  post, DM, reaction, read marker, event and digest-ledger row was destroyed on
  each redeploy. The tests here are the PRD's own acceptance criteria: a boot
  with no ``COMMUNITY_DB_PATH`` lands beside the Asclepius database, a survivor
  test opens the same file from a second store the way a new container does, and
  the dedup ledger survives that restart so a same-day redeploy cannot re-post.
* **§2** The compose pass returns structure and a validator decides what is
  publishable. Every rule in §2.2 has a negative case here, because a contract
  whose rules are only in a prompt is a request, not a contract.
* **§3** The audit's fixes: the purge is recorded, the write path refuses a
  digest body that breaks the style rules, and the empty room can say why.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import realm  # noqa: E402
from community import digest as cdigest  # noqa: E402
from community import digest_contract as contract  # noqa: E402
from community import notify as cnotify  # noqa: E402
from community import store as community_store  # noqa: E402
from community.digest_contract import DigestContractError  # noqa: E402


# ═══ §1 Durability ═══════════════════════════════════════════════════════════

def _clear_paths(monkeypatch):
    for k in ("COMMUNITY_DB_PATH", "ASCLEPIUS_DB_PATH", "ASCLEPIUS_DATA_DIR"):
        monkeypatch.delenv(k, raising=False)


def test_community_db_lands_beside_the_asclepius_db_when_its_own_var_is_unset(
        monkeypatch, tmp_path):
    """The PRD's acceptance criterion, and the whole production fix.

    A deploy sets ASCLEPIUS_DB_PATH because losing it loses the product. It did
    not set COMMUNITY_DB_PATH, and nothing made that a visible choice, so the
    community was written inside the container image and deleted on each
    redeploy. Now the community follows the database that IS configured.
    """
    _clear_paths(monkeypatch)
    volume = tmp_path / "data"
    volume.mkdir()
    monkeypatch.setenv("ASCLEPIUS_DB_PATH", str(volume / "asclepius.db"))
    assert realm.live_community_db() == str(volume / "community.db")


def test_the_data_dir_is_the_second_choice(monkeypatch, tmp_path):
    _clear_paths(monkeypatch)
    monkeypatch.setenv("ASCLEPIUS_DATA_DIR", str(tmp_path))
    assert realm.live_community_db() == str(tmp_path / "community.db")


def test_an_explicit_variable_still_wins(monkeypatch, tmp_path):
    _clear_paths(monkeypatch)
    monkeypatch.setenv("ASCLEPIUS_DB_PATH", str(tmp_path / "asclepius.db"))
    monkeypatch.setenv("COMMUNITY_DB_PATH", "/somewhere/else/community.db")
    assert realm.live_community_db() == "/somewhere/else/community.db"


def test_a_stray_local_database_cannot_relocate_the_live_one(monkeypatch, tmp_path,
                                                             caplog):
    """A file inside the container image must never outrank the volume.

    An earlier version let an existing ``backend/community.db`` win when the
    derived file did not exist yet, to spare a developer from opening an empty
    community. On a container with a stray file at that path — one import of
    ``main`` during an image build makes one — that branch moved the LIVE
    community off the volume and back onto disposable disk, silently, which is
    the exact failure the derivation was added to prevent.

    The developer's file is still not opened behind their back: it stays on
    disk, and the resolver says so at WARNING.
    """
    _clear_paths(monkeypatch)
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "community.db").write_text("pretend this is sqlite")
    volume = tmp_path / "vol"
    volume.mkdir()
    monkeypatch.setattr(realm, "_backend_dir", lambda: str(backend))
    monkeypatch.setenv("ASCLEPIUS_DB_PATH", str(volume / "asclepius.db"))

    with caplog.at_level("WARNING"):
        resolved = realm.live_community_db()
    assert resolved == str(volume / "community.db")
    assert "will NOT be read" in caplog.text
    assert str(backend / "community.db") in caplog.text


def test_the_beside_the_code_path_is_still_the_answer_when_nothing_is_configured(
        monkeypatch, tmp_path):
    """No volume, no data dir: there is nowhere else to put it, and a local run
    whose databases sit beside the code by design must keep working."""
    _clear_paths(monkeypatch)
    backend = tmp_path / "backend"
    backend.mkdir()
    monkeypatch.setattr(realm, "_backend_dir", lambda: str(backend))
    assert realm.live_community_db() == str(backend / "community.db")


def test_the_sandbox_realm_still_derives_from_whatever_live_resolves_to(
        monkeypatch, tmp_path):
    _clear_paths(monkeypatch)
    monkeypatch.setenv("ASCLEPIUS_DB_PATH", str(tmp_path / "asclepius.db"))
    assert realm.sandbox_db_path(realm.live_community_db()) == \
        str(tmp_path / "community_sandbox.db")


def _store_at(path):
    return community_store.CommunityStore(db_path=str(path))


def test_a_post_survives_the_process_that_wrote_it(tmp_path):
    """The redeploy simulation: a SECOND store object opens the same file the
    way a new container does, and the post is still there.

    This is the test whose absence let the loss run for months. Every existing
    community test writes and reads through one store instance, which passes
    just as happily against a database in /tmp that is about to be deleted.
    """
    path = tmp_path / "community.db"
    first = _store_at(path)
    first.ensure_default_channels()
    channel = first.get_channel_by_slug("medical-ai-news")
    msg = first.insert_message(channel_id=channel["id"], author_user_id="u-system",
                               body="Medical AI Digest", kind="digest_news")

    second = _store_at(path)
    again = second.get_channel_by_slug("medical-ai-news")
    msgs, _ = second.list_messages(again["id"])
    assert [m["id"] for m in msgs] == [msg["id"]]
    assert msgs[0]["body"] == "Medical AI Digest"


def test_the_dedup_ledger_survives_a_restart_on_the_same_day(tmp_path):
    """A same-day redeploy must not re-post and re-mail the digest.

    The ledger lives in community.db, so it was wiped with everything else, and
    a restart after 13:00 UTC found the day unclaimed and ran again. The roster
    got the same digest twice from one day's news.
    """
    path = tmp_path / "community.db"
    first = _store_at(path)
    window = "2026-09-08"
    run_id = first.claim_digest_run("news", window_key=window)
    assert run_id is not None
    first.finish_digest_run(run_id, ok=True, items_posted=3, reason="posted")

    restarted = _store_at(path)
    assert restarted.claim_digest_run("news", window_key=window) is None


# ═══ §2.2 The digest contract ════════════════════════════════════════════════

def _payload(**over):
    items = [
        {"headline": "FDA clears autonomous AI for retinopathy screening",
         "deck": "The clearance covers screening without a physician in the "
                 "loop, and reimbursement follows it.",
         "why_it_matters": "First reimbursed autonomous diagnostic; it sets the "
                           "template.",
         "source": "STAT", "url": "https://example.org/a", "section": "Regulation"},
        {"headline": "Frontier models score under 0.3 kappa",
         "deck": "Three leading models graded the same trials and agreed with "
                 "reviewers about as often as chance.",
         "why_it_matters": "More reasoning did not help; the failure is judgment.",
         "source": "Synthesis Bench", "url": "https://example.org/b",
         "section": "Research"},
        {"headline": "Health system rolls back its ambient scribe",
         "deck": "An internal audit found notes the clinicians had signed but "
                 "had not read.",
         "why_it_matters": "Deployment risk sits in the audit trail, not the model.",
         "source": "Modern Healthcare", "url": "https://example.org/c",
         "section": "Deployment"},
    ]
    return {"items": [dict(i) for i in items], **over}


def _with_item(index, **fields):
    p = _payload()
    p["items"][index].update(fields)
    return p


def test_a_conforming_digest_validates_and_keeps_its_items():
    out = contract.validate_payload(_payload(), kind="news")
    assert out["title"] == "Medical AI Digest"
    assert len(out["items"]) == 3
    assert {i["section"] for i in out["items"]} <= set(contract.SECTIONS)


def test_papers_get_their_own_title():
    assert contract.validate_payload(_payload(), kind="papers")["title"] == \
        "Papers of the week"


@pytest.mark.parametrize("payload,rule", [
    ({"items": _payload()["items"][:2]}, "fewer than three items"),
    ({"items": _payload()["items"] + [
        dict(_payload()["items"][0], url=f"https://example.org/x{i}") for i in range(3)]},
     "more than five items"),
    (_with_item(0, headline=" ".join(["word"] * 11)), "headline over ten words"),
    (_with_item(0, headline=""), "empty headline"),
    (_with_item(0, why_it_matters=" ".join(["word"] * 15)), "why over fourteen words"),
    (_with_item(0, deck=" ".join(["word"] * 26)), "deck over 25 words"),
    (_with_item(0, deck="One thing happened. Then a second thing happened."),
     "a two-sentence deck"),
    (_with_item(0, deck="A groundbreaking result lands in clinic."), "hype in a deck"),
    (_with_item(0, deck="The clearance landed on March 14."), "a date in a deck"),
    (_with_item(0, urgent=True), "urgent with no kind at all"),
    (_with_item(0, urgent=True, urgent_kind="interesting"), "an invented urgent kind"),
    (_with_item(0, why_it_matters="One thing. Then a second thing."), "two sentences"),
    (_with_item(0, why_it_matters=""), "empty why"),
    (_with_item(0, section="Hype"), "invented section"),
    (_with_item(0, section=""), "missing section"),
    (_with_item(0, source="statnews.com"), "a url host as the source"),
    (_with_item(0, source=""), "missing source"),
    (_with_item(0, url="ftp://example.org/a"), "a non-http url"),
    (_with_item(0, url=""), "no url"),
    (_with_item(0, headline="A revolutionary model lands in clinic"), "hype in a headline"),
    (_with_item(0, why_it_matters="Exciting for every clinic in the country."),
     "hype in a why"),
    (_with_item(0, headline="FDA clears the tool on March 14"), "a calendar date"),
    (_with_item(1, url="https://example.org/a"), "a repeated url"),
    ({"items": "not a list"}, "items that are not a list"),
    ("not an object", "a non-object payload"),
])
def test_every_contract_rule_has_a_negative_case(payload, rule):
    """One case per §2.2 rule. A rule with no failing input is a comment."""
    with pytest.raises(DigestContractError):
        contract.validate_payload(payload, kind="news")


@pytest.mark.parametrize("raw,clean", [
    ("**Medical AI Digest**", "Medical AI Digest"),
    ("FDA clears it, first of its kind", "FDA clears it, first of its kind"),
    ("Trained on 2020–2024 data", "Trained on 2020 to 2024 data"),
    ("#AIinMedicine wins", "AIinMedicine wins"),
    ("Big result!", "Big result."),
    ("Great \U0001F389 result", "Great result"),
])
def test_repairable_violations_are_stripped_not_refused(raw, clean):
    """Strip when the repair is lossless, fail when it would invent meaning.

    A digit range keeps its meaning ("2020 to 2024"), because turning it into
    "2020, 2024" would say something the source did not.
    """
    assert contract.clean_text(raw) == clean


def test_an_abbreviation_is_not_a_second_sentence():
    """"U.S. regulators cleared it" is one sentence, and refusing it would cost
    the whole day's digest for a rule it did not break."""
    contract.validate_payload(
        _with_item(0, why_it_matters="U.S. regulators cleared it for primary care."),
        kind="news")


def test_the_plain_text_body_carries_no_markdown():
    """The body is a fallback for search and old clients, and it is the string
    that leaked into inboxes when it was markdown."""
    body = contract.plain_text_body(contract.validate_payload(_payload(), kind="news"))
    assert not contract.banned_patterns(
        __import__("re").sub(r"https?://\S+", "", body))
    for item in _payload()["items"]:
        assert item["source"] in body


def test_sections_render_in_the_contract_order_and_empties_are_dropped():
    grouped = contract.grouped_items(contract.validate_payload(_payload(), kind="news"))
    assert [s for s, _ in grouped] == ["Research", "Regulation", "Deployment"]


# ═══ §2.4 The email ══════════════════════════════════════════════════════════

def test_the_digest_email_renders_the_structure_with_no_markdown_left():
    """Grep the rendered HTML, which is what the PRD asks for: the failure this
    replaces was an email that showed '**Medical AI Digest** ... - [Opinion:'
    to physicians verbatim."""
    import re

    from onboarding_emails import (
        build_community_digest_post_email, digest_email_subject,
    )

    payload = contract.validate_payload(_payload(), kind="news")
    assert digest_email_subject(payload) == "Medical AI Digest · 3 items"

    html_out = build_community_digest_post_email(
        payload=payload, community_url="https://example.test/community",
        unsubscribe_url="https://example.test/u?t=tok", first_name="Kalpesh")
    visible = re.sub(r"<[^>]+>", " ", html_out)
    assert not contract.banned_patterns(visible), visible[:400]
    for item in payload["items"]:
        assert item["headline"] in visible
        assert item["url"] in html_out           # the headline links out
    # The section is the TAG beside a headline now, not a heading over a group
    # (Digest Design PRD §1.3). Same information, one fewer line.
    assert "REGULATION" in visible and "RESEARCH" in visible


def test_the_email_leads_with_the_top_story_and_renders_one_deck():
    """§1.3: one hierarchy, two contexts. An email that promoted a different
    item than the card would be one digest read two ways, and the physician who
    opens the mail and then the room is exactly who would notice."""
    import re

    from onboarding_emails import build_community_digest_post_email

    payload = contract.mark_lead(
        contract.validate_payload(_payload(), kind="news"), "https://example.org/c")
    out = build_community_digest_post_email(
        payload=payload, community_url="https://example.test/community",
        unsubscribe_url="https://example.test/u?t=tok")

    lead, rest = contract.lead_and_rest(payload)
    assert lead["url"] == "https://example.org/c"
    assert out.index(lead["headline"]) < min(out.index(r["headline"]) for r in rest)
    assert "TOP STORY" in out and "BREAKING" not in out
    # The deck belongs to the lead alone ON THE PAGE. The compact items keep
    # the decks the compose pass wrote them -- that is what makes promoting one
    # of them non-destructive -- and the email simply does not draw them.
    assert out.count(lead["deck"]) == 1
    for item in rest:
        assert item["deck"], "a compact item was stripped of its deck on disk"
        assert item["deck"] not in out, "a compact item drew a deck"
    # One primary action per item, said the same way everywhere (§0.5).
    assert out.count("Full article →") == len(payload["items"])
    assert re.sub(r"<[^>]+>", " ", out).count("STAT") >= 1


def test_breaking_replaces_top_story_only_when_the_lead_is_urgent():
    """§2.2. The badge is a few mornings a month; a badge that fires by default
    is a badge that has stopped meaning anything by the second week."""
    from onboarding_emails import build_community_digest_post_email

    raw = _with_item(0, urgent=True, urgent_kind="regulatory")
    payload = contract.mark_lead(
        contract.validate_payload(raw, kind="news"), "https://example.org/a")
    out = build_community_digest_post_email(
        payload=payload, community_url="https://example.test/community",
        unsubscribe_url="https://example.test/u?t=tok")
    assert "BREAKING" in out and "TOP STORY" not in out


def test_urgency_on_a_story_that_does_not_lead_is_cleared_not_promoted():
    """§2.2: urgent is allowed only ON the lead. An item the reader meets
    fourth carrying BREAKING is a badge on something nobody is reading first."""
    raw = _with_item(1, urgent=True, urgent_kind="safety")
    payload = contract.mark_lead(
        contract.validate_payload(raw, kind="news"), "https://example.org/a")
    lead, rest = contract.lead_and_rest(payload)
    assert lead["url"] == "https://example.org/a" and lead["urgent"] is False
    assert all(item["urgent"] is False for item in rest)


def test_the_lead_falls_back_to_the_models_first_item_when_the_join_misses():
    """The compose pass may drop the story the select pass ranked highest. The
    digest still has to lead with something, and ``kept`` is already sorted by
    relevance, so the model's own first item is the best answer left."""
    payload = contract.mark_lead(
        contract.validate_payload(_payload(), kind="news"), "https://nobody.invalid/x")
    assert contract.lead_and_rest(payload)[0]["url"] == "https://example.org/a"


def test_every_rendered_digest_field_is_scanned_for_phi():
    """§8.1. ``deck`` is model-written text over somebody else's web page, and
    a field the card renders but the gate never reads is the one hole worth a
    test of its own. Stated over the whole contract rather than over ``deck``
    so the NEXT field added has to land in the tuple too."""
    from community import system_posts

    payload = contract.mark_lead(
        contract.validate_payload(_payload(), kind="news"), "https://example.org/a")
    scanned = system_posts._payload_text(payload)
    lead, rest = contract.lead_and_rest(payload)
    for field in ("headline", "deck", "why_it_matters", "source", "section"):
        assert lead[field] in scanned, f"{field} never reaches the PHI gate"
    for item in rest:
        for field in ("headline", "why_it_matters", "source", "section"):
            assert item[field] in scanned
    assert payload["title"] in scanned


def test_a_one_item_subject_is_not_pluralised():
    payload = {"title": "Medical AI Digest", "items": [{"headline": "x"}]}
    from onboarding_emails import digest_email_subject
    assert digest_email_subject(payload) == "Medical AI Digest · 1 item"


def test_only_a_matching_cadence_gets_the_digest():
    """News is the daily habit and papers ride the weekly preference — the rule
    the standalone mailer has always used, now applied where the mail is sent."""
    news = {"kind": "news"}
    papers = {"kind": "papers"}
    assert cnotify._wants_digest(news, {"news_frequency": "daily"}) is True
    assert cnotify._wants_digest(news, {"news_frequency": "weekly"}) is False
    assert cnotify._wants_digest(papers, {"news_frequency": "weekly"}) is True
    assert cnotify._wants_digest(news, {"news_frequency": "off"}) is False


@pytest.mark.parametrize("message", [
    {"kind": "post", "payload_json": json.dumps({"items": [{"headline": "x"}]})},
    {"kind": "digest_news", "payload_json": None},
    {"kind": "digest_news", "payload_json": "{ not json"},
    {"kind": "digest_news", "payload_json": json.dumps({"items": []})},
])
def test_anything_without_a_usable_payload_falls_back_to_the_activity_line(message):
    """Old digests have no payload and must keep working — that is the whole
    migration, and there is no backfill behind it."""
    assert cnotify.digest_payload_of(message) is None


# ═══ §3 The audit's fixes ════════════════════════════════════════════════════

def test_the_write_path_refuses_a_digest_body_that_breaks_the_style_rules():
    """The contract guards one pipeline; this guards the door.

    A second digest producer, a repaired payload or a hand-built body reaches
    the store without passing the validator, and meets the same rules there.
    """
    from community import system_posts

    channel = {"slug": "medical-ai-news"}
    assert system_posts._house_style_clear(
        channel, "digest_news",
        "Medical AI Digest\n\nResearch\nA model was cleared\n"
        "It changes triage. (Fake Wire) https://example.org/a") is True
    assert system_posts._house_style_clear(
        channel, "digest_news", "**Medical AI Digest** - a story") is False
    # Other bot kinds are markdown-lite by design and are left alone: a rule
    # that stripped the morning brief's asterisks would break its rendering.
    assert system_posts._house_style_clear(
        channel, "morning_brief", "**Good morning** - three things today") is True


def test_a_url_is_not_a_hashtag():
    """A fragment identifier is a '#' and a path can hold a '!'. Masking URLs
    before the check is what stops a legitimate link failing the style gate."""
    from community import system_posts

    assert system_posts._house_style_clear(
        {"slug": "medical-ai-news"}, "digest_news",
        "A model was cleared\nIt changes triage. "
        "(Journal) https://example.org/a#results-2!x") is True


def test_the_payloads_own_strings_are_scanned_not_just_the_body():
    """The body is DERIVED from the payload, and a derivation is exactly the
    step that quietly stops covering everything when a field is added."""
    from community import system_posts

    text = system_posts._payload_text({
        "title": "Medical AI Digest",
        "items": [{"headline": "A headline", "why_it_matters": "A reason.",
                   "source": "STAT", "section": "Research",
                   "url": "https://example.org/a"}],
    })
    assert "A headline" in text and "A reason." in text and "STAT" in text
    # The url is deliberately absent: feeding DOIs and PMIDs to the PHI gate is
    # what the URL masking exists to prevent.
    assert "https://example.org/a" not in text


def test_the_next_run_time_agrees_with_the_scheduler(monkeypatch, tmp_path):
    """Asserted against ``_due`` itself, not against a hand-copied timetable.

    The previous version of this test asserted the arithmetic ("past 13:00, so
    tomorrow") and therefore pinned the bug it was meant to catch: while a run
    was outstanding, ``_due`` said "now" and the empty room said "tomorrow
    afternoon" — during exactly the window the message exists to explain, and
    all day after a failed run.
    """
    from datetime import datetime

    monkeypatch.delenv("COMMUNITY_NEWS_ENABLED", raising=False)
    monkeypatch.setenv("COMMUNITY_DIGEST_NEWS_HOUR_UTC", "13")
    monkeypatch.setattr(community_store, "_stores",
                        dict(community_store._stores), raising=False)
    store = community_store.reset_community_store_for_tests(
        db_path=str(tmp_path / "community.db"))

    def agree(now):
        last_ok = store.last_successful_run_at("news")
        due = cdigest._due("news", now, last_ok)
        nxt = cdigest.next_run_at("news", now=now)
        # Due now means the answer is this window, not the next one.
        if due:
            assert nxt == now.replace(hour=13, minute=0, second=0,
                                      microsecond=0).isoformat() + "Z", (now, nxt)
        else:
            assert nxt > now.isoformat() + "Z", (now, nxt)
        return due, nxt

    # Nothing has ever run: before the fire time it is later today, after it the
    # run is outstanding and the answer is now.
    assert agree(datetime(2026, 9, 8, 9, 0)) == (False, "2026-09-08T13:00:00Z")
    assert agree(datetime(2026, 9, 8, 14, 30)) == (True, "2026-09-08T13:00:00Z")

    # Today's run succeeded: nothing is outstanding, so the answer moves on.
    run_id = store.claim_digest_run("news", window_key="2026-09-08")
    store.finish_digest_run(run_id, ok=True, items_posted=3, reason="posted")
    with store._conn() as conn:
        conn.execute("UPDATE community_digest_runs SET started_at = ? WHERE id = ?",
                     ("2026-09-08T13:00:00Z", run_id))
    assert agree(datetime(2026, 9, 8, 14, 30)) == (False, "2026-09-09T13:00:00Z")

    monkeypatch.setenv("COMMUNITY_NEWS_ENABLED", "0")
    assert cdigest.next_run_at("news", now=datetime(2026, 9, 8, 9, 0)) is None


def test_the_papers_run_lands_on_its_weekday(monkeypatch):
    from datetime import datetime

    monkeypatch.delenv("COMMUNITY_NEWS_ENABLED", raising=False)
    monkeypatch.setenv("COMMUNITY_DIGEST_NEWS_HOUR_UTC", "13")
    monkeypatch.setenv("COMMUNITY_DIGEST_PAPERS_DOW", "0")   # Monday
    # 2026-09-08 is a Tuesday, so the next Monday is the 14th.
    nxt = cdigest.next_run_at("papers", now=datetime(2026, 9, 8, 9, 0))
    assert nxt == "2026-09-14T13:00:00Z"


# ═══ End to end: the payload reaches the client ══════════════════════════════

def test_a_digest_run_stores_its_payload_and_the_api_serves_it(monkeypatch, tmp_path):
    """The card cannot render what the API does not send.

    Drives the real pipeline with a canned model, then reads the message back
    the way the browser does. Asserts BOTH halves of the compatibility promise:
    a structured post carries `payload`, and a legacy markdown post carries
    None so the client falls through to renderBody.
    """
    import asyncio

    import ai.llm_client as llm
    from community import feeds as cfeeds
    from community import router as crouter

    # Rebind the REALM'S store rather than instantiating one and patching the
    # accessor: digest.py and system_posts.py bind get_community_store at
    # import time, so a patched module attribute would leave the pipeline
    # writing to the suite's shared database while this test read an empty one.
    #
    # Restored afterwards. CI shards by a bin-packer, so which file runs next
    # in this process is decided by the packing rather than by anything visible
    # here, and a test that leaves the realm pointed at a torn-down tmp_path is
    # a failure that appears only when an unrelated file is added.
    previous = community_store.get_community_store()
    monkeypatch.setattr(community_store, "_stores",
                        dict(community_store._stores), raising=False)
    store = community_store.reset_community_store_for_tests(
        db_path=str(tmp_path / "community.db"))
    assert previous is not None
    monkeypatch.setattr(crouter, "member_map", lambda **kw: {})
    store.ensure_default_channels()

    async def fake_call_llm(*, role, system, messages, **kw):
        sent = json.loads(messages[0]["content"])
        if "digest_kind" in sent:
            return json.dumps({"items": [
                {"headline": it["title"][:60],
                 "why_it_matters": "It changes what a clinic does.",
                 "source": "Fake Wire", "url": it["url"],
                 "section": "Research"}
                for it in sent["items"]]}), {}
        return json.dumps({"items": [
            {"id": it["id"], "keep": True, "relevance": 0.9,
             "one_liner": "what happened"} for it in sent["items"]]}), {}

    async def fake_rss():
        return [cfeeds._item("rss:test", url=f"https://example.com/s{i}",
                             title=f"AI model cleared for clinical use {i}",
                             abstract="An artificial intelligence system.")
                for i in range(3)]

    monkeypatch.setattr(llm, "call_llm", fake_call_llm)
    monkeypatch.setattr(llm, "first_text", lambda resp: resp)
    monkeypatch.setattr(cfeeds, "fetch_rss", fake_rss)

    result = asyncio.new_event_loop().run_until_complete(cdigest.run_digest("news"))
    assert result["ok"] is True and result["posted"] == 3

    channel = store.get_channel_by_slug("medical-ai-news")
    msgs, _ = store.list_messages(channel["id"])
    assert len(msgs) == 1
    stored = json.loads(msgs[0]["payload_json"])
    assert len(stored["items"]) == 3
    assert stored["title"] == "Medical AI Digest"
    # The body is a plain-text rendering of the same object, not a second
    # description of the post that could drift from it.
    assert stored["items"][0]["headline"] in msgs[0]["body"]

    served = crouter._serialize_messages(msgs, {}, "medical-ai-news")[0]
    assert served["payload"]["items"][0]["section"] == "Research"

    # A post written before the column existed: the client gets None and falls
    # back to rendering the body, which is the entire migration.
    legacy = store.insert_message(channel_id=channel["id"], author_user_id="u-system",
                                  body="Old digest body", kind="digest_news")
    old_served = crouter._serialize_messages([store.get_message(legacy["id"])],
                                             {}, "medical-ai-news")[0]
    assert old_served["payload"] is None


def test_a_deleted_digest_serves_neither_body_nor_payload():
    """The point of a delete is that the content stops being served, and a
    payload left behind would render the whole post under 'Message removed'."""
    from community import router as crouter

    row = {"id": 1, "author_user_id": "u-system", "kind": "digest_news",
           "body": "Medical AI Digest", "created_at": "2026-09-08T13:00:00Z",
           "deleted_at": "2026-09-08T14:00:00Z", "deleted": True,
           "payload_json": json.dumps(_payload()), "cards_json": None,
           "parent_message_id": None, "mentions": [], "attachments": []}

    class _Stub:
        def reply_counts(self, ids): return {}
        def reactions_for(self, ids): return {}
        def pinned_message_ids(self, ids): return set()

    import unittest.mock as mock
    with mock.patch.object(crouter, "_cstore", lambda: _Stub()):
        served = crouter._serialize_messages([row], {}, "medical-ai-news")[0]
    assert served["payload"] is None and served["body"] == ""


def test_a_digest_answers_to_the_news_cadence_and_not_to_the_post_toggle(
        monkeypatch, tmp_path):
    """"Daily news" must not silently mean "daily news, if you also left bot
    posts on". The cadence is the switch the preferences page shows for news;
    requiring a second one nobody was shown is how a member concludes the
    setting is broken and presses the spam button instead.
    """
    import asyncio

    store = _store_at(tmp_path / "community.db")
    store.ensure_default_channels()
    channel = store.get_channel_by_slug("medical-ai-news")
    payload = contract.validate_payload(_payload(), kind="news")
    msg = store.insert_message(
        channel_id=channel["id"], author_user_id="u-system",
        body=contract.plain_text_body(payload), kind="digest_news", payload=payload)
    store.enqueue_notification(user_id="u-1", kind="post", message_id=msg["id"])

    # Bot posts off, news daily: the digest still goes.
    store.set_email_stream("u-1", "post", False)
    store.set_news_frequency("u-1", "daily")

    sent = []

    async def fake_send(to, subject, body):
        sent.append(subject)
        return True

    monkeypatch.setattr("email_utils.send_html_email", fake_send)
    asyncio.new_event_loop().run_until_complete(cnotify.flush_pending(
        store, resolve_member=lambda uid: {"email": "d@example.test",
                                           "display_name": "Dr Test"}))
    assert sent == ["Medical AI Digest · 3 items"]

    # News off: nothing goes, and the row is settled rather than retried forever.
    msg2 = store.insert_message(
        channel_id=channel["id"], author_user_id="u-system",
        body=contract.plain_text_body(payload), kind="digest_news", payload=payload)
    store.enqueue_notification(user_id="u-1", kind="post", message_id=msg2["id"])
    store.set_news_frequency("u-1", "off")
    sent.clear()
    asyncio.new_event_loop().run_until_complete(cnotify.flush_pending(
        store, resolve_member=lambda uid: {"email": "d@example.test",
                                           "display_name": "Dr Test"}))
    assert sent == []
    assert store.unsent_notifications() == []


def test_a_digest_run_mails_through_the_queue_and_nowhere_else(monkeypatch, tmp_path):
    """ONE sender. A digest run must not mail anybody directly.

    There were two: the run queued a notification for every member AND walked
    the member map sending the same email itself. They never collided only
    because the direct sender switched itself off whenever the morning routine
    was enabled, which is the default — so the duplicate was dormant, and one
    person setting COMMUNITY_MORNING_ENABLED=0 would have mailed every
    physician the same digest twice.

    Asserted at the run level rather than by reading the source: this catches a
    second sender however it is reintroduced.
    """
    import asyncio

    import ai.llm_client as llm
    from community import feeds as cfeeds
    from community import router as crouter

    previous = community_store.get_community_store()
    monkeypatch.setattr(community_store, "_stores",
                        dict(community_store._stores), raising=False)
    store = community_store.reset_community_store_for_tests(
        db_path=str(tmp_path / "community.db"))
    # A real member: with an empty map there is nobody to queue for, and the
    # test would pass by having no fan-out rather than by having one sender.
    member = {"user_id": "u-1", "display_name": "Dr Test", "is_staff": False,
              "email": "d@example.test"}
    monkeypatch.setattr(crouter, "member_map", lambda **kw: {"u-1": member})
    store.ensure_default_channels()
    assert previous is not None

    async def fake_call_llm(*, role, system, messages, **kw):
        sent_in = json.loads(messages[0]["content"])
        if "digest_kind" in sent_in:
            return json.dumps({"items": [
                {"headline": it["title"][:60],
                 "why_it_matters": "It changes what a clinic does.",
                 "source": "Fake Wire", "url": it["url"], "section": "Research"}
                for it in sent_in["items"]]}), {}
        return json.dumps({"items": [
            {"id": it["id"], "keep": True, "relevance": 0.9,
             "one_liner": "what happened"} for it in sent_in["items"]]}), {}

    async def fake_rss():
        return [cfeeds._item("rss:test", url=f"https://example.com/q{i}",
                             title=f"AI model cleared for clinical use {i}",
                             abstract="An artificial intelligence system.")
                for i in range(3)]

    sent = []

    async def fake_send(to, subject, body):
        sent.append(subject)
        return True

    monkeypatch.setattr(llm, "call_llm", fake_call_llm)
    monkeypatch.setattr(llm, "first_text", lambda resp: resp)
    monkeypatch.setattr(cfeeds, "fetch_rss", fake_rss)
    monkeypatch.setattr("email_utils.send_html_email", fake_send)
    # The direct sender's own off-switch, turned OFF: this is the configuration
    # under which the duplicate would have fired.
    monkeypatch.setenv("COMMUNITY_MORNING_ENABLED", "0")

    result = asyncio.new_event_loop().run_until_complete(cdigest.run_digest("news"))
    assert result["ok"] is True and result["posted"] == 3
    # Nothing was mailed by the RUN. The mail is queued, and the flush sends it.
    assert sent == []
    assert result.get("email_queued") is True

    pending = store.unsent_notifications()
    assert pending, "the run must queue the digest for the flush to mail"


# ═══ Fixes from the fresh-context audit ══════════════════════════════════════

def test_a_url_the_model_reformatted_still_matches_its_item(monkeypatch, tmp_path):
    """Provenance must survive the model retyping a link.

    The join from composed item back to fetched item was an exact string
    compare, so a trailing slash — or a dropped tracking parameter, or a
    percent-encoded character — meant nothing matched: the digest posted, the
    run recorded "posted 0", every story was filed as skipped, and the link from
    story to message was lost for good. Matched on the normalised url now, which
    is the identity the dedup ledger already uses.
    """
    import asyncio

    import ai.llm_client as llm
    from community import feeds as cfeeds
    from community import router as crouter

    monkeypatch.setattr(community_store, "_stores",
                        dict(community_store._stores), raising=False)
    store = community_store.reset_community_store_for_tests(
        db_path=str(tmp_path / "community.db"))
    monkeypatch.setattr(crouter, "member_map", lambda **kw: {})
    store.ensure_default_channels()

    async def fake_call_llm(*, role, system, messages, **kw):
        sent = json.loads(messages[0]["content"])
        if "digest_kind" in sent:
            return json.dumps({"items": [
                {"headline": it["title"][:60],
                 "why_it_matters": "It changes what a clinic does.",
                 "source": "Fake Wire",
                 # The model retypes the link with a trailing slash.
                 "url": it["url"] + "/",
                 "section": "Research"} for it in sent["items"]]}), {}
        return json.dumps({"items": [
            {"id": it["id"], "keep": True, "relevance": 0.9,
             "one_liner": "what happened"} for it in sent["items"]]}), {}

    async def fake_rss():
        return [cfeeds._item("rss:test", url=f"https://example.com/n{i}",
                             title=f"AI model cleared for clinical use {i}",
                             abstract="An artificial intelligence system.")
                for i in range(3)]

    monkeypatch.setattr(llm, "call_llm", fake_call_llm)
    monkeypatch.setattr(llm, "first_text", lambda resp: resp)
    monkeypatch.setattr(cfeeds, "fetch_rss", fake_rss)

    result = asyncio.new_event_loop().run_until_complete(cdigest.run_digest("news"))
    assert result["posted"] == 3
    with store._conn() as conn:
        rows = conn.execute(
            "SELECT status, posted_message_id FROM community_content_items").fetchall()
    assert [r[0] for r in rows] == ["posted"] * 3
    assert all(r[1] is not None for r in rows), "the story lost its message link"


def test_a_thin_day_holds_its_stories_instead_of_burning_them(monkeypatch, tmp_path):
    """Below the three-item floor, the stories stay candidates.

    Retiring them was pool starvation with a healthy-looking ledger: a feed that
    yields two strong stories a day never reaches the floor, so both were marked
    skipped, tomorrow started from nothing, and the digest reported "found
    items, none worth posting" every day forever.
    """
    import asyncio

    import ai.llm_client as llm
    from community import feeds as cfeeds
    from community import router as crouter

    monkeypatch.setattr(community_store, "_stores",
                        dict(community_store._stores), raising=False)
    store = community_store.reset_community_store_for_tests(
        db_path=str(tmp_path / "community.db"))
    monkeypatch.setattr(crouter, "member_map", lambda **kw: {})
    store.ensure_default_channels()

    async def fake_call_llm(*, role, system, messages, **kw):
        sent = json.loads(messages[0]["content"])
        assert "digest_kind" not in sent, "compose must not be called below the floor"
        ids = [it["id"] for it in sent["items"]]
        return json.dumps({"items": [
            {"id": i, "keep": i in ids[:2], "relevance": 0.9,
             "one_liner": "what happened"} for i in ids]}), {}

    async def fake_rss():
        return [cfeeds._item("rss:test", url=f"https://example.com/t{i}",
                             title=f"AI model cleared for clinical use {i}",
                             abstract="An artificial intelligence system.")
                for i in range(4)]

    monkeypatch.setattr(llm, "call_llm", fake_call_llm)
    monkeypatch.setattr(llm, "first_text", lambda resp: resp)
    monkeypatch.setattr(cfeeds, "fetch_rss", fake_rss)

    result = asyncio.new_event_loop().run_until_complete(cdigest.run_digest("news"))
    assert result["posted"] == 0
    # Its own reason: "the news was thin" and "the sources returned nothing
    # worth posting" call for different fixes.
    assert result["reason"] == cdigest.REASON_BELOW_FLOOR
    with store._conn() as conn:
        statuses = sorted(r[0] for r in conn.execute(
            "SELECT status FROM community_content_items").fetchall())
    # The two the selector wanted are still candidates; the two it rejected are
    # retired, because that was a decision about the story.
    assert statuses == ["new", "new", "skipped", "skipped"], statuses


def test_a_refused_post_is_recorded_as_blocked_not_as_a_crash(monkeypatch, tmp_path):
    """A write-path refusal has its own reason. Letting it raise made a PHI
    finding read like a network error on the admin card."""
    import asyncio

    import ai.llm_client as llm
    from community import feeds as cfeeds
    from community import router as crouter
    from community import system_posts

    monkeypatch.setattr(community_store, "_stores",
                        dict(community_store._stores), raising=False)
    store = community_store.reset_community_store_for_tests(
        db_path=str(tmp_path / "community.db"))
    monkeypatch.setattr(crouter, "member_map", lambda **kw: {})
    store.ensure_default_channels()

    async def fake_call_llm(*, role, system, messages, **kw):
        sent = json.loads(messages[0]["content"])
        if "digest_kind" in sent:
            return json.dumps({"items": [
                {"headline": it["title"][:60],
                 "why_it_matters": "It changes what a clinic does.",
                 "source": "Fake Wire", "url": it["url"], "section": "Research"}
                for it in sent["items"]]}), {}
        return json.dumps({"items": [
            {"id": it["id"], "keep": True, "relevance": 0.9,
             "one_liner": "what happened"} for it in sent["items"]]}), {}

    async def fake_rss():
        return [cfeeds._item("rss:test", url=f"https://example.com/b{i}",
                             title=f"AI model cleared for clinical use {i}",
                             abstract="An artificial intelligence system.")
                for i in range(3)]

    monkeypatch.setattr(llm, "call_llm", fake_call_llm)
    monkeypatch.setattr(llm, "first_text", lambda resp: resp)
    monkeypatch.setattr(cfeeds, "fetch_rss", fake_rss)
    monkeypatch.setattr(system_posts, "_phi_clear", lambda *a, **k: False)

    result = asyncio.new_event_loop().run_until_complete(cdigest.run_digest("news"))
    assert result["ok"] is False
    assert result["reason"] == cdigest.REASON_BLOCKED
    with store._conn() as conn:
        row = conn.execute(
            "SELECT reason FROM community_digest_runs ORDER BY id DESC").fetchone()
    assert row[0] == cdigest.REASON_BLOCKED


@pytest.mark.parametrize("items", [
    "not a list",
    ["a string, not an item"],
    [{"headline": "ok"}, "and a string"],
    [],
])
def test_a_malformed_items_list_never_reaches_the_email_builder(items):
    """The flush loop has no per-member guard, so an AttributeError raised while
    building one member's digest aborts the whole realm's flush — deterministically,
    every tick, stalling the queue for everyone behind that row."""
    assert cnotify.digest_payload_of(
        {"kind": "digest_news", "payload_json": json.dumps({"items": items})}) is None
