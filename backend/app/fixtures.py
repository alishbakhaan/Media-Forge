"""Small synthetic sample files.

Used (a) at startup to *verify* that each backend really reads/writes a format, and
(b) by the test-suite. They are generated locally because the deployment may have no
network access. They are not a substitute for real-world camera/phone files; the
README lists which formats were also tested with real files.
"""
from __future__ import annotations

import struct
from pathlib import Path

from PIL import Image, ImageDraw

from . import proc


def test_image(size=(64, 48), alpha: bool = True) -> Image.Image:
    w, h = size
    im = Image.new("RGBA", size)
    px = im.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = (int(255 * x / (w - 1)), int(255 * y / (h - 1)), 128, 255 if not alpha else (40 + int(215 * x / (w - 1))))
    ImageDraw.Draw(im).rectangle([w // 4, h // 4, w // 2, h // 2], fill=(255, 255, 255, 255))
    return im if alpha else im.convert("RGB")


def animation_frames(n: int = 3, size=(48, 32)) -> list[Image.Image]:
    frames = []
    for i in range(n):
        im = Image.new("RGBA", size, (0, 0, 0, 0))
        ImageDraw.Draw(im).ellipse([4 + i * 8, 4, 20 + i * 8, 20], fill=(220, 40 + i * 60, 40, 255))
        frames.append(im)
    return frames


SVG_SAMPLE = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="80" viewBox="0 0 120 80">'
    '<circle cx="40" cy="40" r="30" fill="#e33" fill-opacity="0.6"/>'
    '<rect x="70" y="20" width="40" height="40" fill="#36c"/></svg>'
)


def write_svg(path: Path) -> Path:
    path.write_text(SVG_SAMPLE, encoding="utf-8")
    return path


def write_linear_dng(path: Path, size=(96, 64)) -> Path | None:
    """A minimal *linear* (demosaiced) DNG. Real camera RAWs use CFA data; see README."""
    try:
        import numpy as np
        import tifffile
    except ImportError:
        return None
    w, h = size
    ys, xs = np.mgrid[0:h, 0:w]
    data = np.stack([xs * 65535 // (w - 1), ys * 65535 // (h - 1), np.full_like(xs, 30000)], axis=-1).astype("uint16")
    extratags = [
        (50706, "B", 4, (1, 4, 0, 0), True),          # DNGVersion
        (50707, "B", 4, (1, 1, 0, 0), True),          # DNGBackwardVersion
        (50708, "s", 0, "MediaForge Test", True),     # UniqueCameraModel
        (50721, "2i", 9, (1, 1, 0, 1, 0, 1, 0, 1, 1, 1, 0, 1, 0, 1, 0, 1, 1, 1), True),  # ColorMatrix1
        (50778, "H", 1, 21, True),                    # CalibrationIlluminant1 (D65)
    ]
    tifffile.imwrite(path, data, photometric=34892, extratags=extratags, planarconfig="contig", subfiletype=0)
    return path


# ------------------------------------------------------------------ HEIC

def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def _fullbox(kind: bytes, version: int, flags: int, payload: bytes) -> bytes:
    return _box(kind, struct.pack(">I", (version << 24) | flags) + payload)


def _find_box(data: bytes, kind: bytes) -> bytes:
    idx = data.find(kind)
    if idx < 4:
        raise ValueError(f"box {kind!r} not found")
    size = struct.unpack(">I", data[idx - 4:idx])[0]
    return data[idx - 4: idx - 4 + size]


def write_heic(path: Path, workdir: Path, size=(64, 48), timeout: float = 30, image: Image.Image | None = None) -> Path | None:
    """Build a single-image HEIC: HEVC frame from FFmpeg/libx265 wrapped in HEIF boxes.

    Returns None when FFmpeg has no HEVC encoder. This is a test fixture only; it is
    not offered to users as an encoder.
    """
    if not proc.which("ffmpeg"):
        return None
    png = workdir / "heic-src.png"
    mp4 = workdir / "heic-src.mp4"
    src = image.convert("RGB") if image is not None else test_image(size, alpha=False)
    if src.width % 2 or src.height % 2:  # 4:2:0 needs even dimensions
        src = src.crop((0, 0, src.width - src.width % 2, src.height - src.height % 2))
    size = src.size
    src.save(png)
    res = proc.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(png),
         "-frames:v", "1", "-c:v", "libx265", "-pix_fmt", "yuv420p",
         "-x265-params", "log-level=error", "-tag:v", "hvc1", "-an", str(mp4)],
        timeout=timeout,
    )
    if not res.ok or not mp4.exists():
        return None
    data = mp4.read_bytes()
    hvcc = _find_box(data, b"hvcC")
    mdat = _find_box(data, b"mdat")
    sample = mdat[8:]
    w, h = size

    ftyp = _box(b"ftyp", b"heic" + struct.pack(">I", 0) + b"mif1heic")
    hdlr = _fullbox(b"hdlr", 0, 0, struct.pack(">I", 0) + b"pict" + b"\x00" * 12 + b"\x00")
    pitm = _fullbox(b"pitm", 0, 0, struct.pack(">H", 1))
    infe = _fullbox(b"infe", 2, 0, struct.pack(">HH", 1, 0) + b"hvc1" + b"\x00")
    iinf = _fullbox(b"iinf", 0, 0, struct.pack(">H", 1) + infe)
    ispe = _fullbox(b"ispe", 0, 0, struct.pack(">II", w, h))
    ipco = _box(b"ipco", hvcc + ispe)
    ipma = _fullbox(b"ipma", 0, 0, struct.pack(">IHB", 1, 1, 2) + bytes([0x81, 0x02]))
    iprp = _box(b"iprp", ipco + ipma)

    def build(offset: int) -> bytes:
        iloc = _fullbox(b"iloc", 0, 0, bytes([0x44, 0x00]) + struct.pack(">HHHHII", 1, 1, 0, 1, offset, len(sample)))
        return _fullbox(b"meta", 0, 0, hdlr + pitm + iloc + iinf + iprp)

    meta = build(0)
    offset = len(ftyp) + len(meta) + 8
    meta = build(offset)
    path.write_bytes(ftyp + meta + _box(b"mdat", sample))
    return path
