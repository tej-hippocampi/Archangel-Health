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
from datetime import datetime
from typing import Any, Dict, List, Optional

# Fixed v1 channel set (PRD §3). No user-created channels.
DEFAULT_CHANNELS = [
    {
        "slug": "general",
        "name": "general",
        "description": "Open discussion between contributor physicians.",
        "post_policy": "all",
    },
    {
        "slug": "task-announcements",
        "name": "task-announcements",
        "description": "New task batches, specialty calls, deadlines. Posts from the Archangel team; replies open in threads.",
        "post_policy": "admin",
    },
    {
        "slug": "questions-help",
        "name": "questions-help",
        "description": "Ask anything about a case, a rubric, or a payout.",
        "post_policy": "all",
    },
]


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
                """
            )

    # ─── Channels ─────────────────────────────────────────────────────────────
    def ensure_default_channels(self) -> None:
        """Idempotently seed the fixed v1 channels (PRD §3)."""
        with self._conn() as conn:
            for pos, ch in enumerate(DEFAULT_CHANNELS):
                conn.execute(
                    """
                    INSERT INTO community_channels (id, slug, name, description, post_policy, position, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(slug) DO UPDATE SET
                        name = excluded.name,
                        description = excluded.description,
                        post_policy = excluded.post_policy,
                        position = excluded.position
                    """,
                    (
                        "ch-" + uuid.uuid4().hex[:12],
                        ch["slug"],
                        ch["name"],
                        ch["description"],
                        ch["post_policy"],
                        pos,
                        _utcnow_iso(),
                    ),
                )

    def list_channels(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM community_channels ORDER BY position ASC"
            ).fetchall()
        return [dict(r) for r in rows]

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
    ) -> Dict[str, Any]:
        now = _utcnow_iso()
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO community_messages
                    (channel_id, author_user_id, parent_message_id, body,
                     mentions_json, attachments_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    channel_id,
                    author_user_id,
                    parent_message_id,
                    body,
                    json.dumps(mentions or []),
                    json.dumps(attachments or []),
                    now,
                ),
            )
            mid = cur.lastrowid
            # Bind any referenced attachment rows to this message.
            for att in attachments or []:
                if att.get("asset_id"):
                    conn.execute(
                        "UPDATE community_attachments SET message_id = ? WHERE asset_id = ?",
                        (mid, att["asset_id"]),
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
    ) -> List[Dict[str, Any]]:
        """One page of TOP-LEVEL messages, ascending id. ``before_id`` pages
        history upward (infinite scroll); ``after_id`` serves the polling
        fallback. Deleted messages are included only when they still anchor a
        live thread (the client renders a tombstone)."""
        limit = max(1, min(int(limit or 50), 200))
        clauses = ["channel_id = ?", "parent_message_id IS NULL"]
        params: List[Any] = [channel_id]
        if before_id is not None:
            clauses.append("id < ?")
            params.append(int(before_id))
        if after_id is not None:
            clauses.append("id > ?")
            params.append(int(after_id))
        sql = (
            "SELECT * FROM community_messages WHERE "
            + " AND ".join(clauses)
            + " ORDER BY id DESC LIMIT ?"
        )
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        out = [self._message_row(r) for r in rows][::-1]
        live = [m for m in out if not m["deleted"]]
        deleted_ids = [m["id"] for m in out if m["deleted"]]
        keep_tombstones = set()
        if deleted_ids:
            with self._conn() as conn:
                qmarks = ",".join("?" * len(deleted_ids))
                rows = conn.execute(
                    f"SELECT DISTINCT parent_message_id AS pid FROM community_messages "
                    f"WHERE parent_message_id IN ({qmarks}) AND deleted_at IS NULL",
                    deleted_ids,
                ).fetchall()
            keep_tombstones = {r["pid"] for r in rows}
        return [m for m in out if not m["deleted"] or m["id"] in keep_tombstones]

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

    def unread_counts(self, user_id: str) -> Dict[str, Dict[str, int]]:
        """Per-channel ``{unread, mentions}`` — messages (incl. thread replies)
        past the read cursor, authored by someone else, not deleted."""
        cursors = self.read_cursors(user_id)
        out: Dict[str, Dict[str, int]] = {}
        with self._conn() as conn:
            for ch in self.list_channels():
                last = cursors.get(ch["id"], 0)
                rows = conn.execute(
                    """
                    SELECT id, mentions_json FROM community_messages
                    WHERE channel_id = ? AND id > ? AND deleted_at IS NULL
                      AND author_user_id != ?
                    """,
                    (ch["id"], last, user_id),
                ).fetchall()
                mentions = 0
                for r in rows:
                    try:
                        if user_id in json.loads(r["mentions_json"] or "[]"):
                            mentions += 1
                    except Exception:
                        pass
                out[ch["slug"]] = {"unread": len(rows), "mentions": mentions}
        return out

    # ─── Search ───────────────────────────────────────────────────────────────
    def search_messages(self, query: str, *, limit: int = 50) -> List[Dict[str, Any]]:
        q = (query or "").strip()
        if not q:
            return []
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM community_messages WHERE deleted_at IS NULL "
                "AND body LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT ?",
                (like, max(1, min(int(limit or 50), 100))),
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

    def flagged_users(self, *, threshold: int) -> List[Dict[str, Any]]:
        """Users whose lifetime block count meets the admin-flag threshold
        (PRD §7.5). Counts only — never content."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT user_id, COUNT(*) AS n, MAX(created_at) AS last_at "
                "FROM community_block_events GROUP BY user_id HAVING n >= ? ORDER BY n DESC",
                (max(1, int(threshold)),),
            ).fetchall()
        return [
            {"user_id": r["user_id"], "block_count": int(r["n"]), "last_block_at": r["last_at"]}
            for r in rows
        ]


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
