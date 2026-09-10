"""Bulk-media acceptance at API, transaction and object transport boundaries."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import time
import uuid

import pytest
from fastapi.testclient import TestClient

import realm
import tests._asclepius as A
from tests.test_upload_scale import _portal
from asclepius import media_store as M, media_service as S, media_storage as B


class ObjectStorage:
    """Remote storage test double with fault injection; never production fallback."""
    def __init__(self):
        self.objects, self.uploads = {}, {}
        self.abort_count = 0

    def readiness(self): pass

    def create(self, row):
        uid = uuid.uuid4().hex
        self.uploads[uid] = {}
        return uid

    def parts(self, row):
        return [{"PartNumber": n, "Size": len(b), "ChecksumSHA256": base64.b64encode(hashlib.sha256(b).digest()).decode(), "ETag": str(n)} for n, b in sorted(self.uploads[row["upload_id"]].items())]

    def sign(self, row, n, checksum): return "https://private.example/part"

    def complete(self, row):
        if row["key"] not in self.objects:
            parts = self.parts(row)
            if len(parts) != row["part_count"]:
                raise M.MediaError("Missing parts")
            self.objects[row["key"]] = b"".join(self.uploads[row["upload_id"]].values())
        return {"VersionId": "immutable-1", "ContentLength": len(self.objects[row["key"]])}

    def chunks(self, row):
        data = self.objects[row["key"]]
        for i in range(0, len(data), 1024): yield data[i:i+1024]

    def abort(self, row): self.abort_count += 1


@pytest.fixture
def media(monkeypatch, tmp_path):
    monkeypatch.setenv("ENV", "test")
    monkeypatch.setenv("ASCLEPIUS_MEDIA_ENABLED", "1")
    monkeypatch.setenv("ASCLEPIUS_PORTAL_BUDGET_MS", "0")
    monkeypatch.setenv("ASCLEPIUS_MEDIA_DATABASE_URL", os.getenv("MEDIA_TEST_POSTGRES_URL") or "sqlite:///" + str(tmp_path / "media.db"))
    store = M.get_store()
    store.migrate()
    # A shared Postgres DSN keeps rows between tests where a per-test SQLite file
    # does not, and `tick` scans the whole realm by design — so a row abandoned by
    # an earlier test gets leased here, against an ObjectStorage double that has
    # never heard of its upload. Every test starts from an empty control plane.
    with store.transaction() as q:
        for table in ("media_files", "media_collections", "media_orgs",
                      "media_audit", "media_deliveries"):
            q(f"DELETE FROM {table}")
    remote = ObjectStorage()
    monkeypatch.setattr(B, "get_storage", lambda: remote)
    return store, remote


def new(store, remote, size=3, org=None, actor="person", token="token", cid=None, sha=None):
    org = org or uuid.uuid4().hex
    cid = cid or store.collection(org)
    return S.initialize(store, remote, store.declare(org, actor, cid, token, "folder/video.mp4", size, sha))


@pytest.mark.parametrize("size", [100*1024**3, 1024**4])
def test_large_metadata_never_allocates_video(media, size):
    store, remote = media
    row = new(store, remote, size)
    assert row["size"] == size and row["part_count"] <= 10000
    assert row["chunk_size"] * row["part_count"] >= size
    assert not remote.objects


def test_quota_resume_and_parallel_reservation(media, monkeypatch):
    store, remote = media
    monkeypatch.setenv("ASCLEPIUS_MEDIA_ORG_BYTES", "10")
    org = uuid.uuid4().hex
    cid = store.collection(org)
    def declare(i):
        try: return new(store, remote, 10, org, token=str(i), cid=cid)
        except M.MediaError as exc: return exc.status
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(declare, range(8)))
    rows = [r for r in results if isinstance(r, dict)]
    assert len(rows) == 1 and results.count(429) == 7
    token = str(next(i for i, r in enumerate(results) if isinstance(r, dict)))
    assert new(store, remote, 10, org, token=token, cid=cid)["id"] == rows[0]["id"]


def test_migrations_preserve_rows_and_scope(media):
    store, remote = media
    row = new(store, remote)
    store.migrate()
    assert store.get(row["org"], row["id"])["id"] == row["id"]
    with realm.scoped("sandbox"):
        with pytest.raises(M.MediaError): M.get_store().get(row["org"], row["id"])
    with pytest.raises(M.MediaError): store.get("another-org", row["id"])
    with pytest.raises(M.MediaError): store.get(row["org"], row["id"], "another-person")


def test_complete_hash_and_no_clinical_ingestion(media, monkeypatch):
    from asclepius import ingestion
    monkeypatch.setattr(ingestion, "process_upload", lambda *a, **k: pytest.fail("clinical ingestion called"))
    store, remote = media
    row = new(store, remote, sha=hashlib.sha256(b"abc").hexdigest())
    remote.uploads[row["upload_id"]][1] = b"abc"
    store.change(row["org"], row["id"], ("uploading",), state="completing")
    assert S.tick(store, remote)
    done = store.get(row["org"], row["id"])
    assert done["state"] == "stored"
    assert done["sha256"] == hashlib.sha256(b"abc").hexdigest()
    assert done["version"] == "immutable-1"
    assert done["release"] == "held" and done["inspection"] == "pending"


def test_worker_recovers_uncertain_completion(media, monkeypatch):
    store, remote = media
    row = new(store, remote)
    remote.uploads[row["upload_id"]][1] = b"abc"
    store.change(row["org"], row["id"], ("uploading",), state="completing")
    original = remote.complete
    def dropped(row):
        original(row)
        raise ConnectionError("completion response dropped")
    monkeypatch.setattr(remote, "complete", dropped)
    with pytest.raises(ConnectionError): S.tick(store, remote)
    monkeypatch.setattr(remote, "complete", original)
    with store.transaction() as q:
        q("UPDATE media_files SET lease=0 WHERE scope=? AND org=? AND id=?", (store.scope, row["org"], row["id"]))
    assert S.tick(store, remote)
    assert store.get(row["org"], row["id"])["state"] == "stored"
    assert len(remote.objects) == 1


def test_corruption_is_held(media):
    store, remote = media
    row = new(store, remote, sha="0"*64)
    remote.uploads[row["upload_id"]][1] = b"abc"
    store.change(row["org"], row["id"], ("uploading",), state="completing")
    S.tick(store, remote)
    assert store.get(row["org"], row["id"])["state"] == "integrity_failed"
    assert remote.objects  # originals retained for investigation


def test_missing_parts_can_resume(media):
    store, remote = media
    row = new(store, remote)
    store.change(row["org"], row["id"], ("uploading",), state="completing")
    S.tick(store, remote)
    assert store.get(row["org"], row["id"])["state"] == "uploading"


def test_cancel_releases_once_and_active_jobs_not_starved(media, monkeypatch):
    store, remote = media
    monkeypatch.setenv("ASCLEPIUS_MEDIA_ORG_BYTES", "3")
    active = new(store, remote)
    row = new(store, remote)
    store.change(row["org"], row["id"], ("uploading",), state="cancelling")
    assert S.tick(store, remote)
    assert store.get(row["org"], row["id"])["state"] == "cancelled"
    assert not S.tick(store, remote)
    assert remote.abort_count == 1
    new(store, remote, org=row["org"], token="replacement", cid=row["collection"])
    assert store.get(active["org"], active["id"])["state"] == "uploading"


def test_api_auth_and_neutral_receipts(media, monkeypatch):
    store, remote = media
    accounts = A.fresh_store()
    api = "/api/asclepius/hs/media"
    with TestClient(A.app, base_url="https://testserver") as client:
        assert client.post(api+"/collections").status_code in (401, 403)
        hs, username = _portal(client, accounts, purpose="brokering")
        cid = client.post(api+"/collections").json()["id"]
        body = dict(collection=cid, token="video1", path="surgery.mp4", size=3)
        res = client.post(api+"/files", json=body)
        assert res.status_code == 200, res.text
        row = res.json()
        assert "policy" not in row and "key" not in row and "purpose" not in row
        private = store.get(hs["hs_id"], row["id"])
        assert private["policy"] == "brokering"
        assert client.post(api+"/files", json={**body, "purpose": "task_creation"}).status_code == 422
        remote.uploads[private["upload_id"]][1] = b"abc"
        assert client.post(api+"/files/"+row["id"]+"/complete").status_code == 202
        S.tick(store, remote)
        assert client.get(api+"/files/"+row["id"]).json()["state"] == "stored"
        monkeypatch.setenv("ASCLEPIUS_MEDIA_ENABLED", "0")
        assert client.post(api+"/collections").status_code == 404


def test_part_listing_paginates_sdk(monkeypatch):
    import boto3
    from botocore.stub import Stubber
    client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test")
    monkeypatch.setenv("ASCLEPIUS_MEDIA_BUCKET", "test-bucket")
    monkeypatch.setenv("ASCLEPIUS_MEDIA_KMS_KEY", "test-key")
    adapter = B.S3Storage(client)
    args = dict(Bucket="test-bucket", Key="key", UploadId="id")
    with Stubber(client) as stub:
        stub.add_response("list_parts", {"Parts": [{"PartNumber": n, "Size": 5*1024**2} for n in range(1,1001)], "IsTruncated": True, "NextPartNumberMarker": 1000}, args)
        stub.add_response("list_parts", {"Parts": [{"PartNumber": 1001, "Size": 1}], "IsTruncated": False}, {**args, "PartNumberMarker": 1000})
        assert len(adapter.parts({"key": "key", "upload_id": "id"})) == 1001
        stub.assert_no_pending_responses()


@pytest.mark.parametrize("path", ["../secret", "/absolute", "dir/../x", "dir//x", "x\x00.mp4"])
def test_unsafe_paths_rejected(media, path):
    store, remote = media
    org = uuid.uuid4().hex
    with pytest.raises(M.MediaError): store.declare(org, "actor", store.collection(org), "token", path, 3)


def test_idle_session_retains_acknowledged_parts(media, monkeypatch):
    store, remote = media
    row = new(store, remote)
    remote.uploads[row['upload_id']][1] = b'abc'
    with store.transaction() as q:
        q("UPDATE media_files SET updated=? WHERE scope=? AND org=? AND id=?", (time.time()-8*86400, store.scope, row["org"], row["id"]))
    def abort(r):
        pytest.fail('age alone cannot authorize deleting acknowledged parts')
    monkeypatch.setattr(remote, "abort", abort)
    S.tick(store, remote)
    assert store.get(row["org"], row["id"])["state"] == "uploading"
    assert remote.uploads[row['upload_id']][1] == b'abc'


def test_delivery_requires_inspection_and_is_buyer_bound(media):
    from asclepius import media_delivery as D
    store, remote = media
    row = new(store, remote)
    remote.uploads[row["upload_id"]][1] = b"abc"
    store.change(row["org"], row["id"], ("uploading",), state="completing", policy="brokering")
    S.tick(store, remote)
    with pytest.raises(M.MediaError): D.create(store, row["org"], [row["id"]], "buyer1", "admin")
    D.review(store, row["org"], row["id"], "admin", {"malware": "scan-42", "privacy": "review-42", "rights": "agreement-42"}, True)
    did = D.create(store, row["org"], [row["id"]], "buyer1", "admin")
    manifest = D.manifest(store, did, "buyer1")
    assert manifest["files"][0]["sha256"] == hashlib.sha256(b"abc").hexdigest()
    with pytest.raises(M.MediaError): D.manifest(store, did, "buyer2")
    D.review(store, row["org"], row["id"], "admin", {"reason": "hold"}, False)
    with pytest.raises(M.MediaError): D.download(store, remote, did, row["id"], "buyer1")
    assert D.manifest(store, did, "buyer1") == manifest  # immutable receipt


def test_source_allowlist_and_version_binding(media, monkeypatch):
    from asclepius.media_sources import resolve
    store, remote = media
    org = uuid.uuid4().hex
    monkeypatch.setenv("ASCLEPIUS_MEDIA_SOURCES", json.dumps([dict(id="hospital", realm=store.scope, org=org, bucket="approved", prefix="export/")]))
    assert resolve(store.scope, org, "hospital", "export/video", "v1")["VersionId"] == "v1"
    for scope, owner, key, version in [("sandbox", org, "export/video", "v1"), (store.scope, "other", "export/video", "v1"), (store.scope, org, "private/video", "v1"), (store.scope, org, "export/video", "null")]:
        with pytest.raises(M.MediaError): resolve(scope, owner, "hospital", key, version)
    cid = store.collection(org)
    source = dict(object={"Bucket": "approved", "Key": "export/video", "VersionId": "v1"}, id="hospital", etag="etag")
    row = store.declare(org, "actor", cid, "token", "video.mp4", 3, source=source)
    with pytest.raises(M.MediaError): store.declare(org, "actor", cid, "token", "video.mp4", 3)
    assert row["source"]["VersionId"] == "v1"


def test_catalog_pagination_and_collection_limit(media):
    store, remote = media
    org = uuid.uuid4().hex
    cid = store.collection(org)
    ids = {store.declare(org, "actor", cid, str(i), "same-name.mp4", 1)["id"] for i in range(205)}
    found, after = set(), ""
    while True:
        page = store.catalog(org, cid, after)
        if not page: break
        assert len(page) <= 100
        found.update(r["id"] for r in page)
        after = page[-1]["id"]
    assert found == ids
    with store.transaction(org) as q:
        q("UPDATE media_collections SET files=100000 WHERE scope=? AND org=? AND id=?", (store.scope, org, cid))
    with pytest.raises(M.MediaError): store.declare(org, "actor", cid, "overflow", "last.mp4", 1)


def test_s3_missing_reordered_corrupt_parts_and_head_identity(monkeypatch):
    import boto3
    from botocore.stub import Stubber
    monkeypatch.setenv("ASCLEPIUS_MEDIA_BUCKET", "test-bucket")
    monkeypatch.setenv("ASCLEPIUS_MEDIA_KMS_KEY", "test-key")
    client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test")
    adapter = B.S3Storage(client)
    row = dict(id="fid", key="key", upload_id="uid", size=3, part_count=1, chunk_size=64*1024**2)
    monkeypatch.setattr(adapter, "head", lambda row: None)
    for parts in [[], [{"PartNumber": 2, "Size": 3, "ChecksumSHA256": "hash"}], [{"PartNumber": 1, "Size": 2, "ChecksumSHA256": "hash"}], [{"PartNumber": 1, "Size": 3}]]:
        monkeypatch.setattr(adapter, "parts", lambda row: parts)
        with pytest.raises(M.MediaError): adapter.complete(row)
    monkeypatch.setattr(adapter, "head", lambda row: {"ContentLength": 3, "VersionId": "null", "Metadata": {"media-id": "fid"}})
    with pytest.raises(M.MediaError): adapter.complete(row)


def test_sqlite_rejected_outside_test(media, monkeypatch):
    monkeypatch.setenv("ENV", "production")
    with pytest.raises(RuntimeError): M.MediaStore("sqlite:///unused", "live")


def test_100000_file_catalog_has_no_missing_or_repeated_ids(media):
    store, _ = media
    org = uuid.uuid4().hex
    cid = store.collection(org)
    with store.transaction(org) as q:
        # Generate real persisted rows without allocating 100,000 media buffers.
        q("WITH RECURSIVE numbers(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM numbers WHERE n<100000) INSERT INTO media_files(scope,org,id,collection,actor,token,state,updated,data) SELECT ?,?,CAST(n AS TEXT),?,'actor',CAST(n AS TEXT),'stored',0,'{\"id\":\"' || CAST(n AS TEXT) || '\"}' FROM numbers", (store.scope, org, cid))
    seen, after = set(), ""
    while True:
        page = store.catalog(org, cid, after)
        if not page: break
        ids = {row["id"] for row in page}
        assert not (seen & ids)
        seen.update(ids)
        after = page[-1]["id"]
    assert seen == {str(i) for i in range(1, 100001)}


def test_revoked_provider_cannot_sign_or_complete(media):
    store, remote = media
    accounts = A.fresh_store()
    api = "/api/asclepius/hs/media"
    with TestClient(A.app, base_url="https://testserver") as client:
        hs, username = _portal(client, accounts, purpose="brokering")
        cid = client.post(api+"/collections").json()["id"]
        row = client.post(api+"/files", json=dict(collection=cid, token="video", path="video.mp4", size=3)).json()
        accounts.set_hs_portal_active(username, False)
        assert client.post(api+"/files/"+row["id"]+"/sign", json={"number": 1, "checksum": base64.b64encode(hashlib.sha256(b"abc").digest()).decode()}).status_code in (401, 403)
        assert client.post(api+"/files/"+row["id"]+"/complete").status_code in (401, 403)
        assert store.get(hs["hs_id"], row["id"])["state"] == "uploading"


def test_s3_completion_reconciles_a_successful_prior_request(monkeypatch):
    import boto3
    from botocore.stub import Stubber
    monkeypatch.setenv("ASCLEPIUS_MEDIA_BUCKET", "test-bucket")
    monkeypatch.setenv("ASCLEPIUS_MEDIA_KMS_KEY", "test-key")
    client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test")
    adapter = B.S3Storage(client)
    row = dict(id="fid", key="key", upload_id="uid", size=3, part_count=1, chunk_size=64*1024**2)
    checksum = base64.b64encode(hashlib.sha256(b"abc").digest()).decode()
    args = {"Bucket": "test-bucket", "Key": "key", "ChecksumMode": "ENABLED"}
    part = {"PartNumber": 1, "Size": 3, "ChecksumSHA256": checksum, "ETag": '"etag"'}
    head = {"ContentLength": 3, "VersionId": "version", "Metadata": {"media-id": "fid"}, "ChecksumSHA256": checksum+"-1"}
    with Stubber(client) as stub:
        stub.add_client_error("head_object", "404", http_status_code=404, expected_params=args)
        stub.add_response("list_parts", {"IsTruncated": False, "Parts": [part]}, adapter.params(row))
        stub.add_response("complete_multipart_upload", {"VersionId": "version"}, {**adapter.params(row), "MultipartUpload": {"Parts": [{k: part[k] for k in ("PartNumber", "ETag", "ChecksumSHA256")}]}})
        stub.add_response("head_object", head, args)
        assert adapter.complete(row)["VersionId"] == "version"
        stub.add_response("head_object", head, args)
        assert adapter.complete(row)["VersionId"] == "version"
        stub.assert_no_pending_responses()


@pytest.mark.parametrize("policy", ["brokering", "storage", None, "unknown"])
def test_legacy_quota_resume_and_brokering_retention(media, monkeypatch, tmp_path, policy):
    from pathlib import Path
    from tests.test_upload_scale import _bundle, _declare, _upload_all
    from asclepius import ingestion
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest"))
    monkeypatch.setenv("ASCLEPIUS_ASSET_STORE", str(tmp_path / "assets"))
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", base64.b64encode(bytes(range(32))).decode())
    accounts = A.fresh_store()
    with TestClient(A.app, base_url="https://testserver") as client:
        _portal(client, accounts, purpose="brokering")
        data = _bundle()
        monkeypatch.setenv("ASCLEPIUS_HS_QUOTA_BYTES", str(len(data)))
        session = _declare(client, data)
        resumed = _declare(client, data)
        assert resumed["session_id"] == session["session_id"]
        response = _upload_all(client, data, session)
        assert response.status_code == 200, response.text
        upload = accounts.get_ingest_upload(response.json()["upload_id"])
        assert upload["purpose"] == "brokering" and not upload.get("retain_raw")
        # Historical unset/unknown policy is storage, never deletion permission.
        with accounts._conn() as q:
            q.execute("UPDATE ingest_uploads SET purpose=? WHERE upload_id=?", (policy, upload["upload_id"]))
        original = Path(upload["raw_path"])
        old = time.time()-31*86400
        os.utime(original, (old, old))
        ingestion.purge_expired_raw(accounts)
        assert original.exists()


def test_retention_metadata_failure_preserves_original(media, monkeypatch, tmp_path):
    from asclepius import ingestion
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest"))
    original = ingestion.quarantine_root() / "unknown.zip.enc"
    original.write_bytes(b"synthetic")
    old = time.time()-31*86400
    os.utime(original, (old, old))
    class Unavailable:
        def list_uploads_with_retained_raw(self): raise ConnectionError("metadata unavailable")
    assert ingestion.purge_expired_raw(Unavailable()) == 0
    assert original.exists()


def test_small_source_copy_executes_and_restart_reuses_parts(media, monkeypatch):
    from asclepius import media_sources
    store, remote = media
    org = uuid.uuid4().hex
    cid = store.collection(org)
    monkeypatch.setenv("ASCLEPIUS_MEDIA_SOURCES", json.dumps([dict(id="source", realm=store.scope, org=org, bucket="approved", prefix="export/")]))
    source = dict(object={"Bucket": "approved", "Key": "export/video", "VersionId": "v1"}, id="source", etag="etag")
    row = S.initialize(store, remote, store.declare(org, "actor", cid, "copy", "video.mp4", 3, source=source))
    assert row["state"] == "importing"
    calls = []
    class CopyClient:
        def upload_part_copy(self, **args):
            calls.append(args)
            assert "CopySourceRange" not in args
            assert args["CopySource"] == source["object"]
            remote.uploads[row["upload_id"]][args["PartNumber"]] = b"abc"
    monkeypatch.setattr(remote, "client", CopyClient(), raising=False)
    monkeypatch.setattr(remote, "params", lambda r: {"Bucket": "dest", "Key": r["key"], "UploadId": r["upload_id"]}, raising=False)
    media_sources.copy_parts(remote, row, lambda: None)
    assert len(calls) == 1
    assert S.tick(store, remote)
    assert len(calls) == 1  # worker restart lists the already copied part
    assert store.get(org, row["id"])["state"] == "stored"


def test_permanent_worker_failure_is_held_then_admin_can_retry(media, monkeypatch):
    from asclepius import media_delivery as D
    store, remote = media
    row = new(store, remote)
    store.change(row["org"], row["id"], ("uploading",), state="completing")
    def unavailable(row): raise ConnectionError("synthetic storage failure")
    monkeypatch.setattr(remote, "complete", unavailable)
    for i in range(10):
        with store.transaction() as q:
            q("UPDATE media_files SET lease=0 WHERE scope=? AND org=? AND id=?", (store.scope, row["org"], row["id"]))
        with pytest.raises(ConnectionError): S.tick(store, remote)
    held = store.get(row["org"], row["id"])
    assert held["state"] == "attention_required" and held["attempts"] == 10
    assert not S.tick(store, remote)
    retried = D.retry(store, row["org"], row["id"], "admin")
    assert retried["state"] == "completing" and retried["attempts"] == 0


def test_orphan_reaper_preserves_tracked_active_mpu(media, monkeypatch):
    from datetime import datetime, timezone
    store, remote = media
    row = new(store, remote)
    old = datetime.fromtimestamp(time.time()-90*86400, timezone.utc)
    calls = []
    class Client:
        def get_paginator(self, name): return self
        def paginate(self, **args):
            return [{"Uploads": [dict(Key=row["key"], UploadId=row["upload_id"], Initiated=old), dict(Key=row["key"], UploadId="unreferenced", Initiated=old)]}]
        def abort_multipart_upload(self, **args): calls.append(args)
    monkeypatch.setattr(remote, "client", Client(), raising=False)
    monkeypatch.setattr(remote, "bucket", "bucket", raising=False)
    assert S.reap_orphans(store, remote) == 1
    assert [c["UploadId"] for c in calls] == ["unreferenced"]
    assert store.get(row["org"], row["id"])["state"] == "uploading"


def test_admin_and_buyer_http_delivery_boundary(media):
    store, remote = media
    accounts = A.fresh_store()
    admin = A.make_user(accounts, role="admin")
    buyer = A.make_user(accounts, role="buyer")
    other = A.make_user(accounts, role="buyer")
    row = new(store, remote)
    remote.uploads[row["upload_id"]][1] = b"abc"
    store.change(row["org"], row["id"], ("uploading",), state="completing", policy="brokering")
    S.tick(store, remote)
    root = "/api/asclepius/admin/media/"+row["org"]
    with TestClient(A.app, base_url="https://testserver") as client:
        assert client.post(root+"/files/"+row["id"]+"/review", json={}).status_code in (401,403)
        reviewed = client.post(root+"/files/"+row["id"]+"/review", headers=A.headers_for(admin), json=dict(approved=True, malware_evidence="scan", privacy_evidence="review", rights_evidence="agreement"))
        assert reviewed.status_code == 200, reviewed.text
        created = client.post(root+"/deliveries", headers=A.headers_for(admin), json=dict(files=[row["id"]], buyer=buyer["id"]))
        assert created.status_code == 200, created.text
        did = created.json()["id"]
        assert client.get("/api/asclepius/buyer/media/"+did, headers=A.headers_for(buyer)).status_code == 200
        assert client.get("/api/asclepius/buyer/media/"+did, headers=A.headers_for(other)).status_code == 404


def test_provider_cli_keeps_credentials_off_storage_and_resumes(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import httpx
    from scripts import media_upload as cli
    root = tmp_path / "footage"
    root.mkdir()
    (root / "clip.mp4").write_bytes(b"abcdef")
    state = tmp_path / "progress.sqlite"
    uploaded, tokens, completions = {}, [], []
    def handle(req):
        if req.url.host == "storage.test":
            assert not req.headers.get("cookie") and not req.headers.get("authorization")
            n = int(req.url.path.rsplit("/", 1)[-1])
            uploaded[n] = req.content
            return httpx.Response(200)
        path = req.url.path
        body = json.loads(req.content) if req.content else None
        data = {}
        if path.endswith("login"): data = {"ok": True}
        elif path.endswith("collections"): data = {"id": "collection"}
        elif path.endswith("/files"):
            tokens.append(body["token"]); data = {"id": "file"}
        elif path.endswith("/sign"): data = {"url": "https://storage.test/"+str(body["number"])}
        elif path.endswith("/complete"): completions.append(True)
        else: data = {"id": "file", "state": "uploading", "chunk_size": 3, "part_count": 2, "parts": [{"number": n, "checksum": base64.b64encode(hashlib.sha256(b).digest()).decode()} for n,b in uploaded.items()]}
        return httpx.Response(200, json=data)
    actual_client = httpx.Client
    monkeypatch.setattr(cli.httpx, "Client", lambda **kw: actual_client(transport=httpx.MockTransport(handle), **kw))
    monkeypatch.setattr(cli.getpass, "getpass", lambda: "test-password")
    args = SimpleNamespace(url="https://portal.test", username="person", realm="live", directory=str(root), state=str(state))
    cli.run(args); cli.run(args)
    assert tokens[0] == tokens[1]
    assert uploaded == {1: b"abc", 2: b"def"} and len(completions) == 2
    assert b"test-password" not in state.read_bytes()
