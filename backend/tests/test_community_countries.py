"""Country channels.

A nephrologist in Riyadh and a nephrologist in Boston share a specialty and
almost nothing else about practising it: the guidelines, the referral
pathways, the conferences worth flying to, and what medical AI even means in a
hospital are all local. #nephrology is the right room for the medicine;
#saudi-arabia is the right room for the rest.

Same visibility rule as specialty channels, for the same reason: a room with
one person in it reads as an empty building, so a country appears once a few
colleagues are actually there, and stays once it has history.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from community import countries
from community.store import CommunityStore


def _fresh_store() -> CommunityStore:
    path = os.path.join(tempfile.mkdtemp(prefix="cstore-country-"), "community.db")
    store = CommunityStore(db_path=path)
    return store


def _members(*specs):
    """(country, is_staff) tuples -> a member map shaped like router.member_map."""
    out = {}
    for i, (country, is_staff) in enumerate(specs):
        out[f"u-{i}"] = {
            "user_id": f"u-{i}", "display_name": f"Dr {i}", "specialty": "nephrology",
            "country": country, "is_staff": is_staff,
        }
    return out


# ─── Seeding ─────────────────────────────────────────────────────────────────
def test_a_channel_is_created_for_each_country_that_has_members():
    store = _fresh_store()
    store.ensure_default_channels(["SA", "IN"])
    slugs = {c["slug"] for c in store.list_channels() if c.get("grp") == "country"}
    assert slugs == {"saudi-arabia", "india"}


def test_countries_with_nobody_in_them_get_no_channel():
    """A rail listing thirty countries, twenty-eight of them empty, is a
    directory rather than a community."""
    store = _fresh_store()
    store.ensure_default_channels(["SA"])
    slugs = {c["slug"] for c in store.list_channels() if c.get("grp") == "country"}
    assert slugs == {"saudi-arabia"}
    assert "australia" not in slugs


def test_seeding_without_a_roster_does_not_retire_existing_country_channels():
    """``None`` means "I did not look them up", not "there are none". A caller
    without the roster to hand must not silently close every country room."""
    store = _fresh_store()
    store.ensure_default_channels(["SA", "IN"])
    store.ensure_default_channels()          # e.g. the store's own __init__
    live = {c["slug"] for c in store.list_channels() if c.get("grp") == "country"}
    assert live == {"saudi-arabia", "india"}


def test_an_unknown_country_code_is_ignored_rather_than_creating_a_junk_channel():
    store = _fresh_store()
    store.ensure_default_channels(["ZZ", "SA"])
    slugs = {c["slug"] for c in store.list_channels() if c.get("grp") == "country"}
    assert slugs == {"saudi-arabia"}


def test_the_country_code_is_stored_on_the_channel():
    store = _fresh_store()
    store.ensure_default_channels(["IN"])
    channel = store.get_channel_by_slug("india")
    assert channel["country"] == "IN"


# ─── Visibility ──────────────────────────────────────────────────────────────
@pytest.fixture
def visible(monkeypatch):
    """router.visible_channels bound to a throwaway store."""
    store = _fresh_store()
    from community import router as crouter

    monkeypatch.setattr(crouter, "_cstore", lambda: store)
    return store, crouter


def test_a_country_channel_is_hidden_until_enough_colleagues_are_there(visible, monkeypatch):
    store, crouter = visible
    monkeypatch.setenv("COMMUNITY_COUNTRY_MIN_MEMBERS", "3")
    store.ensure_default_channels(["SA"])

    two = _members(("SA", False), ("SA", False))
    slugs = {c["slug"] for c in crouter.visible_channels(two)}
    assert "saudi-arabia" not in slugs

    three = _members(("SA", False), ("SA", False), ("SA", False))
    slugs = {c["slug"] for c in crouter.visible_channels(three)}
    assert "saudi-arabia" in slugs


def test_staff_do_not_count_towards_a_country(visible, monkeypatch):
    """Otherwise the first doctor from a country walks into a room containing
    themselves and two members of the Archangel team."""
    store, crouter = visible
    monkeypatch.setenv("COMMUNITY_COUNTRY_MIN_MEMBERS", "3")
    store.ensure_default_channels(["SA"])
    members = _members(("SA", False), ("SA", True), ("SA", True))
    assert "saudi-arabia" not in {c["slug"] for c in crouter.visible_channels(members)}


def test_a_country_channel_with_history_stays_visible(visible, monkeypatch):
    """A room does not vanish because somebody was deactivated: the
    conversation in it happened."""
    store, crouter = visible
    monkeypatch.setenv("COMMUNITY_COUNTRY_MIN_MEMBERS", "3")
    store.ensure_default_channels(["SA"])
    channel = store.get_channel_by_slug("saudi-arabia")
    store.insert_message(channel_id=channel["id"], author_user_id="u-system",
                         body="morning brief")

    nobody = _members()
    assert "saudi-arabia" in {c["slug"] for c in crouter.visible_channels(nobody)}


def test_core_channels_are_never_gated(visible):
    store, crouter = visible
    store.ensure_default_channels(["SA"])
    slugs = {c["slug"] for c in crouter.visible_channels(_members())}
    assert {"general", "introductions", "events"} <= slugs


# ─── Config ──────────────────────────────────────────────────────────────────
def test_every_country_carries_a_timezone_so_the_morning_run_has_a_morning():
    for code, country in countries.COUNTRIES.items():
        assert country.timezone, code
        assert country.slug and country.name


def test_an_unconfigured_country_falls_back_to_the_house_timezone():
    assert countries.timezone_for("ZZ") == countries.DEFAULT_TIMEZONE
    assert countries.timezone_for(None) == countries.DEFAULT_TIMEZONE


def test_timezones_are_real_zones():
    from zoneinfo import ZoneInfo

    for country in countries.COUNTRIES.values():
        ZoneInfo(country.timezone)   # raises if the name is wrong
