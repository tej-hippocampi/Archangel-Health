"""Asclepius Community test suite (Community PRD §9 acceptance).

Covers: the §1 gate (verified contributors only), the fixed §3 channel set and
the admin-only announcements policy, messaging + threads + reactions +
mentions + unread state + search (§4), auto-populated Tier-A-only profiles
(§2), the §7 PHI block-at-send gate (server-side, before persistence, category
+ span only, nothing stored, audit records category not content), attachments
(§7.4), moderation + audit (§7.5), and real-time WebSocket delivery (§4).
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid

import pytest

# Isolated audit DB for this module (the audit chain writes to TEAM_DB_PATH).
os.environ["TEAM_DB_PATH"] = os.path.join("/tmp", f"community_audit_{uuid.uuid4().hex}.db")
os.environ.setdefault("EMAIL_DEV_MODE", "1")

from _asclepius import app, fresh_store, headers_for, make_user, token_for, uniq  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from community import store as community_store  # noqa: E402
from community import phi_gate  # noqa: E402
from community import notify as cnotify  # noqa: E402

client = TestClient(app)

BASE = "/api/community"


# ─── Fixtures ─────────────────────────────────────────────────────────────────
def fresh_community():
    path = os.path.join("/tmp", f"community_{uuid.uuid4().hex}.db")
    return community_store.reset_community_store_for_tests(db_path=path)


def make_verified_physician(store, *, specialty="nephrology", years=17, org="Riverside Nephrology Associates"):
    user = store.create_user(
        email=f"dr-{uniq()}@asclepius.example.com",
        password="pw-12345678",
        role="evaluator",
        specialty=specialty,
        years_experience=years,
        organization=org,
    )
    store.upsert_contributor_credentials(
        id_hashed=user["id_hashed"],
        user_id=user["id"],
        organization=org,
        role_title="Physician (MD)",
        credentials_verified=True,
        ship={
            "degree": "MD",
            "board_certifications": "ABIM — Nephrology (active)",
            "primary_specialty": specialty,
            "subspecialties": ["dialysis", "transplant"],
            "years_in_active_practice": years,
            "fellowship_trained": True,
            "credentials_verified": True,
        },
        verify={
            "full_legal_name": "Jane A. Doe, MD",
            "npi": "1234567893",
            "medical_license_number": "A-104872",
            "practice_address": "1200 Riverside Dr, Sacramento, CA",
        },
    )
    return user


def setup_world():
    """Fresh asclepius + community stores; returns (astore, cstore, physician, admin).

    Also clears the process-global WS hub: sockets left behind by a previous
    test (client side closed, server reader not yet reaped) otherwise get
    reaped mid-broadcast during THIS test, injecting presence noise and close
    latency into order-sensitive receives (observed flake)."""
    from community.ws import hub as _hub
    _hub._sockets.clear()
    astore = fresh_store()
    cstore = fresh_community()
    doc = make_verified_physician(astore)
    admin = make_user(astore, role="admin")
    return astore, cstore, doc, admin


def post_msg(user, slug, body, **kw):
    return client.post(f"{BASE}/channels/{slug}/messages", json={"body": body, **kw},
                       headers=headers_for(user))


def audit_events(action=None):
    with sqlite3.connect(os.environ["TEAM_DB_PATH"]) as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT * FROM audit_events ORDER BY id ASC").fetchall()
        except sqlite3.OperationalError:
            return []
    out = [dict(r) for r in rows]
    if action:
        out = [r for r in out if r["action"] == action]
    return out


# ─── §1 Gate ──────────────────────────────────────────────────────────────────
def test_gate_unauthenticated_401():
    setup_world()
    r = client.get(f"{BASE}/channels")
    assert r.status_code == 401


def test_gate_verified_contributor_enters():
    _, _, doc, _ = setup_world()
    r = client.get(f"{BASE}/me", headers=headers_for(doc))
    assert r.status_code == 200
    data = r.json()
    assert data["member"]["verified"] is True
    assert data["member"]["specialty"] == "nephrology"
    assert "patient-identifiable" in data["notice"]


def test_gate_refuses_buyer_partner_and_unverified():
    astore, _, _, _ = setup_world()
    buyer = make_user(astore, role="buyer")
    partner = make_user(astore, role="data_partner")
    unverified = make_user(astore, role="evaluator")  # no credential profile
    for u in (buyer, partner, unverified):
        r = client.get(f"{BASE}/channels", headers=headers_for(u))
        assert r.status_code == 403, u["role"]
        assert r.json()["detail"] == "Community access is for verified contributors."


def test_gate_admin_and_qa_enter():
    astore, _, _, admin = setup_world()
    qa = make_user(astore, role="qa_reviewer")
    assert client.get(f"{BASE}/me", headers=headers_for(admin)).status_code == 200
    assert client.get(f"{BASE}/me", headers=headers_for(qa)).status_code == 200


def test_gate_enforced_on_every_endpoint():
    astore, cstore, doc, _ = setup_world()
    buyer = make_user(astore, role="buyer")
    hdrs = headers_for(buyer)
    paths = [
        ("get", f"{BASE}/me"), ("get", f"{BASE}/channels"),
        ("get", f"{BASE}/channels/general/messages"),
        ("get", f"{BASE}/members"), ("get", f"{BASE}/search?q=x"),
        ("get", f"{BASE}/messages/1/thread"),
    ]
    for method, path in paths:
        r = getattr(client, method)(path, headers=hdrs)
        assert r.status_code == 403, path
    r = post_msg(buyer, "general", "hello")
    assert r.status_code == 403


# ─── §3 Channels ──────────────────────────────────────────────────────────────
def test_three_fixed_channels():
    # Community v2 expanded the fixed set to seven core channels (specialty
    # channels exist too but are threshold-gated, so a 1-member world shows
    # core only — that gating has its own suite in test_community_v2.py).
    _, _, doc, _ = setup_world()
    r = client.get(f"{BASE}/channels", headers=headers_for(doc))
    assert r.status_code == 200
    slugs = [c["slug"] for c in r.json()["channels"]]
    assert slugs == ["general", "introductions", "task-announcements", "events",
                     "medical-ai-news", "research-and-opportunities",
                     "future-of-medical-ai", "questions-help"]
    policies = {c["slug"]: c["post_policy"] for c in r.json()["channels"]}
    assert policies["task-announcements"] == "admin"
    assert policies["medical-ai-news"] == "admin"
    assert policies["events"] == "admin"


def test_announcements_admin_only_but_thread_replies_open():
    _, _, doc, admin = setup_world()
    # physician cannot post top-level
    r = post_msg(doc, "task-announcements", "can I post here?")
    assert r.status_code == 403
    # admin can
    r = client.post(f"{BASE}/channels/task-announcements/messages",
                    json={"body": "New nephrology batch drops Friday."},
                    headers=headers_for(admin))
    assert r.status_code == 200
    root_id = r.json()["id"]
    # physician CAN reply in the thread
    r = post_msg(doc, "task-announcements", "how many cases?", parent_message_id=root_id)
    assert r.status_code == 200
    assert r.json()["parent_message_id"] == root_id


# ─── §4 Messaging core ────────────────────────────────────────────────────────
def test_post_and_history_roundtrip():
    _, _, doc, _ = setup_world()
    body = "creatinine 1.9, up from 1.4 — thoughts on holding lisinopril?"
    r = post_msg(doc, "general", body)
    assert r.status_code == 200
    msg = r.json()
    assert msg["body"] == body
    assert msg["author"]["display_name"]
    assert msg["author"]["specialty"] == "nephrology"
    assert msg["author"]["years_in_practice"] == 17

    r = client.get(f"{BASE}/channels/general/messages", headers=headers_for(doc))
    assert r.status_code == 200
    bodies = [m["body"] for m in r.json()["messages"]]
    assert body in bodies


def test_history_pagination_upward():
    _, _, doc, _ = setup_world()
    ids = []
    for i in range(7):
        r = post_msg(doc, "general", f"note number {chr(97 + i)}")
        ids.append(r.json()["id"])
    r = client.get(f"{BASE}/channels/general/messages?limit=3", headers=headers_for(doc))
    page1 = r.json()
    assert [m["id"] for m in page1["messages"]] == ids[-3:]
    assert page1["has_more"] is True
    oldest = page1["messages"][0]["id"]
    r = client.get(f"{BASE}/channels/general/messages?limit=3&before={oldest}",
                   headers=headers_for(doc))
    page2 = r.json()
    assert [m["id"] for m in page2["messages"]] == ids[-6:-3]


def test_edit_own_message_only():
    astore, _, doc, _ = setup_world()
    other = make_verified_physician(astore, specialty="cardiology", years=9)
    mid = post_msg(doc, "general", "initial text").json()["id"]
    # someone else cannot edit
    r = client.patch(f"{BASE}/messages/{mid}", json={"body": "hijacked"},
                     headers=headers_for(other))
    assert r.status_code == 403
    # author can
    r = client.patch(f"{BASE}/messages/{mid}", json={"body": "revised text"},
                     headers=headers_for(doc))
    assert r.status_code == 200
    assert r.json()["body"] == "revised text"
    assert r.json()["edited_at"]


def test_delete_own_and_admin_moderation():
    astore, cstore, doc, admin = setup_world()
    other = make_verified_physician(astore)
    mid = post_msg(doc, "general", "to be removed").json()["id"]
    # a peer cannot delete it
    r = client.delete(f"{BASE}/messages/{mid}", headers=headers_for(other))
    assert r.status_code == 403
    # admin can (moderation) — soft delete, body cleared
    r = client.delete(f"{BASE}/messages/{mid}", headers=headers_for(admin))
    assert r.status_code == 200
    row = cstore.get_message(mid)
    assert row["deleted"] and row["body"] == ""
    # gone from history
    r = client.get(f"{BASE}/channels/general/messages", headers=headers_for(doc))
    assert mid not in [m["id"] for m in r.json()["messages"]]


def test_threads_reply_count_and_view():
    astore, _, doc, _ = setup_world()
    other = make_verified_physician(astore, specialty="cardiology")
    root = post_msg(doc, "questions-help", "how do I flag a bad rubric?").json()
    post_msg(other, "questions-help", "use the flag button on the axis",
             parent_message_id=root["id"])
    post_msg(doc, "questions-help", "found it, thanks", parent_message_id=root["id"])
    r = client.get(f"{BASE}/channels/questions-help/messages", headers=headers_for(doc))
    m = next(m for m in r.json()["messages"] if m["id"] == root["id"])
    assert m["reply_count"] == 2
    r = client.get(f"{BASE}/messages/{root['id']}/thread", headers=headers_for(doc))
    t = r.json()
    assert t["root"]["id"] == root["id"]
    assert len(t["replies"]) == 2
    # replies never appear as top-level rows
    top_ids = [m["id"] for m in client.get(
        f"{BASE}/channels/questions-help/messages", headers=headers_for(doc)).json()["messages"]]
    assert all(rep["id"] not in top_ids for rep in t["replies"])


def test_reactions_toggle_and_group():
    astore, _, doc, _ = setup_world()
    other = make_verified_physician(astore)
    mid = post_msg(doc, "general", "shipped the batch").json()["id"]
    r = client.post(f"{BASE}/messages/{mid}/reactions", json={"emoji": "👍"},
                    headers=headers_for(other))
    assert r.status_code == 200 and r.json()["added"] is True
    r = client.post(f"{BASE}/messages/{mid}/reactions", json={"emoji": "👍"},
                    headers=headers_for(doc))
    groups = r.json()["reactions"]
    assert groups[0]["emoji"] == "👍" and groups[0]["count"] == 2
    # toggle off
    r = client.post(f"{BASE}/messages/{mid}/reactions", json={"emoji": "👍"},
                    headers=headers_for(doc))
    assert r.json()["added"] is False
    assert r.json()["reactions"][0]["count"] == 1
    # junk emoji rejected
    r = client.post(f"{BASE}/messages/{mid}/reactions", json={"emoji": "<script>"},
                    headers=headers_for(doc))
    assert r.status_code == 400


def test_mentions_validated_and_queued():
    astore, cstore, doc, _ = setup_world()
    other = make_verified_physician(astore, specialty="cardiology")
    r = post_msg(doc, "general", "paging our cardiologist",
                 mention_user_ids=[other["id"], "u-nonsense"])
    assert r.status_code == 200
    assert r.json()["mentions"] == [other["id"]]  # unknown id silently dropped
    pending = cstore.unsent_notifications()
    assert [(n["user_id"], n["kind"]) for n in pending] == [(other["id"], "mention")]


def test_unread_counts_and_read_cursor():
    astore, _, doc, _ = setup_world()
    other = make_verified_physician(astore)
    post_msg(other, "general", "first")
    last = post_msg(other, "general", "second").json()["id"]
    r = client.get(f"{BASE}/badge", headers=headers_for(doc))
    assert r.json()["eligible"] is True
    assert r.json()["unread"] == 2
    # own messages never count as unread for the author
    r = client.get(f"{BASE}/badge", headers=headers_for(other))
    assert r.json()["unread"] == 0
    # mark read
    r = client.post(f"{BASE}/channels/general/read", json={"last_read_message_id": last},
                    headers=headers_for(doc))
    assert r.status_code == 200
    assert client.get(f"{BASE}/badge", headers=headers_for(doc)).json()["unread"] == 0


def test_badge_soft_for_non_members():
    astore, _, _, _ = setup_world()
    buyer = make_user(astore, role="buyer")
    r = client.get(f"{BASE}/badge", headers=headers_for(buyer))
    assert r.status_code == 200
    assert r.json() == {"eligible": False, "unread": 0, "mentions": 0}


def test_search_scoped_and_excludes_deleted():
    _, _, doc, admin = setup_world()
    keep = post_msg(doc, "general", "tacrolimus trough discussion").json()["id"]
    gone = post_msg(doc, "general", "tacrolimus dosing error thread").json()["id"]
    client.delete(f"{BASE}/messages/{gone}", headers=headers_for(admin))
    r = client.get(f"{BASE}/search?q=tacrolimus", headers=headers_for(doc))
    ids = [m["id"] for m in r.json()["results"]]
    assert keep in ids and gone not in ids
    # LIKE wildcards are escaped — '%' finds nothing rather than everything
    r = client.get(f"{BASE}/search?q=%", headers=headers_for(doc))
    assert r.json()["results"] == []


# ─── §2 Profiles ──────────────────────────────────────────────────────────────
def test_member_list_profile_fields_and_no_tier_b():
    """The §2 profile, across the two payloads that now carry it.

    The member LIST is downloaded by every member on every page open, so it was
    narrowed to what the rail and @mention completion render; the fields that
    make up a profile moved to ``/members/{user_id}``, one request for the one
    colleague somebody opened. Same gate, same scrub, same source, so this
    asserts the whole profile is still served and still Tier-A-only, and that
    the list is no longer the thing serving it."""
    astore, _, doc, admin = setup_world()
    r = client.get(f"{BASE}/members", headers=headers_for(doc))
    assert r.status_code == 200
    members = r.json()["members"]
    me = next(m for m in members if m["user_id"] == doc["id"])
    assert me["specialty"] == "nephrology"
    assert me["verified"] is True
    assert me["specialty_accent"] == "green"
    # The heavy per-row fields are gone from the LIST specifically.
    for absent in ("blurb", "institution", "years_in_practice"):
        assert absent not in me, absent

    p = client.get(f"{BASE}/members/{doc['id']}", headers=headers_for(doc))
    assert p.status_code == 200
    profile = p.json()["member"]
    assert profile["years_in_practice"] == 17
    assert profile["institution"] == "Riverside Nephrology Associates"
    assert "Board-certified" in profile["blurb"] and "17 yrs" in profile["blurb"]
    # Tier B must never appear anywhere in EITHER payload (PRD §2, §10):
    # neither the vault keys nor the vault VALUES. ("NPI-verified." in the blurb
    # is Tier A language, hence the key-shaped '"npi"' check.)
    blob = json.dumps(r.json()).lower() + json.dumps(p.json()).lower()
    for forbidden in ('"npi"', "full_legal_name", "medical_license_number",
                      "practice_address", "1234567893", "a-104872", "jane a. doe",
                      "1200 riverside dr"):
        assert forbidden not in blob, forbidden
    # email is not served either
    assert "email" not in me
    assert "email" not in profile


def test_member_specialty_filter():
    astore, _, doc, _ = setup_world()
    make_verified_physician(astore, specialty="cardiology", years=9)
    r = client.get(f"{BASE}/members?specialty=cardiology", headers=headers_for(doc))
    assert all(m["specialty"] == "cardiology" for m in r.json()["members"])
    assert r.json()["count"] >= 1


def test_message_author_carries_tier_a_profile_only():
    _, _, doc, _ = setup_world()
    msg = post_msg(doc, "general", "quick poll on dialysis starts").json()
    author = msg["author"]
    assert author["specialty"] == "nephrology"
    assert author["blurb"].startswith("Board-certified")
    blob = json.dumps(msg).lower()
    assert '"npi"' not in blob and "practice_address" not in blob and "1234567893" not in blob


# ─── §7 PHI blocking at send ──────────────────────────────────────────────────
PHI_CASES = [
    ("MRN 84921734 worsening AKI", "mrn"),
    ("patient name is John Smith, transplant listed", "patient_name"),
    ("DOB 4/12/1958, needs access", "dob"),
    ("dialyzed on 12/03/2024 at the unit", "exact_date"),
    ("family cell is 916-555-0142", "phone"),
    ("email me at fam@example.com about him", "email"),
    ("lives at 1200 Riverside Dr if transport needed", "address"),
    ("SSN 123-45-6789 on the form", "ssn"),
]


@pytest.mark.parametrize("body,category", PHI_CASES)
def test_phi_blocked_with_category_and_span(body, category):
    _, cstore, doc, _ = setup_world()
    r = post_msg(doc, "general", body)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "phi_detected"
    assert category in detail["categories"]
    assert detail["findings"][0]["span"][1] > detail["findings"][0]["span"][0]
    # §7.3: the identifier itself is NEVER echoed back
    payload_blob = json.dumps(detail)
    for token in ("84921734", "John Smith", "4/12/1958", "12/03/2024",
                  "916-555-0142", "fam@example.com", "Riverside", "123-45-6789"):
        assert token not in payload_blob
    # §7.3: nothing was stored — not the message, not any quarantine copy
    with sqlite3.connect(cstore.db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM community_messages").fetchone()[0]
    assert n == 0


CLINICAL_ALLOWED = [
    "creatinine 1.9 from 1.4, holding lisinopril, rechecking in a week",
    "started tacrolimus 2 mg BID, trough 8.2 — adjust?",
    "male in his 60s, CKD 4, refractory hyperkalemia despite binders",
    "BP 120/80, K 6.1 — gave insulin/D50 and kayexalate",
    "KDIGO 2024 would suggest waiting on the biopsy",
]


@pytest.mark.parametrize("body", CLINICAL_ALLOWED)
def test_clinical_content_posts_normally(body):
    _, _, doc, _ = setup_world()
    r = post_msg(doc, "general", body)
    assert r.status_code == 200, r.json()


def test_phi_gate_runs_on_edit_path():
    _, cstore, doc, _ = setup_world()
    mid = post_msg(doc, "general", "clean initial message").json()["id"]
    r = client.patch(f"{BASE}/messages/{mid}",
                     json={"body": "adding MRN: 99887766 for context"},
                     headers=headers_for(doc))
    assert r.status_code == 422
    # original body untouched
    assert cstore.get_message(mid)["body"] == "clean initial message"


def test_phi_block_audited_category_only_and_repeat_flag():
    _, cstore, doc, _ = setup_world()
    for _i in range(3):
        post_msg(doc, "general", "patient name is John Smith")
    events = [e for e in audit_events("community.phi_block")
              if e["actor_id"] == doc["id"]]
    assert len(events) == 3
    for e in events:
        detail = json.loads(e["detail_json"])
        assert "patient_name" in detail["categories"]
        assert "John" not in e["detail_json"] and "Smith" not in e["detail_json"]
    # threshold (default 3) reached → repeat flag in the last event + admin view
    assert json.loads(events[-1]["detail_json"]).get("repeat_flag") is True
    assert cstore.block_count(doc["id"]) == 3


def test_admin_flags_endpoint():
    _, _, doc, admin = setup_world()
    for _i in range(3):
        post_msg(doc, "general", "MRN: 55443322")
    r = client.get(f"{BASE}/admin/flags", headers=headers_for(admin))
    assert r.status_code == 200
    flagged = r.json()["flagged"]
    assert any(f["user_id"] == doc["id"] and f["block_count"] == 3 for f in flagged)
    # not exposed to non-admins
    assert client.get(f"{BASE}/admin/flags", headers=headers_for(doc)).status_code == 403


# ─── §7: a link is not an identifier ──────────────────────────────────────────
LINKEDIN_POST = "https://www.linkedin.com/feed/update/urn:li:share:7501080186667581440/"


def test_a_linkedin_share_link_posts_clean():
    """WHY: the bare long-digit-run pattern matched the 19-digit share id inside
    a LinkedIn URL exactly as if it were an account number, so a physician
    linking their own talk was told they had posted PHI.

    The BOT path never had this problem -- ``system_posts._mask_urls`` blanks
    URLs before scanning -- which made the member path the stricter of the two
    for no reason anyone chose. That asymmetry is the actual defect.
    """
    _, _, doc, _ = setup_world()
    r = post_msg(doc, "general", "worth a read, from the AI-in-nephrology panel: "
                 + LINKEDIN_POST)
    assert r.status_code == 200, r.json()
    assert not phi_gate.scan_text(LINKEDIN_POST)


@pytest.mark.parametrize("url", [
    "https://pubmed.ncbi.nlm.nih.gov/38472913/",
    "https://doi.org/10.1053/j.ajkd.2024.01.012",
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=1204812",
    "https://x.com/someone/status/1849302847283746112",
])
def test_ordinary_links_are_not_identifiers(url):
    """Every one of these carries a digit run, a slash-separated number pair or
    both. None of them is a patient identifier, and a gate that blocks a DOI is
    a gate physicians route around."""
    _, _, doc, _ = setup_world()
    assert post_msg(doc, "general", "see " + url).status_code == 200


def test_an_ssn_inside_a_url_still_blocks():
    """The exemption is for SHAPE-ONLY categories, and an SSN is not one.

    A link is not a laundering channel: a URL with a social security number in
    its path has still put a social security number in the room, and the same
    goes for a keyword-anchored date of birth.
    """
    _, cstore, doc, _ = setup_world()
    r = post_msg(doc, "general", "the export is at https://files.example.com/p/123-45-6789/chart")
    assert r.status_code == 422
    assert "ssn" in r.json()["detail"]["categories"]
    with sqlite3.connect(cstore.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM community_messages").fetchone()[0] == 0

    r = post_msg(doc, "general", "https://example.com/intake?DOB=4/12/1958")
    assert r.status_code == 422
    assert "dob" in r.json()["detail"]["categories"]


def test_the_link_exemption_does_not_cover_the_prose_around_the_link():
    """Containment, not proximity. A finding that merely sits NEAR a URL is
    ordinary text and blocks exactly as it always did."""
    _, _, doc, _ = setup_world()
    r = post_msg(doc, "general", "see " + LINKEDIN_POST + " -- MRN 84921734 for the case")
    assert r.status_code == 422
    assert "mrn" in r.json()["detail"]["categories"]


def test_the_shared_scanner_second_pass_also_ignores_links():
    """Defense in depth must not reintroduce the bug one layer down.

    The shared platform scanner reports KINDS with no spans, so anything it
    finds becomes a WHOLE-TEXT finding. Handing it the raw string would re-block
    the share id with no span to highlight, which is strictly worse than the
    behaviour it replaced.
    """
    assert not phi_gate.scan_text("posted here: " + LINKEDIN_POST)
    assert phi_gate.mask_urls("a " + LINKEDIN_POST + " b") == (
        "a " + " " * len(LINKEDIN_POST) + " b"), "the mask must preserve offsets"


def test_strip_spans_removes_exactly_the_findings():
    text = "K 6.1 today. MRN: 99887766. Recheck tomorrow."
    findings = phi_gate.scan_text(text)
    assert findings
    cleaned = phi_gate.strip_spans(text, findings)
    assert "99887766" not in cleaned
    assert "K 6.1" in cleaned and "Recheck tomorrow" in cleaned
    assert not phi_gate.scan_text(cleaned)


# ─── Message writes audited (§7.5, §9.8) ──────────────────────────────────────
def test_message_writes_are_audited_without_content():
    _, _, doc, _ = setup_world()
    body = "unique audit probe " + uniq()
    mid = post_msg(doc, "general", body).json()["id"]
    client.patch(f"{BASE}/messages/{mid}", json={"body": "edited " + uniq()},
                 headers=headers_for(doc))
    client.delete(f"{BASE}/messages/{mid}", headers=headers_for(doc))
    for action in ("community.message_create", "community.message_edit",
                   "community.message_delete"):
        evs = [e for e in audit_events(action) if str(mid) == e["resource"]]
        assert evs, action
        assert body not in evs[0]["detail_json"]


# ─── Moderation: community-scoped deactivation (§7.5) ─────────────────────────
def test_admin_deactivates_member_community_only():
    astore, _, doc, admin = setup_world()
    r = client.post(f"{BASE}/admin/members/{doc['id']}/deactivate",
                    headers=headers_for(admin))
    assert r.status_code == 200
    # locked out of the community…
    assert client.get(f"{BASE}/me", headers=headers_for(doc)).status_code == 403
    # …but the evaluation portal account is untouched (PRD §0: additive)
    assert astore.get_user_by_id(doc["id"])["active"] == 1
    # reactivate restores access
    client.post(f"{BASE}/admin/members/{doc['id']}/reactivate", headers=headers_for(admin))
    assert client.get(f"{BASE}/me", headers=headers_for(doc)).status_code == 200


def test_non_admin_cannot_moderate():
    astore, _, doc, _ = setup_world()
    other = make_verified_physician(astore)
    r = client.post(f"{BASE}/admin/members/{other['id']}/deactivate",
                    headers=headers_for(doc))
    assert r.status_code == 403


# ─── Notifications (§4) ───────────────────────────────────────────────────────
def test_announcement_queues_digest_for_all_members(capsys):
    astore, cstore, doc, admin = setup_world()
    other = make_verified_physician(astore, specialty="cardiology")
    client.post(f"{BASE}/channels/task-announcements/messages",
                json={"body": "New cardiology batch open — claim in the portal."},
                headers=headers_for(admin))
    pending = cstore.unsent_notifications()
    recipients = {n["user_id"] for n in pending}
    assert doc["id"] in recipients and other["id"] in recipients
    assert admin["id"] not in recipients  # never the author
    # flush sends one digest per user via the dev-mode transport
    import asyncio
    from community.router import resolve_member_for_notify
    sent = asyncio.run(
        cnotify.flush_pending(cstore, resolve_member=resolve_member_for_notify))
    assert sent == 2
    assert cstore.unsent_notifications() == []
    out = capsys.readouterr().out
    assert "New activity in your Archangel Health community" in out


# ─── New posts and pins produce email (kinds ``post`` and ``pin``) ────────────
def _kinds_for(cstore, kind):
    return {n["user_id"] for n in cstore.unsent_notifications() if n["kind"] == kind}


def test_a_bot_post_queues_a_post_notification_for_every_member():
    """The defect: every bot post was written with ``announce=False``, so the
    whole daily content run, which is the thing that fills six channels every
    morning, produced exactly zero notification rows and nobody was told the
    community had moved."""
    import asyncio

    from community.system_posts import post_system_message

    astore, cstore, doc, admin = setup_world()
    other = make_verified_physician(astore, specialty="cardiology")

    msg = asyncio.run(post_system_message(
        channel_slug="events",
        body="**Coming up**\n\nThree you could actually get to.",
        kind="morning_events", announce=True))

    assert msg is not None
    recipients = _kinds_for(cstore, "post")
    assert {doc["id"], other["id"], admin["id"]} <= recipients


def test_an_ordinary_human_message_queues_no_post_notification():
    """The scoping decision, held by test. A row per member per message in a
    live channel would be a "While you were away" email every five minutes
    describing a conversation the reader is having, which is the one outcome
    that gets the whole mechanism switched off. A member who wants a specific
    message mailed has @mention."""
    astore, cstore, doc, _ = setup_world()
    make_verified_physician(astore, specialty="cardiology")

    post_msg(doc, "general", "Anyone else seeing this on the new rubric?")

    assert _kinds_for(cstore, "post") == set()


def test_pinning_a_message_tells_the_channel(monkeypatch):
    """Pinning was a WebSocket event and nothing else, so it reached whoever
    happened to be connected in that second and nobody else. A pin is the one
    act that means "everybody read this one"."""
    astore, cstore, doc, admin = setup_world()
    other = make_verified_physician(astore, specialty="cardiology")
    posted = post_msg(doc, "general", "The relay handoff note goes above the question.")
    assert posted.status_code == 200
    mid = posted.json()["id"]

    r = client.post(f"{BASE}/messages/{mid}/pin", headers=headers_for(admin))
    assert r.status_code == 200, r.text

    recipients = _kinds_for(cstore, "pin")
    assert other["id"] in recipients
    # Never the person who pressed pin, and never the author: both already know.
    assert admin["id"] not in recipients
    assert doc["id"] not in recipients


def test_the_pin_digest_line_does_not_credit_the_pin_to_the_author(monkeypatch):
    """The queue row points at the pinned MESSAGE, so the author is the only
    person the flush can name. "Dr Doe pinned a message" about Dr Doe's own
    post would be a plain untruth, so the line is written without an actor."""
    import asyncio

    astore, cstore, doc, admin = setup_world()
    make_verified_physician(astore, specialty="cardiology")
    mid = post_msg(doc, "general", "Worth reading before Friday.").json()["id"]
    client.post(f"{BASE}/messages/{mid}/pin", headers=headers_for(admin))

    bodies = []

    async def _capture(to, subject, body):
        bodies.append(body)
        return True

    monkeypatch.setattr("email_utils.send_html_email", _capture)
    from community.router import resolve_member_for_notify

    asyncio.run(cnotify.flush_pending(cstore, resolve_member=resolve_member_for_notify))

    assert bodies, "the pin should have produced a digest email"
    assert "A message was pinned in #general" in bodies[0]


def test_a_member_who_turned_post_emails_off_still_gets_their_mentions(monkeypatch):
    """The reason there are three switches rather than one: "stop telling me
    about the morning brief" and "stop telling me a colleague asked me
    something" are different requests, and the unsubscribe token, which was the
    only control that existed, answers both at once."""
    import asyncio

    from community.system_posts import post_system_message

    astore, cstore, doc, admin = setup_world()
    cstore.set_email_stream(doc["id"], "post", False)

    asyncio.run(post_system_message(
        channel_slug="events", body="**Coming up**\n\nThree events.",
        kind="morning_events", announce=True))
    client.post(f"{BASE}/channels/general/messages",
                json={"body": "A question for you.", "mention_user_ids": [doc["id"]]},
                headers=headers_for(admin))

    leads = []

    async def _capture(to, subject, body):
        leads.append((to, body))
        return True

    monkeypatch.setattr("email_utils.send_html_email", _capture)
    from community.router import resolve_member_for_notify

    asyncio.run(cnotify.flush_pending(cstore, resolve_member=resolve_member_for_notify))

    to_doc = [body for to, body in leads if to == doc["email"]]
    assert to_doc, "the mention must still arrive"
    assert "mentioned you" in to_doc[0]
    assert "posted in #events" not in to_doc[0]
    # Handled, not left pending: a dropped row that stays queued is re-read by
    # every later flush forever.
    assert not [n for n in cstore.unsent_notifications() if n["user_id"] == doc["id"]]


def test_pin_emails_and_post_emails_are_separate_switches(monkeypatch):
    import asyncio

    astore, cstore, doc, admin = setup_world()
    cstore.set_email_stream(doc["id"], "pin", False)
    mid = post_msg(admin, "general", "Read this one.").json()["id"]
    client.post(f"{BASE}/messages/{mid}/pin", headers=headers_for(admin))

    sent = []

    async def _capture(to, subject, body):
        sent.append(to)
        return True

    monkeypatch.setattr("email_utils.send_html_email", _capture)
    from community.router import resolve_member_for_notify

    asyncio.run(cnotify.flush_pending(cstore, resolve_member=resolve_member_for_notify))
    assert doc["email"] not in sent


# ─── Attachments (§7.4) ───────────────────────────────────────────────────────
def _png_bytes(text=None):
    """A small in-memory PNG; optionally with burned-in text rendered."""
    import io
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (240, 60), "white")
    if text:
        ImageDraw.Draw(im).text((6, 20), text, fill="black")
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def test_attachment_text_file_phi_blocked_and_not_stored():
    _, cstore, doc, _ = setup_world()
    r = client.post(f"{BASE}/attachments",
                    files={"file": ("note.txt", b"MRN: 99887766 follow up", "text/plain")},
                    headers=headers_for(doc))
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "phi_detected"
    assert "99887766" not in json.dumps(detail)
    with sqlite3.connect(cstore.db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM community_attachments").fetchone()[0]
    assert n == 0
    assert cstore.block_count(doc["id"]) == 1


def test_attachment_clean_text_roundtrip_on_message():
    _, _, doc, _ = setup_world()
    r = client.post(f"{BASE}/attachments",
                    files={"file": ("labs.txt", b"Cr 1.9 K 5.8 trending", "text/plain")},
                    headers=headers_for(doc))
    assert r.status_code == 200
    asset_id = r.json()["asset_id"]
    r = post_msg(doc, "general", "labs attached", attachment_ids=[asset_id])
    assert r.status_code == 200
    assert r.json()["attachments"][0]["asset_id"] == asset_id
    r = client.get(f"{BASE}/attachments/{asset_id}", headers=headers_for(doc))
    assert r.status_code == 200
    assert r.content == b"Cr 1.9 K 5.8 trending"
    # unauthenticated download refused
    assert client.get(f"{BASE}/attachments/{asset_id}").status_code == 401


def test_attachment_image_metadata_stripped_and_ocr_scanned(monkeypatch):
    _, _, doc, _ = setup_world()
    from community import attachments as catt
    # Deterministic OCR: pretend the screenshot contains an MRN.
    monkeypatch.setattr(catt, "_ocr_image_text", lambda b: ("MRN 99887766", None))
    r = client.post(f"{BASE}/attachments",
                    files={"file": ("ehr.png", _png_bytes(), "image/png")},
                    headers=headers_for(doc))
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "phi_detected"
    # Clean OCR text → accepted, and the stored bytes are a re-encoded PNG
    monkeypatch.setattr(catt, "_ocr_image_text", lambda b: ("ECG strip lead II", None))
    r = client.post(f"{BASE}/attachments",
                    files={"file": ("ecg.png", _png_bytes(), "image/png")},
                    headers=headers_for(doc))
    assert r.status_code == 200
    assert r.json()["mime"] == "image/png"


def test_attachment_unsupported_type_rejected():
    _, _, doc, _ = setup_world()
    r = client.post(f"{BASE}/attachments",
                    files={"file": ("x.exe", b"MZ\x90\x00", "application/x-msdownload")},
                    headers=headers_for(doc))
    assert r.status_code == 415


def test_attachment_of_deleted_message_is_gone():
    _, _, doc, admin = setup_world()
    asset_id = client.post(
        f"{BASE}/attachments",
        files={"file": ("labs.txt", b"Cr 2.4 rising", "text/plain")},
        headers=headers_for(doc)).json()["asset_id"]
    mid = post_msg(doc, "general", "see labs", attachment_ids=[asset_id]).json()["id"]
    client.delete(f"{BASE}/messages/{mid}", headers=headers_for(admin))
    assert client.get(f"{BASE}/attachments/{asset_id}",
                      headers=headers_for(doc)).status_code == 404


# ─── WebSocket (§4, §9.6) ─────────────────────────────────────────────────────
def ws_barrier(ws):
    """Block until this socket's server-side handler has finished the work it
    was already given. Returns once the ``pong`` for a fresh ``ping`` arrives.

    This exists for a limitation in Starlette's ``TestClient``, not for the
    app. Every ``websocket_connect`` and every HTTP request gets its OWN
    blocking portal, so a socket's ASGI ``send`` and the test thread's
    ``receive`` on ANOTHER socket sit in two different event loops. Delivering
    an event to socket A from socket B's loop ends in
    ``anyio`` waking A's stream with ``asyncio.Event.set()``, which schedules
    A's wakeup with ``loop.call_soon`` from a foreign thread; ``call_soon``
    does not write the selector's self-pipe, so if A's loop is already parked
    in ``select(timeout=None)`` waiting for that very event, the wakeup is lost
    and ``receive_json`` blocks forever.

    The race is entirely in the harness: a real deployment runs one uvicorn
    event loop, and the hub itself is provably correct here (instrumenting
    ``Hub._send_one`` shows the event delivered, ``ok=True``, while the client
    thread stays parked). The barrier removes the race by ordering the
    cross-socket send BEFORE the receive: the handler is single-tasked, so a
    ``pong`` proves every earlier broadcast it owed has already completed, and
    the event is therefore sitting in the target socket's buffer where
    ``receive_json`` picks it up without ever parking.
    """
    ws.send_text(json.dumps({"type": "ping"}))
    while True:
        msg = ws.receive_json()
        if msg.get("type") == "pong":
            return


def test_ws_rejects_without_valid_gate():
    astore, _, _, _ = setup_world()
    buyer = make_user(astore, role="buyer")
    with client.websocket_connect(f"{BASE}/ws?token=garbage") as ws:
        # server closes 4401 — receive surfaces the disconnect
        with pytest.raises(Exception):
            ws.receive_json()
    with client.websocket_connect(f"{BASE}/ws?token={token_for(buyer)}") as ws:
        with pytest.raises(Exception):
            ws.receive_json()


def test_ws_delivers_messages_typing_and_presence():
    astore, _, doc, _ = setup_world()
    other = make_verified_physician(astore)
    with client.websocket_connect(f"{BASE}/ws?token={token_for(doc)}") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert doc["id"] in hello["online"]
        # REST post → WS delivery
        post_msg(other, "general", "realtime check-in")
        evt = ws.receive_json()
        assert evt["type"] == "message.created"
        assert evt["message"]["body"] == "realtime check-in"
        assert evt["message"]["channel"] == "general"
        # typing from a second socket relays to the first
        with client.websocket_connect(f"{BASE}/ws?token={token_for(other)}") as ws2:
            ws2.receive_json()  # hello
            # Barrier before every cross-socket receive (see ws_barrier): the
            # presence and typing events are produced by ws2's event loop and
            # consumed on ws1, and only the barrier makes that ordering real.
            ws_barrier(ws2)
            presence = ws.receive_json()
            assert presence["type"] == "presence"
            assert other["id"] in presence["online"]
            ws2.send_text(json.dumps({"type": "typing", "channel": "general"}))
            ws_barrier(ws2)
            typing = ws.receive_json()
            assert typing["type"] == "typing" and typing["user_id"] == other["id"]


def test_ws_never_broadcasts_blocked_phi():
    _, _, doc, _ = setup_world()
    with client.websocket_connect(f"{BASE}/ws?token={token_for(doc)}") as ws:
        ws.receive_json()  # hello
        post_msg(doc, "general", "patient name is John Smith")  # blocked
        post_msg(doc, "general", "clean follow-up message")     # delivered
        evt = ws.receive_json()
        assert evt["type"] == "message.created"
        assert evt["message"]["body"] == "clean follow-up message"


# ─── Audit-fix regressions ────────────────────────────────────────────────────
AUDIT_ALLOWED = [
    # identifier keywords followed by ordinary words must NOT block (the
    # pattern tail now requires a digit)
    "the payout policy changed last week",
    "never post MRN values here, use the case id",
    "check the medical record first before grading",
    "my account balance question for the team",
    # dose titration is not a calendar date
    "titrated 25-50-100 over three weeks",
]


@pytest.mark.parametrize("body", AUDIT_ALLOWED)
def test_audit_false_positives_now_pass(body):
    _, _, doc, _ = setup_world()
    r = post_msg(doc, "general", body)
    assert r.status_code == 200, r.json()


AUDIT_BLOCKED = [
    ("dialyzed 25/12/2024 at the unit", "exact_date"),   # D/M/Y form
    ("medicare id 1EG4-TE5-MK73 on file", "account_number"),  # MBI format
    ("chart # 55x21 attached", "mrn"),
]


@pytest.mark.parametrize("body,category", AUDIT_BLOCKED)
def test_audit_identifiers_still_blocked(body, category):
    _, _, doc, _ = setup_world()
    r = post_msg(doc, "general", body)
    assert r.status_code == 422
    assert category in r.json()["detail"]["categories"]


def test_has_more_survives_tombstone_underfill():
    """A deleted, reply-less message inside a page must not end pagination
    (audit finding: has_more was computed after the tombstone filter)."""
    _, _, doc, admin = setup_world()
    ids = [post_msg(doc, "general", f"note {chr(97 + i)}").json()["id"] for i in range(5)]
    client.delete(f"{BASE}/messages/{ids[-1]}", headers=headers_for(admin))
    r = client.get(f"{BASE}/channels/general/messages?limit=3", headers=headers_for(doc))
    page = r.json()
    # the deleted newest message vanishes and the page under-fills (2 of 3) —
    # but has_more still reports the older history as reachable
    assert page["has_more"] is True
    assert [m["id"] for m in page["messages"]] == ids[2:4]
    # and the next page up delivers it
    r = client.get(f"{BASE}/channels/general/messages?limit=3&before={ids[2]}",
                   headers=headers_for(doc))
    assert [m["id"] for m in r.json()["messages"]] == ids[0:2]


def test_after_poll_pages_forward_without_gap():
    """``after`` polls deliver oldest-first with has_more, so a burst larger
    than one page can be drained in order (audit finding: newest-N left a
    silent hole)."""
    _, _, doc, _ = setup_world()
    ids = [post_msg(doc, "general", f"burst {chr(97 + i)}").json()["id"] for i in range(5)]
    r = client.get(f"{BASE}/channels/general/messages?after=0&limit=2",
                   headers=headers_for(doc))
    page = r.json()
    assert [m["id"] for m in page["messages"]] == ids[:2]
    assert page["has_more"] is True
    r = client.get(f"{BASE}/channels/general/messages?after={ids[1]}&limit=2",
                   headers=headers_for(doc))
    assert [m["id"] for m in r.json()["messages"]] == ids[2:4]


def test_unread_counts_top_level_only():
    """Thread replies must not strand the unread badge (audit finding: the
    read cursor can only ever advance to top-level ids)."""
    astore, _, doc, _ = setup_world()
    other = make_verified_physician(astore)
    root = post_msg(other, "general", "root message").json()
    client.post(f"{BASE}/channels/general/read",
                json={"last_read_message_id": root["id"]}, headers=headers_for(doc))
    assert client.get(f"{BASE}/badge", headers=headers_for(doc)).json()["unread"] == 0
    # a thread reply arrives — the badge must remain clearable/zero
    post_msg(other, "general", "a reply", parent_message_id=root["id"])
    assert client.get(f"{BASE}/badge", headers=headers_for(doc)).json()["unread"] == 0


def test_thread_reply_does_not_advance_read_cursor():
    """Replying in an old thread must not mark unseen top-level messages read
    (audit finding)."""
    astore, _, doc, _ = setup_world()
    other = make_verified_physician(astore)
    old_root = post_msg(other, "general", "an old question").json()
    client.post(f"{BASE}/channels/general/read",
                json={"last_read_message_id": old_root["id"]}, headers=headers_for(doc))
    post_msg(other, "general", "unseen one")
    post_msg(other, "general", "unseen two")
    assert client.get(f"{BASE}/badge", headers=headers_for(doc)).json()["unread"] == 2
    post_msg(doc, "general", "my reply", parent_message_id=old_root["id"])
    assert client.get(f"{BASE}/badge", headers=headers_for(doc)).json()["unread"] == 2


def test_ws_ticket_flow_single_use():
    _, _, doc, _ = setup_world()
    ticket = client.post(f"{BASE}/ws-ticket", headers=headers_for(doc)).json()["ticket"]
    with client.websocket_connect(f"{BASE}/ws?ticket={ticket}") as ws:
        assert ws.receive_json()["type"] == "hello"
    # single use: replaying the ticket is refused
    with client.websocket_connect(f"{BASE}/ws?ticket={ticket}") as ws:
        with pytest.raises(Exception):
            ws.receive_json()
    # non-members cannot mint tickets
    astore = fresh_store()
    buyer = make_user(astore, role="buyer")
    assert client.post(f"{BASE}/ws-ticket", headers=headers_for(buyer)).status_code == 403


def test_unposted_attachment_visible_to_uploader_only():
    astore, _, doc, _ = setup_world()
    other = make_verified_physician(astore)
    asset_id = client.post(
        f"{BASE}/attachments",
        files={"file": ("labs.txt", b"Cr 1.9 trending", "text/plain")},
        headers=headers_for(doc)).json()["asset_id"]
    assert client.get(f"{BASE}/attachments/{asset_id}",
                      headers=headers_for(doc)).status_code == 200
    assert client.get(f"{BASE}/attachments/{asset_id}",
                      headers=headers_for(other)).status_code == 404


def test_attachment_utf16_text_is_scanned_and_canonicalized():
    """A UTF-16 text file must be decoded before scanning (audit finding: the
    scan saw mojibake while the original bytes were stored intact)."""
    _, _, doc, _ = setup_world()
    dirty = "note SSN 123-45-6789 follow up".encode("utf-16")
    r = client.post(f"{BASE}/attachments",
                    files={"file": ("note.txt", dirty, "text/plain")},
                    headers=headers_for(doc))
    assert r.status_code == 422
    assert "ssn" in r.json()["detail"]["categories"]
    # clean UTF-16 uploads store the canonical UTF-8 of the scanned text
    clean = "Cr 1.9 K 5.8 trending".encode("utf-16")
    r = client.post(f"{BASE}/attachments",
                    files={"file": ("labs.txt", clean, "text/plain")},
                    headers=headers_for(doc))
    assert r.status_code == 200
    got = client.get(f"{BASE}/attachments/{r.json()['asset_id']}",
                     headers=headers_for(doc))
    assert got.content.decode("utf-8") == "Cr 1.9 K 5.8 trending"


# A minimal valid one-page PDF with NO text layer (a stand-in for a scan/fax).
_TEXTLESS_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


def test_textless_pdf_fails_closed_without_ocr(monkeypatch):
    """An image-only/scanned PDF must never bypass the PHI gate (audit
    finding: pdfminer returns '' for scans — it does not raise)."""
    _, _, doc, _ = setup_world()
    from community import attachments as catt
    monkeypatch.setattr(catt, "_ocr_pdf_text", lambda data, **kw: None)  # no OCR toolchain
    r = client.post(f"{BASE}/attachments",
                    files={"file": ("scan.pdf", _TEXTLESS_PDF, "application/pdf")},
                    headers=headers_for(doc))
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "attachment_unscannable"
    # with OCR available, recovered text goes through the same PHI gate
    monkeypatch.setattr(catt, "_ocr_pdf_text", lambda data, **kw: "MRN 99887766")
    r = client.post(f"{BASE}/attachments",
                    files={"file": ("scan.pdf", _TEXTLESS_PDF, "application/pdf")},
                    headers=headers_for(doc))
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "phi_detected"


def test_image_upload_fails_closed_when_ocr_unavailable(monkeypatch):
    """OCR screening is a fail-closed control by default (audit finding);
    COMMUNITY_OCR_STRICT=0 is the explicit advisory opt-out."""
    _, _, doc, _ = setup_world()
    from community import attachments as catt
    monkeypatch.setattr(catt, "_ocr_image_text", lambda b: (None, "engine down"))
    r = client.post(f"{BASE}/attachments",
                    files={"file": ("ecg.png", _png_bytes(), "image/png")},
                    headers=headers_for(doc))
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "attachment_unscannable"
    monkeypatch.setenv("COMMUNITY_OCR_STRICT", "0")
    r = client.post(f"{BASE}/attachments",
                    files={"file": ("ecg.png", _png_bytes(), "image/png")},
                    headers=headers_for(doc))
    assert r.status_code == 200


# ─── Direct messages (user-requested extension) ───────────────────────────────
def open_dm(user, peer):
    return client.post(f"{BASE}/dms", json={"user_id": peer["id"]}, headers=headers_for(user))


def post_dm(user, dm_id, body, **kw):
    return client.post(f"{BASE}/dms/{dm_id}/messages", json={"body": body, **kw},
                       headers=headers_for(user))


def test_dm_open_is_idempotent_and_symmetric():
    astore, _, doc, _ = setup_world()
    other = make_verified_physician(astore, specialty="cardiology")
    a = open_dm(doc, other)
    assert a.status_code == 200
    b = open_dm(other, doc)  # same conversation from the other side
    assert b.json()["id"] == a.json()["id"]
    assert a.json()["peer"]["user_id"] == other["id"]
    assert b.json()["peer"]["user_id"] == doc["id"]
    # cannot DM yourself
    assert open_dm(doc, doc).status_code == 400
    # cannot DM someone who cannot enter the community
    buyer = make_user(astore, role="buyer")
    assert open_dm(doc, buyer).status_code == 404


def test_dm_roundtrip_and_listing():
    astore, _, doc, _ = setup_world()
    other = make_verified_physician(astore)
    dm_id = open_dm(doc, other).json()["id"]
    r = post_dm(doc, dm_id, "quick question on the transplant case set")
    assert r.status_code == 200
    assert r.json()["dm"] == dm_id and r.json()["channel"] is None
    # peer sees it, with unread
    r = client.get(f"{BASE}/dms", headers=headers_for(other))
    convo = next(d for d in r.json()["dms"] if d["id"] == dm_id)
    assert convo["unread"] == 1
    assert convo["peer"]["user_id"] == doc["id"]
    r = client.get(f"{BASE}/dms/{dm_id}/messages", headers=headers_for(other))
    assert [m["body"] for m in r.json()["messages"]] == [
        "quick question on the transplant case set"]
    # reading clears the badge
    last = r.json()["messages"][-1]["id"]
    client.post(f"{BASE}/dms/{dm_id}/read", json={"last_read_message_id": last},
                headers=headers_for(other))
    r = client.get(f"{BASE}/badge", headers=headers_for(other))
    assert r.json()["dm_unread"] == 0


def test_dm_badge_includes_dm_unread():
    astore, _, doc, _ = setup_world()
    other = make_verified_physician(astore)
    dm_id = open_dm(doc, other).json()["id"]
    post_dm(doc, dm_id, "ping one")
    post_dm(doc, dm_id, "ping two")
    b = client.get(f"{BASE}/badge", headers=headers_for(other)).json()
    assert b["dm_unread"] == 2
    assert b["unread"] >= 2  # DM unread counts toward the portal badge total


def test_dm_participant_isolation_on_every_path():
    """The privacy invariant: a non-participant — INCLUDING an admin — gets
    404 on every route that could reach DM content."""
    astore, _, doc, admin = setup_world()
    other = make_verified_physician(astore)
    third = make_verified_physician(astore, specialty="oncology")
    dm_id = open_dm(doc, other).json()["id"]
    mid = post_dm(doc, dm_id, "between the two of us: rubric axis 3 is broken").json()["id"]

    for outsider in (third, admin):
        hdrs = headers_for(outsider)
        assert client.get(f"{BASE}/dms/{dm_id}/messages", headers=hdrs).status_code == 404
        assert client.post(f"{BASE}/dms/{dm_id}/messages", json={"body": "hi"},
                           headers=hdrs).status_code == 404
        assert client.post(f"{BASE}/dms/{dm_id}/read", json={"last_read_message_id": mid},
                           headers=hdrs).status_code == 404
        # by message id: react / edit / delete / thread all 404 (no oracle)
        assert client.post(f"{BASE}/messages/{mid}/reactions", json={"emoji": "👀"},
                           headers=hdrs).status_code == 404
        assert client.patch(f"{BASE}/messages/{mid}", json={"body": "x"},
                            headers=hdrs).status_code == 404
        assert client.delete(f"{BASE}/messages/{mid}", headers=hdrs).status_code == 404
        assert client.get(f"{BASE}/messages/{mid}/thread", headers=hdrs).status_code == 404
        # search never surfaces someone else's DM content
        r = client.get(f"{BASE}/search?q=rubric+axis+3", headers=hdrs)
        assert all(m["id"] != mid for m in r.json()["results"])
    # …while a participant CAN search their own DM
    r = client.get(f"{BASE}/search?q=rubric+axis+3", headers=headers_for(other))
    assert any(m["id"] == mid and m["dm"] == dm_id for m in r.json()["results"])


def test_dm_attachment_participants_only():
    astore, _, doc, _ = setup_world()
    other = make_verified_physician(astore)
    third = make_verified_physician(astore)
    dm_id = open_dm(doc, other).json()["id"]
    asset_id = client.post(
        f"{BASE}/attachments",
        files={"file": ("labs.txt", b"Cr 1.9 trending", "text/plain")},
        headers=headers_for(doc)).json()["asset_id"]
    r = post_dm(doc, dm_id, "labs attached", attachment_ids=[asset_id])
    assert r.status_code == 200
    assert client.get(f"{BASE}/attachments/{asset_id}",
                      headers=headers_for(other)).status_code == 200
    assert client.get(f"{BASE}/attachments/{asset_id}",
                      headers=headers_for(third)).status_code == 404


def test_dm_phi_blocked_and_never_stored():
    """1:1 case discussion is exactly where an identifier will be pasted —
    the identical server-side gate, before persistence, category-only."""
    astore, cstore, doc, _ = setup_world()
    other = make_verified_physician(astore)
    dm_id = open_dm(doc, other).json()["id"]
    r = post_dm(doc, dm_id, "patient name is John Smith, MRN 84921734")
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "phi_detected"
    assert "John Smith" not in json.dumps(detail) and "84921734" not in json.dumps(detail)
    with sqlite3.connect(cstore.db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM community_messages").fetchone()[0]
    assert n == 0
    assert cstore.block_count(doc["id"]) == 1
    # audited with categories only
    evs = [e for e in audit_events("community.phi_block") if e["actor_id"] == doc["id"]]
    assert evs and "John" not in evs[-1]["detail_json"]
    # clinical content flows normally
    assert post_dm(doc, dm_id, "K 6.1, gave insulin/D50 — recheck in 2h").status_code == 200


def test_dm_no_threads():
    astore, _, doc, _ = setup_world()
    other = make_verified_physician(astore)
    dm_id = open_dm(doc, other).json()["id"]
    mid = post_dm(doc, dm_id, "first").json()["id"]
    # the DM post schema has no parent_message_id — a reply lands flat
    r = post_dm(other, dm_id, "second", parent_message_id=mid)
    assert r.status_code == 200
    assert r.json()["parent_message_id"] is None


def test_dm_digest_queued_for_recipient_only():
    astore, cstore, doc, _ = setup_world()
    other = make_verified_physician(astore)
    dm_id = open_dm(doc, other).json()["id"]
    post_dm(doc, dm_id, "are you around for a curbside?")
    pending = cstore.unsent_notifications()
    assert [(n["user_id"], n["kind"]) for n in pending] == [(other["id"], "dm")]
    import asyncio
    from community.router import resolve_member_for_notify
    sent = asyncio.run(
        cnotify.flush_pending(cstore, resolve_member=resolve_member_for_notify))
    assert sent == 1


def _recv_until(ws, want_type, tries=10):
    """Drain a socket until an event of the wanted type arrives. The hub is
    process-global across the test module, so presence events from other
    tests' sockets being reaped can interleave — order-strict receives flake."""
    seen = []
    for _ in range(tries):
        evt = ws.receive_json()
        seen.append(evt)
        if evt.get("type") == want_type:
            return evt, seen
    raise AssertionError(f"never received {want_type}; saw {[e.get('type') for e in seen]}")


