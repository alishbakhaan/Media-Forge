"""PDF backend capabilities.

Library choice (licences checked Oct 2026):
* pikepdf (MPL-2.0) on qpdf (Apache-2.0): open/repair, merge, split, rotate, lossless
  structural compression (object streams, stream recompression).
* pypdfium2 (Apache-2.0 / BSD-3-Clause) on PDFium (BSD-3-Clause): rasterise pages.
* Pillow (MIT-CMU): images -> PDF.
Rejected: PyMuPDF and Ghostscript (both AGPL-3.0 unless a commercial licence is bought).

The tools themselves are Milestone 7; here we only verify the libraries work.
"""
from __future__ import annotations

import io
from pathlib import Path

from .. import fixtures

OPERATIONS = [
    {"id": "images_to_pdf", "label": "Images to PDF", "needs": ["pillow"]},
    {"id": "pdf_to_images", "label": "PDF to images", "needs": ["pypdfium2"]},
    {"id": "merge", "label": "Merge PDFs", "needs": ["pikepdf"]},
    {"id": "split", "label": "Split PDF", "needs": ["pikepdf"]},
    {"id": "rotate", "label": "Rotate pages", "needs": ["pikepdf"]},
    {"id": "compress", "label": "Compress PDF", "needs": ["pikepdf", "pillow"]},
]


def detect(work: Path) -> dict:
    libs: dict[str, dict] = {}
    notes: list[str] = []

    # Pillow: build a 2-page PDF from images.
    pdf_bytes = b""
    try:
        from PIL import Image
        pages = [fixtures.test_image(alpha=False), fixtures.test_image((40, 60), alpha=False)]
        buf = io.BytesIO()
        pages[0].save(buf, format="PDF", save_all=True, append_images=pages[1:], resolution=72)
        pdf_bytes = buf.getvalue()
        libs["pillow"] = {"version": Image.__version__, "verified": pdf_bytes.startswith(b"%PDF-"), "license": "MIT-CMU"}
    except Exception as exc:
        libs["pillow"] = {"version": None, "verified": False, "error": str(exc)}

    # pikepdf: open, rotate, merge, save with compression.
    try:
        import pikepdf
        a = pikepdf.open(io.BytesIO(pdf_bytes))
        merged = pikepdf.new()
        merged.pages.extend(a.pages)
        merged.pages.extend(a.pages)
        merged.pages[0].rotate(90, relative=True)
        out = io.BytesIO()
        merged.save(out, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.generate)
        check = pikepdf.open(io.BytesIO(out.getvalue()))
        ok = len(check.pages) == 4 and int(check.pages[0].obj.get("/Rotate", 0)) == 90
        libs["pikepdf"] = {"version": pikepdf.__version__, "qpdf": pikepdf.__libqpdf_version__, "verified": ok,
                           "license": "MPL-2.0 (qpdf: Apache-2.0)"}
    except Exception as exc:
        libs["pikepdf"] = {"version": None, "verified": False, "error": str(exc)}

    # pypdfium2: render page 1.
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(pdf_bytes)
        bitmap = doc[0].render(scale=1).to_pil()
        libs["pypdfium2"] = {"version": pdfium.version.PYPDFIUM_INFO.version if hasattr(pdfium, "version") else "?",
                             "pdfium": str(pdfium.version.PDFIUM_INFO) if hasattr(pdfium, "version") else "?",
                             "verified": bitmap.width > 0 and len(doc) == 2,
                             "license": "Apache-2.0 / BSD-3-Clause (PDFium: BSD-3-Clause)"}
        doc.close()
    except Exception as exc:
        libs["pypdfium2"] = {"version": None, "verified": False, "error": str(exc)}

    ops = []
    for op in OPERATIONS:
        ready = all(libs.get(n, {}).get("verified") for n in op["needs"])
        ops.append({**op, "backend_ready": ready, "implemented": False, "milestone": 7})
    if not libs.get("pikepdf", {}).get("verified"):
        notes.append("pikepdf unavailable: merge/split/rotate/compress cannot be offered.")
    notes.append("PDF compression can only shrink files with recompressible images or redundant structure; "
                 "already-optimised PDFs may not get smaller.")
    return {"libraries": libs, "operations": ops, "notes": notes,
            "rejected": [{"name": "PyMuPDF", "license": "AGPL-3.0 or commercial"},
                         {"name": "Ghostscript", "license": "AGPL-3.0 or commercial"}]}
