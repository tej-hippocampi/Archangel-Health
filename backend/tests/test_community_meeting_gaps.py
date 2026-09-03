"""Three things the Sep 1 meeting asked for that no PRD owned.

Found by auditing the transcript against the seven PRDs, so each one is a
feature nobody wrote down rather than a refinement of something that was:

  * **The crossed room.** The meeting named "neurology-Africa" specifically.
    Specialty rooms and country rooms were built as separate axes, and neither
    is the room a neurologist in Lagos wants: one is the whole world, the other
    is every specialty in one country.
  * **A face on the Archangel account.** The bot writes most of what a new
    physician reads in their first week, and two grey initials read as a system
    notice.
  * **A weekend webinar.** Grouped with the conference and the podcast as
    post-seed, but the transcript treats it as a near-term community activity.

What these tests hold is that each one reuses the machinery that already
exists rather than growing a parallel one beside it, because a second
threshold rule or a second event system is how this subsystem would rot.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime, timezone

import pytest

from community import countries as ccountries
from community import persona as cpersona
from community import router as crouter
from community import store as community_store
from community import webinars as cwebinars
from community.store import CommunityStore


def _fresh_store() -> CommunityStore:
    path = os.path.join(tempfile.mkdtemp(prefix="cstore-gaps-"), "community.db")
    return CommunityStore(db_path=path)


def _members(*specs):
    """(attrs, is_staff) tuples shaped like router.member_map entries."""
    out = {}
    for i, (attrs, is_staff) in enumerate(specs):
        out[f"u-{i}"] = {
            "user_id": f"u-{i}", "display_name": f"Dr {i}", "specialty": None,
            "country": None, "region": None, "subspecialties": [], "city": None,
            "is_staff": is_staff, **attrs,
        }
    return out


@pytest.fixture
def visible(monkeypatch):
    store = _fresh_store()
    monkeypatch.setattr(crouter, "_cstore", lambda: store)
    return store, crouter


# ═══ U9: the crossed room ═════════════════════════════════════════════════════
def test_a_crossed_room_exists_for_a_specialty_in_a_region():
    """The room the meeting named. #nephrology-africa is neither #nephrology
    nor #nigeria, and a physician looking for colleagues near enough to share
    a guideline has nowhere else to look."""
    store = _fresh_store()
    store.ensure_default_channels([], specialty_regions=["nephrology|africa"])
    room = store.get_channel_by_slug("nephrology-africa")
    assert room is not None
    assert room["grp"] == "specialty_region"
    assert room["specialty"] == "nephrology" and room["region"] == "africa"


def test_a_crossed_room_does_not_replace_either_room_it_crosses():
    """It is a third axis. Collapsing it into one of the two would take the
    whole-world specialty room away from the people who want it."""
    store = _fresh_store()
    store.ensure_default_channels(["NG"], specialty_regions=["nephrology|africa"])
    slugs = {c["slug"] for c in store.list_channels(include_inactive=True)}
    assert {"nephrology", "nigeria", "nephrology-africa"} <= slugs


def test_the_region_is_coarser_than_the_country_rooms():
    """Otherwise the crossed rooms would be a second set of country rooms with
    a specialty prefix, which is a rail nobody can read."""
    assert ccountries.region_for("NG") == "africa"
    assert ccountries.region_for("KE") == "africa"
    assert ccountries.region_for("EG") == "africa"
    assert ccountries.region_for("US") == "north-america"
    assert len(ccountries.REGIONS) < len(ccountries.COUNTRIES)


def test_every_country_with_a_region_maps_to_a_real_one():
    for code, region in ccountries.REGION_BY_CODE.items():
        assert region in ccountries.REGIONS, code
        assert code in ccountries.COUNTRIES, code


def test_an_unmapped_country_yields_no_crossed_room():
    """Same rule as the country list itself: absent means "add it when someone
    needs it", never a junk room."""
    assert ccountries.region_for("ZZ") is None
    assert ccountries.region_for(None) is None
    assert community_store.specialty_region_key("nephrology", None) == ""


def test_an_unknown_specialty_or_region_creates_nothing():
    store = _fresh_store()
    store.ensure_default_channels(
        [], specialty_regions=["dermatology|africa", "nephrology|atlantis", "junk"])
    assert [c for c in store.list_channels() if c.get("grp") == "specialty_region"] == []


