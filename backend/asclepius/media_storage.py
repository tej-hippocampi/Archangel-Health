"""Private, versioned S3 multipart transport; no media bytes in the API."""
import os

from asclepius.media_store import MediaError


class S3Storage:
    def __init__(self, client=None):
        import boto3
        from botocore.config import Config
        self.bucket = os.environ["ASCLEPIUS_MEDIA_BUCKET"]
        self.kms = os.environ["ASCLEPIUS_MEDIA_KMS_KEY"]
        self.client = client or boto3.client("s3", config=Config(signature_version="s3v4", retries={"mode": "standard", "max_attempts": 4}, connect_timeout=10, read_timeout=60))

    def readiness(self):
        if self.client.get_bucket_versioning(Bucket=self.bucket).get("Status") != "Enabled":
            raise MediaError("Media bucket versioning must be enabled.", 503)
        block = self.client.get_public_access_block(Bucket=self.bucket)["PublicAccessBlockConfiguration"]
        if not all(block.get(k) for k in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")):
            raise MediaError("Media bucket must block public access.", 503)

    def create(self, row):
        return self.client.create_multipart_upload(Bucket=self.bucket, Key=row["key"],
            ContentType="application/octet-stream", ChecksumAlgorithm="SHA256",
            ServerSideEncryption="aws:kms", SSEKMSKeyId=self.kms,
            Metadata={"media-id": row["id"]})["UploadId"]

    def params(self, row):
        return dict(Bucket=self.bucket, Key=row["key"], UploadId=row["upload_id"])

    def sign(self, row, number, checksum):
        size = min(row["chunk_size"], row["size"] - (number - 1) * row["chunk_size"])
        return self.client.generate_presigned_url("upload_part", Params={**self.params(row),
            "PartNumber": number, "ContentLength": size, "ChecksumSHA256": checksum}, ExpiresIn=300)

    def parts(self, row):
        found = []
        for page in self.client.get_paginator("list_parts").paginate(**self.params(row)):
            found.extend(page.get("Parts", []))
        return found

    def head(self, row):
        from botocore.exceptions import ClientError
        try:
            return self.client.head_object(Bucket=self.bucket, Key=row["key"], ChecksumMode="ENABLED",
                **({"VersionId": row["version"]} if row.get("version") else {}))
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return None
            raise

    def complete(self, row):
        head = self.head(row)
        if not head:
            parts = self.parts(row)
            if len(parts) != row["part_count"]:
                raise MediaError("Some upload parts are missing.")
            for n, part in enumerate(parts, 1):
                size = min(row["chunk_size"], row["size"] - (n-1)*row["chunk_size"])
                if part["PartNumber"] != n or part["Size"] != size or not part.get("ChecksumSHA256"):
                    raise MediaError("Upload parts failed verification.")
            self.client.complete_multipart_upload(**self.params(row), MultipartUpload={"Parts": [
                {k: p[k] for k in ("PartNumber", "ETag", "ChecksumSHA256")} for p in parts]})
            head = self.head(row)
        if not head or head["ContentLength"] != row["size"] or head.get("Metadata", {}).get("media-id") != row["id"] or not head.get("VersionId") or head["VersionId"] == "null":
            raise MediaError("Stored object identity or size failed verification.", 422)
        return head

    def abort(self, row):
        from botocore.exceptions import ClientError
        try:
            self.client.abort_multipart_upload(**self.params(row))
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "NoSuchUpload":
                raise

    def chunks(self, row):
        response = self.client.get_object(Bucket=self.bucket, Key=row["key"], VersionId=row["version"])
        try:
            yield from response["Body"].iter_chunks(chunk_size=8*1024**2)
        finally:
            response["Body"].close()


def get_storage():
    try:
        return S3Storage()
    except KeyError:
        raise MediaError("Bulk uploads are not configured yet.", 503) from None
