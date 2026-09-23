"""Synthetic attachment regressions: complete screening before storage."""
import asyncio
import io
from pathlib import Path
import shutil
import subprocess

import pytest
from fastapi import HTTPException, Request
from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Link, Text
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NullObject, TextStringObject

from community import attachments as attachments
from community import router


def pdf(pages=1, *, width=612, height=792):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=width, height=height)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def document_bytes(writer):
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def ordinary_document():
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    contents = DecodedStreamObject()
    contents.set_data(b"BT /F1 12 Tf 72 720 Td (Clean clinical discussion) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(contents)
    writer.add_annotation(0, Text(rect=(50, 50, 70, 70), text="Useful teaching comment"))
    writer.add_annotation(0, Link(rect=(80, 50, 130, 70), url="https://example.org/guidance"))
    return writer


def fake_ocr(monkeypatch, *, identifier_page=None, fail_page=None, missing_page=None):
    rendered, screened = [], []

    def render(args, **kwargs):
        number = int(args[args.index("-f") + 1])
        assert args[args.index("-l") + 1] == str(number)
        assert 0 < kwargs["timeout"] <= 10
        rendered.append(number)
        if number == fail_page:
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        if number == missing_page:
            return
        with Image.new("RGB", (20, 20), (number, 0, 0)) as image:
            image.save(Path(args[-1]).with_suffix(".png"))

    def ocr(image, *, timeout):
        assert 0 < timeout <= 10
        number = image.getpixel((0, 0))[0]
        screened.append(number)
        return "MRN 99887766" if number == identifier_page else "Clean clinical finding"

    monkeypatch.setattr(attachments.subprocess, "run", render)
    monkeypatch.setattr("pytesseract.image_to_string", ocr)
    return rendered, screened


def test_identifier_on_page_after_old_ten_page_cutoff_is_rejected(monkeypatch):
    rendered, screened = fake_ocr(monkeypatch, identifier_page=12)
    with pytest.raises(attachments.AttachmentRejected) as exc:
        attachments.process_attachment(pdf(12), "application/pdf")
    assert exc.value.payload["code"] == "phi_detected"
    assert rendered == screened == list(range(1, 13))


def test_text_layer_does_not_skip_image_screening_on_any_page(monkeypatch):
    monkeypatch.setattr("pdfminer.high_level.extract_text", lambda *a, **k: "Safe text layer")
    rendered, screened = fake_ocr(monkeypatch, identifier_page=2)
    with pytest.raises(attachments.AttachmentRejected) as exc:
        attachments.process_attachment(pdf(2), "application/pdf")
    assert exc.value.payload["code"] == "phi_detected"
    assert rendered == screened == [1, 2]


def test_clean_supported_pdf_completes_all_pages(monkeypatch):
    rendered, screened = fake_ocr(monkeypatch)
    clean, mime = attachments.process_attachment(pdf(12), "application/pdf")
    assert clean.startswith(b"%PDF-") and mime == "application/pdf"
    assert rendered == screened == list(range(1, 13))


@pytest.mark.parametrize("payload", [pdf(attachments.MAX_PDF_PAGES + 1), pdf(width=100_000, height=100_000)])
def test_unsupported_pdf_budget_is_refused_before_rendering(payload, monkeypatch):
    rendered, screened = fake_ocr(monkeypatch)
    with pytest.raises(attachments.AttachmentRejected) as exc:
        attachments.process_attachment(payload, "application/pdf")
    assert exc.value.payload["code"] == "attachment_unscannable"
    assert rendered == screened == []


def test_failed_later_page_never_returns_partial_clean_result(monkeypatch):
    rendered, screened = fake_ocr(monkeypatch, fail_page=2)
    with pytest.raises(attachments.AttachmentRejected) as exc:
        attachments.process_attachment(pdf(3), "application/pdf")
    assert exc.value.payload["code"] == "attachment_unscannable"
    assert rendered == [1, 2] and screened == [1]


def test_missing_rendered_page_cannot_reuse_previous_clean_raster(monkeypatch):
    rendered, screened = fake_ocr(monkeypatch, missing_page=2)
    assert attachments._ocr_pdf_text(pdf(3)) is None
    assert rendered == [1, 2] and screened == [1]


def test_whole_document_deadline_refuses_incomplete_screening(monkeypatch):
    rendered, screened = fake_ocr(monkeypatch)
    ticks = iter([0, 0, 61])
    monkeypatch.setattr(attachments.time, "monotonic", lambda: next(ticks))
    assert attachments._ocr_pdf_text(pdf(3)) is None
    assert rendered == [1] and screened == []


def test_upload_stops_reading_at_limit_and_preserves_413_payload(monkeypatch):
    limit = 10 * 1024 * 1024
    monkeypatch.setattr(attachments, "max_attachment_bytes", lambda: limit)

    class OversizedUpload:
        content_type = "text/plain"
        consumed = 0

        async def read(self, size=-1):
            assert 0 < size <= 64 * 1024
            self.consumed += size
            return b"x" * size

    upload = OversizedUpload()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(router.upload_attachment(Request({"type": "http"}), upload, {"id": "fixture"}))
    assert upload.consumed == limit + 1
    assert exc.value.status_code == 413
    assert exc.value.detail == {"code": "too_large", "message": "Attachments are limited to 10 MB.", "categories": []}


@pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="Poppler is not installed")
def test_real_renderer_keeps_normal_letter_page_within_pixel_budget(monkeypatch):
    sizes = []

    def ocr(image, *, timeout):
        sizes.append(image.size)
        return "Clean clinical finding"

    monkeypatch.setattr("pytesseract.image_to_string", ocr)
    assert attachments._ocr_pdf_text(pdf()) == "Clean clinical finding"
    assert len(sizes) == 1
    assert 1600 <= sizes[0][0] <= 1800 and 2100 <= sizes[0][1] <= 2300