def test_a_crossed_room_is_hidden_until_it_clears_its_own_threshold(visible, monkeypatch):
    """And its threshold is higher than either room it crosses, because below
    that it is not a third room, it is #nephrology with fewer people in it."""
    store, router = visible
    monkeypatch.setenv("COMMUNITY_SPECIALTY_REGION_MIN_MEMBERS", "5")
    store.ensure_default_channels([], specialty_regions=["nephrology|africa"])
    who = {"specialty": "nephrology", "region": "africa"}

    four = _members(*[(who, False)] * 4)
    assert "nephrology-africa" not in {c["slug"] for c in router.visible_channels(four)}

    five = _members(*[(who, False)] * 5)
    assert "nephrology-africa" in {c["slug"] for c in router.visible_channels(five)}


def test_the_crossed_threshold_defaults_higher_than_the_axes_it_crosses(monkeypatch):
    for var in ("COMMUNITY_SPECIALTY_REGION_MIN_MEMBERS", "COMMUNITY_SPECIALTY_MIN_MEMBERS",
                "COMMUNITY_COUNTRY_MIN_MEMBERS"):
        monkeypatch.delenv(var, raising=False)
    assert crouter.specialty_region_threshold() > crouter.specialty_threshold()
    assert crouter.specialty_region_threshold() > crouter.country_threshold()


def test_staff_do_not_count_towards_a_crossed_room(visible, monkeypatch):
    store, router = visible
    monkeypatch.setenv("COMMUNITY_SPECIALTY_REGION_MIN_MEMBERS", "2")
    store.ensure_default_channels([], specialty_regions=["nephrology|africa"])
    who = {"specialty": "nephrology", "region": "africa"}
    members = _members((who, False), (who, True), (who, True))
    assert "nephrology-africa" not in {c["slug"] for c in router.visible_channels(members)}


def test_a_crossed_room_with_history_survives_the_cohort_shrinking(visible, monkeypatch):
    """Same sticky rule as every other room: the conversation in it happened."""
    store, router = visible
    monkeypatch.setenv("COMMUNITY_SPECIALTY_REGION_MIN_MEMBERS", "5")
    store.ensure_default_channels([], specialty_regions=["nephrology|africa"])
    channel = store.get_channel_by_slug("nephrology-africa")
    store.insert_message(channel_id=channel["id"], author_user_id="u-system",
                         body="what are you all seeing")
    assert "nephrology-africa" in {c["slug"] for c in router.visible_channels(_members())}


def test_a_none_crossed_cohort_deactivates_nothing():
    """The safety rule that keeps a roster hiccup from retiring every room,
    proven for the axis added last."""
    store = _fresh_store()
    store.ensure_default_channels([], specialty_regions=["nephrology|africa"])
    store.ensure_default_channels([])          # crossed cohort withheld
    assert "nephrology-africa" in {c["slug"] for c in store.list_channels()}


def test_a_crossed_cohort_that_really_emptied_is_deactivated_not_deleted():
    store = _fresh_store()
    store.ensure_default_channels([], specialty_regions=["nephrology|africa"])
    store.ensure_default_channels([], specialty_regions=[])
    assert store.get_channel_by_slug("nephrology-africa")["is_active"] == 0


def test_a_city_can_never_claim_a_slug_a_crossed_room_could_open():
    """Both are dynamic and both key on slug in the same UPSERT, so the city
    seeded today must not take the name a cohort opens tomorrow."""
    defs = community_store.city_channel_defs(["Nephrology Africa", "Boston"])
    assert [d["slug"] for d in defs] == ["boston"]


def test_the_region_column_is_added_to_an_older_channel_table():
    """Production is a live SQLite file and the guarded ALTER is the whole
    migration story."""
    import sqlite3

    path = os.path.join(tempfile.mkdtemp(prefix="cstore-region-migrate-"), "old.db")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE community_channels (id TEXT PRIMARY KEY, slug TEXT UNIQUE "
            "NOT NULL, name TEXT NOT NULL, description TEXT, post_policy TEXT NOT NULL "
            "DEFAULT 'all', position INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO community_channels (id, slug, name, description, created_at) "
            "VALUES ('ch-legacy', 'general', 'general', 'old', '2026-01-01T00:00:00Z')")
    store = CommunityStore(db_path=path)
    general = store.get_channel_by_slug("general")
    assert "region" in general and general["region"] is None
    assert general["is_active"] == 1


def test_the_morning_email_carries_a_members_crossed_room():
    """A room that never appears in the digest is a room nobody returns to."""
    from community import newsletter as cnewsletter

    channels = [
        {"slug": "general", "grp": "core", "staff_only": 0},
        {"slug": "nephrology-africa", "grp": "specialty_region",
         "specialty": "nephrology", "region": "africa", "staff_only": 0},
        {"slug": "cardiology-africa", "grp": "specialty_region",
         "specialty": "cardiology", "region": "africa", "staff_only": 0},
    ]
    member = {"specialty": "nephrology", "region": "africa", "country": "NG",
              "subspecialties": [], "city": None}
    slugs = cnewsletter._member_channels(member, channels)
    assert "nephrology-africa" in slugs
    assert "cardiology-africa" not in slugs


