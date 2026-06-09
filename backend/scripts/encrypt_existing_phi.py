"""One-time migration: encrypt PHI fields in an existing plaintext patient-store
snapshot (PRD-6). Idempotent — already-encrypted fields pass through untouched.

Usage (DATA_ENCRYPTION_KEY must be set in the environment):
    cd backend && python3 scripts/encrypt_existing_phi.py [path-to-snapshot.json]
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import field_crypto  # noqa: E402
from main import (  # noqa: E402
    _demo_patient_store_snapshot_path,
    _encrypt_patient_blob,
)


def main() -> int:
    if not field_crypto.is_configured():
        print("ERROR: DATA_ENCRYPTION_KEY is not set — nothing to do.")
        return 2
    path = sys.argv[1] if len(sys.argv) > 1 else _demo_patient_store_snapshot_path()
    if not path or not os.path.isfile(path):
        print(f"No snapshot found at: {path}")
        return 1
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    out = {pid: _encrypt_patient_blob(blob) for pid, blob in data.items() if isinstance(blob, dict)}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False, default=str)
    os.replace(tmp, path)
    print(f"Encrypted PHI fields in {len(out)} patient record(s) -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
