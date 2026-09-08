"""Approved, immutable S3 sources. No arbitrary URL fetching or credentials input.

ASCLEPIUS_MEDIA_SOURCES is a JSON list managed by operators, containing id,
realm, org, bucket and prefix. AWS IAM must separately permit each source.
"""
import json
import os

from asclepius.media_store import MediaError, part_plan


def resolve(scope, org, source_id, key, version):
    sources = json.loads(os.getenv("ASCLEPIUS_MEDIA_SOURCES", "[]"))
    source = next((s for s in sources if (s["realm"], s["org"], s["id"]) == (scope, org, source_id)), None)
    if not source or not key.startswith(source["prefix"]) or not version or version == "null":
        raise MediaError("Source is unavailable or requires an immutable version.", 422)
    return {"Bucket": source["bucket"], "Key": key, "VersionId": version}


def declare_import(store, storage, accounts, user, body):
    from asclepius.media_service import declare_for_account, initialize
    source = resolve(store.scope, user["hs_id"], body["source_id"], body["source_key"], body["source_version"])
    head = storage.client.head_object(**source)
    part_plan(head["ContentLength"])
    payload = {k: body[k] for k in ("collection", "token", "path")}
    payload["size"] = head["ContentLength"]
    payload["source_info"] = {"object": source, "id": body["source_id"], "etag": head["ETag"]}
    row = declare_for_account(store, accounts, user, payload)
    return initialize(store, storage, row)


def copy_parts(storage, row, heartbeat):
    # Revalidate the allowlist on every retry. A removed grant halts new copies.
    source = resolve(row["scope"], row["org"], row["source_id"], row["source"]["Key"], row["source"]["VersionId"])
    if source != row["source"]:
        raise MediaError("The configured source changed; transfer requires review.")
    present = {p["PartNumber"]: p for p in storage.parts(row)}
    for number in range(1, row["part_count"]+1):
        heartbeat()
        if number in present:
            continue
        first = (number-1)*row["chunk_size"]
        last = min(row["size"], first+row["chunk_size"])-1
        storage.client.upload_part_copy(**storage.params(row), PartNumber=number,
            CopySource=source, CopySourceIfMatch=row["source_etag"],
            **({"CopySourceRange": f"bytes={first}-{last}"} if row["part_count"] > 1 else {}))
