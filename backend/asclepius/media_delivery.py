"""Explicit inspection attestations and immutable, buyer-bound release manifests."""
import json
import time
import uuid

from asclepius.media_store import MediaError


def audit(q, store, actor, event, data):
    q("INSERT INTO media_audit(scope,id,actor,event,data,created) VALUES(?,?,?,?,?,?)", (store.scope, uuid.uuid4().hex, actor, event, json.dumps(data), time.time()))


def review(store, org, fid, actor, evidence, approved):
    with store.transaction(org) as q:
        raw = q("SELECT data FROM media_files WHERE scope=? AND org=? AND id=?", (store.scope, org, fid)).fetchone()
        if not raw:
            raise MediaError("File not found.", 404)
        row = json.loads(raw["data"])
        if row["state"] != "stored":
            raise MediaError("Storage verification must finish before inspection.")
        row.update(inspection="cleared" if approved else "rejected", release="approved" if approved else "held")
        q("UPDATE media_files SET data=? WHERE scope=? AND org=? AND id=?", (json.dumps(row), store.scope, org, fid))
        audit(q, store, actor, "inspection_attested", dict(org=org, file=fid, version=row["version"], approved=approved, evidence=evidence))
    return row


def create(store, org, ids, buyer, actor):
    with store.transaction(org) as q:
        files = []
        for fid in ids:
            raw = q("SELECT data FROM media_files WHERE scope=? AND org=? AND id=?", (store.scope, org, fid)).fetchone()
            if not raw:
                raise MediaError("File not found.", 404)
            row = json.loads(raw["data"])
            if row["state"] != "stored" or row["inspection"] != "cleared" or row["release"] != "approved" or row["policy"] != "brokering":
                raise MediaError("Every file requires verified storage and approved inspection and release.")
            files.append({k: row[k] for k in ("id", "key", "path", "size", "version", "sha256")})
        did = uuid.uuid4().hex
        data = {"id": did, "org": org, "files": files, "checksum_algorithm": "SHA256", "checksum_type": "FULL_OBJECT"}
        q("INSERT INTO media_deliveries(scope,id,buyer,data,created) VALUES(?,?,?,?,?)", (store.scope, did, buyer, json.dumps(data), time.time()))
        audit(q, store, actor, "manifest_created", {"id": did, "buyer": buyer, "files": ids})
    return did


def manifest(store, did, buyer):
    with store.transaction() as q:
        row = q("SELECT data FROM media_deliveries WHERE scope=? AND id=? AND buyer=?", (store.scope, did, buyer)).fetchone()
    if not row:
        raise MediaError("Delivery not found.", 404)
    return json.loads(row["data"])


def retry(store, org, fid, actor):
    with store.transaction(org) as q:
        raw = q("SELECT data FROM media_files WHERE scope=? AND org=? AND id=?", (store.scope, org, fid)).fetchone()
        if not raw:
            raise MediaError("File not found.", 404)
        row = json.loads(raw["data"])
        if row["state"] != "attention_required" or row.get("retry_state") not in ("importing", "completing", "verifying", "cancelling"):
            raise MediaError("This transfer cannot be retried from its current state.")
        row.update(state=row["retry_state"], attempts=0, error=None)
        q("UPDATE media_files SET state=?,data=?,lease=0,updated=? WHERE scope=? AND org=? AND id=?", (row["state"], json.dumps(row), time.time(), store.scope, org, fid))
        audit(q, store, actor, "transfer_retry_requested", {"org": org, "file": fid})
    return row


def download(store, storage, did, fid, buyer):
    delivery = manifest(store, did, buyer)
    file = next((f for f in delivery["files"] if f["id"] == fid), None)
    if not file:
        raise MediaError("File not found.", 404)
    with store.transaction(delivery["org"]) as q:
        current = q("SELECT data FROM media_files WHERE scope=? AND org=? AND id=?", (store.scope, delivery["org"], fid)).fetchone()
        row = json.loads(current["data"])
        if row["release"] != "approved" or row["inspection"] != "cleared":
            raise MediaError("This file is on hold.", 403)
        url = storage.client.generate_presigned_url("get_object", Params={"Bucket": storage.bucket, "Key": file["key"], "VersionId": file["version"], "ResponseContentDisposition": "attachment"}, ExpiresIn=300)
        audit(q, store, buyer, "download_link_issued", {"delivery": did, "file": fid, "version": file["version"]})
    return {"url": url, "expires_in": 300}