def test_dm_ws_delivery_targeted():
    """DM events reach the participants' sockets and NEVER a third member's."""
    astore, _, doc, _ = setup_world()
    other = make_verified_physician(astore)
    third = make_verified_physician(astore)
    dm_id = open_dm(doc, other).json()["id"]
    with client.websocket_connect(f"{BASE}/ws?token={token_for(other)}") as ws_peer, \
         client.websocket_connect(f"{BASE}/ws?token={token_for(third)}") as ws_third:
        _recv_until(ws_peer, "hello")
        _recv_until(ws_third, "hello")
        post_dm(doc, dm_id, "realtime dm check")
        evt, _seen = _recv_until(ws_peer, "message.created")
        assert evt["message"]["dm"] == dm_id
        assert evt["message"]["body"] == "realtime dm check"
        # The third member must receive NOTHING for this DM. Post a channel
        # message and drain the third socket up to it: the only
        # message.created it ever sees is the public one.
        post_msg(doc, "general", "public follow-up")
        evt3, seen3 = _recv_until(ws_third, "message.created")
        assert evt3["message"]["body"] == "public follow-up"
        assert all(
            (e.get("message") or {}).get("dm") != dm_id for e in seen3
        ), "DM event leaked to a non-participant socket"


def test_dm_soft_delete():
    astore, cstore, doc, _ = setup_world()
    other = make_verified_physician(astore)
    dm_id = open_dm(doc, other).json()["id"]
    mid = post_dm(doc, dm_id, "typo msg").json()["id"]
    assert client.delete(f"{BASE}/messages/{mid}", headers=headers_for(doc)).status_code == 200
    row = cstore.get_message(mid)
    assert row["deleted"] and row["body"] == ""
    r = client.get(f"{BASE}/dms/{dm_id}/messages", headers=headers_for(other))
    assert mid not in [m["id"] for m in r.json()["messages"]]


