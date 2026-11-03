"""Image format capabilities, verified by real encode/decode round trips.

A format is listed as readable/writable only if a test file was actually decoded or
encoded (and the encoded bytes were confirmed to be that format) on this machine.
"""
from __future__ import annotations

import importlib.util
import io
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PIL import Image, features

from .. import fixtures, proc
from ..sniff import sniff_bytes, sniff_file


@dataclass
class ImageFormat:
    id: str
    label: str
    extensions: list[str]
    mime: str
    priority: bool = True
    pillow_name: str | None = None
    lossy: bool = False
    lossless: bool = False
    quality_param: bool = False
    supports_alpha: bool = False
    animation_possible: bool = False
    multipage_possible: bool = False
    notes: list[str] = field(default_factory=list)


CATALOG: list[ImageFormat] = [
    ImageFormat("jpeg", "JPEG", [".jpg", ".jpeg", ".jfif"], "image/jpeg", pillow_name="JPEG", lossy=True, quality_param=True),
    ImageFormat("png", "PNG", [".png", ".apng"], "image/png", pillow_name="PNG", lossless=True, supports_alpha=True, animation_possible=True),
    ImageFormat("webp", "WebP", [".webp"], "image/webp", pillow_name="WEBP", lossy=True, lossless=True, quality_param=True, supports_alpha=True, animation_possible=True),
    ImageFormat("heic", "HEIC", [".heic", ".hif"], "image/heic", lossy=True, quality_param=True, supports_alpha=True),
    ImageFormat("heif", "HEIF", [".heif"], "image/heif", lossy=True, quality_param=True, supports_alpha=True),
    ImageFormat("avif", "AVIF", [".avif"], "image/avif", pillow_name="AVIF", lossy=True, lossless=True, quality_param=True, supports_alpha=True, animation_possible=True),
    ImageFormat("gif", "GIF", [".gif"], "image/gif", pillow_name="GIF", lossless=True, supports_alpha=True, animation_possible=True,
                notes=["256-colour palette; 1-bit transparency only"]),
    ImageFormat("bmp", "BMP", [".bmp", ".dib"], "image/bmp", pillow_name="BMP", lossless=True),
    ImageFormat("tiff", "TIFF", [".tif", ".tiff"], "image/tiff", pillow_name="TIFF", lossless=True, lossy=True, supports_alpha=True, multipage_possible=True),
    ImageFormat("ico", "ICO", [".ico"], "image/vnd.microsoft.icon", pillow_name="ICO", lossless=True, supports_alpha=True,
                notes=["Max 256×256; stores several sizes"]),
    ImageFormat("svg", "SVG (rasterised)", [".svg"], "image/svg+xml", notes=["Vector input is rasterised; SVG output is not offered"]),
    ImageFormat("raw", "Camera RAW", [".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2", ".raf", ".pef", ".srw"], "image/x-raw",
                notes=["Import only"]),
    # Additional formats discovered from Pillow.
    ImageFormat("jpeg2000", "JPEG 2000", [".jp2", ".j2k", ".jpf"], "image/jp2", priority=False, pillow_name="JPEG2000", lossy=True, lossless=True, quality_param=True, supports_alpha=True),
    ImageFormat("qoi", "QOI", [".qoi"], "image/qoi", priority=False, pillow_name="QOI", lossless=True, supports_alpha=True),
    ImageFormat("tga", "TGA", [".tga"], "image/x-tga", priority=False, pillow_name="TGA", lossless=True, supports_alpha=True),
    ImageFormat("pnm", "PNM/PPM", [".ppm", ".pgm", ".pbm", ".pnm"], "image/x-portable-anymap", priority=False, pillow_name="PPM", lossless=True),
    ImageFormat("pcx", "PCX", [".pcx"], "image/x-pcx", priority=False, pillow_name="PCX", lossless=True),
    ImageFormat("dds", "DDS", [".dds"], "image/vnd-ms.dds", priority=False, pillow_name="DDS", supports_alpha=True),
    ImageFormat("sgi", "SGI", [".sgi", ".rgb"], "image/sgi", priority=False, pillow_name="SGI", lossless=True, supports_alpha=True),
    ImageFormat("icns", "ICNS", [".icns"], "image/icns", priority=False, pillow_name="ICNS", lossless=True, supports_alpha=True),
    ImageFormat("psd", "Photoshop PSD", [".psd"], "image/vnd.adobe.photoshop", priority=False, pillow_name="PSD",
                notes=["Flattened composite only; layers are not preserved"]),
]
BY_ID = {f.id: f for f in CATALOG}
PILLOW_TO_ID = {f.pillow_name: f.id for f in CATALOG if f.pillow_name}
PILLOW_TO_ID["MPO"] = "jpeg"  # multi-picture JPEGs from cameras
PILLOW_TO_ID["HEIF"] = "heic"  # pillow-heif's format name


