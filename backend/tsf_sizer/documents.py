"""Read what the customer sent: PDF files with text and screenshots (for instance of the
Panorama GUI with performance figures), or the screenshots themselves.

Text is read from the PDF text layer. Pages with pictures, and image files, are read with OCR
(tesseract, installed in the Docker image) so figures in screenshots are not lost. Everything
happens inside the container. OCR can misread digits: the agent is told to confirm the figures
that decide the model.
"""

from __future__ import annotations

import io
import json
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw
from PIL import Image

MAX_PAGES = 60
MAX_DOCUMENTS = 10
OCR_DPI = 200
OCR_TIMEOUT_S = 90
OCR_TOTAL_S = 600
MAX_IMAGE_PIXELS = 60_000_000
EXTENSIONS = {".pdf": "pdf", ".png": "image", ".jpg": "image", ".jpeg": "image"}

Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


class DocumentError(ValueError):
    """The file cannot be read; the message is shown to the engineer."""


@dataclass
class PageText:
    number: int
    text: str
    method: str  # text | ocr | text+ocr | empty


@dataclass
class Extracted:
    kind: str
    pages: list[PageText] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return sum(len(p.text) for p in self.pages)

    @property
    def method(self) -> str:
        methods = {p.method for p in self.pages if p.method != "empty"}
        if not methods:
            return "empty"
        return "+".join(sorted({m for x in methods for m in x.split("+")}))


def ocr_status() -> dict:
    exe = shutil.which("tesseract")
    if not exe:
        return {"available": False, "version": None}
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
        version = (out.stdout or out.stderr).splitlines()[0].strip()
    except (OSError, subprocess.SubprocessError, IndexError):
        version = "tesseract"
    return {"available": True, "version": version}


def kind_of(filename: str, head: bytes) -> str:
    """pdf or image, from the content (the extension only has to be plausible)."""
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n") or head.startswith(b"\xff\xd8\xff"):
        return "image"
    raise DocumentError(f"{filename}: not a PDF, PNG or JPEG file")


