#!/usr/bin/env python3
"""Upload the onboarding demo video to a running deployment (Onboarding v2 §0.1).

The video is ~73 MB and does NOT live in the repo. It lives in the asset store on
the server's persistent volume, and this is how it gets there — one command from
your laptop, straight into whichever environment you point it at.

    python3 backend/scripts/upload_onboarding_demo.py \\
        --base-url https://api.archangelhealth.ai \\
        --email you@archangelhealth.ai \\
        --file ~/Desktop/archangel-demo.mp4

You will be prompted for your admin password (or set ASCLEPIUS_ADMIN_PASSWORD).
Re-running with a new file replaces the demo everywhere, immediately — the URL
the player uses is a named slot, not a file name, so nothing else has to change.

Check what is installed without uploading anything:

    python3 backend/scripts/upload_onboarding_demo.py --base-url ... --email ... --status
"""

from __future__ import annotations

import argparse
import getpass
import mimetypes
import os
import sys

try:
    import httpx
except ImportError:  # pragma: no cover - dependency of the backend itself
    sys.exit("httpx is required: pip install httpx")

#: Long enough for a 73 MB upload over a hotel connection. The default (5s) times
#: out mid-transfer and looks like a server fault.
_TIMEOUT = httpx.Timeout(connect=15.0, read=900.0, write=900.0, pool=15.0)

_MIME_BY_EXT = {".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime"}


def _login(client: httpx.Client, base: str, email: str, password: str) -> str:
    r = client.post(f"{base}/api/asclepius/auth/login",
                    json={"email": email, "password": password})
    if r.status_code != 200:
        sys.exit(f"Sign-in failed ({r.status_code}): {r.text[:400]}")
    token = (r.json() or {}).get("token")
    if not token:
        sys.exit("Sign-in returned no token.")
    return token


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", required=True,
                    help="Backend origin, e.g. https://api.archangelhealth.ai")
    ap.add_argument("--email", required=True, help="An Asclepius ADMIN account.")
    ap.add_argument("--password", default=os.getenv("ASCLEPIUS_ADMIN_PASSWORD", ""),
                    help="Admin password (or set ASCLEPIUS_ADMIN_PASSWORD; prompts if absent).")
    ap.add_argument("--file", help="The video. MP4 (H.264 + AAC) plays everywhere.")
    ap.add_argument("--status", action="store_true",
                    help="Report what is currently installed and exit.")
    args = ap.parse_args()

    base = args.base_url.rstrip("/")
    if not args.status and not args.file:
        ap.error("--file is required unless --status is given")

    password = args.password or getpass.getpass(f"Password for {args.email}: ")

    with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
        token = _login(client, base, args.email, password)
        auth = {"Authorization": f"Bearer {token}"}

        if args.status:
            r = client.get(f"{base}/api/asclepius/assets/onboarding-demo/meta", headers=auth)
            if r.status_code != 200:
                sys.exit(f"Could not read demo status ({r.status_code}): {r.text[:400]}")
            meta = r.json()
            if not meta.get("available"):
                reason = meta.get("reason")
                print("No demo installed."
                      + (" (a row exists but its blob is gone — the asset store is not "
                         "persistent)" if reason == "blob_missing" else ""))
                return 1
            print(f"Demo installed · {meta.get('mime')} · "
                  f"{_human(float(meta.get('byte_size') or 0))} · version {meta.get('version')}")
            return 0

        path = os.path.expanduser(args.file)
        if not os.path.isfile(path):
            sys.exit(f"No such file: {path}")
        size = os.path.getsize(path)
        ext = os.path.splitext(path)[1].lower()
        mime = _MIME_BY_EXT.get(ext) or mimetypes.guess_type(path)[0] or ""
        if mime not in _MIME_BY_EXT.values():
            sys.exit(f"{ext or 'that file'} is not a supported video container. "
                     f"Use .mp4 (recommended), .webm or .mov.")

        print(f"Uploading {os.path.basename(path)} ({_human(size)}) to {base} …")
        with open(path, "rb") as fh:
            # httpx streams the file handle rather than reading it into memory.
            r = client.post(
                f"{base}/api/asclepius/admin/assets/onboarding-demo",
                headers=auth,
                files={"file": (os.path.basename(path), fh, mime)},
            )
        if r.status_code != 200:
            sys.exit(f"Upload failed ({r.status_code}): {r.text[:600]}")
        out = r.json()
        print(f"Done · sha {out['sha256'][:12]}… · {_human(float(out['byte_size']))}")
        if out.get("warning"):
            print(f"WARNING: {out['warning']}")
        print("It is live now at /api/asclepius/assets/onboarding-demo "
              "(signed-in physicians only).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
