"""What the Archangel account looks like when it speaks.

The bot writes the welcomes, the morning briefs, the weekly discussion and the
task announcements, which is most of what a new physician reads in their first
week. Rendered as two grey initials it reads as a system notice, and people
scroll past system notices. The Sep 1 meeting was specific about the fix: put
the founders' photo on it, because a post with a face on it reads as somebody
talking to you.

The image is a deployment input, not a repository asset, and this module is
arranged around that. Drop a PNG or JPEG at ``backend/assets/community-persona.png``
(or point ``COMMUNITY_PERSONA_AVATAR`` at one anywhere) and it appears; do
neither and the account falls back to the initials it has always shown. There
is nothing to migrate and nothing to redeploy either way.

It goes through ``asclepius.avatar.store`` rather than being served off disk,
for the same three reasons a physician's headshot does: the bytes decide the
type instead of the filename, the re-encode drops EXIF (the founders' photo is
a phone photograph, and phone photographs carry GPS), and the result is
content-addressed so the URL changes when the picture does and no cache serves
the old one.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger("community.persona")

#: Looked for in order when ``COMMUNITY_PERSONA_AVATAR`` is unset. A convention
#: rather than a setting, so the deploy step is "put the file here".
_DEFAULT_BASENAMES = (
    "community-persona.png",
    "community-persona.jpg",
    "community-persona.jpeg",
)

#: (path, mtime, size) -> (sha, mime). Keyed on the file's identity rather than
#: just its path, so replacing the picture is picked up without a restart while
#: a missing one still costs a single stat per serialize.
_cache: Dict[Tuple[str, float, int], Optional[Tuple[str, str]]] = {}


def display_name() -> str:
    """What the account is called. The company, not a person: the photo says
    who is behind it, and a bot signed with one founder's name would be a
    claim the account cannot keep."""
    return (os.getenv("COMMUNITY_PERSONA_NAME") or "Archangel").strip() or "Archangel"


#: What the account has always rendered as: Archangel Health, the company mark
#: rather than the first two letters of the word "Archangel".
DEFAULT_INITIALS = "AH"


def initials() -> str:
    """The fallback when there is no picture.

    The default is pinned rather than derived, because deriving it from the
    unchanged default name would silently repaint every historical bot post
    from AH to AR. A name somebody actually set does get derived letters, which
    is the case where the old ones would be wrong.
    """
    if not (os.getenv("COMMUNITY_PERSONA_NAME") or "").strip():
        return DEFAULT_INITIALS
    parts = [p for p in display_name().replace(".", " ").split() if p]
    if not parts:
        return DEFAULT_INITIALS
    if len(parts) == 1:
        return parts[0][:2].upper()
    return "".join(p[0] for p in parts[:2]).upper()


def source_path() -> Optional[str]:
    """Where the picture is, or None when nobody has supplied one."""
    explicit = (os.getenv("COMMUNITY_PERSONA_AVATAR") or "").strip()
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    assets_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets")
    for name in _DEFAULT_BASENAMES:
        candidate = os.path.join(assets_dir, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def resolve() -> Optional[Tuple[str, str]]:
    """``(sha256, mime)`` of the processed picture, or None.

    Never raises. A persona avatar that cannot be read is a cosmetic problem
    with a working fallback, and the one thing it must not do is take down the
    serializer that every message in the product goes through.
    """
    path = source_path()
    if not path:
        return None
    try:
        stat = os.stat(path)
        key = (path, stat.st_mtime, stat.st_size)
    except OSError:
        return None
    if key in _cache:
        return _cache[key]

    result: Optional[Tuple[str, str]] = None
    try:
        from asclepius import avatar as asc_avatar  # noqa: PLC0415

        with open(path, "rb") as fh:
            data = fh.read(asc_avatar.avatar_max_bytes() + 1)
        sha, mime = asc_avatar.store(data)
        result = (sha, mime)
        log.info("[persona] avatar loaded from %s (%s)", path, sha[:12])
    except Exception:  # noqa: BLE001 - the initials are a fine outcome
        log.warning("[persona] could not read the persona avatar at %s", path,
                    exc_info=True)
    # Cached either way, the failure included: an unreadable file must not cost
    # a re-read and a log line per message in the channel.
    _cache[key] = result
    return result


def avatar_url() -> Optional[str]:
    """The URL the community client fetches, cache-busted by content hash."""
    resolved = resolve()
    if not resolved:
        return None
    return f"/api/community/persona/avatar?v={resolved[0][:12]}"


def decorate(member: Dict[str, Any]) -> Dict[str, Any]:
    """Fill a copy of the system member with its current display identity.

    Applied at serialize time rather than baked into the module constant, so a
    picture dropped in after boot shows up on the next message rather than the
    next deploy.
    """
    out = dict(member)
    out["display_name"] = display_name()
    out["initials"] = initials()
    out["avatar_url"] = avatar_url()
    return out


def reset_cache_for_tests() -> None:
    _cache.clear()
