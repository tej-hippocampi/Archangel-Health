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
]


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
            if "kind" not in cols("community_messages"):
                conn.execute("ALTER TABLE community_messages ADD COLUMN kind TEXT")

    # ─── Channels ─────────────────────────────────────────────────────────────
    def ensure_default_channels(self) -> None:
        """Idempotently seed the fixed channels (PRD §3 + Community v2): the
        core set plus one channel per enabled specialty. A slug removed from
        the config is DEACTIVATED, never deleted — its history stays in the DB
        and moderation/audit paths can still resolve it."""
        seeded = DEFAULT_CHANNELS + specialty_channel_defs()
        with self._conn() as conn:
            for pos, ch in enumerate(seeded):
                conn.execute(
                    """
                    INSERT INTO community_channels
                        (id, slug, name, description, post_policy, position,
                         specialty, grp, is_active, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                    ON CONFLICT(slug) DO UPDATE SET
                        name = excluded.name,
                        description = excluded.description,
                        post_policy = excluded.post_policy,
                        position = excluded.position,
                        specialty = excluded.specialty,
                        grp = excluded.grp,
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
                        _utcnow_iso(),
                    ),
                )
            qmarks = ",".join("?" * len(seeded))
            conn.execute(
                f"UPDATE community_channels SET is_active = 0 WHERE slug NOT IN ({qmarks})",
                [ch["slug"] for ch in seeded],
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
    ) -> Dict[str, Any]:
        now = _utcnow_iso()
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO community_messages
                    (channel_id, author_user_id, parent_message_id, body,
                     mentions_json, attachments_json, kind, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    channel_id,
                    author_user_id,
                    parent_message_id,
                    body,
                    json.dumps(mentions or []),
                    "[]",
                    kind,
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
        out: Dict[str, Dict[str, int]] = {}
        with self._conn() as conn:
            for ch in (channels if channels is not None else self.list_channels()):
                last = cursors.get(ch["id"], 0)
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS unread,
                           COALESCE(SUM(CASE WHEN mentions_json LIKE ? ESCAPE '\\'
                                             THEN 1 ELSE 0 END), 0) AS mentions
                    FROM community_messages
                    WHERE channel_id = ? AND id > ? AND deleted_at IS NULL
                      AND author_user_id != ? AND parent_message_id IS NULL
                    """,
                    (like, ch["id"], last, user_id),
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
