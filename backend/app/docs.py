"""Document tools: PDF merge / split / rotate / compress / protect, PDF ↔ images and Word ↔ PDF.

Libraries (all permissive): pikepdf (MPL-2.0, on qpdf), pypdfium2 (Apache-2.0/BSD) for rendering and text,
Pillow (MIT-CMU), python-docx (MIT) and reportlab (BSD).

Honest limits, also shown in the interface:
* Word → PDF rebuilds the document from its text, headings, lists, tables and pictures. Exact page layout,
  headers/footers, columns and text boxes are not reproduced.
* PDF → Word extracts the text page by page. Layout is not kept, and scanned PDFs (pictures of text) need OCR,
  which is not built in.
* Compressing only helps PDFs with large pictures; an already-small PDF is reported as such.
Every output is reopened and checked before it is offered.
"""
from __future__ import annotations

import io
import json
import re
import secrets
import shutil
import zipfile
from pathlib import Path

import pikepdf
from PIL import Image, ImageOps

from .store import Store

MAX_FILES = 50


class DocError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


# ------------------------------------------------------------------ Word detection at upload
def is_docx(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as z:
            return "word/document.xml" in z.namelist()
    except (zipfile.BadZipFile, OSError):
        return False


def inspect_docx(path: Path) -> dict:
    from docx import Document
    from .inspect import InspectError
    try:
        doc = Document(str(path))
    except Exception as exc:  # noqa: BLE001 - python-docx raises many types for broken files
        raise InspectError("damaged", f"This Word document could not be opened ({exc}).", 422, detected="Word") from exc
    words = sum(len(p.text.split()) for p in doc.paragraphs)
    images = sum(1 for r in doc.part.rels.values() if "image" in r.reltype)
    info = {"paragraphs": len(doc.paragraphs), "words": words, "tables": len(doc.tables), "images": images}
    return {"kind": "document", "format": {"id": "docx", "label": "Word document", "detail": None}, "document": info, "outputs": []}


# ------------------------------------------------------------------ helpers
def _pages(spec: str, count: int) -> list[int]:
    """'1-3, 5' → [0, 1, 2, 4] (0-based). Empty → every page."""
    if not spec or not spec.strip():
        return list(range(count))
    out: list[int] = []
    for part in spec.replace(" ", "").split(","):
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            raise DocError("bad_request", f"“{part}” is not a page number or range like 2-5.")
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        if a < 1 or b > count or a > b:
            raise DocError("bad_request", f"Pages {part} are outside 1–{count}.")
        out.extend(range(a - 1, b))
    return out


def _ranges(spec: str, count: int) -> list[list[int]]:
    groups = []
    for part in (spec or "").replace(" ", "").split(","):
        if part:
            groups.append(_pages(part, count))
    if not groups:
        raise DocError("bad_request", "Enter the page ranges to split out, like 1-3, 4-6.")
    return groups


def _open_pdf(path: Path) -> pikepdf.Pdf:
    try:
        return pikepdf.open(path)
    except pikepdf.PasswordError as exc:
        raise DocError("encrypted", "This PDF is password-protected. Remove the password and upload it again.", 422) from exc
    except pikepdf.PdfError as exc:
        raise DocError("damaged", "This PDF is damaged and could not be opened.", 422) from exc


def _check_pdf(path: Path, password: str | None = None) -> int:
    with pikepdf.open(path, password=password or "") as pdf:
        return len(pdf.pages)


def _fonts():
    """A Unicode TrueType font for Word → PDF when the system has one; Helvetica (Latin only) otherwise."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    candidates = [("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", None),
                  ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
                  ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
                  ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")]
    for regular, bold in candidates:
        if Path(regular).exists():
            try:
                if "MFBody" not in pdfmetrics.getRegisteredFontNames():
                    pdfmetrics.registerFont(TTFont("MFBody", regular))
                    pdfmetrics.registerFont(TTFont("MFBold", bold if bold and Path(bold).exists() else regular))
                return "MFBody", "MFBold"
            except Exception:  # noqa: BLE001 - unreadable font file: try the next one
                continue
    return "Helvetica", "Helvetica-Bold"


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ------------------------------------------------------------------ tools
def merge(inputs: list[Path], out_dir: Path, stem: str, opts: dict) -> list[Path]:
    if len(inputs) < 2:
        raise DocError("bad_request", "Add at least two PDFs to merge.")
    merged = pikepdf.new()
    for p in inputs:
        with _open_pdf(p) as pdf:
            merged.pages.extend(pdf.pages)
    out = out_dir / f"{stem}-merged.pdf"
    merged.save(out, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.generate)
    return [out]


def split(inputs: list[Path], out_dir: Path, stem: str, opts: dict) -> list[Path]:
    with _open_pdf(inputs[0]) as pdf:
        n = len(pdf.pages)
        groups = [[i] for i in range(n)] if opts.get("mode", "each") == "each" else _ranges(opts.get("ranges", ""), n)
        outs = []
        for g in groups:
            part = pikepdf.new()
            for i in g:
                part.pages.append(pdf.pages[i])
            label = f"page-{g[0] + 1}" if len(g) == 1 else f"pages-{g[0] + 1}-{g[-1] + 1}"
            out = out_dir / f"{stem}-{label}.pdf"
            part.save(out)
            outs.append(out)
    return outs


def rotate(inputs: list[Path], out_dir: Path, stem: str, opts: dict) -> list[Path]:
    angle = int(opts.get("angle", 90))
    if angle not in (90, 180, 270):
        raise DocError("bad_request", "Rotate by 90, 180 or 270 degrees.")
    with _open_pdf(inputs[0]) as pdf:
        for i in _pages(opts.get("pages", ""), len(pdf.pages)):
            pdf.pages[i].rotate(angle, relative=True)
        out = out_dir / f"{stem}-rotated.pdf"
        pdf.save(out)
    return [out]


def compress(inputs: list[Path], out_dir: Path, stem: str, opts: dict) -> list[Path]:
    strong = opts.get("level") == "strong"
    max_side, quality = (1400, 55) if strong else (2000, 75)
    with _open_pdf(inputs[0]) as pdf:
        for page in pdf.pages:
            for _, raw in page.images.items():
                try:
                    img = pikepdf.PdfImage(raw)
                    if img.colorspace not in ("/DeviceRGB", "/DeviceGray") or img.bits_per_component != 8:
                        continue
                    pil = img.as_pil_image()
                except Exception:  # noqa: BLE001 - images we cannot decode are left untouched
                    continue
                if max(pil.size) > max_side:
                    pil.thumbnail((max_side, max_side), Image.LANCZOS)
                buf = io.BytesIO()
                pil.convert("L" if img.colorspace == "/DeviceGray" else "RGB").save(buf, "JPEG", quality=quality, optimize=True)
                if buf.tell() < len(raw.read_raw_bytes()):
                    raw.write(buf.getvalue(), filter=pikepdf.Name.DCTDecode)
                    raw.Width, raw.Height = pil.size
                    raw.ColorSpace = pikepdf.Name.DeviceGray if pil.mode == "L" else pikepdf.Name.DeviceRGB
                    raw.BitsPerComponent = 8
                    for k in ("/DecodeParms", "/Decode"):
                        if k in raw:
                            del raw[k]
        out = out_dir / f"{stem}-compressed.pdf"
        pdf.remove_unreferenced_resources()
        pdf.save(out, compress_streams=True, recompress_flate=True, object_stream_mode=pikepdf.ObjectStreamMode.generate)
    return [out]


def protect(inputs: list[Path], out_dir: Path, stem: str, opts: dict) -> list[Path]:
    password = opts.get("password") or ""
    if len(password) < 4:
        raise DocError("bad_request", "Use a password of at least 4 characters.")
    with _open_pdf(inputs[0]) as pdf:
        out = out_dir / f"{stem}-protected.pdf"
        pdf.save(out, encryption=pikepdf.Encryption(user=password, owner=password + secrets.token_hex(8), R=6))
    try:  # it must not open without the password, and must open with it
        pikepdf.open(out).close()
        raise DocError("verify_failed", "The protected PDF opened without a password; nothing was saved.", 500)
    except pikepdf.PasswordError:
        pass
    _check_pdf(out, password)
    return [out]


def pdf_to_images(inputs: list[Path], out_dir: Path, stem: str, opts: dict) -> list[Path]:
    import pypdfium2 as pdfium
    fmt = "jpg" if opts.get("format") == "jpg" else "png"
    scale = int(opts.get("dpi", 150)) / 72
    doc = pdfium.PdfDocument(str(inputs[0]))
    outs = []
    try:
        for i in range(len(doc)):
            im = doc[i].render(scale=scale).to_pil()
            out = out_dir / f"{stem}-page-{i + 1}.{fmt}"
            (im.convert("RGB").save(out, "JPEG", quality=90) if fmt == "jpg" else im.save(out, "PNG", optimize=True))
            outs.append(out)
    finally:
        doc.close()
    return outs


def images_to_pdf(inputs: list[Path], out_dir: Path, stem: str, opts: dict) -> list[Path]:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    out = out_dir / f"{stem}.pdf"
    c = canvas.Canvas(str(out))
    for p in inputs:
        with Image.open(p) as src:
            upright = src.getexif().get(0x0112, 1) in (0, 1)
            im = ImageOps.exif_transpose(src)
            if im.mode not in ("RGB", "L"):
                bg = Image.new("RGB", im.size, (255, 255, 255))
                bg.paste(im.convert("RGBA"), mask=im.convert("RGBA").getchannel("A"))
                im = bg
            w, h = im.size
            # JPEG photos go in unchanged (no quality loss, no growth); everything else as a high-quality JPEG.
            if src.format == "JPEG" and upright and src.mode in ("RGB", "L"):
                data = p.read_bytes()
            else:
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=92)
                data = buf.getvalue()
            im = io.BytesIO(data)
            if opts.get("page") == "a4":
                pw, ph = A4 if h >= w else (A4[1], A4[0])
                margin = 28
                s = min((pw - 2 * margin) / w, (ph - 2 * margin) / h)
                dw, dh = w * s, h * s
                c.setPageSize((pw, ph))
                c.drawImage(ImageReader(im), (pw - dw) / 2, (ph - dh) / 2, dw, dh)
            else:  # page the size of the photo, 150 pixels per inch
                pw, ph = w * 72 / 150, h * 72 / 150
                c.setPageSize((pw, ph))
                c.drawImage(ImageReader(im), 0, 0, pw, ph)
            c.showPage()
    c.save()
    return [out]


def word_to_pdf(inputs: list[Path], out_dir: Path, stem: str, opts: dict) -> list[Path]:
    from docx import Document
    from docx.oxml.ns import qn
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Image as RLImage
    from reportlab.platypus import ListFlowable, ListItem, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    body, bold = _fonts()
    doc = Document(str(inputs[0]))
    base = ParagraphStyle("body", fontName=body, fontSize=10.5, leading=14.5, spaceAfter=5)
    heads = {i: ParagraphStyle(f"h{i}", parent=base, fontName=bold, fontSize=s, leading=s * 1.25, spaceBefore=8, spaceAfter=5)
             for i, s in ((0, 22), (1, 17), (2, 14), (3, 12), (4, 11))}
    frame_w = A4[0] - 40 * mm

    def runs_markup(par) -> str:
        parts = []
        for r in par.runs:
            t = _esc(r.text)
            if not t:
                continue
            if r.bold:
                t = f'<font name="{bold}">{t}</font>'
            if r.italic:
                t = f"<i>{t}</i>"
            if r.underline:
                t = f"<u>{t}</u>"
            parts.append(t)
        return "".join(parts) or _esc(par.text)

    def pictures(par) -> list:
        flows = []
        for blip in par._p.iter(qn("a:blip")):
            rid = blip.get(qn("r:embed"))
            part = doc.part.related_parts.get(rid) if rid else None
            if part is None:
                continue
            try:
                with Image.open(io.BytesIO(part.blob)) as im:
                    w, h = im.size
            except Exception:  # noqa: BLE001 - e.g. EMF/WMF pictures Pillow cannot read
                continue
            s = min(1.0, frame_w / w, (A4[1] * 0.6) / h)
            flows.append(RLImage(io.BytesIO(part.blob), width=w * s, height=h * s))
        return flows

    story, bullets = [], []

    def flush():
        if bullets:
            story.append(ListFlowable([ListItem(Paragraph(b, base)) for b in bullets], bulletType="bullet", leftIndent=14))
            bullets.clear()

    element_map = {p._p: p for p in doc.paragraphs} | {t._tbl: t for t in doc.tables}
    for el in doc.element.body.iterchildren():
        obj = element_map.get(el)
        if obj is None:
            continue
        if el.tag == qn("w:tbl"):
            flush()
            data = [[Paragraph(_esc(cell.text), base) for cell in row.cells] for row in obj.rows]
            if data:
                t = Table(data, colWidths=[frame_w / max(len(r) for r in data)] * max(len(r) for r in data), repeatRows=1)
                t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
                story += [t, Spacer(1, 6)]
            continue
        style = (obj.style.name or "") if obj.style is not None else ""
        text = runs_markup(obj)
        if 'w:br' in el.xml and 'w:type="page"' in el.xml:
            flush()
            story.append(PageBreak())
        if style.startswith("List") or el.find(".//" + qn("w:numPr")) is not None:
            if obj.text.strip():
                bullets.append(text)
            continue
        flush()
        if style == "Title":
            story.append(Paragraph(text, heads[0]))
        elif style.startswith("Heading") and style[-1:].isdigit():
            story.append(Paragraph(text, heads[min(4, int(style[-1]))]))
        elif obj.text.strip():
            story.append(Paragraph(text, base))
        elif not pictures(obj):
            story.append(Spacer(1, 6))
        story += pictures(obj)
    flush()
    if not story:
        raise DocError("empty", "This Word document has no text or pictures to convert.", 422)
    out = out_dir / f"{stem}.pdf"
    SimpleDocTemplate(str(out), pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=20 * mm, bottomMargin=20 * mm,
                      title=stem).build(story)
    return [out]


def pdf_to_word(inputs: list[Path], out_dir: Path, stem: str, opts: dict) -> list[Path]:
    import pypdfium2 as pdfium
    from docx import Document
    from docx.enum.text import WD_BREAK
    pdf = pdfium.PdfDocument(str(inputs[0]))
    try:
        pages = []
        for i in range(len(pdf)):
            tp = pdf[i].get_textpage()
            text = tp.get_text_bounded().replace("\r\n", "\n").replace("\r", "\n")
            text = re.sub(r"[\x02\ufffe\u00ad]\n?", "", text)  # PDF soft hyphens: join the word back together
            pages.append(re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]", "", text))  # not allowed in Word files
            tp.close()
    finally:
        pdf.close()
    chars = sum(len(t.strip()) for t in pages)
    if chars < 20 * max(1, len(pages)) / 4:
        raise DocError("no_text", "This PDF has (almost) no text — it looks like scanned pictures. "
                                  "Reading scanned pages (OCR) isn't available yet.", 422)
    doc = Document()
    for n, text in enumerate(pages):
        if n:
            doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
        for block in re.split(r"\n\s*\n", text):
            lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
            # Wrapped lines are joined; a short line ending a sentence closes the paragraph.
            full = max((len(ln) for ln in lines), default=0)
            para: list[str] = []
            for ln in lines:
                para.append(ln)
                if len(ln) < 0.8 * full and ln[-1:] in ".!?:":
                    doc.add_paragraph(" ".join(para))
                    para = []
            if para:
                doc.add_paragraph(" ".join(para))
    out = out_dir / f"{stem}.docx"
    doc.save(str(out))
    Document(str(out))  # reopen: must be a valid Word file
    return [out]


TOOLS = {
    "merge": {"run": merge, "input": "pdf", "multi": True},
    "split": {"run": split, "input": "pdf"},
    "rotate": {"run": rotate, "input": "pdf"},
    "compress": {"run": compress, "input": "pdf"},
    "protect": {"run": protect, "input": "pdf"},
    "pdf-to-images": {"run": pdf_to_images, "input": "pdf"},
    "jpg-to-pdf": {"run": images_to_pdf, "input": "image", "multi": True},
    "word-to-pdf": {"run": word_to_pdf, "input": "document"},
    "pdf-to-word": {"run": pdf_to_word, "input": "pdf"},
}
MIME = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".zip": "application/zip",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}


def run_tool(store: Store, tool: str, item_ids: list[str], opts: dict) -> dict:
    spec = TOOLS.get(tool)
    if not spec:
        raise DocError("not_found", "Unknown document tool.", 404)
    if not item_ids or len(item_ids) > MAX_FILES or (len(item_ids) > 1 and not spec.get("multi")):
        raise DocError("bad_request", "Wrong number of files for this tool.")
    metas, inputs = [], []
    for iid in item_ids:
        meta, d = store.read_meta(iid), store.item_dir(iid)
        if not meta or not d:
            raise DocError("not_found", "A file has expired. Upload it again.", 404)
        if meta["report"]["kind"] != spec["input"]:
            want = {"pdf": "a PDF", "image": "an image", "document": "a Word document (.docx)"}[spec["input"]]
            raise DocError("bad_request", f"{meta['name']} is not {want}.")
        if not meta["report"].get("within_limits", True):
            raise DocError("limit_exceeded", f"{meta['name']} is over the size limits.", 413)
        metas.append(meta)
        inputs.append(d / "input.bin")
    from .video.jobs import _stem
    stem = _stem(metas[0]["name"])
    oid = store.new_id()
    out_dir = store.item_dir(item_ids[0]) / "outputs" / oid
    (out_dir / "out").mkdir(parents=True)
    try:
        files = spec["run"](inputs, out_dir / "out", stem, opts or {})
        if not files:
            raise DocError("empty", "Nothing was produced.", 422)
        for f in files:  # every output is reopened before it is offered
            if f.suffix == ".pdf" and tool != "protect":
                _check_pdf(f)
            elif f.suffix in (".png", ".jpg"):
                with Image.open(f) as im:
                    im.load()
    except DocError:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise
    except Exception as exc:  # noqa: BLE001 - never leak tracebacks
        shutil.rmtree(out_dir, ignore_errors=True)
        raise DocError("failed", f"The document could not be processed ({type(exc).__name__}).", 422) from exc

    base = f"/api/items/{item_ids[0]}/outputs/{oid}"
    entries = [{"name": f.name, "url": f"{base}/{f.name}", "size_bytes": f.stat().st_size, "mime": MIME.get(f.suffix, "application/octet-stream")}
               for f in files]
    original = sum(m["size_bytes"] for m in metas)
    result = {"id": oid, "tool": tool, "files": entries, "original_size": original,
              "output_size": sum(e["size_bytes"] for e in entries), "notes": []}
    if len(files) > 1:
        bundle = out_dir / "out" / f"{stem}-{tool}.zip"
        with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as z:
            for f in files:
                z.write(f, f.name)
        result["bundle"] = {"name": bundle.name, "url": f"{base}/{bundle.name}", "size_bytes": bundle.stat().st_size, "mime": MIME[".zip"]}
    first = files[0]
    if first.suffix == ".pdf" and tool != "protect":
        _preview(first, out_dir / "out" / ".preview" / "after.png")
        result["preview"] = f"{base}/.preview/after.png"
        with pikepdf.open(first) as pdf:
            result["pages"] = len(pdf.pages)
    elif first.suffix in (".png", ".jpg"):
        result["preview"] = entries[0]["url"]
    if tool == "compress" and result["output_size"] >= original:
        result["notes"].append("This PDF was already about as small as it can get, so the new file is not smaller.")
    if tool == "word-to-pdf":
        result["notes"].append("Text, headings, lists, tables and pictures are kept; exact page layout may differ from Word.")
    if tool == "pdf-to-word":
        result["notes"].append("The text is kept; the PDF's layout, columns and pictures are not.")
    (out_dir / "result.json").write_text(json.dumps(result))
    return result


def _preview(pdf_path: Path, dest: Path) -> None:
    import pypdfium2 as pdfium
    dest.parent.mkdir(parents=True, exist_ok=True)
    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        im = doc[0].render(scale=1.0).to_pil()
        im.thumbnail((900, 900))
        im.save(dest, "PNG")
    finally:
        doc.close()