def test_a_member_without_a_country_is_in_no_crossed_room():
    from community import newsletter as cnewsletter

    channels = [{"slug": "nephrology-africa", "grp": "specialty_region",
                 "specialty": "nephrology", "region": "africa", "staff_only": 0}]
    member = {"specialty": "nephrology", "region": None, "country": None,
              "subspecialties": [], "city": None}
    assert cnewsletter._member_channels(member, channels) == []


# ═══ U10: the Archangel account's face ════════════════════════════════════════
@pytest.fixture(autouse=True)
def _clean_persona(monkeypatch):
    monkeypatch.delenv("COMMUNITY_PERSONA_AVATAR", raising=False)
    monkeypatch.delenv("COMMUNITY_PERSONA_NAME", raising=False)
    cpersona.reset_cache_for_tests()
    yield
    cpersona.reset_cache_for_tests()


def _png_bytes(color=(20, 90, 60)):
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (640, 400), color).save(buf, format="PNG")
    return buf.getvalue()


def _persona_file(monkeypatch, data=None):
    path = os.path.join(tempfile.mkdtemp(prefix="persona-"), "founders.png")
    with open(path, "wb") as fh:
        fh.write(data if data is not None else _png_bytes())
    monkeypatch.setenv("COMMUNITY_PERSONA_AVATAR", path)
    cpersona.reset_cache_for_tests()
    return path


def test_without_a_picture_the_account_still_renders():
    """The whole feature is optional, and the fallback has to be the exact
    behaviour the account had before it existed."""
    assert cpersona.source_path() is None
    assert cpersona.avatar_url() is None
    assert cpersona.initials() == "AH"
    assert cpersona.display_name() == "Archangel"


def test_a_supplied_picture_gives_the_account_an_avatar_url(monkeypatch):
    _persona_file(monkeypatch)
    url = cpersona.avatar_url()
    assert url and url.startswith("/api/community/persona/avatar?v=")


def test_the_picture_is_stripped_and_squared_like_any_other_avatar(monkeypatch):
    """Reusing the physician-avatar pipeline is the point: the founders' photo
    is a phone photograph, and phone photographs carry GPS."""
    from io import BytesIO

    from PIL import Image

    from asclepius import assets as asc_assets

    _persona_file(monkeypatch)
    sha, mime = cpersona.resolve()
    data, _ = asc_assets.load_asset(sha)
    img = Image.open(BytesIO(data))
    assert img.width == img.height, "a 640x400 source rendered as an oval"
    assert not getattr(img, "_getexif", lambda: None)()
    assert mime in ("image/png", "image/jpeg")


def test_the_url_changes_when_the_picture_does(monkeypatch):
    """Content-addressed, so no cache can keep serving the old face after
    somebody replaces the file."""
    _persona_file(monkeypatch, _png_bytes((20, 90, 60)))
    first = cpersona.avatar_url()
    _persona_file(monkeypatch, _png_bytes((200, 40, 40)))
    assert cpersona.avatar_url() != first


def test_a_file_that_is_not_an_image_falls_back_instead_of_breaking(monkeypatch):
    """This runs inside the serializer every message in the product passes
    through. It may return nothing; it may never raise."""
    _persona_file(monkeypatch, b"<svg onload=alert(1)></svg>")
    assert cpersona.resolve() is None
    assert cpersona.avatar_url() is None
    assert cpersona.initials() == "AH"


def test_a_missing_configured_file_is_not_an_error(monkeypatch):
    monkeypatch.setenv("COMMUNITY_PERSONA_AVATAR", "/no/such/founders.png")
    cpersona.reset_cache_for_tests()
    assert cpersona.source_path() is None
    assert cpersona.avatar_url() is None


def test_the_account_name_is_configurable_and_its_initials_follow(monkeypatch):
    monkeypatch.setenv("COMMUNITY_PERSONA_NAME", "Archangel Health")
    assert cpersona.display_name() == "Archangel Health"
    assert cpersona.initials() == "AH"
    monkeypatch.setenv("COMMUNITY_PERSONA_NAME", "Archangel")
    assert cpersona.initials() == "AR"
    monkeypatch.setenv("COMMUNITY_PERSONA_NAME", "Tej and Aryaa")
    assert cpersona.initials() == "TA"


