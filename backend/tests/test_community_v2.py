"""Community v2 test suite.

Covers the four v2 additions on top of the PRD §9 acceptance suite
(``test_community.py``):
  * the verification gate BRIDGE (``users.verification_status == 'approved'``
    now enters, alongside the Contributors-vault flag),
  * the expanded channel taxonomy + threshold-gated specialty channels,
  * the virtual system author (welcomes + digests, PHI-gated, no email blast),
  * the #medical-ai-news digest pipeline (dedup, curation, run ledger) and its
    internal trigger.
"""

from __future__ import annotations

import asyncio
import os
import uuid

os.environ["TEAM_DB_PATH"] = os.path.join("/tmp", f"community2_audit_{uuid.uuid4().hex}.db")
os.environ.setdefault("EMAIL_DEV_MODE", "1")

from _asclepius import app, fresh_store, headers_for, make_user, uniq  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from community import digest as cdigest  # noqa: E402
from community import feeds as cfeeds  # noqa: E402
from community import store as community_store  # noqa: E402
from community.onboard import welcome_new_member  # noqa: E402
from community.system_posts import SYSTEM_USER_ID, post_system_message  # noqa: E402

client = TestClient(app)

BASE = "/api/community"


def fresh_community():
    path = os.path.join("/tmp", f"community2_{uuid.uuid4().hex}.db")
    return community_store.reset_community_store_for_tests(db_path=path)


def make_approved_physician(store, *, specialty="nephrology", years=11):
    """The NEW path: PRD-B queue approval only — NO contributor_credentials
    vault row is ever written for this user."""
    user = store.create_user(
        email=f"dr-{uniq()}@asclepius.example.com",
        password="pw-12345678",
        role="evaluator",
        specialty=specialty,
        years_experience=years,
        organization="Bridge Test Medical Group",
    )
    store.record_verification_decision(
        user["id"], status="approved", decided_by="admin@test", tier="labeler",
        tier_score=55.0,
    )
    return store.get_user_by_id(user["id"])


def make_vault_physician(store, *, specialty="nephrology", years=17):
    """The ORIGINAL path: Contributors-vault ``credentials_verified`` flag."""
    user = store.create_user(
        email=f"dr-{uniq()}@asclepius.example.com",
        password="pw-12345678",
        role="evaluator",
        specialty=specialty,
        years_experience=years,
        organization="Vault Test Nephrology",
    )
    store.upsert_contributor_credentials(
        id_hashed=user["id_hashed"], user_id=user["id"],
        organization="Vault Test Nephrology", role_title="Physician (MD)",
        credentials_verified=True,
        ship={"degree": "MD", "primary_specialty": specialty,
              "years_in_active_practice": years},
        verify={},
    )
    return user


def setup_world():
    from community.ws import hub as _hub
    _hub._sockets.clear()
    astore = fresh_store()
    cstore = fresh_community()
    admin = make_user(astore, role="admin")
    return astore, cstore, admin


def channel_slugs(user):
    r = client.get(f"{BASE}/channels", headers=headers_for(user))
    assert r.status_code == 200
    return {c["slug"]: c for c in r.json()["channels"]}


def run(coro):
    return asyncio.run(coro)


# ─── W1: the gate bridge ──────────────────────────────────────────────────────
def test_gate_approval_path_enters_without_vault_row():
    astore, _, _ = setup_world()
    doc = make_approved_physician(astore)
    r = client.get(f"{BASE}/me", headers=headers_for(doc))
    assert r.status_code == 200
    me = r.json()["member"]
    assert me["verified"] is True
    assert me["specialty"] == "nephrology"
    # In the directory, not a ghost
    r2 = client.get(f"{BASE}/members", headers=headers_for(doc))
    assert doc["id"] in [m["user_id"] for m in r2.json()["members"]]
    # Can post
    r3 = client.post(f"{BASE}/channels/general/messages",
                     json={"body": "hello from the approval path"},
                     headers=headers_for(doc))
    assert r3.status_code == 200


