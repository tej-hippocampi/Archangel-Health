"""Media review and buyer delivery. Never expose commercial policy to providers."""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from asclepius import auth, media_delivery as D, media_store as M, media_storage as B
from asclepius.store import get_store
from routers.asclepius_media_ingest import execute


def gate():
    if not M.enabled():
        raise HTTPException(404, "Bulk uploads are not enabled.")


router = APIRouter(prefix="/api/asclepius", dependencies=[Depends(gate)])


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Links or identifiers for externally completed malware, rights and video
    # privacy review. No automated clearance is claimed by this application.
    malware_evidence: str = Field(min_length=1, max_length=2000)
    privacy_evidence: str = Field(min_length=1, max_length=2000)
    rights_evidence: str = Field(min_length=1, max_length=2000)
    approved: bool


class Delivery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    files: list[str] = Field(min_length=1, max_length=1000)
    buyer: str = Field(min_length=1, max_length=128)


@router.get("/admin/media/{org}/collections")
def collections(org: str, after: str = Query("", max_length=64), admin=Depends(auth.require_admin)):
    return execute(lambda: {"collections": M.get_store().collections(org, after)})


@router.get("/admin/media/{org}/collections/{cid}")
def catalog(org: str, cid: str, after: str = Query("", max_length=64), admin=Depends(auth.require_admin)):
    return execute(lambda: {"files": M.get_store().catalog(org, cid, after)})


@router.post("/admin/media/{org}/files/{fid}/review")
def review(org: str, fid: str, body: Review, admin=Depends(auth.require_admin)):
    return execute(lambda: D.review(M.get_store(), org, fid, admin["id"], body.model_dump(), body.approved))


@router.post("/admin/media/{org}/deliveries")
def deliver(org: str, body: Delivery, admin=Depends(auth.require_admin)):
    buyer = get_store().get_user_by_id(body.buyer)
    if not buyer or buyer.get("role") != "buyer" or not buyer.get("active", True):
        raise HTTPException(422, "Choose an existing buyer account.")
    return execute(lambda: {"id": D.create(M.get_store(), org, list(dict.fromkeys(body.files)), body.buyer, admin["id"])})


@router.post("/admin/media/{org}/files/{fid}/retry")
def retry(org: str, fid: str, admin=Depends(auth.require_admin)):
    return execute(lambda: D.retry(M.get_store(), org, fid, admin["id"]))


@router.get("/buyer/media/{did}")
def manifest(did: str, buyer=Depends(auth.require_buyer)):
    def work():
        data = D.manifest(M.get_store(), did, buyer["id"])
        return {**data, "files": [{k: v for k, v in f.items() if k != "key"} for f in data["files"]]}
    return execute(work)


@router.post("/buyer/media/{did}/files/{fid}/download")
def download(did: str, fid: str, buyer=Depends(auth.require_buyer)):
    return execute(lambda: D.download(M.get_store(), B.get_storage(), did, fid, buyer["id"]))
