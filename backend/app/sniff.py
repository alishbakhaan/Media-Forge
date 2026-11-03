"""Content-based file type detection (magic bytes). File extensions are never trusted.

This is the first gate. A positive sniff is then confirmed by actually opening the
file with the backend that will process it (see ``inspect.py``).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

HEAD_BYTES = 8192


@dataclass(frozen=True)
class Sniff:
    kind: str          # image | video | pdf | unknown
    format_id: str     # catalog id, e.g. "jpeg", "heic", "mp4"
    detail: str = ""   # e.g. the ISO-BMFF major brand


_HEIC_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"hevm", b"hevs"}
_HEIF_BRANDS = {b"mif1", b"msf1", b"mif2"}
_AVIF_BRANDS = {b"avif", b"avis"}
_MOV_BRANDS = {b"qt  "}
_3GP_PREFIX = (b"3gp", b"3g2")
_CR3_BRANDS = {b"crx "}


def _ftyp_brands(head: bytes) -> tuple[bytes, list[bytes]] | None:
    if len(head) < 16 or head[4:8] != b"ftyp":
        return None
    size = struct.unpack(">I", head[:4])[0]
    if size < 16 or size > len(head):
        size = min(len(head), 64)
    major = head[8:12]
    compat = [head[i:i + 4] for i in range(16, size - 3, 4)]
    return major, compat


def _sniff_isobmff(head: bytes) -> Sniff | None:
    parsed = _ftyp_brands(head)
    if parsed is None:
        # Old QuickTime files can start with other atoms.
        if head[4:8] in (b"moov", b"mdat", b"wide", b"free", b"skip", b"pnot"):
            return Sniff("video", "mov", "atom:" + head[4:8].decode("latin-1"))
        return None
    major, compat = parsed
    brands = [major, *compat]
    tag = major.decode("latin-1").strip()
    if major in _CR3_BRANDS:
        return Sniff("image", "raw", "cr3")
    if major in _AVIF_BRANDS:
        return Sniff("image", "avif", tag)
    if major in _HEIC_BRANDS:
        return Sniff("image", "heic", tag)
    if major in _HEIF_BRANDS:
        if any(b in _AVIF_BRANDS for b in compat):
            return Sniff("image", "avif", tag)
        if any(b in _HEIC_BRANDS for b in compat):
            return Sniff("image", "heic", tag)
        return Sniff("image", "heif", tag)
    if major in _MOV_BRANDS:
        return Sniff("video", "mov", tag)
    if major.startswith(_3GP_PREFIX):
        return Sniff("video", "3gp", tag)
    if any(b in _AVIF_BRANDS for b in brands):
        return Sniff("image", "avif", tag)
    return Sniff("video", "mp4", tag)


def _ebml_doctype(head: bytes) -> str:
    idx = head.find(b"\x42\x82")  # DocType element id
    if idx == -1 or idx + 3 > len(head):
        return "matroska"
    length_byte = head[idx + 2]
    if length_byte & 0x80:
        n = length_byte & 0x7F
        return head[idx + 3: idx + 3 + n].decode("ascii", "replace").strip("\x00") or "matroska"
    return "matroska"


def _is_mpegts(head: bytes) -> bool:
    for packet in (188, 192):
        offset = 0 if packet == 188 else 4
        if len(head) >= offset + packet * 4 and all(
            head[offset + packet * i] == 0x47 for i in range(4)
        ):
            return True
    return False


def _tiff_variant(head: bytes) -> Sniff:
    # TIFF-based camera RAWs.
    if head[8:10] == b"CR":
        return Sniff("image", "raw", "cr2")
    lowered = head.lower()
    if b"nikon" in lowered:
        return Sniff("image", "raw", "nef")
    if b"sony" in lowered:
        return Sniff("image", "raw", "arw")
    if b"pentax" in lowered:
        return Sniff("image", "raw", "pef")
    # DNG: look for the DNGVersion tag (0xC612) in the first IFD.
    le = head[:2] == b"II"
    fmt = "<" if le else ">"
    try:
        ifd = struct.unpack(fmt + "I", head[4:8])[0]
        if 8 <= ifd < len(head) - 2:
            count = struct.unpack(fmt + "H", head[ifd:ifd + 2])[0]
            for i in range(min(count, 512)):
                pos = ifd + 2 + i * 12
                if pos + 2 > len(head):
                    break
                tag = struct.unpack(fmt + "H", head[pos:pos + 2])[0]
                if tag == 0xC612:
                    return Sniff("image", "raw", "dng")
    except struct.error:
        pass
    return Sniff("image", "tiff")


def sniff_bytes(head: bytes) -> Sniff:
    if head.startswith(b"\xff\xd8\xff"):
        return Sniff("image", "jpeg")
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return Sniff("image", "png", "apng" if b"acTL" in head[:4096] else "")
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return Sniff("image", "gif")
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return Sniff("image", "webp")
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return Sniff("video", "avi")
    if head[:2] == b"BM" and len(head) >= 26:
        return Sniff("image", "bmp")
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return _tiff_variant(head)
    if head[:4] in (b"IIRO", b"IIRS", b"MMOR"):
        return Sniff("image", "raw", "orf")
    if head[:4] == b"IIU\x00":
        return Sniff("image", "raw", "rw2")
    if head.startswith(b"FUJIFILMCCD-RAW"):
        return Sniff("image", "raw", "raf")
    if head[:4] == b"\x00\x00\x01\x00" and len(head) > 6 and head[4] > 0:
        return Sniff("image", "ico")
    if head.startswith(b"%PDF-"):
        return Sniff("pdf", "pdf")
    if head[:4] == b"\x1a\x45\xdf\xa3":
        doctype = _ebml_doctype(head)
        return Sniff("video", "webm" if doctype == "webm" else "mkv", doctype)
    if head[:4] == b"\x00\x00\x01\xba":
        return Sniff("video", "mpeg", "ps")
    if head[:4] == b"\x00\x00\x01\xb3":
        return Sniff("video", "mpeg", "es")
    if head[:4] == b"OggS":
        return Sniff("video", "ogv")
    if head[:3] == b"FLV":
        return Sniff("video", "flv")
    if head[:16] == bytes.fromhex("3026b2758e66cf11a6d900aa0062ce6c"):
        return Sniff("video", "asf")
    if head[:4] == bytes.fromhex("060e2b34"):
        return Sniff("video", "mxf")
    if _is_mpegts(head):
        return Sniff("video", "mpegts")
    if len(head) >= 12 and head[4:8] in (b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip", b"pnot"):
        found = _sniff_isobmff(head)
        if found:
            return found
    if head[:4] == b"8BPS":
        return Sniff("image", "psd")
    if head[:4] == b"qoif":
        return Sniff("image", "qoi")
    if head[:12] == b"\x00\x00\x00\x0cjP  \r\n\x87\n" or head[:4] == b"\xff\x4f\xff\x51":
        return Sniff("image", "jpeg2000")
    if head[:4] == b"icns":
        return Sniff("image", "icns")
    if head[:4] == b"DDS ":
        return Sniff("image", "dds")
    text = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    if text[:5] in (b"<?xml", b"<svg ", b"<svg>", b"<!DOC", b"<!--") or text[:4] == b"<svg":
        lowered = head.lower()
        if b"<svg" in lowered:
            return Sniff("image", "svg")
    return Sniff("unknown", "unknown")


def sniff_file(path: Path) -> Sniff:
    with open(path, "rb") as fh:
        return sniff_bytes(fh.read(HEAD_BYTES))