def test_gate_pending_rejected_and_undecided_denied():
    astore, _, _ = setup_world()
    for status in ("pending", "rejected"):
        u = astore.create_user(email=f"dr-{uniq()}@x.com", password="pw-12345678",
                               role="evaluator", specialty="cardiology")
        astore.record_verification_decision(
            u["id"], status=status, decided_by="admin@test")
        r = client.get(f"{BASE}/me", headers=headers_for(astore.get_user_by_id(u["id"])))
        assert r.status_code == 403, status
    # NULL verification_status + no vault row: still out
    u = astore.create_user(email=f"dr-{uniq()}@x.com", password="pw-12345678",
                           role="evaluator")
    assert client.get(f"{BASE}/me", headers=headers_for(u)).status_code == 403


def test_gate_vault_path_still_enters():
    astore, _, _ = setup_world()
    doc = make_vault_physician(astore)
    assert client.get(f"{BASE}/me", headers=headers_for(doc)).status_code == 200


# ─── W1b: welcome on approval ─────────────────────────────────────────────────
def test_welcome_posts_once_and_flags():
    astore, cstore, _ = setup_world()
    doc = make_approved_physician(astore, specialty="oncology", years=9)
    assert run(welcome_new_member(doc)) is True
    # Second call: idempotent
    assert run(welcome_new_member(astore.get_user_by_id(doc["id"]))) is False

    intro = cstore.get_channel_by_slug("introductions")
    msgs, _ = cstore.list_messages(intro["id"])
    welcomes = [m for m in msgs if m.get("kind") == "system_welcome"]
    assert len(welcomes) == 1
    assert welcomes[0]["author_user_id"] == SYSTEM_USER_ID
    assert "oncology" in welcomes[0]["body"]
    assert astore.get_user_by_id(doc["id"])["slack_joined"] == 1
    # Exactly one mention notification, for the new member
    pending = cstore.unsent_notifications()
    assert [(n["user_id"], n["kind"]) for n in pending] == [(doc["id"], "mention")]


def test_approve_endpoint_fires_welcome_and_survives_welcome_failure(monkeypatch):
    astore, cstore, admin = setup_world()
    u = astore.create_user(email=f"dr-{uniq()}@x.com", password="pw-12345678",
                           role="evaluator", specialty="nephrology")
    astore.set_verification_status(u["id"], "pending")
    r = client.post(f"/api/asclepius/verify/queue/{u['id']}/approve",
                    json={"tier": "labeler"}, headers=headers_for(admin))
    assert r.status_code == 200
    intro = cstore.get_channel_by_slug("introductions")
    msgs, _ = cstore.list_messages(intro["id"])
    assert any(m.get("kind") == "system_welcome" for m in msgs)

    # Welcome failure never fails the approval
    import community.onboard as onboard_mod

    async def boom(user):
        raise RuntimeError("welcome exploded")
    monkeypatch.setattr(onboard_mod, "welcome_new_member", boom)
    u2 = astore.create_user(email=f"dr-{uniq()}@x.com", password="pw-12345678",
                            role="evaluator", specialty="cardiology")
    astore.set_verification_status(u2["id"], "pending")
    r2 = client.post(f"/api/asclepius/verify/queue/{u2['id']}/approve",
                     json={"tier": "reviewer"}, headers=headers_for(admin))
    assert r2.status_code == 200
    assert astore.get_user_by_id(u2["id"])["verification_status"] == "approved"


# ─── W2: channel taxonomy + threshold gating ──────────────────────────────────
def test_core_channels_present_with_groups():
    astore, _, _ = setup_world()
    doc = make_vault_physician(astore)
    chans = channel_slugs(doc)
    for slug in ("general", "introductions", "task-announcements", "medical-ai-news",
                 "research-and-opportunities", "future-of-medical-ai", "questions-help"):
        assert slug in chans, slug
        assert chans[slug]["group"] == "core"
    assert chans["medical-ai-news"]["post_policy"] == "admin"
    assert chans["future-of-medical-ai"]["post_policy"] == "all"


