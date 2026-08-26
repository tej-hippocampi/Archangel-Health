"""The generated-content purge: a deployed community can be made empty.

Policy: the community starts empty. Bot-authored posts (news digests,
welcomes) and demo-seeded authors are hard-deleted; channels and human posts
survive. These tests drive the store method directly with an explicit
``valid_user_ids`` set, which is exactly how the internal endpoint and the
shell script call it.
"""

from __future__ import annotations

import os
import tempfile

from community.store import CommunityStore


def _fresh_store() -> CommunityStore:
    path = os.path.join(tempfile.mkdtemp(prefix="cstore-purge-"), "community.db")
    store = CommunityStore(db_path=path)
    store.ensure_default_channels()
    return store


def _general(store: CommunityStore) -> str:
    return store.get_channel_by_slug("general")["id"]


def test_bot_and_orphan_authors_are_purged_humans_survive():
    store = _fresh_store()
    ch = _general(store)
    keep = store.insert_message(channel_id=ch, author_user_id="u-human", body="real post")
    store.insert_message(channel_id=ch, author_user_id="u-system", body="bot news digest")
    store.insert_message(channel_id=ch, author_user_id="u-demo-doc", body="seeded chatter")

    counts = store.purge_generated_content(valid_user_ids=["u-human"])

    assert counts["messages"] == 2
    remaining, _more = store.list_messages(ch)
    assert [m["id"] for m in remaining] == [keep["id"]]


def test_replies_under_a_purged_post_go_with_it():
    """A human answer to a deleted bot post is context-free noise."""
    store = _fresh_store()
    ch = _general(store)
    bot = store.insert_message(channel_id=ch, author_user_id="u-system", body="digest")
    store.insert_message(channel_id=ch, author_user_id="u-human", body="interesting!",
                         parent_message_id=bot["id"])
    human = store.insert_message(channel_id=ch, author_user_id="u-human", body="own thread")

    counts = store.purge_generated_content(valid_user_ids=["u-human"])

    assert counts["messages"] == 2
    remaining, _more = store.list_messages(ch)
    assert [m["id"] for m in remaining] == [human["id"]]


def test_reactions_and_pins_on_purged_posts_are_removed():
    store = _fresh_store()
    ch = _general(store)
    bot = store.insert_message(channel_id=ch, author_user_id="u-system", body="digest")
    store.toggle_reaction(bot["id"], "u-human", "thumbsup")
    with store._conn() as conn:  # pin the bot post the way the router does
        conn.execute(
            "INSERT INTO community_pins (channel_id, message_id, pinned_by, created_at) "
            "VALUES (?, ?, ?, '2026-01-01T00:00:00Z')",
            (ch, bot["id"], "u-human"),
        )

    counts = store.purge_generated_content(valid_user_ids=["u-human"])

    assert counts["messages"] == 1
    assert counts["reactions"] == 1
    assert counts["pins"] == 1


def test_channels_survive_and_purge_is_idempotent():
    store = _fresh_store()
    ch = _general(store)
    store.insert_message(channel_id=ch, author_user_id="u-system", body="digest")
    first = store.purge_generated_content(valid_user_ids=[])
    second = store.purge_generated_content(valid_user_ids=[])
    assert first["messages"] == 1 and second["messages"] == 0
    assert store.get_channel_by_slug("general") is not None


def test_without_a_roster_only_the_bot_is_purged():
    """valid_user_ids=None means 'no roster available': never guess that a
    human author is synthetic."""
    store = _fresh_store()
    ch = _general(store)
    store.insert_message(channel_id=ch, author_user_id="u-system", body="digest")
    unknown = store.insert_message(channel_id=ch, author_user_id="u-unknown", body="post")
    counts = store.purge_generated_content()
    assert counts["messages"] == 1
    assert [m["id"] for m in store.list_messages(ch)[0]] == [unknown["id"]]
