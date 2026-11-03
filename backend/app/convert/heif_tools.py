"""Minimal HEIF/HEIC reader used only for *verification*: it checks a HEIC file's structure and decodes
its primary image's HEVC bitstream with FFmpeg. This lets HEIC *writing* be verified without relying on
the same library's decoder (and HEIC *reading* without relying on its encoder).
"""
from __future__ import annotations

import struct
import tempfile
from pathlib import Path

from .. import proc


def _boxes(data: bytes, start: int = 0, end: int | None = None):
    end = len(data) if end is None else end
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack(">I4s", data[pos:pos + 8])
        hdr = 8
        if size == 1:
            size = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
            hdr = 16
        elif size == 0:
            size = end - pos
        if size < hdr or pos + size > end:
            raise ValueError(f"bad box {kind!r} at {pos}")
        yield kind.decode("latin-1"), pos + hdr, pos + size
        pos += size


def _child(data, start, end, kind, full=False):
    for k, s, e in _boxes(data, start, end):
        if k == kind:
            return (s + 4 if full else s), e
    return None


def _uint(data: bytes, pos: int, n: int) -> int:
    return int.from_bytes(data[pos:pos + n], "big") if n else 0


def parse_primary(data: bytes) -> dict:
    """Return {'type', 'hvcC', 'width', 'height', 'payload'} for the primary item."""
    ftyp = _child(data, 0, len(data), "ftyp")
    if not ftyp or data[ftyp[0]:ftyp[0] + 4] not in (b"heic", b"heix", b"mif1", b"msf1", b"hevc", b"heim", b"heis"):
        raise ValueError("not a HEIC/HEIF file (ftyp brand)")
    meta = _child(data, 0, len(data), "meta", full=True)
    if not meta:
        raise ValueError("no meta box")
    ms, me = meta
    pitm = _child(data, ms, me, "pitm")
    if not pitm:
        raise ValueError("no primary item")
    pv = data[pitm[0]]
    primary = _uint(data, pitm[0] + 4, 2 if pv == 0 else 4)
    # item types
    types = {}
    iinf = _child(data, ms, me, "iinf")
    if iinf:
        s0 = iinf[0]
        v = data[s0]
        s1 = s0 + 4 + (2 if v == 0 else 4)
        for k, s, e in _boxes(data, s1, iinf[1]):
            if k != "infe":
                continue
            iv = data[s]
            if iv >= 2:
                idlen = 2 if iv == 2 else 4
                iid = _uint(data, s + 4, idlen)
                types[iid] = data[s + 4 + idlen + 2: s + 4 + idlen + 6].decode("latin-1")
    # locations
    iloc = _child(data, ms, me, "iloc")
    if not iloc:
        raise ValueError("no iloc")
    s = iloc[0]
    v = data[s]
    p = s + 4
    off_sz, len_sz = data[p] >> 4, data[p] & 15
    base_sz, idx_sz = data[p + 1] >> 4, (data[p + 1] & 15) if v in (1, 2) else 0
    p += 2
    count = _uint(data, p, 2 if v < 2 else 4)
    p += 2 if v < 2 else 4
    extents = {}
    for _ in range(count):
        iid = _uint(data, p, 2 if v < 2 else 4)
        p += 2 if v < 2 else 4
        method = 0
        if v in (1, 2):
            method = _uint(data, p, 2) & 15
            p += 2
        p += 2  # data_reference_index
        base = _uint(data, p, base_sz)
        p += base_sz
        n = _uint(data, p, 2)
        p += 2
        chunks = []
        for _ in range(n):
            p += idx_sz
            off = _uint(data, p, off_sz)
            p += off_sz
            ln = _uint(data, p, len_sz)
            p += len_sz
            chunks.append((base + off, ln))
        extents[iid] = (method, chunks)
    # properties
    iprp = _child(data, ms, me, "iprp")
    if not iprp:
        raise ValueError("no iprp")
    ipco = _child(data, iprp[0], iprp[1], "ipco")
    props = [(k, s, e) for k, s, e in _boxes(data, ipco[0], ipco[1])]
    assoc: dict[int, list[int]] = {}
    for k, s, e in _boxes(data, iprp[0], iprp[1]):
        if k != "ipma":
            continue
        v, flags = data[s], _uint(data, s + 1, 3)
        p = s + 4
        cnt = _uint(data, p, 4)
        p += 4
        for _ in range(cnt):
            iid = _uint(data, p, 2 if v < 1 else 4)
            p += 2 if v < 1 else 4
            na = data[p]
            p += 1
            lst = []
            for _ in range(na):
                if flags & 1:
                    lst.append(_uint(data, p, 2) & 0x7FFF)
                    p += 2
                else:
                    lst.append(data[p] & 0x7F)
                    p += 1
            assoc[iid] = lst
    item = primary
    kind = types.get(primary, "hvc1")
    if kind == "grid":  # verify the first tile instead (iPhone-style images)
        iref = _child(data, ms, me, "iref", full=True)
        tiles = []
        if iref:
            for k, s, e in _boxes(data, iref[0], iref[1]):
                if k == "dimg" and _uint(data, s, 2) == primary:
                    n = _uint(data, s + 2, 2)
                    tiles = [_uint(data, s + 4 + 2 * i, 2) for i in range(n)]
        if not tiles:
            raise ValueError("grid without tiles")
        item = tiles[0]
        kind = types.get(item, "hvc1")
    if kind != "hvc1":
        raise ValueError(f"primary item type {kind!r} is not HEVC")
    hvcc = w = h = None
    for i in assoc.get(item, []):
        if 1 <= i <= len(props):
            k, s, e = props[i - 1]
            if k == "hvcC":
                hvcc = data[s:e]
            elif k == "ispe":
                w, h = struct.unpack(">II", data[s + 4:s + 12])
    if hvcc is None:
        raise ValueError("no hvcC configuration")
    method, chunks = extents.get(item, (0, []))
    if method != 0 or not chunks:
        raise ValueError("unsupported item location")
    payload = b"".join(data[o:o + n] for o, n in chunks)
    return {"type": types.get(primary, "hvc1"), "hvcC": hvcc, "width": w, "height": h, "payload": payload}