def test_specialty_channel_hidden_below_threshold():
    astore, _, _ = setup_world()
    doc = make_vault_physician(astore)  # 1 nephrologist < 3
    chans = channel_slugs(doc)
    assert "nephrology" not in chans
    r = client.get(f"{BASE}/channels/nephrology/messages", headers=headers_for(doc))
    assert r.status_code == 404
    r2 = client.post(f"{BASE}/channels/nephrology/messages", json={"body": "hi"},
                     headers=headers_for(doc))
    assert r2.status_code == 404


def test_specialty_channel_appears_at_threshold():
    astore, _, _ = setup_world()
    doc = make_vault_physician(astore)
    make_approved_physician(astore)          # 2 (bridge member counts too)
    make_vault_physician(astore)             # 3
    chans = channel_slugs(doc)
    assert "nephrology" in chans
    assert chans["nephrology"]["group"] == "specialty"
    assert chans["nephrology"]["specialty"] == "nephrology"
    # Other enabled specialties with zero members stay hidden
    assert "cardiology" not in chans


def test_specialty_channel_sticky_once_it_has_history():
    astore, cstore, admin = setup_world()
    docs = [make_vault_physician(astore) for _ in range(3)]
    r = client.post(f"{BASE}/channels/nephrology/messages",
                    json={"body": "first case discussion"}, headers=headers_for(docs[0]))
    assert r.status_code == 200
    # Deactivate one member → count drops to 2 < 3, but history keeps it visible
    r2 = client.post(f"{BASE}/admin/members/{docs[2]['id']}/deactivate",
                     headers=headers_for(admin))
    assert r2.status_code == 200
    assert "nephrology" in channel_slugs(docs[0])


def test_removed_channel_deactivated_not_deleted():
    astore, cstore, _ = setup_world()
    doc = make_vault_physician(astore)
    # Simulate a channel that used to be seeded and later left the config
    with cstore._conn() as conn:
        conn.execute(
            "INSERT INTO community_channels (id, slug, name, description, post_policy,"
            " position, grp, is_active, created_at) VALUES ('ch-old', 'old-lounge',"
            " 'old-lounge', 'legacy', 'all', 99, 'core', 1, '2026-01-01T00:00:00Z')")
        conn.execute(
            "INSERT INTO community_messages (channel_id, author_user_id, body,"
            " mentions_json, attachments_json, created_at) VALUES ('ch-old', ?,"
            " 'history to preserve', '[]', '[]', '2026-01-01T00:00:00Z')",
            (doc["id"],))
    cstore.ensure_default_channels()
    assert "old-lounge" not in channel_slugs(doc)
    row = next(c for c in cstore.list_channels(include_inactive=True)
               if c["slug"] == "old-lounge")
    assert row["is_active"] == 0
    with cstore._conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM community_messages "
                         "WHERE channel_id = 'ch-old'").fetchone()[0]
    assert n == 1


def test_unread_and_search_scoped_to_visible_channels():
    astore, cstore, admin = setup_world()
    doc = make_vault_physician(astore)
    # A DEACTIVATED channel with history: hidden from the rail, its unread
    # never counts, and search cannot reach its messages. (A merely
    # below-threshold specialty channel that gains a message goes VISIBLE by
    # the sticky rule instead — history is never hidden.)
    with cstore._conn() as conn:
        conn.execute(
            "INSERT INTO community_channels (id, slug, name, description, post_policy,"
            " position, grp, is_active, created_at) VALUES ('ch-dead', 'dead-channel',"
            " 'dead-channel', 'gone', 'all', 98, 'core', 0, '2026-01-01T00:00:00Z')")
    cstore.insert_message(channel_id="ch-dead", author_user_id=admin["id"],
                          body="hidden channel chatter")
    chans = channel_slugs(doc)
    assert "dead-channel" not in chans  # not listed → no unread entry either
    r = client.get(f"{BASE}/search", params={"q": "hidden channel chatter"},
                   headers=headers_for(doc))
    assert r.status_code == 200
    assert r.json()["results"] == []
    # And the below-threshold empty specialty channel contributes nothing
    assert "cardiology" not in chans