# ─── Portal side-panel integration (Side Panel PRD contract) ──────────────────
def test_portal_unread_soft_totals():
    astore, _, doc, _ = setup_world()
    other = make_verified_physician(astore)
    post_msg(other, "general", "one")
    dm_id = open_dm(other, doc).json()["id"]
    post_dm(other, dm_id, "two")
    r = client.get("/community/unread", headers=headers_for(doc))
    assert r.status_code == 200
    assert r.json() == {"total": 2}  # channel unread + dm unread
    # soft for non-members and the unauthenticated — never an error the rail
    # would have to handle
    buyer = make_user(astore, role="buyer")
    assert client.get("/community/unread", headers=headers_for(buyer)).json() == {"total": 0}
    assert client.get("/community/unread").json() == {"total": 0}


def test_portal_handoff_mint_and_redeem():
    astore, _, doc, _ = setup_world()
    r = client.post("/community/handoff", headers=headers_for(doc))
    assert r.status_code == 200
    code = r.json()["token"]
    # redeem → a working Asclepius session for the community
    r = client.post(f"{BASE}/handoff/redeem", json={"token": code})
    assert r.status_code == 200
    jwt = r.json()["token"]
    me = client.get(f"{BASE}/me", headers={"Authorization": "Bearer " + jwt})
    assert me.status_code == 200
    # single use: replay fails
    assert client.post(f"{BASE}/handoff/redeem", json={"token": code}).status_code == 401
    # garbage fails
    assert client.post(f"{BASE}/handoff/redeem", json={"token": "nope"}).status_code == 401
    # non-members cannot mint
    buyer = make_user(astore, role="buyer")
    assert client.post("/community/handoff", headers=headers_for(buyer)).status_code == 403


