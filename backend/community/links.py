"""One definition of every member-facing URL an email can carry.

Written because there were two: ``newsletter.py`` built the unsubscribe link as
``/api/community/unsubscribe`` (the route that exists, ``community.router`` is
mounted at ``prefix="/api/community"``) and ``digest.py`` built it as
``/community/unsubscribe`` (a path the page router does not serve). Every
news-digest unsubscribe link 404'd, which is worse than no link at all: the
member concludes the button is a lie and reports the mail as spam instead, and
one complaint costs the sending domain that every other physician's mail goes
through.

The page paths and the API paths genuinely differ, so the fix is not "pick one
prefix" but "state each one once, here".
"""

from __future__ import annotations

import os
from typing import Optional
from urllib.parse import quote

# The community page (``community.router.page_router``) and the unsubscribe
# route (``community.router.router``, mounted under /api/community) live at
# different prefixes. Both are asserted by tests/test_community_links.py.
_PAGE_PATH = "/community"
_UNSUBSCRIBE_PATH = "/api/community/unsubscribe"


def base_url() -> str:
    """Public origin for links in mail, without a trailing slash.

    ``PUBLIC_BASE_URL`` first, then ``BASE_URL``. Returns "" when neither is
    set, which every caller must treat as "omit the link" rather than emitting
    a relative URL: a relative href in an email client resolves against the
    mail host and lands nowhere.
    """
    raw = os.getenv("PUBLIC_BASE_URL") or os.getenv("BASE_URL") or ""
    return raw.strip().rstrip("/")


def community_url() -> str:
    base = base_url()
    return f"{base}{_PAGE_PATH}" if base else ""


def unsubscribe_url(token: str, *, kind: Optional[str] = None) -> str:
    """The one-click unsubscribe link, or "" when it cannot be built.

    Empty when the origin is unset or the member has no token. Callers render
    the footer sentence only when this is non-empty, so a broken link is never
    shown as a working one.

    ``kind`` narrows the link to ONE stream, so mail that is only about pins
    can offer a button that stops pins rather than everything. Omitting it
    produces exactly the link this has always produced, which matters because
    the same route serves links in mail that was sent months ago.
    """
    base = base_url()
    tok = (token or "").strip()
    if not (base and tok):
        return ""
    url = f"{base}{_UNSUBSCRIBE_PATH}?token={tok}"
    k = (kind or "").strip()
    return f"{url}&kind={quote(k, safe='')}" if k else url