# ─── W3: system posts ─────────────────────────────────────────────────────────
def test_system_post_renders_bot_author_and_kind():
    astore, _, _ = setup_world()
    doc = make_vault_physician(astore)
    posted = run(post_system_message(
        channel_slug="medical-ai-news",
        body="**Digest** — [a story](https://example.com/x)", kind="digest_news"))
    assert posted is not None
    assert posted["kind"] == "digest_news"
    assert posted["author"]["is_bot"] is True
    assert posted["author"]["display_name"] == "Archangel"
    r = client.get(f"{BASE}/channels/medical-ai-news/messages", headers=headers_for(doc))
    got = [m for m in r.json()["messages"] if m["id"] == posted["id"]]
    assert got and got[0]["author"]["is_bot"] is True


def test_system_post_phi_blocked_is_skipped():
    astore, cstore, _ = setup_world()
    make_vault_physician(astore)
    posted = run(post_system_message(
        channel_slug="medical-ai-news",
        body="Patient SSN 123-45-6789 appeared in a headline", kind="digest_news"))
    assert posted is None
    ch = cstore.get_channel_by_slug("medical-ai-news")
    msgs, _ = cstore.list_messages(ch["id"])
    assert msgs == []
    assert cstore.block_count(SYSTEM_USER_ID) == 1


def test_system_post_real_digest_format_passes_phi_gate():
    """Review-finding regression: a REAL compose-format digest carries PMID /
    DOI URLs whose long digit runs pattern-match MRN/account rules. URLs are
    masked before the scan; the post must go through."""
    astore, cstore, _ = setup_world()
    make_vault_physician(astore)
    body = (
        "**Papers of the Week**\n"
        "**Research**\n"
        "- [LLMs read nephrology notes](https://pubmed.ncbi.nlm.nih.gov/39234567/) — "
        "models scored below specialists\n"
        "- [Preprint on triage agents](https://www.medrxiv.org/content/10.64898/2026.07.04.26357301) — "
        "prospective single-center study"
    )
    posted = run(post_system_message(channel_slug="medical-ai-news", body=body,
                                     kind="digest_papers"))
    assert posted is not None, "digit-bearing article URLs must not PHI-block a digest"
    # ...but PHI in VISIBLE text (outside any URL) still blocks
    blocked = run(post_system_message(
        channel_slug="medical-ai-news",
        body="- [story](https://example.com/a) — patient MRN: 12345678 disclosed",
        kind="digest_news"))
    assert blocked is None


def test_failed_run_items_are_retried_not_stranded(monkeypatch):
    """Review-finding regression: a failed run leaves items 'new'; the NEXT
    run must pick them up even though the fetch dedups them, not record a
    hollow ok run."""
    astore, cstore, _ = setup_world()
    make_vault_physician(astore)
    import ai.llm_client as llm

    async def fake_rss():
        return _fake_items(3)
    monkeypatch.setattr(cfeeds, "fetch_rss", fake_rss)

    async def garbage(**kw):
        return "not json", {}
    monkeypatch.setattr(llm, "call_llm", garbage)
    monkeypatch.setattr(llm, "first_text", lambda resp: resp)
    r1 = run(cdigest.run_digest("news"))
    assert r1["ok"] is False

    _mock_llm(monkeypatch)  # LLM recovers; fetch returns the SAME (deduped) urls
    r2 = run(cdigest.run_digest("news"))
    assert r2["ok"] is True and r2["posted"] == 3, "stranded items must be posted"
    ch = cstore.get_channel_by_slug("medical-ai-news")
    msgs, _ = cstore.list_messages(ch["id"])
    assert len(msgs) == 1 and msgs[0]["kind"] == "digest_news"


