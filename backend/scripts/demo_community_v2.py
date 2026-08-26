"""Seed a local Community v2 demo world.

DEV ONLY. Never run this against a production database: it plants fictional
physicians and fabricated chatter. The production community starts EMPTY by
policy (see scripts/purge_community.py for the cleanup that enforces it).
The guard below refuses to run when a production environment is detected.

Run ONCE from ``backend/`` (uses the same DB paths the server will):

    .venv/bin/python scripts/demo_community_v2.py

Creates:
  * an admin (admin@archangel.demo / demo-admin-pw) for the verify queue,
  * 8 demo physicians across nephrology/cardiology/oncology — a MIX of
    approval-path members (verification_status='approved', NO vault row —
    proving the gate bridge) and vault-path members (credentials_verified),
  * one physician left PENDING so you can approve them live in the admin
    queue and watch the welcome appear over WebSocket,
  * two bot welcomes in #introductions and sample human chatter, so the rail
    shows the nephrology specialty channel active (3 members) and cardiology/
    oncology hidden (below threshold).

Then start the server and click through:

    .venv/bin/python -m uvicorn main:app --port 8000 --reload
    open http://localhost:8000/asclepius   (sign in as a demo doctor)

Fire a REAL digest (live PubMed/arXiv/medRxiv/RSS + Claude summarization —
needs ANTHROPIC_API_KEY and INTERNAL_TOOL_SECRET in .env):

    curl -X POST -H "Authorization: Bearer $INTERNAL_TOOL_SECRET" \
      "http://localhost:8000/internal/community/run-digest?kind=news"
    curl -X POST -H "Authorization: Bearer $INTERNAL_TOOL_SECRET" \
      "http://localhost:8000/internal/community/run-digest?kind=papers"

Demo passwords are all ``demo-doctor-pw``.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if (os.getenv("ENV") or "").strip().lower() == "production" or os.getenv("RAILWAY_ENVIRONMENT"):
    print("Refusing to seed demo data: this looks like a production environment.")
    sys.exit(1)

os.environ.setdefault("EMAIL_DEV_MODE", "1")

from asclepius.store import get_store  # noqa: E402
from community.onboard import welcome_new_member  # noqa: E402
from community.store import get_community_store  # noqa: E402

PW = "demo-doctor-pw"

ROSTER = [
    # (name, specialty, years, path)  — 3 nephrology → that channel activates
    ("Priya Raman", "nephrology", 14, "approval"),
    ("Daniel Okafor", "nephrology", 9, "vault"),
    ("Elena Vasquez", "nephrology", 21, "approval"),
    ("Marcus Chen", "cardiology", 11, "vault"),
    ("Sofia Petrov", "cardiology", 7, "approval"),
    ("James Whitfield", "oncology", 18, "vault"),
    ("Amara Diallo", "oncology", 6, "approval"),
]
PENDING = ("Noah Lindqvist", "cardiology", 8)  # approve this one live in the demo

CHATTER = [
    ("general", 0, "Good morning all — first week on the platform, the rubric editor is slicker than I expected."),
    ("general", 1, "Same here. The threading on tricky cases is the part I use most."),
    ("questions-help", 2, "Does a partial-credit rubric line need its own anchor, or can two lines share one?"),
    ("future-of-medical-ai", 3, "Hot take: within five years every discharge summary gets a model first draft and attending sign-off."),
    ("future-of-medical-ai", 5, "The sign-off is the interesting part. Liability follows the signature, not the draft."),
    ("nephrology", 0, "Anyone else seeing models consistently miss permissive creatinine rises in decongestion cases?"),
    ("nephrology", 2, "Constantly. It's the classic trap — the obvious move is wrong and the models take it."),
]


def _slug_email(name: str) -> str:
    return "dr." + name.lower().replace(" ", ".") + "@archangel.demo"


def make_doctor(store, name, specialty, years, path):
    user = store.create_user(
        email=_slug_email(name), password=PW, role="evaluator",
        specialty=specialty, years_experience=years,
        organization="Demo Health Partners",
    )
    with store._conn() as conn:  # demo-only: set display name directly
        conn.execute("UPDATE users SET full_name = ? WHERE id = ?", (name, user["id"]))
    if path == "approval":
        store.record_verification_decision(
            user["id"], status="approved", decided_by="demo-seed",
            tier="labeler", tier_score=60.0)
    else:
        store.upsert_contributor_credentials(
            id_hashed=user["id_hashed"], user_id=user["id"],
            organization="Demo Health Partners", role_title="Physician (MD)",
            credentials_verified=True,
            ship={"degree": "MD", "primary_specialty": specialty,
                  "years_in_active_practice": years, "board_certifications": "ABMS (active)"},
            verify={},
        )
    return store.get_user_by_id(user["id"])


async def main() -> None:
    astore = get_store()
    cstore = get_community_store()  # seeds channels

    if astore.get_user_by_email("admin@archangel.demo"):
        print("Demo world already seeded — delete the *.db files to reseed.")
        return

    admin = astore.create_user(
        email="admin@archangel.demo", password="demo-admin-pw", role="admin")
    print(f"admin: admin@archangel.demo / demo-admin-pw ({admin['id']})")

    doctors = []
    for name, specialty, years, path in ROSTER:
        doc = make_doctor(astore, name, specialty, years, path)
        doctors.append(doc)
        print(f"doctor ({path}): {doc['email']} / {PW}  [{specialty}]")

    # Pending physician for the live-approval demo moment
    pend = astore.create_user(
        email=_slug_email(PENDING[0]), password=PW, role="evaluator",
        specialty=PENDING[1], years_experience=PENDING[2],
        organization="Demo Health Partners")
    with astore._conn() as conn:
        conn.execute("UPDATE users SET full_name = ? WHERE id = ?", (PENDING[0], pend["id"]))
    astore.set_verification_status(pend["id"], "pending")
    print(f"PENDING (approve in admin queue): {pend['email']} / {PW}")

    # Bot welcomes for two members (the rest arrived 'before the feature')
    for doc in (doctors[0], doctors[4]):
        await welcome_new_member(doc)
    print("posted 2 bot welcomes in #introductions")

    # Human chatter
    msg_by_slug = {}
    for slug, who, body in CHATTER:
        ch = cstore.get_channel_by_slug(slug)
        if not ch:
            continue
        m = cstore.insert_message(channel_id=ch["id"],
                                  author_user_id=doctors[who]["id"], body=body)
        msg_by_slug.setdefault(slug, m)
    print(f"posted {len(CHATTER)} sample messages")

    # ── v2.1 social content ──
    from datetime import datetime, timedelta
    from community.system_posts import post_system_message

    # An upcoming event (10 days out) with two Interested, in #events.
    ev_ch = cstore.get_channel_by_slug("events")
    starts = (datetime.utcnow() + timedelta(days=10)).replace(
        hour=22, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    ev = cstore.create_event(
        channel_id=ev_ch["id"], title="Nephrology Journal Club — CardioRenal cases",
        description="We'll walk two decongestion cases models keep getting wrong.",
        starts_at=starts, ends_at=None, timezone="America/New_York",
        location="https://zoom.us/j/9876543210", host="Dr. Priya Raman",
        created_by=admin["id"])
    posted = await post_system_message(
        channel_slug="events",
        body="📅 **Nephrology Journal Club — CardioRenal cases**\nHost: Dr. Priya Raman\n"
             "_See the pinned card at the top of #events for the time and to RSVP._",
        kind="event")
    if posted:
        cstore.link_event_message(ev["id"], posted["id"])
    cstore.toggle_rsvp(ev["id"], doctors[1]["id"])
    cstore.toggle_rsvp(ev["id"], doctors[2]["id"])
    print("posted 1 upcoming event (2 interested) in #events")

    # A poll in #general (linked to a bot message), with a couple of votes.
    gen = cstore.get_channel_by_slug("general")
    poll = cstore.create_poll(
        channel_id=gen["id"],
        question="Rising creatinine on a loop diuretic in acute HF — next move?",
        options=["Stop diuresis, give fluids", "Intensify decongestion", "Hold and reassess in 6h"],
        created_by=doctors[0]["id"])
    pmsg = cstore.insert_message(channel_id=gen["id"], author_user_id=doctors[0]["id"],
                                 body="📊 **Poll:** " + poll["question"], kind="poll")
    cstore.link_poll_message(poll["id"], pmsg["id"])
    opts = cstore.poll_results(poll["id"])["options"]
    cstore.vote_poll(poll["id"], opts[1]["id"], doctors[1]["id"])
    cstore.vote_poll(poll["id"], opts[1]["id"], doctors[2]["id"])
    cstore.vote_poll(poll["id"], opts[0]["id"], doctors[3]["id"])
    print("posted 1 poll (3 votes) in #general")

    # Pin the general intro message + add two bookmarks.
    if "general" in msg_by_slug:
        cstore.pin_message(channel_id=gen["id"],
                           message_id=msg_by_slug["general"]["id"], pinned_by=admin["id"])
        print("pinned 1 message in #general")
    for title, url in [("KDIGO 2024 CKD guideline", "https://kdigo.org/guidelines/"),
                       ("How to post a case (template)", "https://archangelhealth.ai/case-template")]:
        cstore.add_bookmark(channel_id=gen["id"], title=title, url=url, added_by=admin["id"])
    print("added 2 bookmarks in #general")

    # A durable @channel broadcast from the admin in #general.
    cstore.insert_message(
        channel_id=gen["id"], author_user_id=admin["id"],
        body="@channel welcome to everyone who joined this week — introduce yourself in #introductions.",
        mentions=["*channel*"])
    print("posted 1 @channel broadcast in #general")

    print("\nStart the server:  .venv/bin/python -m uvicorn main:app --port 8000 --reload")
    print("Portal:            http://localhost:8000/asclepius")
    print("Community:         http://localhost:8000/community (via the portal side panel)")


if __name__ == "__main__":
    asyncio.run(main())
