"""Read UploadFile contents with a running memory limit."""
from fastapi import HTTPException, UploadFile


async def read_capped(file: UploadFile, limit: int, *, detail: str) -> bytes:
    chunks = []
    remaining = limit
    while True:
        chunk = await file.read(min(64 * 1024, remaining + 1))
        if not chunk:
            return b"".join(chunks)
        remaining -= len(chunk)
        if remaining < 0:
            raise HTTPException(status_code=413, detail=detail)
        chunks.append(chunk)
