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


def test_an_existing_local_database_is_not_stranded(monkeypatch, tmp_path):
    """A developer who adds ASCLEPIUS_DB_PATH must not silently open an empty
    community while their real one sits beside the code, unopened."""
    _clear_paths(monkeypatch)
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "community.db").write_text("pretend this is sqlite")
    volume = tmp_path / "vol"
    volume.mkdir()
    monkeypatch.setattr(realm, "_backend_dir", lambda: str(backend))
    monkeypatch.setenv("ASCLEPIUS_DB_PATH", str(volume / "asclepius.db"))
    assert realm.live_community_db() == str(backend / "community.db")

    # ...and once the durable one exists it wins, so the path is stable rather
    # than flipping back and forth with the state of the developer's disk.
    (volume / "community.db").write_text("pretend this is sqlite too")
    assert realm.live_community_db() == str(volume / "community.db")


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
                               body="Medical AI digest", kind="digest_news")

    second = _store_at(path)
    again = second.get_channel_by_slug("medical-ai-news")
    msgs, _ = second.list_messages(again["id"])
    assert [m["id"] for m in msgs] == [msg["id"]]
    assert msgs[0]["body"] == "Medical AI digest"


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
         "why_it_matters": "First reimbursed autonomous diagnostic; it sets the "
                           "template.",
         "source": "STAT", "url": "https://example.org/a", "section": "Regulation"},
        {"headline": "Frontier models score under 0.3 kappa on risk of bias",
         "why_it_matters": "More reasoning did not help; the failure is judgment.",
         "source": "Synthesis Bench", "url": "https://example.org/b",
         "section": "Research"},
        {"headline": "Health system rolls back its ambient scribe after an audit",
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
    assert out["title"] == "Medical AI digest"
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
    (_with_item(0, headline=" ".join(["word"] * 13)), "headline over twelve words"),
    (_with_item(0, headline=""), "empty headline"),
    (_with_item(0, why_it_matters=" ".join(["word"] * 26)), "why over 25 words"),
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
    assert digest_email_subject(payload) == "Medical AI digest · 3 items"

    html_out = build_community_digest_post_email(
        payload=payload, community_url="https://example.test/community",
        unsubscribe_url="https://example.test/u?t=tok", first_name="Kalpesh")
    visible = re.sub(r"<[^>]+>", " ", html_out)
    assert not contract.banned_patterns(visible), visible[:400]
    for item in payload["items"]:
        assert item["headline"] in visible
        assert item["url"] in html_out           # the headline links out
    assert "Regulation" in visible and "Research" in visible


def test_a_one_item_subject_is_not_pluralised():
    payload = {"title": "Medical AI digest", "items": [{"headline": "x"}]}
    from onboarding_emails import digest_email_subject
    assert digest_email_subject(payload) == "Medical AI digest · 1 item"


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
        "Medical AI digest\n\nResearch\nA model was cleared\n"
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
        "title": "Medical AI digest",
        "items": [{"headline": "A headline", "why_it_matters": "A reason.",
                   "source": "STAT", "section": "Research",
                   "url": "https://example.org/a"}],
    })
    assert "A headline" in text and "A reason." in text and "STAT" in text
    # The url is deliberately absent: feeding DOIs and PMIDs to the PHI gate is
    # what the URL masking exists to prevent.
    assert "https://example.org/a" not in text


def test_the_next_run_time_is_read_off_the_same_rules_the_scheduler_uses(monkeypatch):
    """An empty room that promised a digest at a time the scheduler disagreed
    with would be a worse lie than the empty room."""
    from datetime import datetime

    monkeypatch.delenv("COMMUNITY_NEWS_ENABLED", raising=False)
    monkeypatch.setenv("COMMUNITY_DIGEST_NEWS_HOUR_UTC", "13")
    before = datetime(2026, 9, 8, 9, 0)
    after = datetime(2026, 9, 8, 13, 30)
    assert cdigest.next_run_at("news", now=before) == "2026-09-08T13:00:00Z"
    assert cdigest.next_run_at("news", now=after) == "2026-09-09T13:00:00Z"

    monkeypatch.setenv("COMMUNITY_NEWS_ENABLED", "0")
    assert cdigest.next_run_at("news", now=before) is None


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
    assert stored["title"] == "Medical AI digest"
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
           "body": "Medical AI digest", "created_at": "2026-09-08T13:00:00Z",
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
    assert sent == ["Medical AI digest · 3 items"]

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