def test_nested_metadata_is_removed_without_changing_page_text_or_safe_annotations(monkeypatch):
    fake_ocr(monkeypatch)
    writer = ordinary_document()
    writer.add_metadata({"/Author": "Synthetic hidden author"})
    metadata = DecodedStreamObject()
    metadata[NameObject("/Type")] = NameObject("/Metadata")
    metadata[NameObject("/Subtype")] = NameObject("/XML")
    metadata.set_data(b"<xmp>synthetic-private-metadata</xmp>")
    meta_ref = writer._add_object(metadata)
    page = writer.pages[0]
    page[NameObject("/Metadata")] = meta_ref
    page["/Resources"]["/Font"]["/F1"][NameObject("/Metadata")] = meta_ref
    # A legitimate indirect cycle must terminate while still removing metadata.
    holder = DictionaryObject({NameObject("/Metadata"): meta_ref})
    holder_ref = writer._add_object(holder)
    holder[NameObject("/Cycle")] = holder_ref
    page[NameObject("/AuditFixture")] = holder_ref
    page["/Annots"][0].get_object()[NameObject("/Subj")] = NullObject()
    source = document_bytes(writer)
    clean, _ = attachments.process_attachment(source, "application/pdf")
    result = PdfReader(io.BytesIO(clean))
    assert result.pages[0].extract_text() == PdfReader(io.BytesIO(source)).pages[0].extract_text()
    assert "/Info" not in result.trailer
    assert "/Metadata" not in result.pages[0]
    assert "/Metadata" not in result.pages[0]["/Resources"]["/Font"]["/F1"]
    assert "/Metadata" not in result.pages[0]["/AuditFixture"]
    assert b"synthetic-private-metadata" not in clean
    annotations = result.pages[0]["/Annots"]
    assert len(annotations) == 2
    assert annotations[0].get_object()["/Contents"] == "Useful teaching comment"
    assert annotations[1].get_object()["/A"]["/URI"] == "https://example.org/guidance"


@pytest.mark.parametrize("field,content", [("/Contents", "MRN 99887766"), ("/RC", "&#77;RN 99887766")])
def test_nonrendered_annotation_text_is_screened(monkeypatch, field, content):
    fake_ocr(monkeypatch)
    writer = ordinary_document()
    writer.pages[0]["/Annots"][0].get_object()[NameObject(field)] = TextStringObject(content)
    with pytest.raises(attachments.AttachmentRejected) as exc:
        attachments.process_attachment(document_bytes(writer), "application/pdf")
    assert exc.value.payload["code"] == "phi_detected"


@pytest.mark.parametrize("kind", ["embedded_file", "form", "widget", "sound", "movie"])
def test_unscannable_embedded_containers_fail_closed_even_in_advisory_mode(monkeypatch, kind):
    fake_ocr(monkeypatch)
    monkeypatch.setenv("COMMUNITY_OCR_STRICT", "0")
    writer = ordinary_document()
    if kind == "embedded_file":
        writer.add_attachment("fixture.txt", b"Synthetic embedded document")
    elif kind == "form":
        writer.root_object[NameObject("/AcroForm")] = DictionaryObject({NameObject("/Fields"): ArrayObject()})
    elif kind == "widget":
        writer.pages[0]["/Annots"][0].get_object()[NameObject("/Subtype")] = NameObject("/Widget")
    else:
        writer.pages[0]["/Annots"][1].get_object()[NameObject("/A")] = DictionaryObject({
            NameObject("/S"): NameObject("/Sound" if kind == "sound" else "/Movie"),
        })
    with pytest.raises(attachments.AttachmentRejected) as exc:
        attachments.process_attachment(document_bytes(writer), "application/pdf")
    assert exc.value.payload["code"] == "attachment_unscannable"


@pytest.mark.parametrize("budget,value", [("MAX_PDF_OBJECTS", 3), ("MAX_PDF_OBJECT_DEPTH", 2)])
def test_pdf_object_traversal_refuses_excessive_work_or_depth(monkeypatch, budget, value):
    fake_ocr(monkeypatch)
    monkeypatch.setattr(attachments, budget, value)
    with pytest.raises(attachments.AttachmentRejected) as exc:
        attachments.process_attachment(document_bytes(ordinary_document()), "application/pdf")
    assert exc.value.payload["code"] == "attachment_unscannable"