def test_system_author_excluded_from_admin_flags():
    astore, cstore, admin = setup_world()
    make_vault_physician(astore)
    for _ in range(3):
        run(post_system_message(channel_slug="medical-ai-news",
                                body="SSN 123-45-6789", kind="digest_news"))
    r = client.get(f"{BASE}/admin/flags", headers=headers_for(admin))
    assert r.status_code == 200
    assert SYSTEM_USER_ID not in [f["user_id"] for f in r.json()["flagged"]]


def test_system_digest_post_no_email_blast():
    astore, cstore, _ = setup_world()
    make_vault_physician(astore)
    make_vault_physician(astore)
    run(post_system_message(channel_slug="medical-ai-news",
                            body="**Digest** with no mentions", kind="digest_news"))
    assert cstore.unsent_notifications() == []


def test_system_author_not_a_member_and_not_dmable():
    astore, _, _ = setup_world()
    doc = make_vault_physician(astore)
    r = client.get(f"{BASE}/members", headers=headers_for(doc))
    assert SYSTEM_USER_ID not in [m["user_id"] for m in r.json()["members"]]
    r2 = client.post(f"{BASE}/dms", json={"user_id": SYSTEM_USER_ID},
                     headers=headers_for(doc))
    assert r2.status_code == 404


# ─── W4: digest pipeline ──────────────────────────────────────────────────────
def _fake_items(n=3, source="rss:test"):
    return [cfeeds._item(source,
                         url=f"https://example.com/story-{i}?utm_source=x",
                         title=f"AI model cleared for clinical use {i}",
                         abstract="An artificial intelligence system for medicine.")
            for i in range(n)]


def test_url_and_title_normalization_dedup():
    _, cstore, _ = setup_world()
    a = cfeeds._item("rss:a", url="https://www.Example.com/Story?utm_source=tw",
                     title="AI beats doctors, again!")
    b = cfeeds._item("rss:b", url="http://example.com/story",
                     title="AI beats doctors — again")
    fresh = cstore.upsert_content_items([a])
    assert len(fresh) == 1
    assert cstore.upsert_content_items([a]) == []       # same url_norm
    assert cstore.upsert_content_items([b]) == []       # same title_norm, other outlet


def test_keyword_filter():
    items = [
        cfeeds._item("rss:x", url="https://e.com/1", title="Hospital merger closes"),
        cfeeds._item("rss:x", url="https://e.com/2",
                     title="Large language model reads EHR notes"),
    ]
    kept = cdigest._keyword_filter(items, require=True)
    assert [i["url_norm"] for i in kept] == ["e.com/2"]
    assert len(cdigest._keyword_filter(items, require=False)) == 2


def _mock_llm(monkeypatch, keep_ids=None):
    """Route the two curation passes through canned responses."""
    import ai.llm_client as llm

    async def fake_call_llm(*, role, system, messages, **kw):
        assert role == "community_digest"
        import json as _json
        payload = _json.loads(messages[0]["content"])
        if "digest_kind" in payload:  # compose pass
            lines = ["**Medical AI Digest**", "**Stories**"]
            lines += [f"- [{it['title']}]({it['url']}) — {it['one_liner']}"
                      for it in payload["items"]]
            return "\n".join(lines), {}
        ids = [it["id"] for it in payload["items"]]
        chosen = keep_ids if keep_ids is not None else ids
        return _json.dumps({"items": [
            {"id": i, "keep": i in chosen, "relevance": 0.9, "one_liner": "what happened"}
            for i in ids]}), {}

    monkeypatch.setattr(llm, "call_llm", fake_call_llm)
    monkeypatch.setattr(llm, "first_text", lambda resp: resp)


