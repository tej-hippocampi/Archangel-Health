"""Generated audio has its own public tree, never the shared scratch directory."""
from pathlib import Path
import tempfile


def audio_root() -> Path:
    root = Path(tempfile.gettempdir()) / "archangel-generated-audio"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def cached_audio_url(url: str) -> str | None:
    # Only URLs minted by the audio writer can resolve to local public files.
    import re
    match = re.fullmatch(r"/audio/([a-f0-9]{32}\.mp3)", url or "")
    if not match:
        return None
    path = audio_root() / match.group(1)
    return url if path.is_file() and not path.is_symlink() else None