@dataclass
class Support:
    backend: str
    verified: bool
    detail: str = ""


def _sniff_matches(data: bytes, fmt_id: str) -> bool:
    found = sniff_bytes(data[:8192])
    if fmt_id == "pnm":
        return data[:1] == b"P" and data[1:2] in b"123456"
    if fmt_id in ("tga", "pcx", "sgi"):
        # No reliable magic; confirm by re-opening with Pillow instead.
        return True
    return found.format_id == fmt_id


def _pillow_roundtrip(fmt: ImageFormat) -> tuple[Support | None, Support | None, dict]:
    """Encode a test image with Pillow, confirm the bytes, decode it again."""
    extra: dict = {}
    name = fmt.pillow_name
    if not name or name not in Image.SAVE:
        read = None
        if name and name in Image.OPEN:
            read = Support("pillow", False, "decoder registered; no sample to verify")
        return read, None, extra
    src = fixtures.test_image(alpha=fmt.supports_alpha)
    kwargs: dict = {}
    if fmt.id == "pnm":
        src = src.convert("RGB")
    if fmt.id == "ico":
        kwargs["sizes"] = [(16, 16), (32, 32)]
    buf = io.BytesIO()
    try:
        src.save(buf, format=name, **kwargs)
    except Exception as exc:  # encoder missing or broken
        return None, None, {"write_error": f"{type(exc).__name__}: {exc}"}
    data = buf.getvalue()
    if not _sniff_matches(data, fmt.id):
        return None, None, {"write_error": "encoder produced bytes of a different format"}
    try:
        back = Image.open(io.BytesIO(data))
        back.load()
    except Exception as exc:
        return None, None, {"write_error": f"could not decode own output: {exc}"}
    if PILLOW_TO_ID.get(back.format) != fmt.id:
        return None, None, {"write_error": f"decoded as {back.format}"}
    extra["alpha_roundtrip"] = "A" in back.getbands() or "transparency" in back.info
    write = Support("pillow", True)
    read = Support("pillow", True)

    if fmt.animation_possible:
        frames = fixtures.animation_frames(3)
        abuf = io.BytesIO()
        try:
            frames[0].save(abuf, format=name, save_all=True, append_images=frames[1:], duration=100, loop=0)
            anim = Image.open(io.BytesIO(abuf.getvalue()))
            extra["animation_write"] = getattr(anim, "n_frames", 1) == 3
            extra["animation_read"] = extra["animation_write"]
        except Exception as exc:
            extra["animation_write"] = False
            extra["animation_error"] = str(exc)
    if fmt.multipage_possible:
        pages = [fixtures.test_image(alpha=False), fixtures.test_image((32, 32), alpha=False)]
        mbuf = io.BytesIO()
        try:
            pages[0].save(mbuf, format=name, save_all=True, append_images=pages[1:])
            multi = Image.open(io.BytesIO(mbuf.getvalue()))
            extra["multipage_write"] = getattr(multi, "n_frames", 1) == 2
            extra["multipage_read"] = extra["multipage_write"]
        except Exception as exc:
            extra["multipage_write"] = False
            extra["multipage_error"] = str(exc)
    return read, write, extra


# ------------------------------------------------------------------ ImageMagick

def imagemagick_info(timeout: float = 10) -> dict | None:
    cmd = proc.imagemagick("convert")
    if not cmd:
        return None
    exe = cmd[0]
    res = proc.run([*cmd, "-version"], timeout=timeout)
    if not res.ok:
        return None
    text = res.stdout.decode("utf-8", "replace")
    m = re.search(r"Version: ImageMagick (\S+)", text)
    return {"version": m.group(1) if m else "unknown", "major": 7 if "ImageMagick 7" in text else 6, "exe": exe}


def _im_decode(coder: str, path: Path, out: Path, timeout: float) -> tuple[bool, str]:
    cmd = proc.imagemagick("convert")
    if not cmd:
        return False, "ImageMagick not installed"
    try:
        res = proc.run([*cmd, f"{coder}:{path}", "-strip", f"png:{out}"], timeout=timeout)
    except proc.ProcessTimeout as exc:
        return False, str(exc)
    if not res.ok or not out.exists():
        return False, _clean(res.err_text(2000), path.parent)
    try:
        with Image.open(out) as im:
            im.load()
            return True, f"{im.width}x{im.height}"
    except Exception as exc:
        return False, f"output unreadable: {exc}"


