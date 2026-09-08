"""Recoverable lifecycle and dedicated worker for bulk originals."""
import hashlib
import json
import time
import uuid

from asclepius.media_store import MediaError


def declare_for_account(store, accounts, user, body):
    account = accounts.get_hs_portal_user(user["username"])
    policy = (account or {}).get("purpose") or "storage"
    if policy not in ("storage", "brokering", "task_creation"):
        policy = "storage"
    return store.declare(user["hs_id"], user["username"], body["collection"],
        body["token"], body["path"], body["size"], body.get("sha256"), policy, body.get("source_info"))


def public(row):
    return {k: row.get(k) for k in ("id", "collection", "path", "size", "chunk_size",
        "part_count", "state", "sha256", "inspection", "created")}


def initialize(store, storage, row):
    # Serialized with declarations, cancellation and reservations for this org.
    # An uncertain S3 create can leave an unreferenced MPU, never an original;
    # the explicit orphan reconciler removes these after seven days. Do not set
    # an age-based bucket MPU lifecycle: it would destroy active long transfers.
    with store.transaction(row["org"]) as q:
        raw = q("SELECT data FROM media_files WHERE scope=? AND org=? AND id=?", (store.scope, row["org"], row["id"])).fetchone()
        row = json.loads(raw["data"])
        if row["state"] == "initiating":
            storage.readiness()
            row.update(upload_id=storage.create(row), state="importing" if row.get("source") else "uploading")
            q("UPDATE media_files SET state=?,updated=?,data=? WHERE scope=? AND org=? AND id=?", (row["state"], time.time(), json.dumps(row), store.scope, row["org"], row["id"]))
    return row


def tick(store, storage):
    """One durable work item. Multiple workers use fenced leases, not RAM jobs."""
    now = time.time()
    with store.transaction() as q:
        raw = q("SELECT * FROM media_files WHERE scope=? AND (state IN ('completing','verifying','cancelling','importing') OR (state IN ('uploading','initiating') AND updated<?)) AND lease<? ORDER BY updated LIMIT 1", (store.scope, now-7*86400, now)).fetchone()
        if not raw:
            return False
    with store.transaction(raw["org"]) as q:
        raw = q("SELECT * FROM media_files WHERE scope=? AND org=? AND id=?", (store.scope, raw["org"], raw["id"])).fetchone()
        row = json.loads(raw["data"])
        if raw["lease"] >= now or row["state"] not in ("completing", "verifying", "cancelling", "uploading", "initiating", "importing"):
            return False
        if row["state"] in ("uploading", "initiating") and raw["updated"] > now-7*86400:
            return False
        if row["state"] in ("uploading", "initiating"):
            row["state"] = "cancelling"
        updated = q("UPDATE media_files SET lease=?,state=?,data=? WHERE scope=? AND org=? AND id=? AND lease<? AND updated=?", (now+120, row["state"], json.dumps(row), store.scope, row["org"], row["id"], now, raw["updated"]))
        if updated.rowcount != 1:
            return False
        lease = now+120

    def save(**fields):
        nonlocal lease
        with store.transaction(row["org"]) as q:
            current = q("SELECT lease,state FROM media_files WHERE scope=? AND org=? AND id=?", (store.scope, row["org"], row["id"])).fetchone()
            if current["lease"] != lease:
                raise MediaError("Worker lease lost.")
            row.update(fields)
            new_lease = time.time()+120
            q("UPDATE media_files SET state=?,updated=?,lease=?,data=? WHERE scope=? AND org=? AND id=?", (row["state"], time.time(), new_lease, json.dumps(row), store.scope, row["org"], row["id"]))
            lease = new_lease

    def failure(exc):
        attempts = row.get("attempts", 0)+1
        fields = dict(attempts=attempts, error="transfer_failed")
        if attempts >= 10:
            fields.update(state="attention_required", retry_state=row["state"])
        try:
            save(**fields)
        except MediaError:
            pass  # Another worker or cancellation owns the row now.

    try:
        if row["state"] in ("cancelling", "uploading", "initiating"):
            if row.get("upload_id"):
                storage.abort(row)
            with store.transaction(row["org"]) as q:
                changed = q("UPDATE media_files SET state='cancelled',data=?,updated=? WHERE scope=? AND org=? AND id=? AND lease=?", (json.dumps({**row, "state": "cancelled"}), time.time(), store.scope, row["org"], row["id"], lease))
                if changed.rowcount == 1:
                    q("UPDATE media_orgs SET bytes=bytes-? WHERE scope=? AND org=?", (row["size"], store.scope, row["org"]))
                    q("UPDATE media_collections SET bytes=bytes-?,files=files-1 WHERE scope=? AND org=? AND id=?", (row["size"], store.scope, row["org"], row["collection"]))
            return True
        if row["state"] == "importing":
            from asclepius.media_sources import copy_parts
            copy_parts(storage, row, save)
            save(state="completing")
        if row["state"] == "completing":
            head = storage.complete(row)
            save(state="verifying", version=head["VersionId"], storage_checksum=head.get("ChecksumSHA256"), storage_checksum_type="COMPOSITE")
        digest, size, heartbeat = hashlib.sha256(), 0, time.time()
        for chunk in storage.chunks(row):
            digest.update(chunk)
            size += len(chunk)
            if time.time()-heartbeat > 30:
                save()
                heartbeat = time.time()
        sha = digest.hexdigest()
        if size != row["size"] or (row.get("expected_sha256") and row["expected_sha256"] != sha):
            save(state="integrity_failed")
        else:
            # Inspection and commercial release are distinct gates. No parser,
            # model call or clinical task creation is reachable from this worker.
            save(state="stored", sha256=sha, checksum_type="FULL_OBJECT")
    except MediaError as exc:
        # Missing parts can be fixed; an uncertain remote completion is retried
        # through HEAD by the next worker after this lease expires.
        if row["state"] == "completing":
            save(state="uploading" if exc.status == 409 and not row.get("source") else "integrity_failed")
        else:
            failure(exc)
            raise
    except Exception as exc:
        failure(exc)
        raise
    return True


def reap_orphans(store, storage):
    """Only unreferenced multipart fragments, never completed object versions.

    A crashed create leaves an MPU outside the DB transaction. Tracked active
    uploads have no age ceiling; their inactivity is handled by tick instead.
    """
    count = 0
    for page in storage.client.get_paginator("list_multipart_uploads").paginate(Bucket=storage.bucket, Prefix=f"media/{store.scope}/"):
        for upload in page.get("Uploads", []):
            if upload["Initiated"].timestamp() > time.time()-7*86400:
                continue
            fid = upload["Key"].rsplit("/", 1)[-1]
            with store.transaction() as q:
                raw = q("SELECT data FROM media_files WHERE scope=? AND id=?", (store.scope, fid)).fetchone()
                row = json.loads(raw["data"]) if raw else None
                if row and row.get("upload_id") == upload["UploadId"] and row["state"] != "cancelled":
                    continue
                storage.client.abort_multipart_upload(Bucket=storage.bucket, Key=upload["Key"], UploadId=upload["UploadId"])
                count += 1
    return count