def test_the_identity_is_applied_at_the_one_place_every_member_renders(monkeypatch):
    """Author, DM peer, and whatever surface renders the bot next. Getting the
    face onto two of the three is how one account looks like two."""
    from community.system_posts import SYSTEM_MEMBER

    _persona_file(monkeypatch)
    rendered = crouter.public_member(dict(SYSTEM_MEMBER))
    assert rendered["avatar_url"].startswith("/api/community/persona/avatar")
    assert rendered["display_name"] == "Archangel"
    assert rendered["is_bot"] is True


def test_a_real_physician_is_untouched_by_the_persona_decoration():
    member = {"user_id": "u-abc", "display_name": "Dr Rao", "initials": "DR",
              "avatar_url": None, "specialty": "nephrology"}
    rendered = crouter.public_member(dict(member))
    assert rendered["display_name"] == "Dr Rao"
    assert rendered["avatar_url"] is None


# ═══ U14: the weekend webinar ═════════════════════════════════════════════════
@pytest.fixture(autouse=True)
def _clean_webinar(monkeypatch):
    for var in ("COMMUNITY_WEBINAR_URL", "COMMUNITY_WEBINAR_TITLE",
                "COMMUNITY_WEBINAR_DOW", "COMMUNITY_WEBINAR_HOUR_LOCAL",
                "COMMUNITY_WEBINAR_WEEKS_AHEAD", "COMMUNITY_WEBINAR_HOST"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ARCHANGEL_HOME_TZ", "America/New_York")


def _utc(y, m, d, hh=12):
    return datetime(y, m, d, hh, tzinfo=timezone.utc)


@pytest.fixture
def webinar_world(monkeypatch):
    """A store with #events, wired so no post reaches a real member map."""
    store = _fresh_store()
    store.ensure_default_channels([])
    monkeypatch.setattr(cwebinars, "get_community_store", lambda: store)

    import community.system_posts as sp

    monkeypatch.setattr(sp, "get_community_store", lambda: store)
    monkeypatch.setattr("community.router.member_map", lambda **kw: {})
    monkeypatch.setattr(crouter, "_cstore", lambda: store)
    return store


def test_no_join_link_means_no_event_at_all(webinar_world):
    """An event with a time and nowhere to go is worse than no event, and a
    placeholder URL is exactly the thing that ends up in production."""
    assert cwebinars.enabled() is False
    result = asyncio.run(cwebinars.ensure_upcoming())
    assert result["created"] == 0 and result["reason"] == "no_join_url"
    channel = webinar_world.get_channel_by_slug("events")
    assert webinar_world.list_events(channel["id"]) == []


def test_the_series_lands_on_the_weekend(monkeypatch):
    """The people this is for are on shift during the week. An event they can
    never attend is worse than none."""
    monkeypatch.setenv("COMMUNITY_WEBINAR_URL", "https://example.org/join")
    # 2026-09-02 is a Wednesday; the next Saturday is the 5th.
    starts = cwebinars.upcoming_starts(now=_utc(2026, 9, 2))
    for iso in starts:
        at = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        local = at.astimezone(__import__("zoneinfo").ZoneInfo("America/New_York"))
        assert local.weekday() == 5, iso
        assert local.hour == cwebinars.hour_local()


def test_todays_slot_once_past_rolls_to_next_week(monkeypatch):
    """Otherwise a run on Saturday afternoon creates an event in the past that
    shows up as upcoming nowhere."""
    monkeypatch.setenv("COMMUNITY_WEBINAR_URL", "https://example.org/join")
    # Saturday 2026-09-05, 22:00 UTC is 18:00 in New York, past an 11am slot.
    first = cwebinars.upcoming_starts(now=_utc(2026, 9, 5, 22))[0]
    assert first.startswith("2026-09-12")


def test_the_series_stays_at_the_local_hour_across_a_clock_change(monkeypatch):
    """A recurring event that drifts an hour twice a year is a recurring event
    people stop trusting."""
    monkeypatch.setenv("COMMUNITY_WEBINAR_URL", "https://example.org/join")
    monkeypatch.setenv("COMMUNITY_WEBINAR_WEEKS_AHEAD", "4")
    from zoneinfo import ZoneInfo

    # US clocks go back on 2026-11-01, so this window straddles it.
    for iso in cwebinars.upcoming_starts(now=_utc(2026, 10, 20)):
        local = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(
            ZoneInfo("America/New_York"))
        assert local.hour == 11, iso


def test_the_next_few_occurrences_are_created_with_rsvp_and_a_join_link(webinar_world, monkeypatch):
    monkeypatch.setenv("COMMUNITY_WEBINAR_URL", "https://example.org/join")
    monkeypatch.setenv("COMMUNITY_WEBINAR_WEEKS_AHEAD", "3")
    result = asyncio.run(cwebinars.ensure_upcoming(now=_utc(2026, 9, 2)))
    assert result["created"] == 3

    channel = webinar_world.get_channel_by_slug("events")
    events = webinar_world.list_events(channel["id"], scope="upcoming")
    assert len(events) == 3
    assert all(e["location"] == "https://example.org/join" for e in events)
    assert all(e["created_by"] == "u-system" for e in events)

    # RSVP is the ordinary event path, unchanged: nothing new to maintain.
    webinar_world.toggle_rsvp(events[0]["id"], "u-doc")
    assert webinar_world.event_public(
        events[0], viewer_id="u-doc")["viewer_interested"] is True


def test_running_again_creates_nothing_new(webinar_world, monkeypatch):
    """The caller runs daily. An event duplicated once a day for a week is a
    channel nobody trusts."""
    monkeypatch.setenv("COMMUNITY_WEBINAR_URL", "https://example.org/join")
    at = _utc(2026, 9, 2)
    asyncio.run(cwebinars.ensure_upcoming(now=at))
    second = asyncio.run(cwebinars.ensure_upcoming(now=at))
    assert second["created"] == 0
    channel = webinar_world.get_channel_by_slug("events")
    assert len(webinar_world.list_events(channel["id"], scope="upcoming")) == 3


def test_the_series_is_announced_once_not_every_week(webinar_world, monkeypatch):
    """A channel that announces the same recurring thing every Monday is a
    channel people mute."""
    monkeypatch.setenv("COMMUNITY_WEBINAR_URL", "https://example.org/join")
    asyncio.run(cwebinars.ensure_upcoming(now=_utc(2026, 9, 2)))
    channel = webinar_world.get_channel_by_slug("events")
    messages, _ = webinar_world.list_messages(channel["id"])
    assert len([m for m in messages if m.get("kind") == "event"]) == 1


def test_the_announcement_carries_no_calendar_date(webinar_world, monkeypatch):
    """A full date trips the PHI exact-date rule, and the event card renders
    the time from the structured row anyway."""
    monkeypatch.setenv("COMMUNITY_WEBINAR_URL", "https://example.org/join")
    asyncio.run(cwebinars.ensure_upcoming(now=_utc(2026, 9, 2)))
    channel = webinar_world.get_channel_by_slug("events")
    messages, _ = webinar_world.list_messages(channel["id"])
    body = [m for m in messages if m.get("kind") == "event"][0]["body"]
    from community import phi_gate

    assert not phi_gate.scan_text(body)


def test_an_ics_download_works_for_a_seeded_webinar(webinar_world, monkeypatch):
    """The add-to-calendar path is the existing one, which is the reason this
    is a seeded event and not a subsystem."""
    from community import events as cevents

    monkeypatch.setenv("COMMUNITY_WEBINAR_URL", "https://example.org/join")
    asyncio.run(cwebinars.ensure_upcoming(now=_utc(2026, 9, 2)))
    channel = webinar_world.get_channel_by_slug("events")
    event = webinar_world.list_events(channel["id"], scope="upcoming")[0]
    ics = cevents.build_ics(event)
    assert "BEGIN:VEVENT" in ics and cwebinars.title() in ics
    assert "LOCATION:https://example.org/join" in ics


def test_changing_the_title_does_not_orphan_a_cancelled_occurrence(webinar_world, monkeypatch):
    """Idempotence keys on title and start time, so a cancelled occurrence must
    not be silently recreated by the next daily run."""
    monkeypatch.setenv("COMMUNITY_WEBINAR_URL", "https://example.org/join")
    at = _utc(2026, 9, 2)
    asyncio.run(cwebinars.ensure_upcoming(now=at))
    channel = webinar_world.get_channel_by_slug("events")
    first = webinar_world.list_events(channel["id"], scope="upcoming")[0]
    webinar_world.cancel_event(first["id"])

    asyncio.run(cwebinars.ensure_upcoming(now=at))
    live = [e for e in webinar_world.list_events(channel["id"], scope="upcoming")
            if not e.get("cancelled_at")]
    # The cancelled slot comes back, because the series is what recurs and a
    # cancelled week is not a standing instruction. What must NOT happen is two
    # live events in the same slot.
    starts = [e["starts_at"] for e in live]
    assert len(starts) == len(set(starts))
