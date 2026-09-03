"""Community activation (PRD group E).

The community was fully built and entirely dormant. This suite covers the four
things that turning it on adds, and it is arranged around the properties that
decide whether each one helps or hurts:

  * **Rooms people are actually in.** Subspecialty and city rooms follow the
    country rule exactly, because the country rule was learned the hard way: a
    room of one reads as an empty building. The alias map is the new part, and
    what it has to guarantee is that "CKD", "chronic kidney disease" and
    "C.K.D." are one room rather than three empty ones.
  * **A question people can answer with one tap.** The weekly discussion
    becomes a real poll, authored by the bot, without loosening the
    member-facing polls API. A poll with nothing to choose between is worse
    than a question, so an empty search falls back to prose.
  * **The company voice, on purpose.** An admin can post as Archangel, into
    channels where a bot post is expected, with their own id on the audit row.
  * **A room members cannot see.** The staff spotlight is the first channel in
    the product with an access rule, so the three ways out of it (the channel
    list, a message by id, the WebSocket) are each held closed by a test. Two
    of the three were the gaps the earlier attempt at this found.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone

import pytest

# Isolated audit DB for this module (the audit chain writes to TEAM_DB_PATH).
os.environ["TEAM_DB_PATH"] = os.path.join("/tmp", f"community_act_audit_{uuid.uuid4().hex}.db")
os.environ.setdefault("EMAIL_DEV_MODE", "1")

from _asclepius import app, fresh_store, headers_for, make_user, token_for, uniq  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from community import digest as cdigest  # noqa: E402
from community import morning as cmorning  # noqa: E402
from community import newsletter as cnewsletter  # noqa: E402
from community import router as crouter  # noqa: E402
from community import store as community_store  # noqa: E402
from community import subspecialties as csub  # noqa: E402
from community import websearch  # noqa: E402
from community.store import CommunityStore  # noqa: E402

client = TestClient(app)
BASE = "/api/community"
ADMIN_BASE = "/api/asclepius/admin"


def _fresh_store() -> CommunityStore:
    path = os.path.join(tempfile.mkdtemp(prefix="cstore-activation-"), "community.db")
    return CommunityStore(db_path=path)


def _members(*specs):
    """(cohort-dict, is_staff) tuples -> a member map shaped like member_map."""
    out = {}
    for i, (attrs, is_staff) in enumerate(specs):
        out[f"u-{i}"] = {
            "user_id": f"u-{i}", "display_name": f"Dr {i}",
            "specialty": "nephrology", "country": None,
            "subspecialties": [], "city": None, "is_staff": is_staff,
            **attrs,
        }
    return out


@pytest.fixture
def visible(monkeypatch):
    """router.visible_channels bound to a throwaway store."""
    store = _fresh_store()
    monkeypatch.setattr(crouter, "_cstore", lambda: store)
    return store, crouter


# ═══ Subspecialty rooms ═══════════════════════════════════════════════════════
def test_the_alias_map_collapses_spellings_of_one_subspecialty_onto_one_room():
    """The reason the map exists at all. Subspecialties are free text, so the
    same practice arrives written five ways, and slugifying the text directly
    would open three rooms of one person each instead of one room of three."""
    for spelling in ("CKD", "ckd", "C.K.D.", "chronic kidney disease",
                     "Chronic Kidney Disease", "chronic-kidney-disease"):
        assert csub.get(spelling).slug == "ckd", spelling
    defs = csub.channel_defs(["CKD", "chronic kidney disease", "C.K.D."])
    assert [d["slug"] for d in defs] == ["ckd"]


def test_an_unmapped_subspecialty_creates_nothing():
    """The deliberate cost of curating the map: an unknown subspecialty has no
    room until a person adds it, rather than a junk room appearing on its own."""
    assert csub.get("interventional pulmonology") is None
    assert csub.channel_defs(["interventional pulmonology"]) == []
    store = _fresh_store()
    store.ensure_default_channels([], subspecialties=["interventional pulmonology"])
    assert [c for c in store.list_channels() if c.get("grp") == "subspecialty"] == []


def test_no_alias_is_claimed_by_two_subspecialties():
    """A duplicated alias would silently decide which room a physician lands in
    by declaration order in a config file."""
    seen = {}
    for sub in csub.SUBSPECIALTIES:
        for alias in (sub.slug,) + sub.aliases:
            key = csub.normalize(alias)
            assert key not in seen or seen[key] is sub, (
                f"{alias!r} is claimed by both {seen.get(key)} and {sub.slug}")
            seen[key] = sub


def test_a_subspecialty_slug_never_collides_with_another_kind_of_room():
    """The channel table keys on slug and the seeding path UPSERTs, so a
    collision would rewrite a country or specialty room in place."""
    from community.countries import COUNTRIES

    others = {c["slug"] for c in community_store.DEFAULT_CHANNELS}
    others |= {c["slug"] for c in community_store.specialty_channel_defs()}
    others |= {c.slug for c in COUNTRIES.values()}
    for sub in csub.SUBSPECIALTIES:
        assert sub.slug not in others, sub.slug


def test_a_subspecialty_room_is_hidden_until_enough_colleagues_are_there(visible, monkeypatch):
    store, router = visible
    monkeypatch.setenv("COMMUNITY_SUBSPECIALTY_MIN_MEMBERS", "3")
    store.ensure_default_channels([], subspecialties=["dialysis"])

    two = _members(({"subspecialties": ["dialysis"]}, False),
                   ({"subspecialties": ["dialysis"]}, False))
    assert "dialysis" not in {c["slug"] for c in router.visible_channels(two)}

    three = _members(({"subspecialties": ["dialysis"]}, False),
                     ({"subspecialties": ["dialysis"]}, False),
                     ({"subspecialties": ["dialysis"]}, False))
    assert "dialysis" in {c["slug"] for c in router.visible_channels(three)}


def test_staff_do_not_count_towards_a_subspecialty(visible, monkeypatch):
    """Otherwise the first physician in a subspecialty walks into a room
    containing themselves and two members of the Archangel team."""
    store, router = visible
    monkeypatch.setenv("COMMUNITY_SUBSPECIALTY_MIN_MEMBERS", "3")
    store.ensure_default_channels([], subspecialties=["dialysis"])
    members = _members(({"subspecialties": ["dialysis"]}, False),
                       ({"subspecialties": ["dialysis"]}, True),
                       ({"subspecialties": ["dialysis"]}, True))
    assert "dialysis" not in {c["slug"] for c in router.visible_channels(members)}


def test_a_subspecialty_room_with_history_survives_the_cohort_shrinking(visible, monkeypatch):
    """A room does not vanish because somebody was deactivated: the
    conversation in it happened."""
    store, router = visible
    monkeypatch.setenv("COMMUNITY_SUBSPECIALTY_MIN_MEMBERS", "3")
    store.ensure_default_channels([], subspecialties=["dialysis"])
    channel = store.get_channel_by_slug("dialysis")
    store.insert_message(channel_id=channel["id"], author_user_id="u-system",
                         body="morning brief")
    assert "dialysis" in {c["slug"] for c in router.visible_channels(_members())}


def test_a_physician_with_several_subspecialties_counts_towards_each(visible, monkeypatch):
    """They really do belong in all of them, and the alternative rule (count
    only the first) would keep every second room permanently empty."""
    store, router = visible
    monkeypatch.setenv("COMMUNITY_SUBSPECIALTY_MIN_MEMBERS", "2")
    store.ensure_default_channels([], subspecialties=["dialysis", "transplant"])
    both = {"subspecialties": ["dialysis", "transplant-nephrology"]}
    members = _members((both, False), (both, False))
    slugs = {c["slug"] for c in router.visible_channels(members)}
    assert {"dialysis", "transplant-nephrology"} <= slugs


# ═══ City rooms ═══════════════════════════════════════════════════════════════
def test_city_spellings_normalize_onto_one_room():
    """Free text again, and the seeding path and the counting path have to
    agree exactly or the room can never reach its threshold."""
    for spelling in ("Boston", "boston", " Boston ", "Boston, MA"):
        assert community_store.city_slug(spelling) == "boston", spelling
    assert community_store.city_slug("São Paulo") == "sao-paulo"
    assert community_store.city_slug("") == ""
    defs = community_store.city_channel_defs(["Boston", "Boston, MA", "boston"])
    assert [d["slug"] for d in defs] == ["boston"]


def test_a_city_never_claims_a_country_or_specialty_room():
    """Singapore is both a country room and a city a physician types, and the
    seeding UPSERT keys on slug."""
    defs = community_store.city_channel_defs(["Singapore", "Nephrology", "Boston"])
    assert [d["slug"] for d in defs] == ["boston"]


def test_a_city_room_is_hidden_until_enough_colleagues_are_there(visible, monkeypatch):
    store, router = visible
    monkeypatch.setenv("COMMUNITY_CITY_MIN_MEMBERS", "3")
    store.ensure_default_channels([], cities=["Boston"])

    two = _members(({"city": "boston"}, False), ({"city": "boston"}, False))
    assert "boston" not in {c["slug"] for c in router.visible_channels(two)}

    three = _members(({"city": "boston"}, False), ({"city": "boston"}, False),
                     ({"city": "boston"}, False))
    assert "boston" in {c["slug"] for c in router.visible_channels(three)}


def test_a_city_room_with_history_survives_the_cohort_shrinking(visible, monkeypatch):
    store, router = visible
    monkeypatch.setenv("COMMUNITY_CITY_MIN_MEMBERS", "3")
    store.ensure_default_channels([], cities=["Boston"])
    channel = store.get_channel_by_slug("boston")
    store.insert_message(channel_id=channel["id"], author_user_id="u-system",
                         body="hello Boston")
    assert "boston" in {c["slug"] for c in router.visible_channels(_members())}


def test_city_rooms_stay_dormant_while_nobody_reports_a_city():
    """The profile field does not exist yet, so this is the state the feature
    ships in and it has to cost nothing."""
    store = _fresh_store()
    store.ensure_default_channels([], cities=[])
    assert [c for c in store.list_channels() if c.get("grp") == "city"] == []


# ═══ Seeding safety ═══════════════════════════════════════════════════════════
def test_a_none_cohort_input_deactivates_nothing():
    """None means "I did not look them up", not "there are none". A roster
    hiccup on boot must not retire every room in the product."""
    store = _fresh_store()
    store.ensure_default_channels(["SA"], subspecialties=["dialysis"], cities=["Boston"])
    store.ensure_default_channels()          # e.g. the store's own __init__
    live = {c["slug"] for c in store.list_channels()}
    assert {"saudi-arabia", "dialysis", "boston"} <= live


def test_the_dimensions_are_independent():
    """A caller may hand over one cohort and withhold the others, and the two
    it withheld must not be retired by the one it supplied."""
    store = _fresh_store()
    store.ensure_default_channels(["SA"], subspecialties=["dialysis"], cities=["Boston"])
    store.ensure_default_channels(["SA"])    # subspecialties/cities withheld
    live = {c["slug"] for c in store.list_channels()}
    assert {"saudi-arabia", "dialysis", "boston"} <= live


def test_a_cohort_that_really_disappeared_is_deactivated_not_deleted():
    """Mirrors the country rule: the history stays in the DB and moderation
    paths can still resolve the channel."""
    store = _fresh_store()
    store.ensure_default_channels([], subspecialties=["dialysis"])
    store.ensure_default_channels([], subspecialties=[])
    assert store.get_channel_by_slug("dialysis") is not None
    assert store.get_channel_by_slug("dialysis")["is_active"] == 0
    assert "dialysis" not in {c["slug"] for c in store.list_channels()}


def test_the_new_columns_are_added_to_a_channel_table_that_predates_them():
    """Production is a live SQLite file with no migration framework, so the
    guarded ALTER is the whole migration story."""
    path = os.path.join(tempfile.mkdtemp(prefix="cstore-migrate-"), "old.db")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE community_channels (id TEXT PRIMARY KEY, slug TEXT UNIQUE "
            "NOT NULL, name TEXT NOT NULL, description TEXT, post_policy TEXT NOT NULL "
            "DEFAULT 'all', position INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO community_channels (id, slug, name, description, created_at) "
            "VALUES ('ch-legacy', 'general', 'general', 'old', '2026-01-01T00:00:00Z')")
    store = CommunityStore(db_path=path)
    cols = {c["slug"]: c for c in store.list_channels(include_inactive=True)}
    assert "subspecialty" in cols["general"] and "city" in cols["general"]
    # The pre-existing room is exactly as visible as it was.
    assert cols["general"]["staff_only"] == 0
    assert cols["general"]["is_active"] == 1


# ═══ The weekly discussion, as a poll ═════════════════════════════════════════
@pytest.fixture
def wired(monkeypatch):
    """A store, all channels visible, and no real model calls."""
    store = _fresh_store()
    store.ensure_default_channels([])
    monkeypatch.setattr(cmorning, "get_community_store", lambda: store)
    monkeypatch.setattr(cmorning, "ensure_country_channels", lambda: None)
    monkeypatch.setattr(
        cmorning, "_visible_channel_slugs",
        lambda: {c["slug"] for c in store.list_channels()})

    import community.system_posts as sp

    monkeypatch.setattr(sp, "get_community_store", lambda: store)
    monkeypatch.setattr("community.router.member_map", lambda **kw: {})
    monkeypatch.setattr(crouter, "_cstore", lambda: store)
    return store


def _topic(options=None):
    item = {"title": "Should a model triage before a human sees the chart?",
            "url": "https://example.org/topic",
            "summary": "Two hospitals published opposite results.",
            "prompt": "Would you let it triage before you read the chart?"}
    if options is not None:
        item["options"] = options
    return item


def _discussion_scope():
    return cmorning.Scope(key="morning:discussion", channel="future-of-medical-ai",
                          tz="America/New_York")


def _run_discussion(store, monkeypatch, options):
    async def _one(**kwargs):
        return [_topic(options)]

    monkeypatch.setattr(websearch, "search_discussion_topic", _one)
    return asyncio.run(cmorning.run_scope(_discussion_scope(), force=True))


def test_the_weekly_discussion_posts_a_poll_authored_by_the_bot(wired, monkeypatch):
    """The bot has to be the author. Routing this through the member-facing
    polls API would have made the weekly prompt read as "Dr. X asked", or
    forced a system-authorship branch onto a member endpoint."""
    result = _run_discussion(wired, monkeypatch, ["Yes, with an audit trail", "No, never"])
    assert result["outcome"] == "posted"

    channel = wired.get_channel_by_slug("future-of-medical-ai")
    messages, _ = wired.list_messages(channel["id"])
    polls = [m for m in messages if m.get("kind") == "poll"]
    assert len(polls) == 1
    poll = wired.poll_for_message(polls[0]["id"])
    assert poll and poll["created_by"] == "u-system"
    assert polls[0]["author_user_id"] == "u-system"


def test_the_poll_always_offers_a_way_out_of_its_own_options(wired, monkeypatch):
    """A poll that forecloses the answer kills the thread it exists to start."""
    _run_discussion(wired, monkeypatch, ["Yes, with an audit trail", "No, never"])
    channel = wired.get_channel_by_slug("future-of-medical-ai")
    messages, _ = wired.list_messages(channel["id"])
    poll = wired.poll_for_message(
        [m for m in messages if m.get("kind") == "poll"][0]["id"])
    texts = [o["text"] for o in wired.poll_results(poll["id"])["options"]]
    assert texts[-1] == cmorning.DISCUSSION_ESCAPE_OPTION
    assert len(texts) == 3


def test_a_topic_search_with_no_options_falls_back_to_prose(wired, monkeypatch):
    """A poll with one real option and an escape hatch is not a question, it is
    a button. Requirement 11: prose beats a one-option poll."""
    result = _run_discussion(wired, monkeypatch, None)
    assert result["outcome"] == "posted"
    channel = wired.get_channel_by_slug("future-of-medical-ai")
    messages, _ = wired.list_messages(channel["id"])
    assert [m["kind"] for m in messages] == [cmorning.KIND_DISCUSSION]
    assert not [m for m in messages if m.get("kind") == "poll"]


def test_a_single_proposed_option_is_not_enough_for_a_poll(wired, monkeypatch):
    _run_discussion(wired, monkeypatch, ["Yes, with an audit trail"])
    channel = wired.get_channel_by_slug("future-of-medical-ai")
    messages, _ = wired.list_messages(channel["id"])
    assert [m["kind"] for m in messages] == [cmorning.KIND_DISCUSSION]


def test_the_discussion_poll_posts_once_a_day_however_often_it_is_triggered(wired, monkeypatch):
    """The trigger runs hourly and an admin can fire it by hand."""
    assert _run_discussion(wired, monkeypatch, ["A", "B"])["outcome"] == "posted"

    async def _one(**kwargs):
        return [_topic(["A", "B"])]

    monkeypatch.setattr(websearch, "search_discussion_topic", _one)
    assert asyncio.run(cmorning.run_scope(_discussion_scope()))["outcome"] == "not_due"

    channel = wired.get_channel_by_slug("future-of-medical-ai")
    messages, _ = wired.list_messages(channel["id"])
    assert len([m for m in messages if m.get("kind") == "poll"]) == 1


def test_a_member_vote_on_the_bots_poll_counts_like_any_other(wired, monkeypatch):
    """Same store rows and same serializer as a member poll: only the author
    differs, so voting and results cannot diverge."""
    _run_discussion(wired, monkeypatch, ["Yes, with an audit trail", "No, never"])
    channel = wired.get_channel_by_slug("future-of-medical-ai")
    messages, _ = wired.list_messages(channel["id"])
    poll = wired.poll_for_message(
        [m for m in messages if m.get("kind") == "poll"][0]["id"])
    option = wired.poll_results(poll["id"])["options"][0]

    wired.vote_poll(poll["id"], option["id"], "u-voter")
    results = wired.poll_results(poll["id"], viewer_id="u-voter")
    assert results["total_votes"] == 1
    assert results["your_vote"] == option["id"]
    # And the bot never votes for itself.
    assert wired.poll_results(poll["id"], viewer_id="u-system")["your_vote"] is None


def test_the_poll_payload_rides_the_message_the_client_receives(wired, monkeypatch):
    """The serializer attaches the poll by looking up the link, so posting
    before linking would broadcast a poll-kind message with no poll in it."""
    _run_discussion(wired, monkeypatch, ["A", "B"])
    channel = wired.get_channel_by_slug("future-of-medical-ai")
    messages, _ = wired.list_messages(channel["id"])
    msg = [m for m in messages if m.get("kind") == "poll"][0]
    serialized = crouter._serialize_messages([msg], {}, channel["slug"])[0]
    assert serialized["poll"]["options"], "the poll card would render empty"


# ═══ Admin persona posting ════════════════════════════════════════════════════
def fresh_community():
    return community_store.reset_community_store_for_tests(
        db_path=os.path.join("/tmp", f"community_act_{uuid.uuid4().hex}.db"))


def make_verified_physician(store, *, specialty="nephrology"):
    user = store.create_user(email=f"dr-{uniq()}@asclepius.example.com",
                             password="pw-12345678", role="evaluator",
                             specialty=specialty, years_experience=12,
                             organization="Riverside Nephrology")
    store.upsert_contributor_credentials(
        id_hashed=user["id_hashed"], user_id=user["id"],
        organization="Riverside Nephrology", role_title="Physician (MD)",
        credentials_verified=True,
        ship={"degree": "MD", "primary_specialty": specialty,
              "subspecialties": ["dialysis"], "credentials_verified": True},
        verify={"full_legal_name": "Jane A. Doe, MD"},
    )
    return user


def setup_world():
    from community.ws import hub as _hub
    _hub._sockets.clear()
    astore = fresh_store()
    cstore = fresh_community()
    doc = make_verified_physician(astore)
    admin = make_user(astore, role="admin")
    return astore, cstore, doc, admin


def audit_events(action):
    with sqlite3.connect(os.environ["TEAM_DB_PATH"]) as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT * FROM audit_events ORDER BY id ASC").fetchall()
        except sqlite3.OperationalError:
            return []
    return [dict(r) for r in rows if r["action"] == action]


def test_the_persona_endpoint_posts_as_the_bot_not_as_the_admin():
    """An announcement signed by whichever admin happened to be awake renders
    as "Former member" the day that account is closed."""
    _, cstore, doc, admin = setup_world()
    r = client.post(f"{ADMIN_BASE}/community/post", headers=headers_for(admin),
                    json={"channel_slug": "events", "body": "Journal club moved to Thursday."})
    assert r.status_code == 200, r.text
    assert r.json()["message"]["author"]["is_bot"] is True
    assert r.json()["message"]["author"]["display_name"] == "Archangel"

    msgs = client.get(f"{BASE}/channels/events/messages",
                      headers=headers_for(doc)).json()["messages"]
    assert any("Thursday" in m["body"] for m in msgs)


def test_a_plain_member_cannot_post_as_the_company():
    _, _, doc, _ = setup_world()
    r = client.post(f"{ADMIN_BASE}/community/post", headers=headers_for(doc),
                    json={"channel_slug": "events", "body": "hello"})
    assert r.status_code in (401, 403)


def test_the_persona_cannot_speak_in_a_room_of_colleagues():
    """#general and the specialty rooms are members talking to each other. The
    company account appearing there as though it were one of them is the thing
    the channel list is protecting."""
    _, _, _, admin = setup_world()
    r = client.post(f"{ADMIN_BASE}/community/post", headers=headers_for(admin),
                    json={"channel_slug": "general", "body": "hello everyone"})
    assert r.status_code == 400


def test_every_persona_post_records_which_admin_pressed_the_button():
    """post_system_message logs the bot as the actor, which is right for the
    reader and useless for an investigation."""
    _, _, _, admin = setup_world()
    r = client.post(f"{ADMIN_BASE}/community/post", headers=headers_for(admin),
                    json={"channel_slug": "medical-ai-news", "body": "One story worth reading."})
    assert r.status_code == 200
    rows = [e for e in audit_events("community.persona_post")
            if e["actor_id"] == admin["id"]]
    assert rows, "no audit row carries the acting admin"
    assert json.loads(rows[-1]["detail_json"])["channel"] == "medical-ai-news"


def test_announce_fans_out_only_from_task_announcements():
    """The fan-out rule post_system_message documents: a routine events post
    must not mail the whole community."""
    _, cstore, doc, admin = setup_world()
    r = client.post(f"{ADMIN_BASE}/community/post", headers=headers_for(admin),
                    json={"channel_slug": "events", "body": "Rounds on Friday.",
                          "announce": True})
    assert r.status_code == 200
    assert r.json()["announced"] is False
    assert all(n["user_id"] != doc["id"] for n in cstore.unsent_notifications())

    r = client.post(f"{ADMIN_BASE}/community/post", headers=headers_for(admin),
                    json={"channel_slug": "task-announcements",
                          "body": "New nephrology batch is live.", "announce": True})
    assert r.status_code == 200
    assert r.json()["announced"] is True
    assert any(n["user_id"] == doc["id"] for n in cstore.unsent_notifications())


def test_a_persona_post_that_trips_the_phi_gate_tells_the_admin():
    """The bot path drops a blocked post silently, because inside a digest run
    there is nobody to tell. There is somebody here."""
    _, _, _, admin = setup_world()
    r = client.post(f"{ADMIN_BASE}/community/post", headers=headers_for(admin),
                    json={"channel_slug": "events", "body": "Case: MRN 12345678 review"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "post_rejected"


# ═══ The staff-only spotlight ═════════════════════════════════════════════════
def _seed_pool(cstore, n=2, status="new"):
    items = [
        {"source": "rss:example", "external_id": None,
         "url": f"https://example.org/story-{i}", "url_norm": f"example.org/story-{i}",
         "title": f"Story {i}", "title_norm": f"story {i}",
         "published_at": None, "abstract": "What happened, in one line."}
        for i in range(n)
    ]
    fresh = cstore.upsert_content_items(items)
    if status != "new":
        cstore.mark_content_items([f["id"] for f in fresh], status=status)
    return fresh


def test_a_member_cannot_see_the_staff_room_in_the_channel_list():
    _, _, doc, admin = setup_world()
    member_slugs = [c["slug"] for c in
                    client.get(f"{BASE}/channels", headers=headers_for(doc)).json()["channels"]]
    staff_slugs = [c["slug"] for c in
                   client.get(f"{BASE}/channels", headers=headers_for(admin)).json()["channels"]]
    assert "team-ai-spotlight" not in member_slugs
    assert "team-ai-spotlight" in staff_slugs


def test_a_member_gets_the_same_404_on_the_staff_channel_as_on_an_unknown_one():
    """No oracle: a hidden channel must not answer differently from one that
    does not exist."""
    _, _, doc, _ = setup_world()
    hidden = client.get(f"{BASE}/channels/team-ai-spotlight/messages",
                        headers=headers_for(doc))
    unknown = client.get(f"{BASE}/channels/no-such-channel/messages",
                         headers=headers_for(doc))
    assert hidden.status_code == 404
    assert hidden.json() == unknown.json()


def test_a_member_cannot_reach_a_staff_message_by_id():
    """The first of the two visibility gaps: every by-id path (thread, edit,
    react, attachment) used to treat any channel message as visible to any
    member, which is harmless only while no channel has an access rule."""
    _, cstore, doc, admin = setup_world()
    _seed_pool(cstore)
    posted = asyncio.run(cdigest.run_spotlight_digest(force=True))
    assert posted["outcome"] == "posted", posted
    mid = posted["message_id"]

    assert client.get(f"{BASE}/messages/{mid}/thread",
                      headers=headers_for(doc)).status_code == 404
    assert client.post(f"{BASE}/messages/{mid}/reactions", json={"emoji": "👍"},
                       headers=headers_for(doc)).status_code == 404
    # And a staff account reaches the same message perfectly well.
    assert client.get(f"{BASE}/messages/{mid}/thread",
                      headers=headers_for(admin)).status_code == 200


def test_a_staff_post_is_not_pushed_down_a_members_websocket():
    """The second gap, and the one the REST checks cannot cover: hiding a room
    from the channel list means nothing if the socket delivers its contents to
    every connected physician anyway."""
    _, cstore, doc, admin = setup_world()
    _seed_pool(cstore)
    with client.websocket_connect(f"{BASE}/ws?token={token_for(doc)}") as ws:
        assert ws.receive_json()["type"] == "hello"
        asyncio.run(cdigest.run_spotlight_digest(force=True))
        # A post the member IS entitled to, sent after the staff one: if the
        # staff frame had been fanned out it would arrive first.
        client.post(f"{BASE}/channels/general/messages", json={"body": "afternoon all"},
                    headers=headers_for(doc))
        evt = ws.receive_json()
        assert evt["type"] == "message.created"
        assert evt["message"]["channel"] == "general"


def test_the_spotlight_posts_one_story_a_day_whichever_job_runs_first():
    """It reads 'skipped' rows precisely so run order against the news digest
    cannot starve it: the digest marks everything it did not publish skipped."""
    _, cstore, _, _ = setup_world()
    fresh = _seed_pool(cstore, n=3, status="skipped")
    assert fresh, "the pool fixture wrote nothing"

    first = asyncio.run(cdigest.run_spotlight_digest(force=True))
    assert first["outcome"] == "posted"
    second = asyncio.run(cdigest.run_spotlight_digest())
    assert second["outcome"] == "not_due"

    channel = cstore.get_channel_by_slug("team-ai-spotlight")
    messages, _ = cstore.list_messages(channel["id"])
    assert len([m for m in messages if m.get("kind") == "spotlight"]) == 1


def test_the_spotlight_does_not_re_offer_the_story_it_used():
    _, cstore, _, _ = setup_world()
    _seed_pool(cstore, n=1)
    asyncio.run(cdigest.run_spotlight_digest(force=True))
    assert cstore.candidate_items_for_spotlight() == []


def test_an_empty_pool_is_a_quiet_day_not_a_failure():
    """A channel that greets the team with an apology every morning teaches
    them to stop looking, and a failed run would page somebody."""
    _, cstore, _, _ = setup_world()
    result = asyncio.run(cdigest.run_spotlight_digest(force=True))
    assert result["outcome"] == "quiet" and result["ok"] is True
    assert cstore.last_successful_run_at("spotlight")


# ═══ The morning email ════════════════════════════════════════════════════════
def test_the_staff_room_never_reaches_a_members_inbox():
    """The channel is hidden in the app and the email is a separate path out of
    the same rows."""
    channels = [
        {"slug": "general", "grp": "core", "staff_only": 0},
        {"slug": "team-ai-spotlight", "grp": "core", "staff_only": 1},
        {"slug": "nephrology", "grp": "specialty", "specialty": "nephrology",
         "staff_only": 0},
    ]
    member = {"specialty": "nephrology", "country": "US",
              "subspecialties": [], "city": None}
    slugs = cnewsletter._member_channels(member, channels)
    assert "team-ai-spotlight" not in slugs
    assert {"general", "nephrology"} <= set(slugs)
    # True for staff too: the in-app channel is where that content is read.
    staff = {**member, "is_staff": True}
    assert "team-ai-spotlight" not in cnewsletter._member_channels(staff, channels)


def test_a_doctor_whose_rooms_were_silent_gets_no_email():
    """A daily message that is empty four days a week trains people to filter
    it, and then the one that mattered goes to the same place."""
    _, cstore, doc, _ = setup_world()

    class _NoTasks:
        def list_tasks(self, **kw):
            return []

    member = {"user_id": doc["id"], "email": doc["email"], "display_name": "Dr Doe",
              "specialty": "nephrology", "country": "US", "subspecialties": [],
              "city": None}
    outcome = asyncio.run(cnewsletter.send_for_member(
        cstore, _NoTasks(), member, cstore.list_channels()))
    assert outcome == "quiet"


def test_a_cohort_that_already_had_its_morning_is_skipped_on_a_re_run():
    """One email per doctor per day, however many times the hourly trigger
    fires. The ledger key is per country cohort."""
    _, cstore, _, _ = setup_world()
    key = "morning:newsletter:US"
    run_id = cstore.start_digest_run(key)
    cstore.finish_digest_run(run_id, ok=True)
    assert cmorning.is_due(cstore.last_successful_run_at(key), "America/New_York",
                           now=datetime.now(timezone.utc)) is False


def test_an_unsubscribed_doctor_is_not_mailed_even_when_their_rooms_were_busy():
    _, cstore, doc, _ = setup_world()
    cstore.set_news_frequency(doc["id"], "off")

    class _Tasks:
        def list_tasks(self, **kw):
            return []

    member = {"user_id": doc["id"], "email": doc["email"], "display_name": "Dr Doe",
              "specialty": "nephrology", "country": "US", "subspecialties": [],
              "city": None}
    outcome = asyncio.run(cnewsletter.send_for_member(
        cstore, _Tasks(), member, cstore.list_channels()))
    assert outcome == "unsubscribed"


# ═══ The status endpoint ══════════════════════════════════════════════════════
@pytest.fixture
def internal_secret(monkeypatch):
    monkeypatch.setenv("INTERNAL_TOOL_SECRET", "test-internal-secret-0123456789")
    return {"Authorization": "Bearer test-internal-secret-0123456789"}


def test_the_status_endpoint_reports_the_gate_and_the_loop_separately(internal_secret):
    """They diverge when startup raised after the gate passed, so inferring one
    from the other is exactly the blindness this endpoint exists to remove."""
    setup_world()
    r = client.get("/internal/community/status", headers=internal_secret)
    assert r.status_code == 200, r.text
    body = r.json()
    for block in ("news_digest", "morning_routine"):
        assert "gate" in body[block] and "loop_running" in body[block]
        assert isinstance(body[block]["gate"], bool)
        assert isinstance(body[block]["loop_running"], bool)
    assert "spotlight" in body["runs"]


def test_the_status_endpoint_never_returns_a_secrets_value(internal_secret):
    """It reports whether a dependency is configured, which is the question,
    and a status page that echoed the key would be a credential leak behind one
    shared bearer token."""
    setup_world()
    body = client.get("/internal/community/status", headers=internal_secret).json()
    blob = json.dumps(body)
    assert "test-internal-secret-0123456789" not in blob
    assert body["dependencies"]["anthropic_api_key_set"] in (True, False)
    for key in ("ANTHROPIC_API_KEY", "SENDGRID_API_KEY", "AUTH_SECRET",
                "INTERNAL_TOOL_SECRET"):
        value = os.getenv(key)
        if value:
            assert value not in blob, key


def test_the_status_endpoint_is_not_public(internal_secret):
    """The body names every gate and dependency this deployment has. It is a
    map of what is switched off, which is exactly what an attacker wants."""
    setup_world()
    assert client.get("/internal/community/status").status_code == 401
    assert client.get("/internal/community/status",
                      headers={"Authorization": "Bearer wrong"}).status_code == 401
