"""Application-layer field encryption for PHI at rest (PRD-6).

AES-256-GCM (authenticated encryption) via the ``cryptography`` library. Keys come
from the environment (inject from a KMS / Secrets Manager in production):

  - ``DATA_ENCRYPTION_KEY``       base64-encoded 32-byte active key
  - ``DATA_ENCRYPTION_KEY_VERSION`` label for the active key (default "1")
  - ``DATA_ENCRYPTION_KEY_RING``  optional "ver:b64,ver:b64" of older keys kept for
                                  decryption during key rotation

Token format: ``enc:v<version>:<nonce_b64>:<ct_b64>``. The version lets old data
keep decrypting after the active key rotates. Plaintext (legacy / unencrypted)
values pass through ``decrypt_field`` unchanged, so encryption can be rolled out
incrementally — values are upgraded to ciphertext on their next write.

This is **defense-in-depth** layered on top of the cloud volume encryption that
protects the database and disk at rest (see docs/security/ENCRYPTION.md).
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any, Dict, Optional, Tuple

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_PREFIX = "enc:"


def _b64d(s: str) -> bytes:
    return base64.b64decode(s)


def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _ring() -> Tuple[Dict[str, bytes], str]:
    """Return ({version: 32-byte key}, active_version)."""
    ring: Dict[str, bytes] = {}
    active = (os.getenv("DATA_ENCRYPTION_KEY_VERSION") or "1").strip() or "1"
    cur = (os.getenv("DATA_ENCRYPTION_KEY") or "").strip()
    if cur:
        try:
            ring[active] = _b64d(cur)
        except Exception:
            pass
    for pair in (os.getenv("DATA_ENCRYPTION_KEY_RING") or "").split(","):
        pair = pair.strip()
        if ":" in pair:
            v, b = pair.split(":", 1)
            try:
                ring[v.strip()] = _b64d(b.strip())
            except Exception:
                pass
    return ring, active


def is_configured() -> bool:
    """True if a valid 32-byte active key is available (encryption is active)."""
    ring, active = _ring()
    return active in ring and len(ring[active]) == 32


def is_encrypted(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(_PREFIX)


def encrypt_field(plaintext: Optional[str]) -> Optional[str]:
    """Encrypt a string. Returns the ``enc:`` token, or the plaintext unchanged
    when no key is configured (dev fall-back; production is guarded at startup)."""
    if plaintext is None:
        return None
    if not isinstance(plaintext, str):
        plaintext = str(plaintext)
    ring, active = _ring()
    key = ring.get(active)
    if not key or len(key) != 32:
        return plaintext  # not configured -> passthrough
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return f"{_PREFIX}v{active}:{_b64e(nonce)}:{_b64e(ct)}"


def decrypt_field(token: Optional[str]) -> Optional[str]:
    """Decrypt an ``enc:`` token. Non-token (legacy plaintext) values pass through
    unchanged. Raises ValueError if a token is present but cannot be decrypted
    (tampered, or its key version is missing)."""
    if not is_encrypted(token):
        return token
    try:
        _, ver, nonce_b64, ct_b64 = token.split(":", 3)
        version = ver[1:] if ver.startswith("v") else ver
        ring, _active = _ring()
        key = ring.get(version)
        if not key:
            raise ValueError(f"no key for version {version!r}")
        pt = AESGCM(key).decrypt(_b64d(nonce_b64), _b64d(ct_b64), None)
        return pt.decode("utf-8")
    except Exception as exc:
        raise ValueError(f"decrypt_field failed: {exc}") from exc


# ─── JSON-value helpers (for dict/list PHI fields like structured_data) ────────
def encrypt_value(value: Any) -> Any:
    """Encrypt any JSON-serializable value to a token (None passes through)."""
    if value is None:
        return None
    return encrypt_field(json.dumps(value, default=str))


def decrypt_value(token: Any) -> Any:
    """Inverse of encrypt_value: decrypt a token back to its original JSON value."""
    if not is_encrypted(token):
        return token
    return json.loads(decrypt_field(token))


# ─── Binary-blob helpers (for raw bytes like uploaded audio) ──────────────────
# Audio (and other binary PHI) is encrypted as a self-describing binary blob
# rather than a base64 text token, to avoid ~33% size inflation on large files.
# Layout: _BMAGIC | 1-byte version length | version (ascii) | 12-byte nonce | ct.
# Plaintext/legacy bytes that don't start with the magic pass through unchanged,
# so encryption can be rolled out incrementally (mirrors decrypt_field).
_BMAGIC = b"FCRYPTB1"
# magic + version-length byte + >=1 version byte + 12-byte nonce + >=1 ct byte
_BMIN_LEN = len(_BMAGIC) + 1 + 1 + 12 + 1


def is_encrypted_bytes(value: Any) -> bool:
    return (
        isinstance(value, (bytes, bytearray))
        and len(value) >= _BMIN_LEN
        and bytes(value[: len(_BMAGIC)]) == _BMAGIC
    )


def encrypt_bytes(data: Optional[bytes]) -> Optional[bytes]:
    """Encrypt raw bytes to a binary blob. Returns the bytes unchanged when no
    key is configured (dev fall-back; production is guarded at startup)."""
    if data is None:
        return None
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("encrypt_bytes expects bytes")
    data = bytes(data)
    ring, active = _ring()
    key = ring.get(active)
    if not key or len(key) != 32:
        return data  # not configured -> passthrough
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, data, None)
    version = active.encode("ascii")
    return _BMAGIC + bytes([len(version)]) + version + nonce + ct


def decrypt_bytes(blob: Optional[bytes]) -> Optional[bytes]:
    """Decrypt a binary blob produced by ``encrypt_bytes``. Non-blob (legacy
    plaintext) bytes pass through unchanged. Raises ValueError if a blob is
    present but cannot be decrypted (tampered, or its key version is missing)."""
    if blob is None:
        return None
    if not is_encrypted_bytes(blob):
        return blob
    blob = bytes(blob)
    try:
        i = len(_BMAGIC)
        vlen = blob[i]
        i += 1
        version = blob[i : i + vlen].decode("ascii")
        i += vlen
        nonce = blob[i : i + 12]
        i += 12
        ct = blob[i:]
        ring, _active = _ring()
        key = ring.get(version)
        if not key:
            raise ValueError(f"no key for version {version!r}")
        return AESGCM(key).decrypt(nonce, ct, None)
    except Exception as exc:
        raise ValueError(f"decrypt_bytes failed: {exc}") from exc