def to_annexb(hvcc: bytes, payload: bytes) -> bytes:
    nal_len = (hvcc[21] & 3) + 1
    out = bytearray()
    p = 23
    for _ in range(hvcc[22]):
        p += 1
        n = struct.unpack(">H", hvcc[p:p + 2])[0]
        p += 2
        for _ in range(n):
            ln = struct.unpack(">H", hvcc[p:p + 2])[0]
            p += 2
            out += b"\x00\x00\x00\x01" + hvcc[p:p + ln]
            p += ln
    q = 0
    while q + nal_len <= len(payload):
        ln = int.from_bytes(payload[q:q + nal_len], "big")
        q += nal_len
        out += b"\x00\x00\x00\x01" + payload[q:q + ln]
        q += ln
    return bytes(out)


def check_heic(path: Path, timeout: float = 30) -> tuple[bool, str, tuple[int, int]]:
    """(ok, detail, (width, height)) — structure parsed and HEVC bitstream decoded by FFmpeg."""
    try:
        info = parse_primary(path.read_bytes())
    except Exception as exc:
        return False, f"structure: {exc}", (0, 0)
    with tempfile.TemporaryDirectory() as d:
        es = Path(d) / "x.hevc"
        es.write_bytes(to_annexb(info["hvcC"], info["payload"]))
        out = Path(d) / "x.png"
        res = proc.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-err_detect", "explode", "-xerror", "-f", "hevc", "-i", str(es),
                        "-frames:v", "1", str(out)], timeout=timeout)
        if not res.ok or not out.exists():
            return False, "FFmpeg could not decode the HEVC data: " + res.err_text(160), (0, 0)
        from PIL import Image
        with Image.open(out) as im:
            dims = im.size
    if info["width"] and info["type"] != "grid" and (info["width"], info["height"]) != dims:
        return False, f"declared {info['width']}×{info['height']} but decoded {dims}", dims
    return True, f"structure valid; HEVC decoded by FFmpeg ({dims[0]}×{dims[1]})", (info["width"] or dims[0], info["height"] or dims[1])