def test_run_digest_posts_and_records(monkeypatch):
    astore, cstore, _ = setup_world()
    make_vault_physician(astore)
    _mock_llm(monkeypatch)

    async def fake_rss():
        return _fake_items(3)
    monkeypatch.setattr(cfeeds, "fetch_rss", fake_rss)

    result = run(cdigest.run_digest("news"))
    assert result["ok"] is True and result["posted"] == 3
    ch = cstore.get_channel_by_slug("medical-ai-news")
    msgs, _ = cstore.list_messages(ch["id"])
    assert len(msgs) == 1 and msgs[0]["kind"] == "digest_news"
    with cstore._conn() as conn:
        st = [r[0] for r in conn.execute(
            "SELECT status FROM community_content_items").fetchall()]
        runrow = conn.execute(
            "SELECT ok, items_posted FROM community_digest_runs ORDER BY id DESC").fetchone()
    assert st == ["posted"] * 3
    assert (runrow[0], runrow[1]) == (1, 3)
    # Re-run: everything deduped → ok run, NO second post
    result2 = run(cdigest.run_digest("news"))
    assert result2["ok"] is True and result2["posted"] == 0
    msgs2, _ = cstore.list_messages(ch["id"])
    assert len(msgs2) == 1


def test_run_digest_nothing_kept_posts_nothing(monkeypatch):
    astore, cstore, _ = setup_world()
    make_vault_physician(astore)
    _mock_llm(monkeypatch, keep_ids=[])

    async def fake_rss():
        return _fake_items(2)
    monkeypatch.setattr(cfeeds, "fetch_rss", fake_rss)
    result = run(cdigest.run_digest("news"))
    assert result["ok"] is True and result["posted"] == 0
    ch = cstore.get_channel_by_slug("medical-ai-news")
    assert cstore.list_messages(ch["id"])[0] == []


def test_papers_run_survives_one_source_raising(monkeypatch):
    astore, cstore, _ = setup_world()
    make_vault_physician(astore)
    _mock_llm(monkeypatch)

    async def boom(days=7):
        raise RuntimeError("pubmed down")

    async def ok_arxiv(days=7):
        return _fake_items(2, source="arxiv")

    async def empty(days=7):
        return []
    monkeypatch.setattr(cfeeds, "fetch_pubmed", boom)
    monkeypatch.setattr(cfeeds, "fetch_arxiv", ok_arxiv)
    monkeypatch.setattr(cfeeds, "fetch_medrxiv", empty)
    result = run(cdigest.run_digest("papers"))
    assert result["ok"] is True and result["posted"] == 2


def test_llm_parse_failure_fails_run_and_counts(monkeypatch):
    astore, cstore, _ = setup_world()
    make_vault_physician(astore)
    import ai.llm_client as llm

    async def garbage(**kw):
        return "not json at all", {}
    monkeypatch.setattr(llm, "call_llm", garbage)
    monkeypatch.setattr(llm, "first_text", lambda resp: resp)

    # fresh urls each round, one shared counter, so dedup can't shortcut to an ok run
    batch = {"n": 0}

    async def fake_rss_unique():
        batch["n"] += 1
        return [cfeeds._item("rss:test", url=f"https://e.com/f{batch['n']}-{i}",
                             title=f"AI story batch {batch['n']} item {i}")
                for i in range(2)]
    monkeypatch.setattr(cfeeds, "fetch_rss", fake_rss_unique)

    for _ in range(3):
        result = run(cdigest.run_digest("news"))
        assert result["ok"] is False
    assert cstore.consecutive_digest_failures("news") >= 3
    ch = cstore.get_channel_by_slug("medical-ai-news")
    assert cstore.list_messages(ch["id"])[0] == []


def test_internal_digest_trigger_auth(monkeypatch):
    setup_world()
    monkeypatch.setenv("INTERNAL_TOOL_SECRET", "test-secret")
    r = client.post("/internal/community/run-digest?kind=news")
    assert r.status_code == 401
    r2 = client.post("/internal/community/run-digest?kind=bogus",
                     headers={"Authorization": "Bearer test-secret"})
    assert r2.status_code == 400
