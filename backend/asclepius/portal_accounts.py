"""Naming and credential helpers for health-system portal accounts.

Lifted verbatim out of ``routers/asclepius_admin.py``, which was their only
caller until self-signup needed the same username derivation. The provider
router cannot import the admin router to get at them: that module pulls in the
whole admin surface, and everything reachable from the provider file is held to
a stricter standard than the admin file is. A leaf module both can import is the
cheap fix.

``asclepius_admin`` re-exports these names, so its own call sites and the tests
that reach them through it are unchanged.
"""

from __future__ import annotations

import re
import secrets
from typing import Any

# Words that carry no identity: stripping them turns "Mass General Hospital"
# into something the recipient recognises rather than something they retype.
_USERNAME_STOPWORDS = {
    "health", "hospital", "hospitals", "system", "systems", "medical",
    "medicine", "center", "centers", "centre", "centres", "clinic", "clinics",
    "the", "of", "and", "for", "group", "network", "regional", "university",
    "institute", "foundation", "associates", "partners", "care",
}


def derive_hs_username(org_name: str) -> str:
    """A username the recipient can recognise ("Mass General Hospital" ->
    "massgeneral"). Falls back to the full word list when stopwords would strip
    everything (e.g. "University Health System" -> "universityhealthsystem")."""
    words = re.findall(r"[a-z0-9]+", (org_name or "").lower())
    kept = [w for w in words if w not in _USERNAME_STOPWORDS]
    base = "".join(kept or words)[:20]
    return base or "partner"


def unique_hs_username(store: Any, base: str) -> str:
    """Collision-suffix: base, base2 … base9, then a short random suffix."""
    if not store.hs_username_exists(base):
        return base
    for n in range(2, 10):
        cand = f"{base}{n}"
        if not store.hs_username_exists(cand):
            return cand
    while True:
        cand = f"{base}-{secrets.token_hex(2)}"
        if not store.hs_username_exists(cand):
            return cand


# ─── Passphrase generation ───────────────────────────────────────────────────
# Word-based so hospital IT can retype it from an email without transcription
# errors; the trailing hex keeps the space large. Shown once, stored hashed,
# and must_reset=1 forces replacement at first login.
_PASSPHRASE_WORDS = [
    "amber", "aspen", "basil", "birch", "canyon", "cedar", "clover", "coral",
    "delta", "dune", "ember", "fjord", "garnet", "grove", "harbor", "hazel",
    "indigo", "juniper", "kestrel", "lagoon", "linden", "lumen", "maple",
    "meadow", "north", "opal", "orchid", "prairie", "quartz", "raven", "river",
    "saffron", "sierra", "summit", "thistle", "tundra", "umber", "violet",
    "willow", "zephyr",
]


def generate_portal_passphrase() -> str:
    words = [secrets.choice(_PASSPHRASE_WORDS) for _ in range(3)]
    return "-".join(words) + "-" + secrets.token_hex(3)
