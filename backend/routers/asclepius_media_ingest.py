"""Authenticated bulk file control API. Video bytes go directly to S3."""
import base64

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from asclepius import hs_access, hs_states, media_service, media_storage, media_store
from asclepius.store import get_store
from routers.asclepius_provider import PortalRoute, require_hs_surface

router = APIRouter(prefix="/api/asclepius/hs/media", route_class=PortalRoute)


def user_gate(user=Depends(require_hs_surface(hs_access.UPLOAD))):
    if not media_store.enabled():
        raise HTTPException(404, "Bulk uploads are not enabled.")
    if user.get("must_reset") or not hs_states.can_upload(user.get("health_system")):
        raise HTTPException(403, "This account cannot upload yet.")
    readiness = hs_states.data_readiness_error(get_store(), user['hs_id'])
    if readiness:
        raise HTTPException(403, readiness)
    return user


def execute(fn):
    try:
        return fn()
    except media_store.MediaError as exc:
        raise HTTPException(exc.status, str(exc)) from None


class Declare(BaseModel):
    model_config = ConfigDict(extra="forbid")
    collection: str = Field(min_length=1, max_length=64)
    token: str = Field(min_length=1, max_length=128)
    path: str = Field(min_length=1, max_length=1024)
    size: int = Field(gt=0, le=1024**4)
    sha256: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")


class Sign(BaseModel):
    model_config = ConfigDict(extra="forbid")
    number: int = Field(ge=1, le=10000)
    checksum: str = Field(min_length=44, max_length=44)


class Import(BaseModel):
    model_config = ConfigDict(extra="forbid")
    collection: str = Field(min_length=1, max_length=64)
    token: str = Field(min_length=1, max_length=128)
    path: str = Field(min_length=1, max_length=1024)
    source_id: str = Field(min_length=1, max_length=128)
    source_key: str = Field(min_length=1, max_length=1024)
    source_version: str = Field(min_length=1, max_length=1024)


@router.post("/imports", status_code=202)
def cloud_import(body: Import, user=Depends(user_gate)):
    from asclepius.media_sources import declare_import
    return execute(lambda: media_service.public(declare_import(media_store.get_store(),
        media_storage.get_storage(), get_store(), user, body.model_dump())))


@router.get("/capabilities")
def capabilities(user=Depends(user_gate)):
    return {"enabled": True, "max_file_bytes": 1024**4, "max_collection_files": 100000}


@router.get("/collections")
def collections(after: str = Query("", max_length=64), user=Depends(user_gate)):
    return execute(lambda: {"collections": media_store.get_store().collections(user['hs_id'], after)})


@router.post("/collections")
def collection(user=Depends(user_gate)):
    return execute(lambda: {"id": media_store.get_store().collection(user["hs_id"])})


@router.get("/collections/{cid}")
def catalog(cid: str, after: str = Query("", max_length=64), user=Depends(user_gate)):
    return execute(lambda: {"files": [media_service.public(r) for r in media_store.get_store().catalog(user["hs_id"], cid, after)]})


@router.post("/files")
def declare(body: Declare, user=Depends(user_gate)):
    def work():
        store = media_store.get_store()
        row = media_service.declare_for_account(store, get_store(), user, body.model_dump())
        return media_service.public(media_service.initialize(store, media_storage.get_storage(), row))
    return execute(work)


@router.get("/files/{fid}")
def state(fid: str, user=Depends(user_gate)):
    def work():
        store = media_store.get_store()
        row = store.get(user["hs_id"], fid, user["username"])
        parts = []
        if row["state"] == "uploading":
            parts = [{"number": p["PartNumber"], "checksum": p.get("ChecksumSHA256"), "size": p["Size"]} for p in media_storage.get_storage().parts(row)]
        return {**media_service.public(row), "parts": parts}
    return execute(work)


@router.post("/files/{fid}/sign")
def sign(fid: str, body: Sign, user=Depends(user_gate)):
    try:
        if len(base64.b64decode(body.checksum, validate=True)) != 32:
            raise ValueError()
    except ValueError:
        raise HTTPException(422, "Invalid part checksum.") from None
    def work():
        store = media_store.get_store()
        row = store.get(user["hs_id"], fid, user["username"])
        if body.number > row["part_count"]:
            raise media_store.MediaError("Invalid part number.", 422)
        # Refresh inactivity before signing. Closed sessions cannot mint URLs.
        row = store.change(user["hs_id"], fid, ("uploading",))
        return {"url": media_storage.get_storage().sign(row, body.number, body.checksum), "expires_in": 300}
    return execute(work)


@router.post("/files/{fid}/complete", status_code=202)
def complete(fid: str, user=Depends(user_gate)):
    def work():
        store = media_store.get_store()
        row = store.get(user["hs_id"], fid, user["username"])
        if row["state"] == "uploading":
            row = store.change(user["hs_id"], fid, ("uploading",), state="completing")
        return media_service.public(row)
    return execute(work)


@router.delete("/files/{fid}", status_code=202)
def cancel(fid: str, user=Depends(user_gate)):
    def work():
        store = media_store.get_store()
        row = store.get(user["hs_id"], fid, user["username"])
        if row["state"] not in ("cancelled", "cancelling"):
            row = store.change(user["hs_id"], fid, ("initiating", "uploading", "importing"), state="cancelling")
        return media_service.public(row)
    return execute(work)