def _clean(msg: str, folder: Path) -> str:
    """Remove server paths and tool prefixes from error text shown to users."""
    msg = msg.replace(str(folder) + "/", "")
    lines = [re.sub(r"^(convert(-im6\.q16)?|magick): ", "", ln).strip() for ln in msg.splitlines()]
    lines = [re.sub(r" @ \S+/\S+\.c/\S+/\d+\.?$", "", ln) for ln in lines if "no images defined" not in ln]
    return " ".join(ln for ln in lines if ln)[:240]


def _ffmpeg_svg_decode(path: Path, out: Path, timeout: float) -> tuple[bool, str]:
    exe = proc.which("ffmpeg")
    if not exe:
        return False, "FFmpeg not installed"
    try:
        res = proc.run([exe, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "svg_pipe", "-i", str(path), "-frames:v", "1", str(out)], timeout=timeout)
    except proc.ProcessTimeout as exc:
        return False, str(exc)
    if not res.ok or not out.exists():
        return False, res.err_text(300)
    with Image.open(out) as im:
        return True, f"{im.width}x{im.height} {im.mode}"


def _pillow_heif_available() -> bool:
    return importlib.util.find_spec("pillow_heif") is not None


def _pillow_heif_checks(fixture: Path | None) -> tuple[Support | None, Support | None, list[str]]:
    """Verify pillow-heif decoding and encoding SEPARATELY. Installing the package proves neither:
    wheels may lack the HEVC decoder or the x265 encoder, and system builds vary.

    * decode: a HEIC produced by a different encoder (FFmpeg/libx265, see fixtures.write_heic)
    * encode: pillow-heif writes a HEIC; the bytes must sniff as HEIC, parse, and decode with FFmpeg's HEVC
      decoder. Neither check depends on the other.
    """
    notes: list[str] = []
    try:
        import pillow_heif  # type: ignore
        pillow_heif.register_heif_opener()
        info = getattr(pillow_heif, "libheif_info", lambda: {})()
        notes.append(f"pillow-heif {pillow_heif.__version__}; libheif plugins: {info}")
    except Exception as exc:
        return None, None, [f"pillow-heif installed but failed to load: {exc}"]
    read = write = None
    if fixture is None:
        notes.append("pillow-heif decode not verified: no independent HEIC sample could be built (FFmpeg/libx265 missing)")
    else:
        try:
            with Image.open(fixture) as im:
                im.load()
                if im.size == (64, 48):
                    read = Support("pillow-heif", True, "decoded an HEIC made by FFmpeg/libx265")
                else:
                    notes.append(f"pillow-heif decoded the wrong size {im.size}")
        except Exception as exc:
            notes.append(f"pillow-heif decode failed: {exc}")
    try:
        buf = io.BytesIO()
        fixtures.test_image(alpha=False).save(buf, format="HEIF", quality=80)
        data = buf.getvalue()
        if sniff_bytes(data).format_id not in ("heic", "heif"):
            notes.append(f"pillow-heif encode produced {sniff_bytes(data).format_id}, not HEIC")
        else:
            # Verified independently of pillow-heif's decoder: parse the HEIF boxes ourselves and
            # decode the HEVC bitstream with FFmpeg. Reading is NOT required for writing.
            import tempfile
            from ..convert.heif_tools import check_heic
            with tempfile.TemporaryDirectory() as d:
                out = Path(d) / "enc.heic"
                out.write_bytes(data)
                ok, detail, dims = check_heic(out)
            if ok and dims == (64, 48):
                write = Support("pillow-heif", True, "encoded; structure + HEVC verified with FFmpeg")
            else:
                notes.append(f"pillow-heif encode output failed independent verification: {detail}")
    except Exception as exc:
        notes.append(f"pillow-heif encode failed: {exc}")
    return read, write, notes


# ------------------------------------------------------------------ detection

