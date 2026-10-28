"""Small PDF and image files for the tests, built on the fly (nothing from a customer)."""

import io

from PIL import Image, ImageDraw, ImageFont

ROWS = [
    ("Metric", "Avg", "Max"),
    ("Throughput (Mbps)", "812", "1,850"),
    ("Connections per second", "2,450", "6,100"),
    ("Concurrent sessions", "91,200", "143,000"),
    ("Dataplane CPU %", "18", "61"),
]


def screenshot(rows=ROWS, host="fw-branch-1.corp.example.com 10.20.30.40") -> Image.Image:
    img = Image.new("RGB", (1100, 60 * (len(rows) + 2)), "white")
    d = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=26)
    d.text((30, 10), f"Panorama - {host}", fill="black", font=font)
    y = 70
    for a, b, c in rows:
        d.text((30, y), a, fill="black", font=font)
        d.text((600, y), b, fill="black", font=font)
        d.text((850, y), c, fill="black", font=font)
        y += 60
    return img


def png_bytes(**kw) -> bytes:
    buf = io.BytesIO()
    screenshot(**kw).save(buf, format="PNG")
    return buf.getvalue()


def image_pdf_bytes(**kw) -> bytes:
    buf = io.BytesIO()
    screenshot(**kw).save(buf, format="PDF")
    return buf.getvalue()


def text_pdf_bytes(pages: list[str]) -> bytes:
    """A PDF with a real text layer, one string per page (no pictures)."""
    objs: list[bytes] = []

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    add(b"<< /Type /Catalog /Pages 2 0 R >>")
    add(b"")  # pages, filled in below
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    kids = []
    for text in pages:
        lines = text.split("\n")
        stream = (
            b"BT /F1 12 Tf 14 TL 50 740 Td "
            + b" ".join(
                b"(" + ln.replace("(", "[").replace(")", "]").encode("latin-1") + b") Tj T*"
                for ln in lines
            )
            + b" ET"
        )
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        page = add(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents %d 0 R "
            b"/Resources << /Font << /F1 %d 0 R >> >> >>" % (content, font)
        )
        kids.append(page)
    objs[1] = (
        b"<< /Type /Pages /Kids ["
        + b" ".join(b"%d 0 R" % k for k in kids)
        + b"] /Count %d >>" % len(kids)
    )
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def _zip(files: dict[str, bytes]) -> bytes:
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def docx_bytes(
    paragraphs: list[str], table: list[list[str]] | None = None, picture: bytes | None = None
) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    if table:
        rows = "".join(
            "<w:tr>"
            + "".join(f"<w:tc><w:p><w:r><w:t>{c}</w:t></w:r></w:p></w:tc>" for c in row)
            + "</w:tr>"
            for row in table
        )
        body += f"<w:tbl>{rows}</w:tbl>"
    files = {
        "[Content_Types].xml": b"<Types/>",
        "word/document.xml": f"<w:document {W}><w:body>{body}</w:body></w:document>".encode(),
    }
    if picture:
        files["word/media/image1.png"] = picture
    return _zip(files)


def xlsx_bytes(sheets: dict[str, list[list]]) -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def pptx_bytes(slides: list[list[str]], picture_on_slide: tuple[int, bytes] | None = None) -> bytes:
    A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    files = {"ppt/presentation.xml": b"<p/>"}
    for i, paras in enumerate(slides, 1):
        runs = "".join(f"<a:p><a:r><a:t>{t}</a:t></a:r></a:p>" for t in paras)
        files[f"ppt/slides/slide{i}.xml"] = f"<sld {A}><txBody>{runs}</txBody></sld>".encode()
    if picture_on_slide:
        n, data = picture_on_slide
        files["ppt/media/image1.png"] = data
        files[f"ppt/slides/_rels/slide{n}.xml.rels"] = (
            b'<Relationships><Relationship Id="rId1" Target="../media/image1.png"/></Relationships>'
        )
    return _zip(files)


def odt_bytes(paragraphs: list[str]) -> bytes:
    body = "".join(f"<text:p>{p}</text:p>" for p in paragraphs)
    xml = (
        '<office:document-content xmlns:office="urn:o" xmlns:text="urn:t">'
        f"<office:body>{body}</office:body></office:document-content>"
    )
    return _zip(
        {"mimetype": b"application/vnd.oasis.opendocument.text", "content.xml": xml.encode()}
    )
