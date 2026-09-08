"""Durable bulk-media control plane. Originals never enter the clinical store.

Production requires PostgreSQL. SQLite is an explicit test-only adapter; all
mutations lock the organization ledger before testing quotas or changing state.
The file row is also the durable work item, eliminating a dual-write job gap.
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
import uuid

import realm


class MediaError(ValueError):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def enabled():
    return os.getenv("ASCLEPIUS_MEDIA_ENABLED") == "1"


def part_plan(size):
    if not 0 < size <= 1024**4:
        raise MediaError("Files must be between 1 byte and 1 TiB.", 413)
    chunk = max(64 * 1024**2, ((size + 9999) // 10000 + 1024**2 - 1) // 1024**2 * 1024**2)
    return chunk, (size + chunk - 1) // chunk


class MediaStore:
    def __init__(self, dsn, scope):
        self.dsn, self.scope = dsn, scope
        self.sqlite = dsn.startswith("sqlite:///")
        if self.sqlite and os.getenv("ENV") != "test":
            raise RuntimeError("Media metadata requires PostgreSQL outside tests")

    @contextlib.contextmanager
    def transaction(self, org=None):
        if self.sqlite:
            db = sqlite3.connect(self.dsn[10:], timeout=30)
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            execute = db.execute
        else:
            import psycopg
            from psycopg.rows import dict_row
            db = psycopg.connect(self.dsn, row_factory=dict_row,
                **({"sslmode": "require"} if os.getenv("ENV") != "test" else {}))
            execute = lambda sql, args=(): db.execute(sql.replace("?", "%s"), args)
        try:
            if org:
                execute("INSERT INTO media_orgs(scope,org,bytes) VALUES(?,?,0) ON CONFLICT(scope,org) DO NOTHING", (self.scope, org))
                # UPDATE acquires a row lock on PostgreSQL, held until commit.
                execute("UPDATE media_orgs SET bytes=bytes WHERE scope=? AND org=?", (self.scope, org))
            yield execute
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def migrate(self):
        with self.transaction() as q:
            q("CREATE TABLE IF NOT EXISTS media_orgs(scope TEXT NOT NULL,org TEXT NOT NULL,bytes BIGINT NOT NULL,PRIMARY KEY(scope,org))")
            q("CREATE TABLE IF NOT EXISTS media_collections(scope TEXT NOT NULL,org TEXT NOT NULL,id TEXT NOT NULL,bytes BIGINT NOT NULL DEFAULT 0,files BIGINT NOT NULL DEFAULT 0,PRIMARY KEY(scope,org,id))")
            q("CREATE TABLE IF NOT EXISTS media_files(scope TEXT NOT NULL,org TEXT NOT NULL,id TEXT NOT NULL,collection TEXT NOT NULL,actor TEXT NOT NULL,token TEXT NOT NULL,state TEXT NOT NULL,updated DOUBLE PRECISION NOT NULL,lease DOUBLE PRECISION NOT NULL DEFAULT 0,data TEXT NOT NULL,PRIMARY KEY(scope,org,id),UNIQUE(scope,org,actor,collection,token))")
            q("CREATE INDEX IF NOT EXISTS media_catalog ON media_files(scope,org,collection,id)")
            q("CREATE INDEX IF NOT EXISTS media_jobs ON media_files(scope,state,lease,updated)")
            q("CREATE INDEX IF NOT EXISTS media_identity ON media_files(scope,id)")
            q("CREATE TABLE IF NOT EXISTS media_audit(scope TEXT NOT NULL,id TEXT NOT NULL,actor TEXT NOT NULL,event TEXT NOT NULL,data TEXT NOT NULL,created DOUBLE PRECISION NOT NULL,PRIMARY KEY(scope,id))")
            q("CREATE TABLE IF NOT EXISTS media_deliveries(scope TEXT NOT NULL,id TEXT NOT NULL,buyer TEXT NOT NULL,data TEXT NOT NULL,created DOUBLE PRECISION NOT NULL,PRIMARY KEY(scope,id))")
            q("CREATE INDEX IF NOT EXISTS media_buyer_deliveries ON media_deliveries(scope,buyer,id)")

    def collection(self, org):
        cid = uuid.uuid4().hex
        with self.transaction(org) as q:
            q("INSERT INTO media_collections(scope,org,id) VALUES(?,?,?)", (self.scope, org, cid))
        return cid

    def declare(self, org, actor, collection, token, path, size, sha256=None, policy=None, source=None):
        chunk, count = part_plan(size)
        path = path.replace("\\", "/")
        if not path or len(path) > 1024 or path.startswith("/") or any(p in ("", ".", "..") for p in path.split("/")) or any(ord(c) < 32 for c in path):
            raise MediaError("Invalid relative file path.", 422)
        with self.transaction(org) as q:
            existing = q("SELECT data FROM media_files WHERE scope=? AND org=? AND actor=? AND collection=? AND token=?", (self.scope, org, actor, collection, token)).fetchone()
            if existing:
                row = json.loads(existing["data"])
                if (row["path"], row["size"], row.get("expected_sha256")) != (path, size, sha256):
                    raise MediaError("This upload key belongs to a different file.")
                if row.get("source_info") != source:
                    raise MediaError("This upload key belongs to another source.")
                return row
            coll = q("SELECT * FROM media_collections WHERE scope=? AND org=? AND id=?", (self.scope, org, collection)).fetchone()
            if not coll:
                raise MediaError("Collection not found.", 404)
            used = q("SELECT bytes FROM media_orgs WHERE scope=? AND org=?", (self.scope, org)).fetchone()["bytes"]
            if used + size > int(os.getenv("ASCLEPIUS_MEDIA_ORG_BYTES", str(100 * 10**12))) or coll["bytes"] + size > int(os.getenv("ASCLEPIUS_MEDIA_COLLECTION_BYTES", str(100 * 10**12))) or coll["files"] >= 100000:
                raise MediaError("Collection or organization capacity reached.", 429)
            fid = uuid.uuid4().hex
            row = dict(id=fid, collection=collection, org=org, actor=actor, path=path, scope=self.scope,
                       size=size, chunk_size=chunk, part_count=count, state="initiating",
                       key=f"media/{self.scope}/{fid}", expected_sha256=sha256,
                       policy=policy or "storage", retention="explicit_disposition",
                       inspection="pending", release="held", created=time.time())
            if source:
                row.update(source_info=source, source=source["object"], source_id=source["id"], source_etag=source["etag"])
            q("INSERT INTO media_files(scope,org,id,collection,actor,token,state,updated,data) VALUES(?,?,?,?,?,?,?,?,?)", (self.scope, org, fid, collection, actor, token, row["state"], time.time(), json.dumps(row)))
            q("UPDATE media_orgs SET bytes=bytes+? WHERE scope=? AND org=?", (size, self.scope, org))
            q("UPDATE media_collections SET bytes=bytes+?,files=files+1 WHERE scope=? AND org=? AND id=?", (size, self.scope, org, collection))
            return row

    def get(self, org, fid, actor=None):
        with self.transaction() as q:
            raw = q("SELECT data FROM media_files WHERE scope=? AND org=? AND id=?", (self.scope, org, fid)).fetchone()
        if not raw:
            raise MediaError("Upload not found.", 404)
        row = json.loads(raw["data"])
        if actor is not None and row["actor"] != actor:
            raise MediaError("Upload not found.", 404)
        return row

    def change(self, org, fid, states, **fields):
        with self.transaction(org) as q:
            raw = q("SELECT data FROM media_files WHERE scope=? AND org=? AND id=?", (self.scope, org, fid)).fetchone()
            if not raw:
                raise MediaError("Upload not found.", 404)
            row = json.loads(raw["data"])
            if row["state"] not in states:
                raise MediaError("Upload state changed; refresh its receipt.")
            row.update(fields)
            q("UPDATE media_files SET state=?,updated=?,lease=0,data=? WHERE scope=? AND org=? AND id=?", (row["state"], time.time(), json.dumps(row), self.scope, org, fid))
            return row

    def catalog(self, org, collection, after="", limit=100):
        with self.transaction() as q:
            rows = q("SELECT data FROM media_files WHERE scope=? AND org=? AND collection=? AND id>? ORDER BY id LIMIT ?", (self.scope, org, collection, after, min(100, max(1, limit)))).fetchall()
        return [json.loads(r["data"]) for r in rows]


def get_store():
    dsn = os.getenv("ASCLEPIUS_MEDIA_DATABASE_URL", "")
    if not dsn:
        raise MediaError("Bulk uploads are not configured yet.", 503)
    return MediaStore(dsn, realm.current())