def detect(work: Path, timeout: float = 30) -> dict:
    Image.init()  # load every Pillow plugin so Image.OPEN / Image.SAVE are complete
    im_info = imagemagick_info()
    formats: dict[str, dict] = {}
    for fmt in CATALOG:
        read, write, extra = _pillow_roundtrip(fmt)
        formats[fmt.id] = {"read": read, "write": write, "extra": extra, "notes": list(fmt.notes)}

    # --- HEIC / HEIF -----------------------------------------------------
    heic_fixture = fixtures.write_heic(work / "sample.heic", work)
    heic_notes: list[str] = []
    heic_read = heic_write = None
    if _pillow_heif_available():
        heic_read, heic_write, detail = _pillow_heif_checks(heic_fixture)
        heic_notes.extend(detail)
    if heic_read is None and im_info and heic_fixture:
        ok, detail = _im_decode("heic", heic_fixture, work / "heic-out.png", timeout)
        if ok:
            heic_read = Support("imagemagick", True, detail)
        else:
            heic_notes.append(f"ImageMagick HEIC decode failed on a valid sample: {detail}")
    if heic_read is None:
        heic_notes.append("HEIC input unavailable: no working HEVC decoder for HEIF. Install pillow-heif, "
                          "or the libheif HEVC decoder plugin (e.g. libheif-plugin-libde265).")
    if heic_write is None:
        heic_notes.append("HEIC export disabled: no verified HEIC encoder (ImageMagick here is read-only for HEIC "
                          "and silently writes PNG data instead).")
    for fid in ("heic", "heif"):
        # Reading and writing are gated independently. Output is offered once, as .heic.
        formats[fid] = {"read": heic_read, "write": heic_write if fid == "heic" else None, "extra": {},
                        "notes": heic_notes + formats[fid]["notes"]}

    # --- SVG ---------------------------------------------------------------
    svg_path = fixtures.write_svg(work / "sample.svg")
    ok, detail = _ffmpeg_svg_decode(svg_path, work / "svg-out.png", timeout)
    formats["svg"]["read"] = Support("ffmpeg-librsvg", True, detail) if ok else None
    if not ok:
        formats["svg"]["notes"].append(f"No SVG rasteriser available: {detail}")

    # --- Camera RAW ------------------------------------------------------------
    raw_read = None
    dng = fixtures.write_linear_dng(work / "sample.dng")
    if im_info and dng:
        ok, detail = _im_decode("dng", dng, work / "dng-out.png", timeout)
        if ok:
            raw_read = Support("imagemagick-libraw", True, "verified on a linear DNG sample")
            formats["raw"]["notes"].append("Decoded by LibRaw via ImageMagick. Verified with a synthetic DNG only; "
                                           "other RAW types use the same decoder but were not tested here.")
        else:
            formats["raw"]["notes"].append(f"RAW decode failed: {detail}")
    elif not dng:
        formats["raw"]["notes"].append("Could not create a DNG test sample (tifffile/numpy missing); RAW unverified.")
    formats["raw"]["read"] = raw_read

    # --- PSD: Pillow can only read; create sample with ImageMagick if allowed ----
    if formats["psd"]["read"] and not formats["psd"]["read"].verified:
        formats["psd"]["notes"].append("Reader registered in Pillow but not verified (no PSD sample available).")

    readable, writable, table = [], [], []
    for fmt in CATALOG:
        entry = formats[fmt.id]
        read: Support | None = entry["read"]
        write: Support | None = entry["write"]
        extra = entry["extra"]
        row = {
            "id": fmt.id,
            "label": fmt.label,
            "extensions": fmt.extensions,
            "mime": fmt.mime,
            "priority": fmt.priority,
            "read": asdict(read) if read else None,
            "write": asdict(write) if write else None,
            "features": {
                "lossy": fmt.lossy,
                "lossless": fmt.lossless,
                "quality": fmt.quality_param and bool(write),
                "alpha_write": bool(write) and bool(extra.get("alpha_roundtrip")),
                "animation_read": bool(extra.get("animation_read")),
                "animation_write": bool(extra.get("animation_write")),
                "multipage_read": bool(extra.get("multipage_read")),
                "multipage_write": bool(extra.get("multipage_write")),
            },
            "notes": entry["notes"],
        }
        if "write_error" in extra:
            row["notes"].append(f"Writer failed self-test: {extra['write_error']}")
        table.append(row)
        if read and read.verified:
            readable.append(fmt.id)
        if write and write.verified:
            writable.append(fmt.id)

    return {
        "formats": table,
        "readable": readable,
        "writable": writable,
        "backends": {
            "pillow": {"version": Image.__version__,
                       "features": {k: bool(features.check(k)) for k in ("webp", "avif", "jpg", "jpg_2000", "zlib", "libtiff", "littlecms2")}},
            "pillow_heif": {"installed": _pillow_heif_available()},
            "imagemagick": im_info,
        },
    }
