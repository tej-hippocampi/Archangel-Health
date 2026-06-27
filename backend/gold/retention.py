"""Raw-audio retention enforcement (PRD §10).

Raw audio is the most sensitive artifact and is only needed until STT + QA are
done. This purges audio blobs older than ``GOLD_AUDIO_RETENTION_DAYS`` (default
30) once a transcript exists, deletes the file from disk, and marks the visit
``audio_deleted = 1``. The de-identified transcript/note (the product) are kept.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from gold import store
from gold.config import audio_retention_days

log = logging.getLogger("gold.retention")


def purge_expired_audio() -> int:
    """Delete audio past the retention window. Returns the number purged."""
    purged = 0
    for row in store.expired_audio_visits(audio_retention_days()):
        path = row.get("audio_path")
        try:
            if path and Path(path).exists():
                os.remove(path)
        except OSError as exc:  # pragma: no cover - fs dependent
            log.warning("gold retention: could not delete %s: %s", path, exc)
            continue
        store.update_visit(row["id"], audio_deleted=1, audio_path=None)
        purged += 1
    if purged:
        log.info("gold retention: purged %d expired audio blob(s)", purged)
    return purged
