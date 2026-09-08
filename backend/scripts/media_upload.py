"""Resumable provider-side uploader for local folders / mounted NAS.

python -m scripts.media_upload --url https://portal.example --username NAME DIR
Passwords are prompted, never saved. Progress contains metadata only. Requires
httpx; bytes go directly to the signed object URL. Ctrl-C safely pauses.
"""
import argparse
import base64
import getpass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid
from urllib.parse import urlsplit

import httpx


def run(args):
    url = args.url.rstrip("/")
    if urlsplit(url).scheme != "https" or urlsplit(url).username or urlsplit(url).password:
        raise SystemExit("Use an HTTPS portal URL without credentials.")
    root = Path(args.directory).resolve()
    if not root.exists():
        raise SystemExit("The source path does not exist.")
    state_path = Path(args.state).resolve()
    fd = os.open(state_path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    db = sqlite3.connect(state_path, isolation_level=None)
    db.execute("CREATE TABLE IF NOT EXISTS progress(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    def read(key):
        row = db.execute("SELECT value FROM progress WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None
    def write(key, value):
        db.execute("INSERT INTO progress(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))
    state = read("collection") or {}
    identity = [url, args.username, args.realm, str(root)]
    if state and state.get("identity") != identity:
        raise SystemExit("This progress file belongs to another account or folder.")
    state.setdefault("identity", identity)
    def save():
        write("collection", state)

    with httpx.Client(base_url=url+"/api/asclepius/hs/", timeout=60,
                      headers={"X-Asclepius-Realm": args.realm}) as portal, httpx.Client(timeout=300) as transport:
        def api(method, path, body=None):
            response = portal.request(method, path, json=body)
            response.raise_for_status()
            return response.json()
        api("POST", "login", {"username": args.username, "password": getpass.getpass()})
        if not state.get("collection"):
            state["collection"] = api("POST", "media/collections")["id"]
            save()
        paths = [root] if root.is_file() else root.rglob("*")
        for path in paths:
            if not path.is_file() or path.is_symlink() or str(path.resolve()) in (str(state_path), str(state_path)+"-journal", str(state_path)+"-wal", str(state_path)+"-shm"):
                continue
            relative = path.name if root.is_file() else path.relative_to(root).as_posix()
            stat = path.stat()
            fingerprint = json.dumps([relative, stat.st_size, stat.st_mtime_ns])
            entry = read(fingerprint) or {"token": uuid.uuid4().hex}
            write(fingerprint, entry)
            row = api("POST", "media/files", dict(collection=state["collection"], token=entry["token"], path=relative, size=stat.st_size))
            row = api("GET", "media/files/"+row["id"])
            if row["state"] != "uploading":
                print(relative, row["state"])
                continue
            found = {p["number"]: p["checksum"] for p in row["parts"]}
            with path.open("rb") as file:
                for n in range(1, row["part_count"]+1):
                    data = file.read(row["chunk_size"])
                    checksum = base64.b64encode(hashlib.sha256(data).digest()).decode()
                    if n in found:
                        if found[n] != checksum:
                            raise RuntimeError("File changed; use a new progress file.")
                        continue
                    for attempt in range(5):
                        try:
                            signed = api("POST", "media/files/"+row["id"]+"/sign", dict(number=n, checksum=checksum))
                            response = transport.put(signed["url"], content=data, headers={"x-amz-checksum-sha256": checksum})
                            response.raise_for_status()
                            break
                        except (httpx.TransportError, httpx.HTTPStatusError):
                            if attempt == 4: raise
                            time.sleep(2**attempt)
                    print(relative, f"{n}/{row['part_count']} parts")
            if path.stat().st_size != stat.st_size or path.stat().st_mtime_ns != stat.st_mtime_ns:
                raise RuntimeError("File changed while reading; completion withheld.")
            api("POST", "media/files/"+row["id"]+"/complete")
            print(relative, "verification queued")
    db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--realm", choices=["live", "sandbox"], default="live")
    parser.add_argument("--state", default=".archangel-upload.sqlite")
    parser.add_argument("directory")
    run(parser.parse_args())
