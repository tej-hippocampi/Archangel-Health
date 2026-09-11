"""File acknowledgements must follow durable bytes and directory entries."""
import os
import tempfile
from pathlib import Path


def sync_directory(path):
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)



def sync_ancestors(path):
    current = Path(path).resolve()
    while current.parent != current:
        sync_directory(current)
        current = current.parent


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        sync_ancestors(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
