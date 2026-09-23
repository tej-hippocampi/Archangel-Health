"""Rejected gzip members must consume the whole upload's decompression budget."""
import gzip
import hashlib
import io
import zipfile

import pytest

from asclepius import ingestion


def _bundle(member, count=4):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for index in range(count):
            archive.writestr(f"member-{index}.txt.gz", member)
    return buffer.getvalue()


@pytest.mark.parametrize("failure", ["entry_size", "ratio", "corrupt"])
@pytest.mark.parametrize("spill", [False, True])
def test_rejected_gzip_members_exhaust_archive_budget(monkeypatch, tmp_path, failure, spill):
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest"))
    monkeypatch.setattr(ingestion, "max_entry_bytes", lambda: 1_000_000)
    monkeypatch.setattr(ingestion, "entry_compression_ratio_cap", lambda: 1_000_000)
    if failure == "corrupt":
        # Return two chunks successfully, then fail on the missing gzip footer.
        member = gzip.compress(b"x" * (524_288 + 13))[:-4]
        budget = 600_000
    else:
        member = gzip.compress(b"x" * 200_000)
        budget = 300_000
        if failure == "entry_size":
            monkeypatch.setattr(ingestion, "max_entry_bytes", lambda: 100_000)
        else:
            monkeypatch.setattr(ingestion, "entry_compression_ratio_cap", lambda: 2)
    monkeypatch.setattr(ingestion, "total_output_budget", lambda _size: budget)
    original = tmp_path / "accepted-original.zip"
    original.write_bytes(_bundle(member))
    digest = hashlib.sha256(original.read_bytes()).hexdigest()
    with pytest.raises(ingestion.BundleRejected, match="budget"):
        ingestion.unpack_bundle_from_path(str(original), spill=spill)
    assert hashlib.sha256(original.read_bytes()).hexdigest() == digest
    assert not list(ingestion.quarantine_root().glob("entries-*"))


def test_small_gzip_rejections_preserve_valid_neighbors_when_budget_remains(monkeypatch, tmp_path):
    monkeypatch.setattr(ingestion, "max_entry_bytes", lambda: 100_000)
    monkeypatch.setattr(ingestion, "total_output_budget", lambda _size: 1_000_000)
    member = gzip.compress(b"x" * 200_000)
    buffer = io.BytesIO(_bundle(member, count=1))
    with zipfile.ZipFile(buffer, "a") as archive:
        archive.writestr("note.txt.gz", gzip.compress(b"Synthetic clinical note."))
    original = tmp_path / "mixed.zip"
    original.write_bytes(buffer.getvalue())
    result = ingestion.unpack_bundle_from_path(str(original), spill=False)
    assert result["entries"][0]["kind"] == "rejected"
    assert result["entries"][1]["data"] == b"Synthetic clinical note."
