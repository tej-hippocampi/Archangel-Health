"""Present retained audio only after the caller has authorized this patient.

Legacy audio lived directly in /tmp. Never mount that directory or move its
contents: copy only an exact, patient-bound legacy recording on an authorized
read, and keep both the source bytes and stored patient metadata unchanged.
"""
from collections import OrderedDict
import os
from pathlib import Path
import re
import stat
import threading
import uuid

import realm
from audio_storage import audio_root, cached_audio_url

LEGACY_AUDIO_ROOT = Path("/tmp")
_MAX_LEGACY_BYTES = 128 * 1024 * 1024
_MIGRATED: OrderedDict[tuple, str] = OrderedDict()
_LOCK = threading.Lock()
_SUFFIXES = ("", "_diagnosis", "_treatment", "_preop", "_postop", "_chat")


def present_audio_url(url, patient_id: str):
    """Return a playable URL for an already-authorized patient's stored audio."""
    if not isinstance(url, str) or not url:
        return url
    # External audio remains a browser URL; this helper never fetches it.
    if url.startswith(("https://", "http://")):
        return url
    if cached_audio_url(url):
        return url
    # The old flat directory has no sandbox provenance. Do not infer ownership
    # of a live legacy recording from a colliding sandbox patient identifier.
    if realm.is_sandbox() or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", patient_id or ""):
        return None
    allowed = {f"/audio/audio_{patient_id}{suffix}.mp3" for suffix in _SUFFIXES}
    if url not in allowed:
        return None

    source = LEGACY_AUDIO_ROOT / url.removeprefix("/audio/")
    destination = None
    created = False
    try:
        # O_NOFOLLOW closes the check/open symlink race; fstat also excludes
        # directories and special files. O_NONBLOCK prevents opening a FIFO
        # from waiting before its type can be checked.
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as original:
            info = os.fstat(original.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_LEGACY_BYTES:
                return None
            key = (str(source), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            with _LOCK:
                cached = _MIGRATED.get(key)
                if cached and cached_audio_url(cached):
                    _MIGRATED.move_to_end(key)
                    return cached
                destination = audio_root() / (uuid.uuid4().hex + ".mp3")
                # Exclusive creation prevents overwriting any existing file.
                with destination.open("xb") as output:
                    created = True
                    copied = 0
                    while chunk := original.read(1024 * 1024):
                        copied += len(chunk)
                        if copied > _MAX_LEGACY_BYTES:
                            raise OSError("Legacy recording exceeds copy limit")
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                after = os.fstat(original.fileno())
                if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
                        info.st_size, info.st_mtime_ns, info.st_ctime_ns):
                    raise OSError("Legacy recording changed during copy")
                result = f"/audio/{destination.name}"
                _MIGRATED[key] = result
                while len(_MIGRATED) > 256:
                    _MIGRATED.popitem(last=False)
                return result
    except OSError:
        # Only remove an incomplete derived copy. The original is never opened
        # for writing, renamed, or deleted, including when storage fails.
        if created:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
        return None


def audio_presentation(value, patient_id: str):
    """Return a response copy with playable audio; never rewrite source data."""
    if isinstance(value, dict):
        return {
            key: (present_audio_url(item, patient_id)
                  if key in {"voice_audio_url", "audioUrl"}
                  else audio_presentation(item, patient_id))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [audio_presentation(item, patient_id) for item in value]
    return value
