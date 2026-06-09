"""PRD-6 — AES-256-GCM field encryption at rest."""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import field_crypto as fc  # noqa: E402

_KEY = base64.b64encode(b"k" * 32).decode()


@pytest.fixture()
def key(monkeypatch):
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", _KEY)
    monkeypatch.delenv("DATA_ENCRYPTION_KEY_RING", raising=False)
    monkeypatch.setenv("DATA_ENCRYPTION_KEY_VERSION", "1")


# ─── Primitive ────────────────────────────────────────────────────────────────

def test_roundtrip(key):
    assert fc.is_configured() is True
    t = fc.encrypt_field("Maria Lopez, MRN AB123")
    assert fc.is_encrypted(t) and t.startswith("enc:v1:")
    assert fc.decrypt_field(t) == "Maria Lopez, MRN AB123"


def test_plaintext_passthrough(key):
    assert fc.decrypt_field("not-a-token") == "not-a-token"
    assert fc.decrypt_field(None) is None
    assert fc.is_encrypted("plain") is False


def test_tamper_detected(key):
    t = fc.encrypt_field("secret")
    tampered = t[:-6] + "AAAAAA"
    with pytest.raises(ValueError):
        fc.decrypt_field(tampered)


def test_unique_nonce(key):
    a, b = fc.encrypt_field("same"), fc.encrypt_field("same")
    assert a != b  # random nonce per encryption
    assert fc.decrypt_field(a) == fc.decrypt_field(b) == "same"


def test_key_rotation(monkeypatch):
    # Encrypt under v1, then rotate: v2 active + v1 kept in the ring -> old token
    # still decrypts, new tokens use v2.
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", _KEY)
    monkeypatch.setenv("DATA_ENCRYPTION_KEY_VERSION", "1")
    old = fc.encrypt_field("legacy")
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", base64.b64encode(b"j" * 32).decode())
    monkeypatch.setenv("DATA_ENCRYPTION_KEY_VERSION", "2")
    monkeypatch.setenv("DATA_ENCRYPTION_KEY_RING", f"1:{_KEY}")
    assert fc.decrypt_field(old) == "legacy"
    assert fc.encrypt_field("x").startswith("enc:v2:")


def test_not_configured_passthrough(monkeypatch):
    monkeypatch.delenv("DATA_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("DATA_ENCRYPTION_KEY_RING", raising=False)
    assert fc.is_configured() is False
    # No key -> plaintext passthrough so dev still works.
    assert fc.encrypt_field("hello") == "hello"


def test_value_helpers(key):
    payload = {"procedure": "Appendectomy", "date": "2026-06-01", "meds": ["aspirin"]}
    t = fc.encrypt_value(payload)
    assert fc.is_encrypted(t)
    assert fc.decrypt_value(t) == payload


# ─── Patient-store snapshot encryption (main) ────────────────────────────────

def test_snapshot_blob_encrypted_at_rest(key, tmp_path, monkeypatch):
    import main

    pid = "enc_pt_1"
    main.app.state.patient_store.clear()
    main.app.state.patient_store[pid] = {
        "name": "Jane Patient", "phone": "+13105550000", "email": "jane@example.com",
        "voice_script": "Hey Jane, your surgery went well.",
        "structured_data": {"procedure_name": "Appendectomy", "mrn": "AB12345"},
        "pipeline_type": "post_op",
    }
    snap = tmp_path / "snap.json"
    monkeypatch.setattr(main, "_demo_patient_store_snapshot_path", lambda: str(snap))

    main._persist_demo_patient_store()
    raw = snap.read_text()
    # No plaintext PHI on disk.
    for leak in ("Jane Patient", "3105550000", "jane@example.com", "AB12345", "Appendectomy"):
        assert leak not in raw, f"plaintext PHI leaked to disk: {leak}"
    assert "enc:v1:" in raw

    # Round-trips back to plaintext on load.
    main.app.state.patient_store.clear()
    main._load_demo_patient_store_snapshot()
    blob = main.app.state.patient_store[pid]
    assert blob["name"] == "Jane Patient"
    assert blob["structured_data"]["mrn"] == "AB12345"


def test_snapshot_plaintext_legacy_still_loads(key, tmp_path, monkeypatch):
    import main
    # A pre-encryption snapshot (plaintext fields) must still load.
    snap = tmp_path / "legacy.json"
    snap.write_text(json.dumps({"p9": {"name": "Old Plain", "structured_data": {"x": 1}}}))
    monkeypatch.setattr(main, "_demo_patient_store_snapshot_path", lambda: str(snap))
    main.app.state.patient_store.clear()
    main._load_demo_patient_store_snapshot()
    assert main.app.state.patient_store["p9"]["name"] == "Old Plain"
