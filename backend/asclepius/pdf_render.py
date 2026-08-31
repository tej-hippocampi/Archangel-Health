"""A minimal, correct, dependency-free PDF writer for plain-text documents.

Lifted out of ``asclepius/credentials.py``, which had been the only caller until
a countersigned data licensing agreement needed the same thing. Both documents
are the same shape -- headings and wrapped body text on US Letter, one font, no
images -- and both are things a third party may hold onto for years, so neither
should depend on a PDF library being present at render time.

``credentials.py`` re-exports the names it used to define, so its call sites and
tests reach them unchanged.

WHY NOT reportlab: a legal record has to render the same way in five years as it
does today. A hand-built object graph of a few hundred lines has no version to
drift, no C extension to fail to build, and no upstream that can change how a
line breaks between releases. The tradeoff -- no font embedding, no Unicode
beyond cp1252 -- is real, and is why ``render_text_pdf`` substitutes rather than
silently dropping a character it cannot draw.
"""

from __future__ import annotations

from typing import Iterable, List, Sequence, Tuple

# ─── Page geometry ───────────────────────────────────────────────────────────
PAGE_W, PAGE_H = 612, 792          # US Letter, 72 dpi
MARGIN = 54
LINE_H = 14
FONT_SIZE = 10
HEAD_SIZE = 15
SUB_SIZE = 11
BANNER_SIZE = 8
MAX_CHARS = 92                      # wrap width at 10pt Helvetica

#: The row kinds ``render_text_pdf`` understands. Anything else is drawn as body.
KIND_HEAD = "head"
KIND_SUB = "sub"
KIND_BODY = "body"
KIND_GAP = "gap"
KIND_MONO = "mono"                  # drawn in Courier: hashes, ids, anything to be read character by character


def pdf_escape(text: str) -> str:
    return (text or "").replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def wrap(text: str, width: int = MAX_CHARS) -> List[str]:
    """Greedy wrap that preserves blank lines and hard-breaks long tokens."""
    out: List[str] = []
    for raw in (text or "").split("\n"):
        raw = raw.rstrip()
        if not raw:
            out.append("")
            continue
        line = ""
        for word in raw.split(" "):
            if not line:
                line = word
            elif len(line) + 1 + len(word) <= width:
                line += " " + word
            else:
                out.append(line)
                line = word
            # hard-break absurdly long tokens
            while len(line) > width:
                out.append(line[:width])
                line = line[width:]
        out.append(line)
    return out


def render_text_pdf(rows: Sequence[Tuple[str, str]], *, banner: str = "") -> bytes:
    """Lay ``(kind, text)`` rows out across as many Letter pages as needed.

    ``banner`` is drawn once at the top of every page (a confidentiality
    watermark, a document version). Empty means no banner and the extra space is
    given back to the body.
    """
    usable_top = PAGE_H - MARGIN - (24 if banner else 0)
    usable_bottom = MARGIN
    lines_per_page = max(1, int((usable_top - usable_bottom) / LINE_H))

    pages: List[str] = []
    cur: List[str] = []

    def flush() -> None:
        if cur:
            pages.append("".join(cur))

    def begin_page() -> None:
        cur.clear()
        if banner:
            cur.append("BT /F1 %d Tf 1 0 0 1 %d %d Tm (%s) Tj ET\n" % (
                BANNER_SIZE, MARGIN, PAGE_H - MARGIN + 6, pdf_escape(banner)))

    y_state = {"y": usable_top}
    begin_page()
    count = 0
    for kind, text in rows:
        if count >= lines_per_page:
            flush()
            begin_page()
            y_state["y"] = usable_top
            count = 0
        y = y_state["y"]
        if kind != KIND_GAP:
            if kind == KIND_HEAD:
                size, font = HEAD_SIZE, "/F1"
            elif kind == KIND_SUB:
                size, font = SUB_SIZE, "/F1"
            elif kind == KIND_MONO:
                size, font = FONT_SIZE, "/F2"
            else:
                size, font = FONT_SIZE, "/F1"
            cur.append("BT %s %d Tf 1 0 0 1 %d %d Tm (%s) Tj ET\n" % (
                font, size, MARGIN, int(y), pdf_escape(text)))
        y_state["y"] = y - (LINE_H + (6 if kind in (KIND_HEAD, KIND_SUB) else 0))
        count += 1
    flush()
    if not pages:
        begin_page()
        flush()

    return assemble_pdf(pages)


def assemble_pdf(page_streams: Iterable[str]) -> bytes:
    """Build the PDF object graph (catalog, pages, per-page content + fonts)."""
    streams = list(page_streams)
    objects: List[bytes] = []

    def add(obj: bytes) -> int:
        objects.append(obj)
        return len(objects)  # 1-based object number

    # WinAnsiEncoding (≈ cp1252) so accented Latin names (e.g. "José") render
    # correctly rather than as the wrong StandardEncoding glyph.
    font_num = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                   b"/Encoding /WinAnsiEncoding >>")
    mono_num = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier "
                   b"/Encoding /WinAnsiEncoding >>")

    page_obj_nums: List[int] = []
    content_nums: List[int] = []
    for stream in streams:
        # 'replace' rather than 'ignore': a character this font cannot draw
        # becomes a visible '?' instead of vanishing. In a signed document a
        # silently dropped character is a changed word.
        data = stream.encode("cp1252", "replace")
        content = b"<< /Length %d >>\nstream\n%s\nendstream" % (len(data), data)
        content_nums.append(add(content))

    # placeholder for pages tree number (filled after page objects exist)
    pages_tree_num = len(objects) + len(streams) + 1
    for cnum in content_nums:
        page = (
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] "
            b"/Resources << /Font << /F1 %d 0 R /F2 %d 0 R >> >> /Contents %d 0 R >>"
            % (pages_tree_num, PAGE_W, PAGE_H, font_num, mono_num, cnum)
        )
        page_obj_nums.append(add(page))

    kids = b" ".join(b"%d 0 R" % n for n in page_obj_nums)
    pages_tree = b"<< /Type /Pages /Count %d /Kids [%s] >>" % (len(page_obj_nums), kids)
    pages_tree_actual = add(pages_tree)
    assert pages_tree_actual == pages_tree_num, (pages_tree_actual, pages_tree_num)
    catalog_num = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_tree_num)

    # Serialize with a cross-reference table.
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0] * (len(objects) + 1)
    for i, obj in enumerate(objects, start=1):
        offsets[i] = len(out)
        out += b"%d 0 obj\n" % i
        out += obj
        out += b"\nendobj\n"
    xref_pos = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for i in range(1, len(objects) + 1):
        out += b"%010d 00000 n \n" % offsets[i]
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\n" % (len(objects) + 1, catalog_num)
    out += b"startxref\n%d\n%%%%EOF\n" % xref_pos
    return bytes(out)
