"""CommunityStore — SQLite persistence for the Asclepius Community (PRD §6).

Follows the ``AsclepiusStore`` pattern (``_conn()`` + row_factory, WAL,
parameterized SQL, JSON columns deserialized on read) but writes to its OWN
database file (``COMMUNITY_DB_PATH``, default ``backend/community.db``). It
never touches ``asclepius.db`` or ``team.db`` — user rows are referenced by
Asclepius user id only.

Tables (PRD §6):
  community_channels       fixed v1 channels (#general, #task-announcements, #questions-help)
  community_messages       messages + threads (parent_message_id) — soft delete only (§7.5)
  community_reactions      (message_id, user_id, emoji)
  community_reads          per-user last-read cursor per channel
  community_attachments    metadata-stripped, PHI-screened attachment refs
  community_notifications  mention/announcement email-digest queue
  community_block_events   PHI block counters (category only — NEVER content, §7.5)
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

# Fixed channel set (PRD §3 + Community v2). Still no user-created channels —
# the channel list is code, seeded idempotently on boot. ``grp`` drives the
# rail grouping ('core' | 'specialty'); a 'specialty' channel carries the
# specialty it belongs to and is threshold-gated at read time (router
# ``visible_channels``) so a young community never shows a dead channel.
DEFAULT_CHANNELS = [
    {
        "slug": "general",
        "name": "general",
        "description": "Open discussion between contributor physicians.",
        "post_policy": "all",
        "grp": "core",
    },
    {
        "slug": "introductions",
        "name": "introductions",
        "description": "New here? Say hello — specialty, where you practice, what you're curious about.",
        "post_policy": "all",
        "grp": "core",
    },
    {
        "slug": "task-announcements",
        "name": "task-announcements",
        "description": "New task batches, specialty calls, deadlines. Posts from the Archangel team; replies open in threads.",
        "post_policy": "admin",
        "grp": "core",
    },
    {
        "slug": "events",
        "name": "events",
        "description": "Journal clubs, CME, grand rounds, meetups. The next event is pinned at the top; tap Interested to get a reminder.",
        "post_policy": "admin",
        "grp": "core",
    },
    {
        "slug": "medical-ai-news",
        "name": "medical-ai-news",
        "description": "Curated medical-AI news and research digests, posted by the Archangel bot. Discuss in threads.",
        "post_policy": "admin",
        "grp": "core",
    },
    {
        "slug": "research-and-opportunities",
        "name": "research-and-opportunities",
        "description": "Studies, benchmarks, collaborations, and paid opportunities beyond the task queue. Posts from the Archangel team; replies open in threads.",
        "post_policy": "admin",
        "grp": "core",
    },
    {
        "slug": "future-of-medical-ai",
        "name": "future-of-medical-ai",
        "description": "Where is AI in medicine actually going? Open debate — takes, papers, predictions.",
        "post_policy": "all",
        "grp": "core",
    },
    {
        "slug": "questions-help",
        "name": "questions-help",
        "description": "Ask anything about a case, a rubric, or a payout.",
        "post_policy": "all",
        "grp": "core",
    },
    # The one room in the product that members cannot see. It exists because
    # the team needs a daily habit of reading what is happening in medical AI,
    # and a channel physicians can read is a channel the team writes for an
    # audience instead of for itself. ``staff_only`` is enforced in
    # ``visible_channels``, ``_require_message_access`` and the WS fan-out --
    # three places, because a "hidden" channel that leaks through any one of
    # them is not hidden.
    {
        "slug": "team-ai-spotlight",
        "name": "team-ai-spotlight",
        "description": "Archangel team only. One story a day on where AI in medicine is moving.",
        "post_policy": "admin",
        "grp": "core",
        "staff_only": 1,
    },
]


#: How a specialty and a region are carried as one cohort value through the
#: seeding call and the member counts. One string rather than a tuple because
#: it travels through JSON, a set, and a SQL parameter on the way.
SPECIALTY_REGION_SEP = "|"


def specialty_region_key(specialty: Optional[str], region: Optional[str]) -> str:
    """The cohort key for one specialty in one region, or "" when incomplete.

    Both halves are needed: a physician with no country has no region, and
    counting them towards a crossed room would open a room they cannot be
    found in.
    """
    spec = (specialty or "").strip().lower()
    reg = (region or "").strip().lower()
    if not spec or not reg:
        return ""
    return f"{spec}{SPECIALTY_REGION_SEP}{reg}"


def specialty_region_channel_defs(keys: List[str]) -> List[Dict[str, Any]]:
    """One channel per specialty-in-region cohort that has members.

    The room the Sep 1 meeting asked for by name: #neurology-africa, so a
    physician can find their own specialty near enough to be the same
    conversation. It is a THIRD axis, not a replacement for either of the two
    it crosses: #neurology stays the whole world and #nigeria stays every
    specialty in one country.

    Crossing every specialty with every region would be four times nine rooms
    for a community this size, so nothing is created speculatively: a room
    exists only when the caller passes a cohort that actually has members in
    it, and it is hidden until it clears its own threshold, which is set
    higher than the plain specialty and country thresholds because a crossed
    room is a subset of two rooms that already exist.
    """
    from community.countries import region_name  # noqa: PLC0415 - config only

    valid = {c["slug"] for c in specialty_channel_defs()}
    out: List[Dict[str, Any]] = []
    seen: List[str] = []
    for raw in keys or ():
        key = str(raw or "").strip().lower()
        if SPECIALTY_REGION_SEP not in key:
            continue
        specialty, region = key.split(SPECIALTY_REGION_SEP, 1)
        display = region_name(region)
        # An unknown specialty or region produces nothing, which is the same
        # rule the country and subspecialty lists follow.
        if specialty not in valid or not display or key in seen:
            continue
        seen.append(key)
        out.append({
            "slug": f"{specialty}-{region}",
            "name": f"{specialty}-{region}",
            "description": (
                f"{specialty.title()} in {display}: colleagues close enough that "
                "the guidelines, the drug availability and the meetings worth "
                "attending are the same conversation."
            ),
            "post_policy": "all",
            "grp": "specialty_region",
            "specialty": specialty,
            "region": region,
        })
    return out


def city_slug(raw: Optional[str]) -> str:
    """The one canonical form of a self-reported practice city.

    Free text, so "New York", "new york" and "New York, NY" have to become one
    room rather than three. Lives here rather than at either call site because
    the seeding path and the counting path (``router.member_map``) must agree
    exactly: a city that seeds as ``new-york`` and counts as ``new-york-ny``
    is a room that can never reach its threshold.

    Accents are folded rather than stripped, so São Paulo is ``sao-paulo`` and
    not ``s-o-paulo``.
    """
    import unicodedata  # noqa: PLC0415 - only this function needs it

    text = str(raw or "").strip().lower()
    if not text:
        return ""
    # A city typed as "Boston, MA" is the same room as "Boston".
    text = text.split(",")[0].strip()
    folded = "".join(
        ch for ch in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(ch)
    )
    return "-".join(part for part in re.split(r"[^a-z0-9]+", folded) if part)


def reserved_channel_slugs() -> set:
    """Slugs a city may never claim.

    Singapore is both a country room and a city a physician types, and the
    seeding UPSERT keys on slug: without this the city cohort would silently
    rewrite the country channel's group and description.
    """
    from community.countries import COUNTRIES, REGIONS  # noqa: PLC0415 - config only
    from community.subspecialties import SUBSPECIALTIES  # noqa: PLC0415 - config only

    specialties = {c["slug"] for c in specialty_channel_defs()}
    out = {c["slug"] for c in DEFAULT_CHANNELS}
    out |= specialties
    out |= {c.slug for c in COUNTRIES.values()}
    out |= {s.slug for s in SUBSPECIALTIES}
    # Every crossed room that COULD exist, not only the ones that do: a city
    # room seeded today must not claim a slug a cohort could open tomorrow.
    out |= {f"{s}-{r}" for s in specialties for r in REGIONS}
    return out


def city_channel_defs(cities: List[str]) -> List[Dict[str, Any]]:
    """One channel per city that has members, from self-reported practice city.

    Only the cities that have members, for the country-channel reason: a rail
    of empty rooms is a directory, not a community.
    """
    reserved = reserved_channel_slugs()
    out: List[Dict[str, Any]] = []
    seen: List[str] = []
    for raw in cities or ():
        slug = city_slug(raw)
        if not slug or slug in seen or slug in reserved:
            continue
        seen.append(slug)
        out.append({
            "slug": slug,
            "name": slug,
            "description": (
                f"Colleagues practising in {slug.replace('-', ' ').title()}: who is "
                "nearby, what is on locally, and which hospitals are actually "
                "deploying this."
            ),
            "post_policy": "all",
            "grp": "city",
            "city": slug,
        })
    return out


def specialty_channel_defs() -> List[Dict[str, Any]]:
    """One channel per ENABLED specialty, derived from the asclepius specialty
    registry (config-only module — no DB touch, so plane isolation holds).
    Adding a specialty to the registry auto-creates its channel on next boot."""
    from asclepius.specialties import SPECIALTY_REGISTRY  # noqa: PLC0415 — config only

    out: List[Dict[str, Any]] = []
    for cfg in SPECIALTY_REGISTRY.values():
        if not cfg.enabled:
            continue
        out.append({
            "slug": cfg.name,
            "name": cfg.name,
            "description": f"For {cfg.name} colleagues — cases (de-identified), literature, and specialty task talk.",
            "post_policy": "all",
            "grp": "specialty",
            "specialty": cfg.name,
        })
    return out


#: How often the news digest is emailed. 'off' still leaves the in-app channel
#: readable: unsubscribing from email is not leaving the community.
NEWS_FREQUENCIES = ("daily", "weekly", "off")
DEFAULT_NEWS_FREQUENCY = "daily"


def _utcnow_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


class CommunityStore:
    def __init__(self, db_path: Optional[str] = None):
        base_dir = os.path.dirname(os.path.dirname(__file__))  # backend/
        default_path = os.path.join(base_dir, "community.db")
        self.db_path = db_path or os.getenv("COMMUNITY_DB_PATH") or default_path
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()
        self.ensure_default_channels()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS community_channels (
                    id TEXT PRIMARY KEY,
                    slug TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT,
                    post_policy TEXT NOT NULL DEFAULT 'all',
                    position INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS community_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id TEXT NOT NULL,
                    author_user_id TEXT NOT NULL,
                    parent_message_id INTEGER,
                    body TEXT NOT NULL,
                    mentions_json TEXT NOT NULL DEFAULT '[]',
                    attachments_json TEXT NOT NULL DEFAULT '[]',
                    edited_at TEXT,
                    deleted_at TEXT,
                    deleted_by TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_cmsg_channel ON community_messages(channel_id, id);
                CREATE INDEX IF NOT EXISTS idx_cmsg_parent ON community_messages(parent_message_id);
                CREATE TABLE IF NOT EXISTS community_reactions (
                    message_id INTEGER NOT NULL,
                    user_id TEXT NOT NULL,
                    emoji TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (message_id, user_id, emoji)
                );
                CREATE TABLE IF NOT EXISTS community_reads (
                    user_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    last_read_message_id INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, channel_id)
                );
                CREATE TABLE IF NOT EXISTS community_attachments (
                    asset_id TEXT PRIMARY KEY,
                    sha256 TEXT NOT NULL,
                    mime TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    orig_name TEXT,
                    uploader_user_id TEXT NOT NULL,
                    message_id INTEGER,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS community_notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    emailed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_cnotif_unsent
                    ON community_notifications(emailed_at, user_id);
                CREATE TABLE IF NOT EXISTS community_dms (
                    id TEXT PRIMARY KEY,
                    user_a TEXT NOT NULL,
                    user_b TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (user_a, user_b)
                );
                CREATE INDEX IF NOT EXISTS idx_cdm_a ON community_dms(user_a);
                CREATE INDEX IF NOT EXISTS idx_cdm_b ON community_dms(user_b);
                CREATE TABLE IF NOT EXISTS community_bans (
                    user_id TEXT PRIMARY KEY,
                    banned_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS community_block_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    surface TEXT NOT NULL,
                    categories_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_cblock_user ON community_block_events(user_id, id);
                CREATE TABLE IF NOT EXISTS community_content_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    external_id TEXT,
                    url TEXT NOT NULL,
                    url_norm TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    title_norm TEXT NOT NULL,
                    published_at TEXT,
                    abstract TEXT,
                    summary TEXT,
                    relevance REAL,
                    status TEXT NOT NULL DEFAULT 'new',
                    fetched_at TEXT NOT NULL,
                    posted_message_id INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_ccontent_status
                    ON community_content_items(status, fetched_at);
                CREATE INDEX IF NOT EXISTS idx_ccontent_title
                    ON community_content_items(title_norm, fetched_at);
                CREATE TABLE IF NOT EXISTS community_digest_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    ok INTEGER,
                    items_fetched INTEGER,
                    items_posted INTEGER,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_cdigest_kind
                    ON community_digest_runs(kind, id);
                -- Paid-search spend control. Counts CALLS per provider per UTC
                -- day, not dollars: per-provider pricing drifts and a number
                -- this file cannot verify is worse than no number. Durable
                -- rather than in-process, so a restart cannot reset the day's
                -- budget and a redeploy loop cannot spend it repeatedly.
                CREATE TABLE IF NOT EXISTS community_search_budget (
                    day       TEXT NOT NULL,
                    provider  TEXT NOT NULL,
                    calls     INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (day, provider)
                );
                -- ─── Community v2.1: events / polls / pins / bookmarks ───────────
                CREATE TABLE IF NOT EXISTS community_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id TEXT NOT NULL,
                    message_id INTEGER,          -- linked kind='event' message (threads/discussion)
                    title TEXT NOT NULL,
                    description TEXT,
                    starts_at TEXT NOT NULL,     -- ISO-Z UTC
                    ends_at TEXT,
                    timezone TEXT,               -- IANA, e.g. America/New_York (display only)
                    location TEXT,               -- address or join URL
                    host TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    cancelled_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_cevent_start
                    ON community_events(channel_id, starts_at);
                CREATE TABLE IF NOT EXISTS community_event_rsvps (
                    event_id INTEGER NOT NULL,
                    user_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    reminded_at TEXT,
                    PRIMARY KEY (event_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS community_polls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id TEXT NOT NULL,
                    message_id INTEGER,
                    question TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    closes_at TEXT,
                    closed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_cpoll_msg ON community_polls(message_id);
                CREATE TABLE IF NOT EXISTS community_poll_options (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    poll_id INTEGER NOT NULL,
                    idx INTEGER NOT NULL,
                    text TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_cpollopt_poll ON community_poll_options(poll_id, idx);
                CREATE TABLE IF NOT EXISTS community_poll_votes (
                    poll_id INTEGER NOT NULL,
                    option_id INTEGER NOT NULL,
                    user_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (poll_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS community_pins (
                    channel_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    pinned_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (channel_id, message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_cpin_channel ON community_pins(channel_id, created_at);
                CREATE TABLE IF NOT EXISTS community_bookmarks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    added_by TEXT NOT NULL,
                    position INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_cbookmark_channel
                    ON community_bookmarks(channel_id, position);

                -- Email preferences, one row per member.
                --
                -- A row is written lazily on the first read, so ABSENCE means
                -- "never asked", not "opted out". The default lives in one
                -- place (DEFAULT_NEWS_FREQUENCY) rather than being implied by a
                -- missing row, because a missing row is also what a failed
                -- write looks like.
                --
                -- unsubscribe_token is a bearer credential that turns off mail
                -- for one person, so it is per-user, long, and rotated whenever
                -- preferences change from inside the product.
                CREATE TABLE IF NOT EXISTS community_email_prefs (
                    user_id            TEXT PRIMARY KEY,
                    news_frequency     TEXT NOT NULL DEFAULT 'daily',
                    unsubscribe_token  TEXT NOT NULL UNIQUE,
                    updated_at         TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_cprefs_freq
                    ON community_email_prefs(news_frequency);
                """
            )
            # Column migrations for tables that predate Community v2 —
            # CREATE IF NOT EXISTS never adds columns to an existing table
            # (mirrors the AsclepiusStore ``cols()`` migration pattern).
            def cols(table: str) -> set:
                return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}

            ch_cols = cols("community_channels")
            if "specialty" not in ch_cols:
                conn.execute("ALTER TABLE community_channels ADD COLUMN specialty TEXT")
            if "grp" not in ch_cols:
                conn.execute(
                    "ALTER TABLE community_channels ADD COLUMN grp TEXT NOT NULL DEFAULT 'core'"
                )
            if "is_active" not in ch_cols:
                conn.execute(
                    "ALTER TABLE community_channels ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1"
                )
            if "country" not in ch_cols:
                conn.execute("ALTER TABLE community_channels ADD COLUMN country TEXT")
            # The two cohort dimensions added after countries. Same shape and
            # same rules: the value is the channel's own slug, the seeding call
            # decides which exist, and visibility is counted at read time.
            if "subspecialty" not in ch_cols:
                conn.execute("ALTER TABLE community_channels ADD COLUMN subspecialty TEXT")
            if "city" not in ch_cols:
                conn.execute("ALTER TABLE community_channels ADD COLUMN city TEXT")
            # The crossed rooms reuse the existing ``specialty`` column for
            # their first half and carry the second here, so a crossed room and
            # a plain specialty room are told apart by ``grp`` alone.
            if "region" not in ch_cols:
                conn.execute("ALTER TABLE community_channels ADD COLUMN region TEXT")
            # Defaults to 0 so every channel that predates the column stays
            # exactly as visible as it was. A migration that made anything
            # staff-only by default would hide live rooms on deploy.
            if "staff_only" not in ch_cols:
                conn.execute(
                    "ALTER TABLE community_channels "
                    "ADD COLUMN staff_only INTEGER NOT NULL DEFAULT 0"
                )
            msg_cols = cols("community_messages")
            if "kind" not in msg_cols:
                conn.execute("ALTER TABLE community_messages ADD COLUMN kind TEXT")
            # Link cards on a bot post: title, url, domain, one-line summary,
            # and an optional discussion prompt. Stored as JSON beside the body
            # rather than pasted into it, so the client can render a card and a
            # digest email can render a list from the same row.
            if "cards_json" not in msg_cols:
                conn.execute("ALTER TABLE community_messages ADD COLUMN cards_json TEXT")
            # Activity mail (mentions, DMs, broadcasts, announcements) is a
            # separate switch from the news cadence: a physician who wants less
            # news still wants to know they were @mentioned. Defaults to on,
            # because every existing member was already receiving it. The
            # unsubscribe token turns BOTH off — see unsubscribe_by_token.
            pref_cols = cols("community_email_prefs")
            if "activity_emails" not in pref_cols:
                conn.execute(
                    "ALTER TABLE community_email_prefs "
                    "ADD COLUMN activity_emails INTEGER NOT NULL DEFAULT 1"
                )

    # ─── Channels ─────────────────────────────────────────────────────────────
    def ensure_default_channels(
        self,
        country_codes: Optional[List[str]] = None,
        *,
        subspecialties: Optional[List[str]] = None,
        cities: Optional[List[str]] = None,
        specialty_regions: Optional[List[str]] = None,
    ) -> None:
        """Idempotently seed the fixed channels (PRD §3 + Community v2): the
        core set, one channel per enabled specialty, and one per country,
        subspecialty, city and specialty-in-region cohort that has members. A slug removed from the config
        is DEACTIVATED, never deleted: its history stays in the DB and
        moderation/audit paths can still resolve it.

        The cohort arguments come from the caller because they are the inputs
        this module cannot compute: they live on the asclepius plane, and
        reaching across for them here would put a users query inside the
        community store. Passing None for one means "leave those channels as
        they are", NOT "deactivate them": a caller without the roster to hand
        must not silently retire every country, subspecialty, city or crossed
        room. They are independent, so a caller may hand over one and withhold
        the rest.
        """
        from community.countries import channel_defs  # noqa: PLC0415 — config only
        from community.subspecialties import (  # noqa: PLC0415 - config only
            channel_defs as subspecialty_channel_defs,
        )

        cohort_channels = (
            channel_defs(country_codes or [])
            + subspecialty_channel_defs(subspecialties or [])
            + city_channel_defs(cities or [])
            + specialty_region_channel_defs(specialty_regions or [])
        )
        seeded = DEFAULT_CHANNELS + specialty_channel_defs() + cohort_channels
        with self._conn() as conn:
            for pos, ch in enumerate(seeded):
                conn.execute(
                    """
                    INSERT INTO community_channels
                        (id, slug, name, description, post_policy, position,
                         specialty, grp, country, subspecialty, city,
                         region, staff_only, is_active, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                    ON CONFLICT(slug) DO UPDATE SET
                        name = excluded.name,
                        description = excluded.description,
                        post_policy = excluded.post_policy,
                        position = excluded.position,
                        specialty = excluded.specialty,
                        grp = excluded.grp,
                        country = excluded.country,
                        subspecialty = excluded.subspecialty,
                        city = excluded.city,
                        region = excluded.region,
                        staff_only = excluded.staff_only,
                        is_active = 1
                    """,
                    (
                        "ch-" + uuid.uuid4().hex[:12],
                        ch["slug"],
                        ch["name"],
                        ch["description"],
                        ch["post_policy"],
                        pos,
                        ch.get("specialty"),
                        ch.get("grp") or "core",
                        ch.get("country"),
                        ch.get("subspecialty"),
                        ch.get("city"),
                        ch.get("region"),
                        1 if ch.get("staff_only") else 0,
                        _utcnow_iso(),
                    ),
                )
            keep = [ch["slug"] for ch in seeded]
            # No roster in hand for a dimension: leave its existing rooms alone
            # rather than retiring rooms whose members we simply did not look
            # up on this call.
            withheld = [
                grp for grp, arg in (
                    ("country", country_codes),
                    ("subspecialty", subspecialties),
                    ("city", cities),
                    ("specialty_region", specialty_regions),
                ) if arg is None
            ]
            if withheld:
                marks = ",".join("?" * len(withheld))
                keep += [
                    r["slug"] for r in conn.execute(
                        f"SELECT slug FROM community_channels WHERE grp IN ({marks})",
                        withheld,
                    ).fetchall()
                ]
            qmarks = ",".join("?" * len(keep))
            conn.execute(
                f"UPDATE community_channels SET is_active = 0 WHERE slug NOT IN ({qmarks})",
                keep,
            )

    def list_channels(self, *, include_inactive: bool = False) -> List[Dict[str, Any]]:
        where = "" if include_inactive else "WHERE is_active = 1"
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM community_channels {where} ORDER BY position ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def channel_has_messages(self, channel_id: str) -> bool:
        """Cheap EXISTS probe — powers sticky activation (a specialty channel
        with history never disappears when membership dips below threshold)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM community_messages WHERE channel_id = ? AND deleted_at IS NULL LIMIT 1",
                (channel_id,),
            ).fetchone()
        return bool(row)

    def get_channel_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM community_channels WHERE slug = ?", ((slug or "").strip().lower(),)
            ).fetchone()
        return dict(row) if row else None

    # ─── Messages ─────────────────────────────────────────────────────────────
    def insert_message(
        self,
        *,
        channel_id: str,
        author_user_id: str,
        body: str,
        parent_message_id: Optional[int] = None,
        mentions: Optional[List[str]] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
        kind: Optional[str] = None,
        cards: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        now = _utcnow_iso()
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO community_messages
                    (channel_id, author_user_id, parent_message_id, body,
                     mentions_json, attachments_json, kind, cards_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    channel_id,
                    author_user_id,
                    parent_message_id,
                    body,
                    json.dumps(mentions or []),
                    "[]",
                    kind,
                    json.dumps(cards) if cards else None,
                    now,
                ),
            )
            mid = cur.lastrowid
            # ATOMICALLY claim each referenced attachment (audit finding: two
            # concurrent posts by the same uploader could both pass the
            # router's "already posted" check). The conditional UPDATE is the
            # arbiter — only attachments this message actually won are
            # recorded on it, so a lost race drops the ref instead of listing
            # an attachment that belongs to another message.
            claimed = []
            for att in attachments or []:
                if not att.get("asset_id"):
                    continue
                res = conn.execute(
                    "UPDATE community_attachments SET message_id = ? "
                    "WHERE asset_id = ? AND message_id IS NULL",
                    (mid, att["asset_id"]),
                )
                if res.rowcount:
                    claimed.append(att)
            if claimed:
                conn.execute(
                    "UPDATE community_messages SET attachments_json = ? WHERE id = ?",
                    (json.dumps(claimed), mid),
                )
        return self.get_message(mid)  # type: ignore[return-value]

    def get_message(self, message_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM community_messages WHERE id = ?", (message_id,)
            ).fetchone()
        return self._message_row(row) if row else None

    def has_system_post_of_kind(self, channel_id: str, kind: str) -> bool:
        """Has the bot already posted this kind of message in this channel?

        What makes the pinned channel-topic post idempotent: it is written once
        and then left alone, however many times the morning run fires.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM community_messages WHERE channel_id = ? "
                "AND author_user_id = 'u-system' AND kind = ? "
                "AND deleted_at IS NULL LIMIT 1",
                (channel_id, kind),
            ).fetchone()
        return row is not None

    def system_post_exists_since(
        self, *, channel_id: str, kind: str, body: str, since_iso: str
    ) -> bool:
        """Has the bot already posted this exact text in this channel since
        ``since_iso``? Guards the repeat-announcement case: ten uploads of one
        nephrology task in an afternoon are one piece of news, not ten.
        Exact-body match on purpose — a different count is different news.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM community_messages "
                "WHERE channel_id = ? AND author_user_id = 'u-system' AND kind = ? "
                "AND body = ? AND created_at >= ? AND deleted_at IS NULL LIMIT 1",
                (channel_id, kind, (body or "").strip(), since_iso),
            ).fetchone()
        return row is not None

    @staticmethod
    def _message_row(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        d["mentions"] = json.loads(d.pop("mentions_json", "[]") or "[]")
        d["attachments"] = json.loads(d.pop("attachments_json", "[]") or "[]")
        d["deleted"] = bool(d.get("deleted_at"))
        return d

    def edit_message(
        self, message_id: int, *, body: str, mentions: Optional[List[str]] = None
    ) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            if mentions is None:
                conn.execute(
                    "UPDATE community_messages SET body = ?, edited_at = ? "
                    "WHERE id = ? AND deleted_at IS NULL",
                    (body, _utcnow_iso(), message_id),
                )
            else:
                conn.execute(
                    "UPDATE community_messages SET body = ?, edited_at = ?, mentions_json = ? "
                    "WHERE id = ? AND deleted_at IS NULL",
                    (body, _utcnow_iso(), json.dumps(mentions), message_id),
                )
        return self.get_message(message_id)

    def soft_delete_message(self, message_id: int, *, deleted_by: str) -> Optional[Dict[str, Any]]:
        """Soft delete (PRD §7.5): the row stays (audit chain intact), the body
        is cleared so deleted content cannot be re-read through any endpoint."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE community_messages SET body = '', deleted_at = ?, deleted_by = ?, "
                "mentions_json = '[]', attachments_json = '[]' "
                "WHERE id = ? AND deleted_at IS NULL",
                (_utcnow_iso(), deleted_by, message_id),
            )
            # A deleted message must not linger in the pinned list (v2.1).
            conn.execute("DELETE FROM community_pins WHERE message_id = ?", (message_id,))
        return self.get_message(message_id)

    def list_messages(
        self,
        channel_id: str,
        *,
        before_id: Optional[int] = None,
        after_id: Optional[int] = None,
        limit: int = 50,
    ) -> tuple:
        """One page of TOP-LEVEL messages, ascending id. Returns
        ``(messages, has_more)``. ``before_id`` pages history upward (infinite
        scroll); ``after_id`` serves the polling fallback and pages FORWARD
        (oldest-first) so a burst larger than ``limit`` is delivered in order
        with ``has_more`` set, never with a silent gap (audit finding).

        ``has_more`` is computed from the RAW row count (a limit+1 sentinel
        row) BEFORE the tombstone filter below — otherwise a page containing a
        reply-less deleted message under-fills and paging dead-ends with older
        history still unreachable (audit finding). Deleted messages are kept
        only when they still anchor a live thread (client renders a tombstone).
        """
        limit = max(1, min(int(limit or 50), 200))
        clauses = ["channel_id = ?", "parent_message_id IS NULL"]
        params: List[Any] = [channel_id]
        if before_id is not None:
            clauses.append("id < ?")
            params.append(int(before_id))
        if after_id is not None:
            clauses.append("id > ?")
            params.append(int(after_id))
        order = "ASC" if after_id is not None else "DESC"
        sql = (
            "SELECT * FROM community_messages WHERE "
            + " AND ".join(clauses)
            + f" ORDER BY id {order} LIMIT ?"
        )
        params.append(limit + 1)
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        out = [self._message_row(r) for r in rows]
        if order == "DESC":
            out = out[::-1]
        deleted_ids = [m["id"] for m in out if m["deleted"]]
        keep_tombstones = set()
        if deleted_ids:
            with self._conn() as conn:
                qmarks = ",".join("?" * len(deleted_ids))
                trows = conn.execute(
                    f"SELECT DISTINCT parent_message_id AS pid FROM community_messages "
                    f"WHERE parent_message_id IN ({qmarks}) AND deleted_at IS NULL",
                    deleted_ids,
                ).fetchall()
            keep_tombstones = {r["pid"] for r in trows}
        return (
            [m for m in out if not m["deleted"] or m["id"] in keep_tombstones],
            has_more,
        )

    def list_thread(self, parent_message_id: int) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM community_messages WHERE parent_message_id = ? "
                "AND deleted_at IS NULL ORDER BY id ASC",
                (parent_message_id,),
            ).fetchall()
        return [self._message_row(r) for r in rows]

    def reply_counts(self, message_ids: List[int]) -> Dict[int, Dict[str, Any]]:
        if not message_ids:
            return {}
        qmarks = ",".join("?" * len(message_ids))
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT parent_message_id AS pid, COUNT(*) AS n, MAX(created_at) AS last_at
                FROM community_messages
                WHERE parent_message_id IN ({qmarks}) AND deleted_at IS NULL
                GROUP BY parent_message_id
                """,
                message_ids,
            ).fetchall()
        return {r["pid"]: {"count": int(r["n"]), "last_at": r["last_at"]} for r in rows}

    # ─── Reactions ────────────────────────────────────────────────────────────
    def toggle_reaction(self, message_id: int, user_id: str, emoji: str) -> bool:
        """Add the reaction if absent, remove if present. Returns True when the
        reaction now exists."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM community_reactions WHERE message_id = ? AND user_id = ? AND emoji = ?",
                (message_id, user_id, emoji),
            ).fetchone()
            if row:
                conn.execute(
                    "DELETE FROM community_reactions WHERE message_id = ? AND user_id = ? AND emoji = ?",
                    (message_id, user_id, emoji),
                )
                return False
            conn.execute(
                "INSERT INTO community_reactions (message_id, user_id, emoji, created_at) VALUES (?, ?, ?, ?)",
                (message_id, user_id, emoji, _utcnow_iso()),
            )
            return True

    def reactions_for(self, message_ids: List[int]) -> Dict[int, List[Dict[str, Any]]]:
        """Grouped reactions per message: ``[{emoji, count, user_ids}]`` in
        first-reacted order."""
        if not message_ids:
            return {}
        qmarks = ",".join("?" * len(message_ids))
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT message_id, emoji, user_id, created_at FROM community_reactions
                WHERE message_id IN ({qmarks}) ORDER BY created_at ASC, rowid ASC
                """,
                message_ids,
            ).fetchall()
        out: Dict[int, List[Dict[str, Any]]] = {}
        for r in rows:
            groups = out.setdefault(r["message_id"], [])
            g = next((g for g in groups if g["emoji"] == r["emoji"]), None)
            if not g:
                g = {"emoji": r["emoji"], "count": 0, "user_ids": []}
                groups.append(g)
            g["count"] += 1
            g["user_ids"].append(r["user_id"])
        return out

    # ─── Reads / unread ───────────────────────────────────────────────────────
    def set_read(self, user_id: str, channel_id: str, last_read_message_id: int) -> None:
        """Advance (never rewind) the user's read cursor for a channel."""
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO community_reads (user_id, channel_id, last_read_message_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, channel_id) DO UPDATE SET
                    last_read_message_id = MAX(community_reads.last_read_message_id, excluded.last_read_message_id),
                    updated_at = excluded.updated_at
                """,
                (user_id, channel_id, int(last_read_message_id), _utcnow_iso()),
            )

    def read_cursors(self, user_id: str) -> Dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT channel_id, last_read_message_id FROM community_reads WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        return {r["channel_id"]: int(r["last_read_message_id"]) for r in rows}

    def unread_counts(
        self, user_id: str, *, channels: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Dict[str, int]]:
        """Per-channel ``{unread, mentions}`` — TOP-LEVEL messages past the
        read cursor, authored by someone else, not deleted.

        ``channels`` lets the caller pass an already-visibility-filtered list
        (Community v2 threshold gating) so a hidden channel never contributes
        to a badge; default remains every active channel.

        Top-level only, deliberately: the read cursor advances via top-level
        message ids, so counting thread replies would strand a badge no read
        action could ever clear (audit finding). Reply activity is surfaced by
        the root's reply count in the channel, and reply @mentions still reach
        the member through the email digest. Counting happens in SQL — no row
        materialization on this hot path (badge + channel list)."""
        cursors = self.read_cursors(user_id)
        # user ids are ``u-<hex>``; escape anyway so LIKE metachars are inert.
        like = ('%"' + user_id.replace("\\", "\\\\").replace("%", "\\%")
                .replace("_", "\\_") + '"%')
        # The @channel broadcast sentinel lights every member's mention badge.
        bcast_like = '%"*channel*"%'
        out: Dict[str, Dict[str, int]] = {}
        with self._conn() as conn:
            for ch in (channels if channels is not None else self.list_channels()):
                last = cursors.get(ch["id"], 0)
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS unread,
                           COALESCE(SUM(CASE WHEN mentions_json LIKE ? ESCAPE '\\'
                                             OR mentions_json LIKE ?
                                             THEN 1 ELSE 0 END), 0) AS mentions
                    FROM community_messages
                    WHERE channel_id = ? AND id > ? AND deleted_at IS NULL
                      AND author_user_id != ? AND parent_message_id IS NULL
                    """,
                    (like, bcast_like, ch["id"], last, user_id),
                ).fetchone()
                out[ch["slug"]] = {
                    "unread": int(row["unread"] or 0),
                    "mentions": int(row["mentions"] or 0),
                }
        return out

    # ─── Direct messages (conversations) ──────────────────────────────────────
    # A DM is a private two-person conversation. Its messages live in
    # ``community_messages`` with ``channel_id`` set to the DM id (``dm-…``),
    # so the whole message pipeline — PHI gate, reactions, edit/delete, soft
    # delete, read cursors, audit — is shared with channels. VISIBILITY is the
    # caller's job: the router checks participant membership on every path
    # that can reach a message by id.
    def get_or_create_dm(self, user_x: str, user_y: str) -> Dict[str, Any]:
        if user_x == user_y:
            raise ValueError("cannot open a conversation with yourself")
        a, b = sorted([user_x, user_y])
        dm_id = "dm-" + uuid.uuid4().hex[:16]
        with self._conn() as conn:
            # Race-safe get-or-create: two simultaneous opens both reach the
            # INSERT; ON CONFLICT DO NOTHING lets the loser fall through to
            # the SELECT instead of raising a UNIQUE violation.
            conn.execute(
                "INSERT INTO community_dms (id, user_a, user_b, created_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_a, user_b) DO NOTHING",
                (dm_id, a, b, _utcnow_iso()),
            )
            row = conn.execute(
                "SELECT * FROM community_dms WHERE user_a = ? AND user_b = ?", (a, b)
            ).fetchone()
        return dict(row)

    def get_dm(self, dm_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM community_dms WHERE id = ?", (dm_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_dms_for(self, user_id: str) -> List[Dict[str, Any]]:
        """The user's conversations, most-recent-activity first, each with the
        peer id, the last live message id/time, and the unread count (messages
        past the user's read cursor, authored by the peer, not deleted)."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT d.*,
                       (SELECT MAX(m.id) FROM community_messages m
                         WHERE m.channel_id = d.id AND m.deleted_at IS NULL) AS last_message_id,
                       (SELECT m2.created_at FROM community_messages m2
                         WHERE m2.channel_id = d.id AND m2.deleted_at IS NULL
                         ORDER BY m2.id DESC LIMIT 1) AS last_message_at,
                       COALESCE((SELECT r.last_read_message_id FROM community_reads r
                         WHERE r.user_id = ? AND r.channel_id = d.id), 0) AS cursor
                FROM community_dms d
                WHERE d.user_a = ? OR d.user_b = ?
                """,
                (user_id, user_id, user_id),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["peer_user_id"] = d["user_b"] if d["user_a"] == user_id else d["user_a"]
                unread_row = conn.execute(
                    "SELECT COUNT(*) AS n FROM community_messages "
                    "WHERE channel_id = ? AND id > ? AND deleted_at IS NULL AND author_user_id != ?",
                    (d["id"], int(d["cursor"] or 0), user_id),
                ).fetchone()
                d["unread"] = int(unread_row["n"] or 0)
                d.pop("cursor", None)
                out.append(d)
        out.sort(key=lambda d: (d.get("last_message_id") or 0), reverse=True)
        return out

    def dm_unread_total(self, user_id: str) -> int:
        return sum(d["unread"] for d in self.list_dms_for(user_id))

    # ─── Search ───────────────────────────────────────────────────────────────
    def search_messages(
        self, query: str, *, channel_ids: List[str], limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Search ONLY within the given channel/DM ids — the caller passes the
        set the user is allowed to see (public channels + their own DMs), so a
        query can never surface someone else's direct messages."""
        q = (query or "").strip()
        if not q or not channel_ids:
            return []
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        qmarks = ",".join("?" * len(channel_ids))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM community_messages WHERE deleted_at IS NULL "
                f"AND channel_id IN ({qmarks}) "
                f"AND body LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT ?",
                (*channel_ids, like, max(1, min(int(limit or 50), 100))),
            ).fetchall()
        return [self._message_row(r) for r in rows]

    # ─── Attachments ──────────────────────────────────────────────────────────
    def insert_attachment(
        self,
        *,
        asset_id: str,
        sha256: str,
        mime: str,
        byte_size: int,
        orig_name: Optional[str],
        uploader_user_id: str,
    ) -> Dict[str, Any]:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO community_attachments
                    (asset_id, sha256, mime, byte_size, orig_name, uploader_user_id, message_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (asset_id, sha256, mime, byte_size, orig_name, uploader_user_id, _utcnow_iso()),
            )
        return self.get_attachment(asset_id)  # type: ignore[return-value]

    def get_attachment(self, asset_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM community_attachments WHERE asset_id = ?", (asset_id,)
            ).fetchone()
        return dict(row) if row else None

    # ─── Notification queue (email digests, PRD §4) ───────────────────────────
    def enqueue_notification(self, *, user_id: str, kind: str, message_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO community_notifications (user_id, kind, message_id, created_at) VALUES (?, ?, ?, ?)",
                (user_id, kind, int(message_id), _utcnow_iso()),
            )

    def unsent_notifications(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM community_notifications WHERE emailed_at IS NULL ORDER BY user_id, id ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_notifications_sent(self, ids: List[int]) -> None:
        if not ids:
            return
        qmarks = ",".join("?" * len(ids))
        with self._conn() as conn:
            conn.execute(
                f"UPDATE community_notifications SET emailed_at = ? WHERE id IN ({qmarks})",
                [_utcnow_iso(), *ids],
            )

    # ─── Moderation: community-scoped bans (PRD §7.5) ─────────────────────────
    # "Deactivate a member" is community-scoped by design: it removes community
    # access without touching the evaluation account (PRD §0 — additive, the
    # evaluation flow is untouched).
    def ban_member(self, *, user_id: str, banned_by: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO community_bans (user_id, banned_by, created_at) VALUES (?, ?, ?)",
                (user_id, banned_by, _utcnow_iso()),
            )

    def unban_member(self, user_id: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM community_bans WHERE user_id = ?", (user_id,))

    def is_banned(self, user_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM community_bans WHERE user_id = ?", (user_id,)
            ).fetchone()
        return bool(row)

    # ── Generated-content purge ──────────────────────────────────────────

    def purge_generated_content(
        self, *, valid_user_ids: Optional[List[str]] = None
    ) -> Dict[str, int]:
        """Hard-delete every machine-authored post so a community can start
        empty: everything by the ``u-system`` bot (news digests, welcomes) and,
        when ``valid_user_ids`` is given, everything by an author id with no
        account in the users plane (demo-seeded doctors). Replies nested under
        a purged message go with it, because a human answer to a deleted bot
        post is context-free noise. Channels, DMs and human top-level posts
        survive, as do the read markers and email prefs of members who still
        have an account.

        When ``valid_user_ids`` is given, the read markers and email prefs of
        authors with no account go too: they are per-user rows pointing at
        users who do not exist, left behind by the same synthetic traffic.

        Hard delete, not the soft delete moderation uses: these rows are
        synthetic and carry no audit value, and the point of the purge is that
        they stop existing. Attachment blobs may be orphaned on disk; only the
        rows are removed here.
        """
        valid = set(valid_user_ids) if valid_user_ids is not None else None
        counts = {"messages": 0, "reactions": 0, "pins": 0,
                  "notifications": 0, "attachments": 0,
                  "reads": 0, "email_prefs": 0}
        with self._conn() as conn:
            authors = [
                r["author_user_id"]
                for r in conn.execute(
                    "SELECT DISTINCT author_user_id FROM community_messages"
                ).fetchall()
            ]
            targets = {
                a for a in authors
                if a == "u-system" or (valid is not None and a not in valid)
            }
            if valid is not None:
                # Keyed off the rows themselves rather than off message authors:
                # a ghost's read markers outlive the messages that revealed them,
                # so deriving the sweep from `targets` would strand them once the
                # posts are gone. This way a second run finishes what a first left.
                for table, key in (
                    ("community_reads", "reads"),
                    ("community_email_prefs", "email_prefs"),
                ):
                    ghosts = [
                        r["user_id"]
                        for r in conn.execute(
                            f"SELECT DISTINCT user_id FROM {table}"
                        ).fetchall()
                        if r["user_id"] != "u-system" and r["user_id"] not in valid
                    ]
                    if not ghosts:
                        continue
                    ghost_marks = ",".join("?" for _ in ghosts)
                    cur = conn.execute(
                        f"DELETE FROM {table} WHERE user_id IN ({ghost_marks})",
                        tuple(ghosts),
                    )
                    counts[key] = cur.rowcount
            if not targets:
                return counts
            marks = ",".join("?" for _ in targets)
            ids = [
                r["id"]
                for r in conn.execute(
                    f"SELECT id FROM community_messages WHERE author_user_id IN ({marks})",
                    tuple(targets),
                ).fetchall()
            ]
            if ids:
                id_marks = ",".join("?" for _ in ids)
                ids_t = tuple(ids)
                # Threads are one level deep, so a single parent sweep is total.
                child_ids = [
                    r["id"]
                    for r in conn.execute(
                        f"SELECT id FROM community_messages WHERE parent_message_id IN ({id_marks})",
                        ids_t,
                    ).fetchall()
                ]
                ids = list(dict.fromkeys(ids + child_ids))
                id_marks = ",".join("?" for _ in ids)
                ids_t = tuple(ids)
                for table, key in (
                    ("community_reactions", "reactions"),
                    ("community_pins", "pins"),
                    ("community_notifications", "notifications"),
                    ("community_attachments", "attachments"),
                ):
                    cur = conn.execute(
                        f"DELETE FROM {table} WHERE message_id IN ({id_marks})", ids_t
                    )
                    counts[key] = cur.rowcount
                cur = conn.execute(
                    f"DELETE FROM community_messages WHERE id IN ({id_marks})", ids_t
                )
                counts["messages"] = cur.rowcount
        return counts

    # ── Email preferences ────────────────────────────────────────────────

    def email_prefs(self, user_id: str) -> Dict[str, Any]:
        """Preferences for one member, created on first read.

        Lazily written rather than defaulted in Python so the unsubscribe token
        exists the moment anyone could receive an email carrying it.
        """
        import secrets as _secrets  # noqa: PLC0415

        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM community_email_prefs WHERE user_id = ?", (user_id,)
            ).fetchone()
            if row:
                return dict(row)
            token = _secrets.token_urlsafe(32)
            conn.execute(
                "INSERT OR IGNORE INTO community_email_prefs "
                "(user_id, news_frequency, unsubscribe_token, updated_at) VALUES (?,?,?,?)",
                (user_id, DEFAULT_NEWS_FREQUENCY, token, _utcnow_iso()),
            )
            row = conn.execute(
                "SELECT * FROM community_email_prefs WHERE user_id = ?", (user_id,)
            ).fetchone()
            return dict(row)

    def set_news_frequency(self, user_id: str, frequency: str) -> Dict[str, Any]:
        if frequency not in NEWS_FREQUENCIES:
            raise ValueError(f"unknown frequency {frequency!r}")
        self.email_prefs(user_id)  # ensure the row and its token exist
        with self._conn() as conn:
            conn.execute(
                "UPDATE community_email_prefs SET news_frequency = ?, updated_at = ? "
                "WHERE user_id = ?",
                (frequency, _utcnow_iso(), user_id),
            )
        return self.email_prefs(user_id)

    def unsubscribe_by_token(self, token: str) -> Optional[str]:
        """One click, no sign-in. Returns the user_id it turned off, or None.

        Deliberately does NOT require a session: an unsubscribe link that makes
        someone log in first is an unsubscribe link that gets a spam complaint
        instead, and one complaint costs the sending domain that every other
        physician's mail goes through.
        """
        tok = (token or "").strip()
        if not tok:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT user_id FROM community_email_prefs WHERE unsubscribe_token = ?",
                (tok,),
            ).fetchone()
            if not row:
                return None
            # One click stops EVERY non-transactional email, not only the news
            # cadence. The member pressed a button that said "stop these"; if
            # the 5-minute activity digest kept arriving afterwards the button
            # was a lie, and the next click is the spam button.
            conn.execute(
                "UPDATE community_email_prefs "
                "SET news_frequency = 'off', activity_emails = 0, updated_at = ? "
                "WHERE unsubscribe_token = ?",
                (_utcnow_iso(), tok),
            )
            return row["user_id"]

    def set_activity_emails(self, user_id: str, enabled: bool) -> Dict[str, Any]:
        """Turn mention/DM/broadcast/announcement digests on or off.

        Separate from ``set_news_frequency`` so a member can keep being told
        they were mentioned while taking no news at all.
        """
        self.email_prefs(user_id)  # ensure the row and its token exist
        with self._conn() as conn:
            conn.execute(
                "UPDATE community_email_prefs SET activity_emails = ?, updated_at = ? "
                "WHERE user_id = ?",
                (1 if enabled else 0, _utcnow_iso(), user_id),
            )
        return self.email_prefs(user_id)

    def wants_activity_email(self, user_id: str) -> bool:
        """Whether this member still takes activity digests. Absence of a row
        means yes (the prefs row is created lazily on first read, and a member
        who has never been emailed has not opted out of anything)."""
        prefs = self.email_prefs(user_id)
        raw = prefs.get("activity_emails")
        return True if raw is None else bool(int(raw))

    # ─── Paid-search budget ──────────────────────────────────────────────────
    def search_calls_today(self, provider: str, *, day: Optional[str] = None) -> int:
        d = day or _utcnow_iso()[:10]
        with self._conn() as conn:
            row = conn.execute(
                "SELECT calls FROM community_search_budget WHERE day = ? AND provider = ?",
                (d, provider),
            ).fetchone()
            return int(row["calls"]) if row else 0

    def claim_search_call(
        self, provider: str, *, cap: int, day: Optional[str] = None
    ) -> bool:
        """Reserve one paid search call against today's cap.

        Returns True if the call may proceed. The increment and the check are
        one statement under the store's own lock, so two concurrent morning
        scopes cannot both read "one left" and both spend it.

        A cap of 0 or less means unlimited, which is what a deployment that has
        not opted into paid search wants: the providers are keyless there and
        never called anyway.
        """
        d = day or _utcnow_iso()[:10]
        if cap is None or int(cap) <= 0:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO community_search_budget (day, provider, calls) "
                    "VALUES (?, ?, 1) ON CONFLICT(day, provider) DO UPDATE SET "
                    "calls = calls + 1",
                    (d, provider),
                )
            return True
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO community_search_budget (day, provider, calls) "
                "VALUES (?, ?, 1) ON CONFLICT(day, provider) DO UPDATE SET "
                "calls = calls + 1 WHERE community_search_budget.calls < ? "
                "RETURNING calls",
                (d, provider, int(cap)),
            ).fetchone()
            return cur is not None

    def news_email_recipients(self, *, weekly: bool) -> List[Dict[str, Any]]:
        """Members due a news email on this run.

        A weekly run takes 'weekly' only; a daily run takes 'daily' only. 'off'
        is never included, and members with no row yet are handled by the caller
        (which reads their prefs, creating the row and its token).
        """
        want = "weekly" if weekly else "daily"
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM community_email_prefs WHERE news_frequency = ?", (want,)
            ).fetchall()
            return [dict(r) for r in rows]

    def banned_user_ids(self) -> List[str]:
        with self._conn() as conn:
            rows = conn.execute("SELECT user_id FROM community_bans").fetchall()
        return [r["user_id"] for r in rows]

    # ─── PHI block events (counters only — §7.5) ──────────────────────────────
    def record_block_event(self, *, user_id: str, surface: str, categories: List[str]) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO community_block_events (user_id, surface, categories_json, created_at) VALUES (?, ?, ?, ?)",
                (user_id, surface, json.dumps(sorted(categories or [])), _utcnow_iso()),
            )

    def block_count(self, user_id: str) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM community_block_events WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return int(row["n"] or 0)

    def flagged_users(self, *, threshold: int,
                      exclude_user_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Users whose lifetime block count meets the admin-flag threshold
        (PRD §7.5). Counts only — never content. ``exclude_user_ids`` keeps
        non-member authors (the system bot) off the moderation surface — its
        blocks are surfaced through the digest-run ledger instead."""
        excl = list(exclude_user_ids or [])
        qmarks = ",".join("?" * len(excl)) or "''"
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT user_id, COUNT(*) AS n, MAX(created_at) AS last_at "
                f"FROM community_block_events WHERE user_id NOT IN ({qmarks}) "
                f"GROUP BY user_id HAVING n >= ? ORDER BY n DESC",
                (*excl, max(1, int(threshold))),
            ).fetchall()
        return [
            {"user_id": r["user_id"], "block_count": int(r["n"]), "last_block_at": r["last_at"]}
            for r in rows
        ]

    # ─── Content items (Community v2 — #medical-ai-news digest pipeline) ──────
    def upsert_content_items(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Insert fetched items, deduplicating persistently: an item whose
        ``url_norm`` already exists is skipped (UNIQUE), and so is one whose
        ``title_norm`` matches any row fetched in the last 14 days (the same
        story syndicated under a different URL). Returns the FRESH rows only."""
        fresh: List[Dict[str, Any]] = []
        now = _utcnow_iso()
        # Cutoff computed in Python so the comparison is same-format ISO-Z
        # against ``fetched_at`` (sqlite datetime('now') renders without the
        # 'T'/'Z' and breaks lexicographic comparison at the boundary).
        title_cutoff = (datetime.utcnow() - timedelta(days=14)) \
            .replace(microsecond=0).isoformat() + "Z"
        with self._conn() as conn:
            for it in items:
                dup = conn.execute(
                    "SELECT 1 FROM community_content_items "
                    "WHERE title_norm = ? AND fetched_at >= ? LIMIT 1",
                    (it["title_norm"], title_cutoff),
                ).fetchone()
                if dup:
                    continue
                cur = conn.execute(
                    """
                    INSERT INTO community_content_items
                        (source, external_id, url, url_norm, title, title_norm,
                         published_at, abstract, status, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)
                    ON CONFLICT(url_norm) DO NOTHING
                    """,
                    (
                        it["source"], it.get("external_id"), it["url"], it["url_norm"],
                        it["title"], it["title_norm"], it.get("published_at"),
                        it.get("abstract"), now,
                    ),
                )
                if cur.rowcount:
                    fresh.append({**it, "id": cur.lastrowid})
        return fresh

    def new_content_items(self, *, max_age_days: int = 3) -> List[Dict[str, Any]]:
        """All ``status='new'`` items fetched within the window — a failed run
        leaves its items 'new', and the next run MUST pick them up (otherwise
        that day's stories are silently dropped forever and the retry records
        a hollow ok run)."""
        cutoff = (datetime.utcnow() - timedelta(days=max(1, int(max_age_days)))) \
            .replace(microsecond=0).isoformat() + "Z"
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM community_content_items WHERE status = 'new' "
                "AND fetched_at >= ? ORDER BY id ASC",
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]

    def candidate_items_for_spotlight(
        self, *, max_age_days: int = 7, limit: int = 40,
    ) -> List[Dict[str, Any]]:
        """The pool the daily staff spotlight picks its one story from.

        ``skipped`` rows count, and that is the whole reason this is a separate
        query. The news digest marks everything it did not publish as
        ``skipped``, so a spotlight that only read ``status='new'`` would find
        an empty pool on every day the digest happened to run first: the
        spotlight would work or starve depending on the order two jobs fired
        in, which is the kind of bug that looks like "quiet week".

        Ranked by the relevance the digest's curation pass already scored, so
        the story the team reads is the best one available rather than the
        oldest.
        """
        cutoff = (datetime.utcnow() - timedelta(days=max(1, int(max_age_days)))) \
            .replace(microsecond=0).isoformat() + "Z"
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM community_content_items "
                "WHERE status IN ('new', 'skipped') AND fetched_at >= ? "
                "ORDER BY COALESCE(relevance, 0) DESC, id DESC LIMIT ?",
                (cutoff, max(1, int(limit))),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_content_items(
        self, ids: List[int], *, status: str, posted_message_id: Optional[int] = None,
        summaries: Optional[Dict[int, Dict[str, Any]]] = None,
    ) -> None:
        if not ids:
            return
        with self._conn() as conn:
            for iid in ids:
                s = (summaries or {}).get(iid) or {}
                conn.execute(
                    "UPDATE community_content_items SET status = ?, posted_message_id = ?, "
                    "summary = COALESCE(?, summary), relevance = COALESCE(?, relevance) "
                    "WHERE id = ?",
                    (status, posted_message_id, s.get("summary"), s.get("relevance"), iid),
                )

    # ─── Digest runs (three-outcome: ok NULL=running / 1 / 0) ─────────────────
    def start_digest_run(self, kind: str) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO community_digest_runs (kind, started_at) VALUES (?, ?)",
                (kind, _utcnow_iso()),
            )
            return int(cur.lastrowid)

    def finish_digest_run(
        self, run_id: int, *, ok: bool, items_fetched: int = 0,
        items_posted: int = 0, error: Optional[str] = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE community_digest_runs SET finished_at = ?, ok = ?, "
                "items_fetched = ?, items_posted = ?, error = ? WHERE id = ?",
                (_utcnow_iso(), 1 if ok else 0, items_fetched, items_posted,
                 (error or None), run_id),
            )

    def last_successful_run_at(self, kind: str) -> Optional[str]:
        """Started-at of the newest ok run — the restart-safe schedule marker."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT started_at FROM community_digest_runs "
                "WHERE kind = ? AND ok = 1 ORDER BY id DESC LIMIT 1",
                (kind,),
            ).fetchone()
        return row["started_at"] if row else None

    def last_run_attempt_at(self, kind: str) -> Optional[str]:
        """Started-at of the newest run of ANY outcome — drives the scheduler's
        failure backoff (a failing digest must not retry every tick all day)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT started_at FROM community_digest_runs "
                "WHERE kind = ? ORDER BY id DESC LIMIT 1",
                (kind,),
            ).fetchone()
        return row["started_at"] if row else None

    def consecutive_digest_failures(self, kind: str) -> int:
        """Failed runs since the last success (running rows ignored)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT ok FROM community_digest_runs WHERE kind = ? AND ok IS NOT NULL "
                "ORDER BY id DESC LIMIT 10",
                (kind,),
            ).fetchall()
        n = 0
        for r in rows:
            if int(r["ok"] or 0) == 1:
                break
            n += 1
        return n

    # ─── Events (Community v2.1) ──────────────────────────────────────────────
    def create_event(
        self, *, channel_id: str, title: str, description: Optional[str],
        starts_at: str, ends_at: Optional[str], timezone: Optional[str],
        location: Optional[str], host: Optional[str], created_by: str,
    ) -> Dict[str, Any]:
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO community_events
                    (channel_id, title, description, starts_at, ends_at, timezone,
                     location, host, created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (channel_id, title, description, starts_at, ends_at, timezone,
                 location, host, created_by, _utcnow_iso()),
            )
            new_id = int(cur.lastrowid)
        # Fetch AFTER the write commits — a fresh connection can't see an
        # uncommitted row under WAL.
        return self.get_event(new_id)  # type: ignore[return-value]

    def link_event_message(self, event_id: int, message_id: int) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE community_events SET message_id = ? WHERE id = ?",
                         (message_id, event_id))

    def get_event(self, event_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM community_events WHERE id = ?", (event_id,)
            ).fetchone()
        return dict(row) if row else None

    def event_for_message(self, message_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM community_events WHERE message_id = ?", (message_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_events(self, channel_id: str, *, scope: str = "upcoming") -> List[Dict[str, Any]]:
        """Upcoming = not cancelled AND starts_at >= now, soonest first; past =
        everything else, most-recent first (cancelled events show as past)."""
        now = _utcnow_iso()
        with self._conn() as conn:
            if scope == "upcoming":
                rows = conn.execute(
                    "SELECT * FROM community_events WHERE channel_id = ? "
                    "AND cancelled_at IS NULL AND starts_at >= ? ORDER BY starts_at ASC",
                    (channel_id, now),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM community_events WHERE channel_id = ? "
                    "AND (cancelled_at IS NOT NULL OR starts_at < ?) "
                    "ORDER BY starts_at DESC",
                    (channel_id, now),
                ).fetchall()
        return [dict(r) for r in rows]

    def latest_upcoming_event(self, channel_id: str) -> Optional[Dict[str, Any]]:
        ups = self.list_events(channel_id, scope="upcoming")
        return ups[0] if ups else None

    def cancel_event(self, event_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            conn.execute(
                "UPDATE community_events SET cancelled_at = ? WHERE id = ? AND cancelled_at IS NULL",
                (_utcnow_iso(), event_id),
            )
        return self.get_event(event_id)

    def toggle_rsvp(self, event_id: int, user_id: str) -> bool:
        """Add the Interested mark if absent, remove if present. Returns True
        when the user is now Interested."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM community_event_rsvps WHERE event_id = ? AND user_id = ?",
                (event_id, user_id),
            ).fetchone()
            if row:
                conn.execute(
                    "DELETE FROM community_event_rsvps WHERE event_id = ? AND user_id = ?",
                    (event_id, user_id),
                )
                return False
            conn.execute(
                "INSERT INTO community_event_rsvps (event_id, user_id, created_at) VALUES (?, ?, ?)",
                (event_id, user_id, _utcnow_iso()),
            )
            return True

    def rsvp_count(self, event_id: int) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM community_event_rsvps WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        return int(row["n"] or 0)

    def is_interested(self, event_id: int, user_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM community_event_rsvps WHERE event_id = ? AND user_id = ?",
                (event_id, user_id),
            ).fetchone()
        return bool(row)

    def events_needing_reminder(self, *, within_minutes: int) -> List[Dict[str, Any]]:
        """Events starting within the window, not cancelled, that have at least
        one interested member who has NOT yet been reminded. Returns
        ``[{event, user_ids:[...]}]``."""
        now = datetime.utcnow()
        horizon = (now + timedelta(minutes=max(1, int(within_minutes)))) \
            .replace(microsecond=0).isoformat() + "Z"
        now_iso = now.replace(microsecond=0).isoformat() + "Z"
        out: List[Dict[str, Any]] = []
        with self._conn() as conn:
            evs = conn.execute(
                "SELECT * FROM community_events WHERE cancelled_at IS NULL "
                "AND starts_at >= ? AND starts_at <= ? ORDER BY starts_at ASC",
                (now_iso, horizon),
            ).fetchall()
            for ev in evs:
                uids = [r["user_id"] for r in conn.execute(
                    "SELECT user_id FROM community_event_rsvps "
                    "WHERE event_id = ? AND reminded_at IS NULL",
                    (ev["id"],),
                ).fetchall()]
                if uids:
                    out.append({"event": dict(ev), "user_ids": uids})
        return out

    def mark_reminded(self, event_id: int, user_ids: List[str]) -> None:
        if not user_ids:
            return
        qmarks = ",".join("?" * len(user_ids))
        with self._conn() as conn:
            conn.execute(
                f"UPDATE community_event_rsvps SET reminded_at = ? "
                f"WHERE event_id = ? AND user_id IN ({qmarks})",
                [_utcnow_iso(), event_id, *user_ids],
            )

    def event_public(self, event: Dict[str, Any], *, viewer_id: Optional[str] = None) -> Dict[str, Any]:
        """API shape for an event row (+ rsvp count and viewer's interest)."""
        eid = event["id"]
        return {
            "id": eid,
            "channel_id": event["channel_id"],
            "message_id": event.get("message_id"),
            "title": event["title"],
            "description": event.get("description"),
            "starts_at": event["starts_at"],
            "ends_at": event.get("ends_at"),
            "timezone": event.get("timezone"),
            "location": event.get("location"),
            "host": event.get("host"),
            "cancelled": bool(event.get("cancelled_at")),
            "rsvp_count": self.rsvp_count(eid),
            "viewer_interested": self.is_interested(eid, viewer_id) if viewer_id else False,
        }

    # ─── Polls (Community v2.1) ───────────────────────────────────────────────
    def create_poll(
        self, *, channel_id: str, question: str, options: List[str],
        created_by: str, closes_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO community_polls (channel_id, question, created_by, created_at, closes_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (channel_id, question, created_by, _utcnow_iso(), closes_at),
            )
            pid = int(cur.lastrowid)
            for i, opt in enumerate(options):
                conn.execute(
                    "INSERT INTO community_poll_options (poll_id, idx, text) VALUES (?, ?, ?)",
                    (pid, i, opt),
                )
        return self.get_poll(pid)  # type: ignore[return-value]

    def link_poll_message(self, poll_id: int, message_id: int) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE community_polls SET message_id = ? WHERE id = ?",
                         (message_id, poll_id))

    def get_poll(self, poll_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM community_polls WHERE id = ?", (poll_id,)
            ).fetchone()
        return dict(row) if row else None

    def poll_for_message(self, message_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM community_polls WHERE message_id = ?", (message_id,)
            ).fetchone()
        return dict(row) if row else None

    def vote_poll(self, poll_id: int, option_id: int, user_id: str) -> None:
        """Single-choice: an INSERT-or-REPLACE flips the user's vote to the new
        option (one row per (poll, user))."""
        with self._conn() as conn:
            valid = conn.execute(
                "SELECT 1 FROM community_poll_options WHERE id = ? AND poll_id = ?",
                (option_id, poll_id),
            ).fetchone()
            if not valid:
                raise ValueError("option does not belong to poll")
            conn.execute(
                "INSERT INTO community_poll_votes (poll_id, option_id, user_id, created_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(poll_id, user_id) DO UPDATE SET option_id = excluded.option_id, "
                "created_at = excluded.created_at",
                (poll_id, option_id, user_id, _utcnow_iso()),
            )

    def close_poll(self, poll_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE community_polls SET closed_at = ? WHERE id = ? AND closed_at IS NULL",
                (_utcnow_iso(), poll_id),
            )

    def poll_results(self, poll_id: int, *, viewer_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        poll = self.get_poll(poll_id)
        if not poll:
            return None
        with self._conn() as conn:
            opts = conn.execute(
                "SELECT id, idx, text FROM community_poll_options WHERE poll_id = ? ORDER BY idx ASC",
                (poll_id,),
            ).fetchall()
            counts = {r["option_id"]: int(r["n"]) for r in conn.execute(
                "SELECT option_id, COUNT(*) AS n FROM community_poll_votes "
                "WHERE poll_id = ? GROUP BY option_id", (poll_id,),
            ).fetchall()}
            your_vote = None
            if viewer_id:
                vr = conn.execute(
                    "SELECT option_id FROM community_poll_votes WHERE poll_id = ? AND user_id = ?",
                    (poll_id, viewer_id),
                ).fetchone()
                your_vote = vr["option_id"] if vr else None
        total = sum(counts.values())
        return {
            "id": poll_id,
            "question": poll["question"],
            "closed": bool(poll.get("closed_at")),
            "created_by": poll["created_by"],
            "total_votes": total,
            "your_vote": your_vote,
            "options": [
                {"id": o["id"], "text": o["text"], "votes": counts.get(o["id"], 0)}
                for o in opts
            ],
        }

    # ─── Pinned messages (Community v2.1) ─────────────────────────────────────
    def pin_message(self, *, channel_id: str, message_id: int, pinned_by: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO community_pins (channel_id, message_id, pinned_by, created_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(channel_id, message_id) DO NOTHING",
                (channel_id, message_id, pinned_by, _utcnow_iso()),
            )

    def unpin_message(self, *, channel_id: str, message_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM community_pins WHERE channel_id = ? AND message_id = ?",
                (channel_id, message_id),
            )

    def is_pinned(self, message_id: int) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM community_pins WHERE message_id = ?", (message_id,)
            ).fetchone()
        return bool(row)

    def pinned_message_ids(self, message_ids: List[int]) -> set:
        """Which of the given message ids are pinned (batch, for serialization)."""
        if not message_ids:
            return set()
        qmarks = ",".join("?" * len(message_ids))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT message_id FROM community_pins WHERE message_id IN ({qmarks})",
                message_ids,
            ).fetchall()
        return {r["message_id"] for r in rows}

    def list_pins(self, channel_id: str) -> List[Dict[str, Any]]:
        """Pinned messages of a channel, newest pin first, as message rows."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT p.message_id FROM community_pins p WHERE p.channel_id = ? "
                "ORDER BY p.created_at DESC", (channel_id,),
            ).fetchall()
        out = []
        for r in rows:
            msg = self.get_message(r["message_id"])
            if msg and not msg.get("deleted"):
                out.append(msg)
        return out

    # ─── Channel bookmarks (Community v2.1) ───────────────────────────────────
    def add_bookmark(self, *, channel_id: str, title: str, url: str, added_by: str) -> Dict[str, Any]:
        with self._conn() as conn:
            pos_row = conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 AS p FROM community_bookmarks WHERE channel_id = ?",
                (channel_id,),
            ).fetchone()
            cur = conn.execute(
                "INSERT INTO community_bookmarks (channel_id, title, url, added_by, position, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (channel_id, title, url, added_by, int(pos_row["p"]), _utcnow_iso()),
            )
            bid = int(cur.lastrowid)
            row = conn.execute("SELECT * FROM community_bookmarks WHERE id = ?", (bid,)).fetchone()
        return dict(row)

    def get_bookmark(self, bookmark_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM community_bookmarks WHERE id = ?", (bookmark_id,)
            ).fetchone()
        return dict(row) if row else None

    def remove_bookmark(self, bookmark_id: int) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM community_bookmarks WHERE id = ?", (bookmark_id,))

    def list_bookmarks(self, channel_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM community_bookmarks WHERE channel_id = ? ORDER BY position ASC, id ASC",
                (channel_id,),
            ).fetchall()
        return [dict(r) for r in rows]


# ─── Process-wide singleton ───────────────────────────────────────────────────
_store_lock = threading.Lock()
_store: Optional[CommunityStore] = None


def get_community_store() -> CommunityStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = CommunityStore()
    return _store


def reset_community_store_for_tests(db_path: Optional[str] = None) -> CommunityStore:
    """Rebind the singleton to a fresh DB (mirrors ``reset_store_for_tests``)."""
    global _store
    with _store_lock:
        _store = CommunityStore(db_path=db_path)
    return _store