def _ocr(img: Image.Image) -> str:
    exe = shutil.which("tesseract")
    if not exe:
        raise FileNotFoundError("tesseract")
    img = img.convert("RGB")
    if img.width < 1600:  # screenshots are small: bigger letters read better
        factor = 1600 / img.width
        img = img.resize((1600, max(1, int(img.height * factor))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    out = subprocess.run(
        [exe, "stdin", "stdout", "--psm", "6", "-c", "preserve_interword_spaces=1"],
        input=buf.getvalue(),
        capture_output=True,
        timeout=OCR_TIMEOUT_S,
    )
    return out.stdout.decode("utf-8", errors="replace").strip()


def _clean(text: str) -> str:
    lines = [ln.rstrip() for ln in text.replace("\r", "\n").split("\n")]
    out, blank = [], 0
    for ln in lines:
        blank = blank + 1 if not ln.strip() else 0
        if blank <= 1:
            out.append(ln)
    return "\n".join(out).strip()


def _merge(layer: str, ocr: str) -> str:
    """The text layer, plus the OCR lines that are not already in it."""
    have = {" ".join(ln.split()) for ln in layer.split("\n")}
    extra = [ln for ln in ocr.split("\n") if ln.strip() and " ".join(ln.split()) not in have]
    if not extra:
        return layer
    head = layer + "\n\n" if layer else ""
    return head + "[text read from screenshots on this page]\n" + "\n".join(extra)


def extract(path: Path, filename: str) -> Extracted:
    with path.open("rb") as fh:
        kind = kind_of(filename, fh.read(8))
    ocr_ok = ocr_status()["available"]
    result = Extracted(kind)
    if kind == "image":
        try:
            img = Image.open(path)
            img.load()
        except (OSError, Image.DecompressionBombError) as e:
            raise DocumentError(f"{filename}: the image cannot be read ({e})") from None
        if not ocr_ok:
            result.warnings.append("OCR is not installed: the text in this image was not read.")
            result.pages.append(PageText(1, "", "empty"))
            return result
        try:
            text = _clean(_ocr(img))
        except (OSError, subprocess.SubprocessError) as e:
            raise DocumentError(f"{filename}: OCR failed ({e})") from None
        result.pages.append(PageText(1, text, "ocr" if text else "empty"))
        return result

    try:
        pdf = pdfium.PdfDocument(str(path))
    except pdfium.PdfiumError:
        raise DocumentError(f"{filename}: the PDF is damaged or password protected") from None
    total = len(pdf)
    if total > MAX_PAGES:
        result.warnings.append(f"Only the first {MAX_PAGES} of {total} pages were read.")
    spent = 0.0
    import time

    skipped_ocr = False
    for i in range(min(total, MAX_PAGES)):
        page = pdf[i]
        layer = _clean(page.get_textpage().get_text_bounded() or "")
        images = list(page.get_objects(filter=[pdfium_raw.FPDF_PAGEOBJ_IMAGE]))
        needs_ocr = bool(images) or not layer  # a picture, or no text at all
        method = "text" if layer else "empty"
        if needs_ocr and ocr_ok and spent < OCR_TOTAL_S:
            started = time.monotonic()
            try:
                rendered = page.render(scale=OCR_DPI / 72).to_pil()
                ocr_text = _clean(_ocr(rendered))
            except (OSError, subprocess.SubprocessError):
                ocr_text = ""
                result.warnings.append(f"Page {i + 1}: OCR failed.")
            spent += time.monotonic() - started
            merged = _merge(layer, ocr_text)
            if ocr_text:
                method = "text+ocr" if layer else "ocr"
            layer = merged
        elif needs_ocr and images:
            skipped_ocr = True
        result.pages.append(PageText(i + 1, layer, method if layer else "empty"))
    if skipped_ocr:
        result.warnings.append(
            "OCR is not installed or ran out of time: text inside screenshots was not read."
        )
    if all(p.method == "empty" for p in result.pages):
        result.warnings.append("No text found in this file.")
    return result


# --------------------------------------------------------------------------- storage


def add_pending(conn: sqlite3.Connection, analysis_id: int, filename: str) -> int:
    return conn.execute(
        "INSERT INTO document (analysis_id, filename, status) VALUES (?, ?, 'reading')",
        (analysis_id, filename[:200]),
    ).lastrowid


def store(conn: sqlite3.Connection, doc_id: int, extracted: Extracted) -> None:
    conn.execute(
        "UPDATE document SET status='done', kind=?, pages=?, chars=?, method=?, pages_json=?, "
        "warnings_json=? WHERE id=?",
        (
            extracted.kind,
            len(extracted.pages),
            extracted.chars,
            extracted.method,
            json.dumps(
                [{"n": p.number, "text": p.text, "method": p.method} for p in extracted.pages]
            ),
            json.dumps(extracted.warnings),
            doc_id,
        ),
    )


def fail(conn: sqlite3.Connection, doc_id: int, message: str) -> None:
    conn.execute(
        "UPDATE document SET status='error', warnings_json=? WHERE id=?",
        (json.dumps([message]), doc_id),
    )


def process(db_path: str, items: list[tuple[int, Path, str]]) -> None:
    """Background job: read each (document id, file, display name). Never raises; the files are
    deleted afterwards."""
    from . import db

    conn = db.connect(db_path)
    try:
        for doc_id, path, name in items:
            try:
                extracted = extract(path, name)
                with conn:
                    store(conn, doc_id, extracted)
            except DocumentError as e:
                with conn:
                    fail(conn, doc_id, str(e))
            except Exception as e:  # noqa: BLE001 - one bad file must not stop the others
                with conn:
                    fail(conn, doc_id, f"{name}: could not be read ({type(e).__name__})")
            finally:
                path.unlink(missing_ok=True)
    finally:
        conn.close()


def list_for(conn: sqlite3.Connection, analysis_id: int) -> list[dict]:
    out = []
    for r in conn.execute(
        "SELECT id, filename, kind, status, pages, chars, method, warnings_json, pages_json "
        "FROM document WHERE analysis_id=? ORDER BY id",
        (analysis_id,),
    ):
        d = dict(r)
        d["warnings"] = json.loads(d.pop("warnings_json") or "[]")
        d["pages_data"] = json.loads(d.pop("pages_json") or "[]")
        out.append(d)
    return out