def test_portal_handoff_gate_rechecked_at_redemption():
    """A code minted before a ban must not open the community after it."""
    astore, cstore, doc, admin = setup_world()
    code = client.post("/community/handoff", headers=headers_for(doc)).json()["token"]
    client.post(f"{BASE}/admin/members/{doc['id']}/deactivate", headers=headers_for(admin))
    assert client.post(f"{BASE}/handoff/redeem", json={"token": code}).status_code == 401


# ─── No public path in (§9.3) ─────────────────────────────────────────────────
def test_no_registration_or_invite_routes():
    for path in (f"{BASE}/register", f"{BASE}/signup", f"{BASE}/invite", f"{BASE}/join"):
        r = client.post(path, json={})
        assert r.status_code in (404, 405), path


def test_community_page_shell_served():
    r = client.get("/community")
    assert r.status_code == 200
    assert "community" in r.text.lower()


def test_app_lifespan_boots_and_stops_community():
    """Production-boot insurance: the ``startup_community`` hook must init the
    store (seeding the three channels) and start the digest loop, and shutdown
    must stop it. The rest of the suite never runs lifespan (module-level
    TestClient), so a boot-time regression here would only surface in prod."""
    from fastapi.testclient import TestClient as TC

    from community import notify as n

    fresh_community()
    with TC(app) as booted:
        assert getattr(app.state, "community_store", None) is not None
        slugs = [c["slug"] for c in app.state.community_store.list_channels()]
        assert slugs[:4] == ["general", "introductions", "task-announcements",
                             "events"]  # #events added in v2.1
        assert "medical-ai-news" in slugs
        assert "nephrology" in slugs  # specialty channels seed too (gated at read)
        assert n._loop_task is not None and not n._loop_task.done()
        # the app is actually serving while the loop runs
        assert booted.get("/community").status_code == 200
    assert n._loop_task is None  # stop_digest_loop ran on shutdown
