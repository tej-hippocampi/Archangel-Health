"""What an applicant sees where the community will be.

A physician waiting on a decision should be able to see what they are applying
to. They must not be able to see the community itself, and that is not a
technicality: the rooms are worth reading precisely because everyone in them is
a credential-verified clinician, an account that has done nothing but submit a
form is not that yet, and rejecting the application afterwards does not unread
the messages. ``community/router.py`` refuses them, and this module does not
change that by a line.

So they are shown a PREVIEW: the real community interface, rendered from the
fixture below.

THE ONE WAY THIS COULD MISLEAD SOMEBODY is a reader mistaking these for real
colleagues. Two things guard it. The names are plainly illustrative rather than
plausible-looking physicians, and every payload carries ``preview: true`` and a
banner the client renders and cannot dismiss.

This module has NO STORE ACCESS, deliberately and structurally. It imports
nothing that can read a database, so it is not "careful not to leak real
messages", it is incapable of it. Keep it that way: if this file ever needs a
store, the feature has become something else and should be reconsidered rather
than wired up.
"""

from __future__ import annotations

from typing import Any, Dict, List

#: Rendered above every preview channel, and not dismissible. The one sentence
#: that keeps a fixture from reading as a room full of real colleagues.
PREVIEW_BANNER = (
    "Preview. These are example conversations, not real colleagues. "
    "The community opens when your application is approved."
)

_MEMBERS: List[Dict[str, Any]] = [
    {"user_id": "preview-1", "display_name": "Dr. A. Rivera",
     "initials": "AR", "accent": "green", "specialty": "Nephrology"},
    {"user_id": "preview-2", "display_name": "Dr. S. Okafor",
     "initials": "SO", "accent": "orange", "specialty": "Cardiology"},
    {"user_id": "preview-3", "display_name": "Dr. M. Lindqvist",
     "initials": "ML", "accent": "pink", "specialty": "Oncology"},
    {"user_id": "preview-team", "display_name": "Archangel team",
     "initials": "AH", "accent": "lime", "specialty": ""},
]

_CHANNELS: List[Dict[str, Any]] = [
    {"slug": "general", "name": "general",
     "topic": "Everyone here is credential-verified.", "unread": 0},
    {"slug": "cases", "name": "cases",
     "topic": "Discuss a case you labelled. De-identified only.", "unread": 0},
    {"slug": "questions-help", "name": "questions-help",
     "topic": "Ask anything about the work or the rubric.", "unread": 0},
    {"slug": "announcements", "name": "announcements",
     "topic": "Archangel team posts. Replies open in threads.", "unread": 0},
]

#: Deliberately mundane. These stand in for the shape of the room, not for its
#: content, and inventing dramatic clinical debate would be inventing colleagues
#: with opinions.
_MESSAGES: Dict[str, List[Dict[str, Any]]] = {
    "general": [
        {"id": "p1", "author": "preview-team",
         "body": "Welcome. Introduce yourself here when you are approved: "
                 "specialty, where you practise, and what you want to see built.",
         "at": "09:12"},
        {"id": "p2", "author": "preview-1",
         "body": "Nephrology, academic centre. Mostly interested in how the "
                 "rubric handles dosing questions.",
         "at": "09:31"},
        {"id": "p3", "author": "preview-2",
         "body": "Same question from the cardiology side. Happy to compare notes "
                 "once we have both done a few.",
         "at": "10:02"},
    ],
    "cases": [
        {"id": "p4", "author": "preview-3",
         "body": "Worth flagging: when both answers miss the same contraindication, "
                 "reject both rather than picking the less wrong one.",
         "at": "Yesterday"},
        {"id": "p5", "author": "preview-1",
         "body": "Agreed. The reference panel notes say the same, and it is the "
                 "part people get wrong first.",
         "at": "Yesterday"},
    ],
    "questions-help": [
        {"id": "p6", "author": "preview-2",
         "body": "Does the clock keep running if I open the chart in a second tab?",
         "at": "Monday"},
        {"id": "p7", "author": "preview-team",
         "body": "No. The session is measured server side, and only one is open "
                 "at a time.",
         "at": "Monday"},
    ],
    "announcements": [
        {"id": "p8", "author": "preview-team",
         "body": "Payouts run weekly. Earnings shows what has accrued, and a "
                 "case counts once it is accepted.",
         "at": "Last week"},
    ],
}


def preview_payload() -> Dict[str, Any]:
    """Everything the client needs to render the preview, in one response.

    One call rather than the four the real client makes, because there is no
    server state to page through and a fixture that pretends to paginate is a
    fixture that will eventually be asked to.
    """
    return {
        "preview": True,
        "banner": PREVIEW_BANNER,
        "can_post": False,
        "members": [dict(m) for m in _MEMBERS],
        "channels": [dict(c) for c in _CHANNELS],
        "messages": {slug: [dict(m) for m in msgs] for slug, msgs in _MESSAGES.items()},
    }
