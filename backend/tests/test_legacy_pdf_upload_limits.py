"""The existing anonymous PDF helper keeps its response and bounds input abuse."""
import asyncio

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import main
import ratelimit
from tests.test_community_attachment_screening import document_bytes, ordinary_document


def test_pdf_helper_stops_reading_at_25mb():
    class OversizedUpload:
        consumed = 0

        async def read(self, size=-1):
            assert 0 < size <= 64 * 1024
            self.consumed += size
            return b"x" * size

    upload = OversizedUpload()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.upload_pdf(upload))
    assert upload.consumed == 25 * 1024 * 1024 + 1
    assert exc.value.status_code == 413
    assert exc.value.detail == "File exceeds 25MB limit"


@pytest.fixture()
def pdf_client(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "1")
    ratelimit.reset()
    yield TestClient(main.app)
    ratelimit.reset()


def test_pdf_helper_keeps_anonymous_success_shape_and_limits_one_client(pdf_client):
    data = document_bytes(ordinary_document())
    for _ in range(10):
        response = pdf_client.post("/api/upload-pdf", files={"file": ("fixture.pdf", data, "application/pdf")})
        assert response.status_code == 200
        assert response.json() == {"text": "Clean clinical discussion", "pages": 1}
    refused = pdf_client.post("/api/upload-pdf", files={"file": ("fixture.pdf", data, "application/pdf")})
    assert refused.status_code == 429 and refused.headers["retry-after"]


def test_pdf_helper_also_has_a_shared_global_limit(pdf_client, monkeypatch):
    # Independent synthetic clients should still share the volumetric cap.
    monkeypatch.setattr(ratelimit, "client_ip", lambda request: request.headers.get("test-client-id", "fixture"))
    data = document_bytes(ordinary_document())
    for number in range(60):
        response = pdf_client.post("/api/upload-pdf", headers={"test-client-id": str(number)},
                                   files={"file": ("fixture.pdf", data, "application/pdf")})
        assert response.status_code == 200
    refused = pdf_client.post("/api/upload-pdf", headers={"test-client-id": "another"},
                              files={"file": ("fixture.pdf", data, "application/pdf")})
    assert refused.status_code == 429 and refused.headers["retry-after"]
