"""Image conversion pipeline (Pillow).

decode → apply EXIF orientation → colour management → transparency policy → encode → verify.

Runs inside a short-lived worker process (see worker.py) so that a timeout or a memory
limit can kill it without touching the server.
"""
from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageCms, ImageOps, ImageSequence

from .. import proc
from ..sniff import sniff_file
from .settings import CONTROLS, TARGET_KNOB, Settings

Image.MAX_IMAGE_PIXELS = None  # replaced by explicit limits below

try:  # optional HEIC support; only offered when capability detection verified it
    import pillow_heif  # type: ignore
    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover - not installed in the reference environment
    pillow_heif = None


class ConvertError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Target:
    pillow: str
    ext: str
    mime: str
    modes: tuple[str, ...]          # modes the encoder accepts, preferred first
    alpha: str = "none"             # none | full | binary
    icc: bool = False
    exif: bool = False
    animation: bool = False
    multipage: bool = False
    max_dim: int = 65500
    square: bool = False            # ICO / ICNS: padded to a square canvas
    max_icon: int = 0               # ICO 256, ICNS 1024
    lossless: bool = True           # default encoding in Milestone 2 (compression controls come in M3)


TARGETS: dict[str, Target] = {
    "jpeg": Target("JPEG", ".jpg", "image/jpeg", ("RGB", "L"), icc=True, exif=True, lossless=False),
    "png": Target("PNG", ".png", "image/png", ("RGBA", "RGB", "LA", "L"), alpha="full", icc=True, exif=True, animation=True, max_dim=1 << 30),
    "webp": Target("WEBP", ".webp", "image/webp", ("RGBA", "RGB"), alpha="full", icc=True, exif=True, animation=True, max_dim=16383),
    "avif": Target("AVIF", ".avif", "image/avif", ("RGBA", "RGB"), alpha="full", icc=True, exif=True, animation=True, max_dim=65535, lossless=False),
    "gif": Target("GIF", ".gif", "image/gif", ("P",), alpha="binary", animation=True, max_dim=65535),
    "bmp": Target("BMP", ".bmp", "image/bmp", ("RGB", "L")),
    "tiff": Target("TIFF", ".tif", "image/tiff", ("RGBA", "RGB", "L"), alpha="full", icc=True, exif=True, multipage=True, max_dim=1 << 30),
    "ico": Target("ICO", ".ico", "image/vnd.microsoft.icon", ("RGBA",), alpha="full", square=True, max_icon=256),
    "jpeg2000": Target("JPEG2000", ".jp2", "image/jp2", ("RGBA", "RGB", "L"), alpha="full"),
    "qoi": Target("QOI", ".qoi", "image/qoi", ("RGBA", "RGB"), alpha="full"),
    "tga": Target("TGA", ".tga", "image/x-tga", ("RGBA", "RGB", "L"), alpha="full"),
    "pnm": Target("PPM", ".ppm", "image/x-portable-pixmap", ("RGB", "L")),
    "pcx": Target("PCX", ".pcx", "image/x-pcx", ("RGB", "L")),
    "dds": Target("DDS", ".dds", "image/vnd-ms.dds", ("RGBA", "RGB"), alpha="full"),
    "sgi": Target("SGI", ".sgi", "image/sgi", ("RGBA", "RGB", "L"), alpha="full"),
    "icns": Target("ICNS", ".icns", "image/icns", ("RGBA",), alpha="full", square=True, max_icon=1024),
    # Only reachable when pillow-heif passed its separate encode check (never in the reference environment).
    "heic": Target("HEIF", ".heic", "image/heic", ("RGBA", "RGB"), alpha="full", icc=True, exif=True, lossless=False),
}

LOSSLESS_SOURCES = {"png", "gif", "bmp", "tiff", "ico", "qoi", "tga", "pnm", "pcx", "sgi", "dds", "icns", "svg"}

_TRANSPOSE = {
    2: [Image.Transpose.FLIP_LEFT_RIGHT],
    3: [Image.Transpose.ROTATE_180],
    4: [Image.Transpose.FLIP_TOP_BOTTOM],
    5: [Image.Transpose.TRANSPOSE],
    6: [Image.Transpose.ROTATE_270],
    7: [Image.Transpose.TRANSVERSE],
    8: [Image.Transpose.ROTATE_90],
}

_SRGB = ImageCms.createProfile("sRGB")
SRGB_ICC = ImageCms.ImageCmsProfile(_SRGB).tobytes()


def bg_rgb(color: str) -> tuple[int, int, int]:
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]


@dataclass
class Frame:
    image: Image.Image
    duration: int = 100


@dataclass
class Decoded:
    frames: list[Frame]
    total_frames: int
    kind: str                     # static | animated | multipage
    icc: bytes | None
    exif: bytes | None            # serialised with the orientation tag reset to 1
    loop: int | None
    orientation: int | None
    source_format: str
    notes: list[str] = field(default_factory=list)
    exif_nogps: bytes | None = None
    xmp: bytes | None = None      # orientation reset to 1
    has_gps: bool = False
    high_bit_depth: bool = False


# ======================================================================= decode

def _open_source(path: Path, fmt: str, backend: str, work: Path, timeout: float) -> Image.Image:
    if backend == "ffmpeg-librsvg":
        out = work / "svg-raster.png"
        res = proc.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "svg_pipe",
                        "-i", str(path), "-frames:v", "1", str(out)], timeout=timeout)
        if not res.ok or not out.exists():
            raise ConvertError("damaged", "The SVG could not be rendered.")
        path = out
    elif backend.startswith("imagemagick"):
        im_cmd = proc.imagemagick("convert")
        out = work / "im-decoded.png"
        coder = {"raw": "DNG"}.get(fmt, fmt.upper())
        res = proc.run([*im_cmd, f"{coder}:{path}", f"png:{out}"], timeout=timeout) if im_cmd else None
        if not res or not res.ok or not out.exists():
            raise ConvertError("damaged", "The image could not be decoded.")
        path = out
    try:
        return Image.open(path)
    except MemoryError:
        raise
    except Exception as exc:
        raise ConvertError("damaged", f"The image could not be opened: {exc}") from exc


def _source_bit_depth(path: Path, im: Image.Image) -> int:
    if im.mode.startswith("I;16") or im.mode in ("I", "F"):
        return 16
    try:
        if im.format == "PNG":
            with open(path, "rb") as fh:
                head = fh.read(26)
            return head[24] if head[12:16] == b"IHDR" else 8
        if im.format == "TIFF":
            bps = im.tag_v2.get(258)
            return max(bps) if isinstance(bps, tuple) else int(bps or 8)
    except Exception:
        pass
    return 8


# TIFF "EXIF" is the image's own IFD: these tags describe pixel layout/storage and must never be copied
# into another file (copying Compression/StripOffsets into a new TIFF corrupts it).
_STRUCTURAL_TAGS = {254, 255, 256, 257, 258, 259, 262, 263, 266, 273, 277, 278, 279, 280, 281, 282, 283, 284, 286, 287,
                    290, 291, 296, 297, 317, 318, 319, 320, 321, 322, 323, 324, 325, 330, 338, 339, 340, 341, 347,
                    530, 531, 532, 700, 33723, 34377, 34675, 37724, 50341}


def _clean_exif(exif: "Image.Exif") -> None:
    for tag in list(exif.keys()):
        if tag in _STRUCTURAL_TAGS:
            del exif[tag]


def _fix_xmp(xmp: bytes) -> bytes:
    """Orientation is baked into the pixels, so XMP must say 'normal' too."""
    import re
    xmp = re.sub(rb'tiff:Orientation="\d"', b'tiff:Orientation="1"', xmp)
    return re.sub(rb"<tiff:Orientation>\d</tiff:Orientation>", b"<tiff:Orientation>1</tiff:Orientation>", xmp)


def decode(path: Path, fmt: str, backend: str, opts: Settings, limits: dict, work: Path) -> Decoded:
    im = _open_source(path, fmt, backend, work, limits["probe_timeout_s"])
    w, h = im.size
    max_px = limits["max_image_megapixels"] * 1_000_000
    if w * h > max_px:
        raise ConvertError("limit_exceeded", f"{w}×{h} is {w * h / 1e6:.1f} MP; the limit is {limits['max_image_megapixels']} MP.")
    try:
        total = getattr(im, "n_frames", 1) or 1
    except MemoryError:
        raise
    except Exception as exc:  # Pillow raises IndexError/EOFError/OSError on truncation
        raise ConvertError("damaged", f"The frames of this image could not be read ({exc}).") from exc
    pil_fmt = im.format or ""
    kind = "static" if total == 1 else ("animated" if pil_fmt in ("GIF", "PNG", "WEBP", "AVIF") else "multipage")
    use = total if (opts.frames in ("keep", "separate")) else 1
    decoded_limit = limits["max_decoded_megapixels"] * 1_000_000
    if w * h * use > decoded_limit:
        raise ConvertError("limit_exceeded", f"{use} frames × {w}×{h} = {w * h * use / 1e6:.0f} MP decoded; "
                                             f"the limit is {limits['max_decoded_megapixels']} MP. Choose 'first frame only'.")
    notes: list[str] = []
    exif_bytes = exif_nogps = None
    orientation = None
    has_gps = False
    try:
        exif = im.getexif()
        orientation = exif.get(0x0112)
        _clean_exif(exif)
        if len(exif):
            if 0x0112 in exif:
                exif[0x0112] = 1
            exif_bytes = exif.tobytes()
            has_gps = 0x8825 in exif and bool(exif.get_ifd(0x8825))
            if 0x8825 in exif:
                del exif[0x8825]
            # Vendor MakerNotes are opaque and some carry location; remove them with GPS.
            if 0x8769 in exif:  # get_ifd() would otherwise create an empty Exif IFD that breaks TIFF output
                exif_ifd = exif.get_ifd(0x8769)
                if 0x927C in exif_ifd:
                    del exif_ifd[0x927C]
            exif_nogps = exif.tobytes() if len(exif) else None
    except Exception:
        exif_bytes = exif_nogps = None
        notes.append("EXIF metadata could not be copied (unreadable EXIF block)")
    xmp = im.info.get("xmp") or im.info.get("XML:com.adobe.xmp")
    if isinstance(xmp, str):
        xmp = xmp.encode("utf-8")
    if xmp:
        xmp = _fix_xmp(xmp)
        has_gps = has_gps or b"GPSLatitude" in xmp
    depth = _source_bit_depth(path, im) if backend.startswith("pillow") else 8
    icc = im.info.get("icc_profile")
    loop = im.info.get("loop")
    frames: list[Frame] = []
    try:
        for i, fr in enumerate(ImageSequence.Iterator(im)):
            if i >= use:
                break
            fr.load()
            if fr.size != (w, h) and kind == "multipage":
                pass  # multipage TIFF pages may differ in size; each is kept as is
            frames.append(Frame(fr.copy(), int(fr.info.get("duration", im.info.get("duration", 100)) or 100)))
    except MemoryError:
        raise
    except Exception as exc:  # truncated/corrupt frame data surfaces as many exception types
        raise ConvertError("damaged", f"The image is damaged or truncated ({type(exc).__name__}: {exc}).") from exc
    if len(frames) < use:
        raise ConvertError("damaged", f"Only {len(frames)} of {use} frames could be decoded.")
    if depth > 8:
        notes.append(f"{depth}-bit per channel source converted to 8-bit per channel")
    return Decoded(frames, total, kind, icc, exif_bytes, loop, orientation, fmt, notes,
                   exif_nogps=exif_nogps, xmp=xmp, has_gps=has_gps, high_bit_depth=depth > 8)


# ======================================================================= normalise

def _icc_space(icc: bytes) -> str:
    try:
        return ImageCms.ImageCmsProfile(io.BytesIO(icc)).profile.xcolor_space.strip()
    except Exception:
        return ""


def _to_basic_mode(im: Image.Image) -> Image.Image:
    """Bring any Pillow mode to one of 1/L/LA/RGB/RGBA/CMYK, preserving tone and alpha."""
    m = im.mode
    if m in ("L", "LA", "RGB", "RGBA", "CMYK"):
        return im
    if m == "1":
        return im.convert("L")
    if m in ("I;16", "I;16L", "I;16B", "I;16N", "I"):
        # 16-bit grey: scale to 8-bit (Pillow's convert('L') clips instead of scaling).
        return im.convert("I").point(lambda v: v * (1 / 256)).convert("L")
    if m == "F":
        return im.convert("L")
    if m in ("P", "PA"):
        return im.convert("RGBA") if ("transparency" in im.info or m == "PA") else im.convert("RGB")
    if m in ("La",):
        return im.convert("LA")
    if m in ("RGBa", "RGBX"):
        return im.convert("RGBA" if m == "RGBa" else "RGB")
    return im.convert("RGB")


def _is_srgb(icc: bytes) -> bool:
    try:
        return "srgb" in (ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(icc))) or "").lower()
    except Exception:
        return False


def _color_manage(im: Image.Image, icc: bytes | None, keep_icc: bool, notes: list[str]) -> tuple[Image.Image, bytes | None]:
    """Returns (image, icc_to_embed). Converts to sRGB when the profile cannot travel with the pixels.
    keep_icc=False also means 'convert to sRGB and drop the profile' (appearance is preserved)."""
    if im.mode == "CMYK":
        if icc and _icc_space(icc) == "CMYK":
            try:
                im = ImageCms.profileToProfile(im, ImageCms.ImageCmsProfile(io.BytesIO(icc)), _SRGB, outputMode="RGB")
                notes.append("CMYK converted to sRGB using the embedded profile")
                return im, (SRGB_ICC if keep_icc else None)
            except Exception:
                pass
        notes.append("CMYK converted to RGB without a profile (colours may shift slightly)")
        return im.convert("RGB"), None
    if not icc:
        return im, None
    space = _icc_space(icc)
    if keep_icc and ((space == "RGB" and im.mode in ("RGB", "RGBA")) or (space == "GRAY" and im.mode in ("L", "LA"))):
        return im, icc
    # Target cannot carry the profile (or it doesn't match): bake it into sRGB pixels.
    try:
        alpha = im.getchannel("A") if im.mode in ("RGBA", "LA") else None
        base = im.convert("L" if im.mode in ("L", "LA") else "RGB") if alpha is not None else im
        if space == "GRAY" and base.mode == "L":
            out = ImageCms.profileToProfile(base, ImageCms.ImageCmsProfile(io.BytesIO(icc)), _SRGB, outputMode="RGB")
        elif space == "RGB" and base.mode == "RGB":
            out = ImageCms.profileToProfile(base, ImageCms.ImageCmsProfile(io.BytesIO(icc)), _SRGB, outputMode="RGB")
        else:
            notes.append("colour profile ignored (does not match the image's colour mode)")
            return im, None
        if alpha is not None:
            out.putalpha(alpha)
        if not _is_srgb(icc):
            notes.append("colours converted from the embedded profile to sRGB; profile not embedded")
        return out, None
    except Exception:
        notes.append("colour profile could not be applied and was dropped")
        return im, None


def _has_transparency(frames: list[Image.Image]) -> bool:
    for im in frames:
        if im.mode in ("RGBA", "LA") and im.getchannel("A").getextrema()[0] < 255:
            return True
    return False


def _flatten(im: Image.Image, bg: tuple[int, int, int]) -> Image.Image:
    if im.mode not in ("RGBA", "LA"):
        return im
    rgba = im.convert("RGBA")
    canvas = Image.new("RGBA", rgba.size, bg + (255,))
    canvas.alpha_composite(rgba)
    out = canvas.convert("RGB")
    return out.convert("L") if im.mode == "LA" and bg[0] == bg[1] == bg[2] else out


def _to_target_mode(im: Image.Image, t: Target) -> Image.Image:
    if im.mode in t.modes:
        return im
    if im.mode == "LA" and "RGBA" in t.modes:
        return im.convert("RGBA")
    if im.mode == "L" and "RGB" in t.modes:
        return im.convert("RGB")
    if im.mode == "RGB" and "RGBA" in t.modes and "RGB" not in t.modes:
        return im.convert("RGBA")
    if im.mode in ("RGBA", "LA") and "RGB" in t.modes:
        return im.convert("RGB")  # only reached after flattening
    return im.convert(t.modes[0])


def _square(im: Image.Image, t: Target, bg: tuple[int, int, int], keep_alpha: bool) -> Image.Image:
    w, h = im.size
    side = max(w, h)
    if side > t.max_icon:
        scale = t.max_icon / side
        im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.Resampling.LANCZOS)
        w, h = im.size
        side = max(w, h)
    if w == h:
        return im
    fill = (0, 0, 0, 0) if keep_alpha else bg + (255,)
    canvas = Image.new("RGBA", (side, side), fill)
    canvas.paste(im.convert("RGBA"), ((side - w) // 2, (side - h) // 2))
    return canvas


def _gif_frame(im: Image.Image, keep_transparency: bool, bg: tuple[int, int, int], colors: int = 256,
               dither: bool = True) -> tuple[Image.Image, bool]:
    """Quantise to a palette. With transparency kept, pixels with alpha < 128 become the transparent
    index and partially transparent pixels are blended onto the background colour first."""
    rgba = im.convert("RGBA")
    alpha = rgba.getchannel("A")
    transparent = keep_transparency and alpha.getextrema()[0] < 128
    flat = _flatten(rgba, bg).convert("RGB")
    d = Image.Dither.FLOYDSTEINBERG if dither else Image.Dither.NONE
    if not transparent:
        return flat.quantize(colors=colors, method=Image.Quantize.MEDIANCUT, dither=d), False
    n = max(1, min(colors, 256) - 1)
    pal = flat.quantize(colors=n, method=Image.Quantize.MEDIANCUT, dither=d)
    mask = alpha.point(lambda a: 255 if a < 128 else 0)
    pal.paste(n, mask=mask)
    palette = (pal.getpalette() or [])[: n * 3]
    palette += [0, 0, 0] * (n - len(palette) // 3) + [0, 0, 0]
    pal.putpalette(palette)
    pal.info["transparency"] = n
    return pal, True


# ======================================================================= encode

BROWSER_PREVIEWABLE = {"jpeg", "png", "webp", "avif", "gif", "bmp"}
PREVIEW_MAX = 4096
MAX_TARGET_ATTEMPTS = 16


def _resize(im: Image.Image, size: tuple[int, int]) -> Image.Image:
    if im.size == size:
        return im
    # Resample with premultiplied alpha so transparent pixels don't bleed dark fringes into edges.
    if im.mode == "RGBA":
        return im.convert("RGBa").resize(size, Image.Resampling.LANCZOS).convert("RGBA")
    if im.mode == "LA":
        return im.convert("La").resize(size, Image.Resampling.LANCZOS).convert("LA")
    return im.resize(size, Image.Resampling.LANCZOS)


@dataclass
class Stage:
    """Frames after orientation, colour management and transparency decisions (before geometry)."""
    frames: list[Image.Image]
    durations: list[int]
    has_alpha: bool
    alpha_mode: str          # what the target will get: full | binary | none
    icc_out: bytes | None
    notes: list[str]
    preview_src: Image.Image | None = None   # frame 0 before any flattening, for the "before" preview


def stage_a(decoded: Decoded, s: Settings, t: "Target", feats: dict) -> Stage:
    notes = list(decoded.notes)
    alpha_mode = t.alpha if (feats.get("alpha_write", t.alpha != "none") or t.alpha == "binary") else "none"
    ops = _TRANSPOSE.get(decoded.orientation or 1, [])
    if ops:
        notes.append("EXIF orientation applied to the pixels; orientation tag reset to normal")
    keep_icc = t.icc and s.icc == "keep"
    frames, icc_out = [], None
    for fr in decoded.frames:
        im = _to_basic_mode(fr.image)
        for op in ops:
            im = im.transpose(op)
        im, icc_out = _color_manage(im, decoded.icc, keep_icc, notes)
        frames.append(im)
    preview_src = frames[0]
    if s.remove_background:
        from . import bgremove
        frames = [bgremove.cutout(im) for im in frames]
        notes.append("background removed (AI cut-out of the main subject); check the edges")
    has_alpha = _has_transparency(frames)
    if has_alpha:
        if alpha_mode == "none" or s.transparency == "flatten":
            frames = [_flatten(im, bg_rgb(s.background)) for im in frames]
            notes.append(f"transparency flattened onto {s.background}")
            alpha_mode = "none"
        elif alpha_mode == "binary":
            notes.append(f"GIF keeps 1-bit transparency: pixels under 50% opacity become transparent, "
                         f"semi-transparent edges are blended onto {s.background}")
    else:
        frames = [im.convert("RGB") if im.mode == "RGBA" else (im.convert("L") if im.mode == "LA" else im) for im in frames]
        alpha_mode = "none"
    return Stage(frames, [f.duration for f in decoded.frames], has_alpha, alpha_mode, icc_out, list(dict.fromkeys(notes)),
                 preview_src=preview_src)


def frame_size(im: Image.Image, s: Settings, scale: float) -> tuple[tuple[int, int], list[str]]:
    w, h, notes = s.resize.compute(im.width, im.height)
    if scale < 1:
        w, h = max(1, round(w * scale)), max(1, round(h * scale))
    return (w, h), notes


def geometry(im: Image.Image, size: tuple[int, int], t: "Target", s: Settings, alpha_mode: str) -> Image.Image:
    im = _resize(im, size)
    if t.square:
        im = _square(im, t, bg_rgb(s.background), keep_alpha=alpha_mode != "none")
    if max(im.size) > t.max_dim:
        raise ConvertError("too_large_for_format", f"{t.pillow} supports at most {t.max_dim} px per side; result would be {im.width}×{im.height}.")
    return im


def _encode_prep(im: Image.Image, t: "Target", s: Settings, values: dict, alpha_mode: str) -> tuple[Image.Image, int | None]:
    """Final pixel format for the encoder. Returns (image, transparent palette index or None)."""
    if t.pillow == "GIF":
        pal, tr = _gif_frame(im, keep_transparency=alpha_mode == "binary", bg=bg_rgb(s.background),
                             colors=int(values.get("colors", 256)), dither=bool(values.get("dither", True)))
        return pal, (pal.info.get("transparency") if tr else None)
    if t.pillow == "PNG" and values.get("palette"):
        src = im if im.mode in ("RGB", "RGBA") else im.convert("RGBA" if "A" in im.getbands() else "RGB")
        method = Image.Quantize.FASTOCTREE if src.mode == "RGBA" else Image.Quantize.MEDIANCUT
        return src.quantize(colors=256, method=method, dither=Image.Dither.FLOYDSTEINBERG), None
    return _to_target_mode(im, t), None


_SUBSAMPLING = {"444": 0, "422": 1, "420": 2}
_TIFF_COMPRESSION = {"lzw": "tiff_lzw", "deflate": "tiff_adobe_deflate", "none": None}


def _jp2_ratio(q: int) -> float:
    return round(2 ** ((100 - q) / 12.5 + 1), 2)


def _metadata_kwargs(t: "Target", decoded: Decoded, s: Settings, kw: dict) -> dict:
    """EXIF/XMP according to the user's choice. Returns a human-readable summary."""
    summary = {}
    if not t.exif:
        summary["exif"] = "not supported by this format" if decoded.exif else "none in source"
    elif s.metadata == "remove_all" or not decoded.exif:
        summary["exif"] = "removed" if decoded.exif else "none in source"
    elif s.metadata == "remove_gps":
        if decoded.exif_nogps:
            kw["exif"] = decoded.exif_nogps
        summary["exif"] = ("kept; GPS location and any camera MakerNote removed" if decoded.has_gps
                           else "kept; no GPS in source; any camera MakerNote removed")
    else:
        kw["exif"] = decoded.exif
        summary["exif"] = "kept" + (" (includes GPS location)" if decoded.has_gps else "")
    if t.pillow == "TIFF" and kw.get("exif"):
        # Pillow's libtiff writer cannot write EXIF sub-IFDs (Exif/GPS pointers) and fails outright, so TIFF
        # output keeps only the main EXIF block (camera make/model, software, date…).
        ex = Image.Exif()
        ex.load(kw["exif"])
        dropped = [n for tag, n in ((0x8769, "exposure details"), (0x8825, "GPS location")) if tag in ex]
        for tag in (0x8769, 0x8825, 0xA005):
            if tag in ex:
                del ex[tag]
        kw["exif"] = ex.tobytes()
        if dropped:
            summary["exif"] += f"; TIFF output omits EXIF sub-blocks ({', '.join(dropped)})"
    if decoded.xmp:
        if s.metadata != "keep":
            summary["xmp"] = "removed (XMP can contain location and personal data)"
        elif t.pillow in ("JPEG", "WEBP", "AVIF"):
            kw["xmp"] = decoded.xmp
            summary["xmp"] = "kept"
        elif t.pillow == "PNG":
            from PIL import PngImagePlugin
            info = PngImagePlugin.PngInfo()
            info.add_itxt("XML:com.adobe.xmp", decoded.xmp.decode("utf-8", "replace"))
            kw["pnginfo"] = info
            summary["xmp"] = "kept"
        else:
            summary["xmp"] = "not supported by this format"
    return summary


def save_kwargs(t: "Target", values: dict, n_frames: int) -> tuple[dict, dict]:
    kw: dict = {}
    used: dict = {}
    p = t.pillow
    if p == "JPEG":
        kw.update(quality=int(values["quality"]), optimize=True, progressive=bool(values["progressive"]),
                  subsampling=_SUBSAMPLING[values["subsampling"]])
        used.update(quality=values["quality"], progressive=values["progressive"], subsampling=values["subsampling"])
    elif p == "WEBP":
        if values["lossless"]:
            kw.update(lossless=True, quality=100, method=int(values["effort"]))
            used.update(mode="lossless", effort=values["effort"])
        else:
            kw.update(quality=int(values["quality"]), method=int(values["effort"]))
            used.update(mode="lossy", quality=values["quality"], effort=values["effort"])
    elif p == "AVIF":
        kw.update(quality=int(values["quality"]), speed=10 - int(values["effort"]))
        used.update(mode="lossy", quality=values["quality"], effort=values["effort"])
    elif p == "HEIF":
        kw.update(quality=int(values["quality"]))
        used.update(mode="lossy", quality=values["quality"])
    elif p == "PNG":
        kw.update(compress_level=int(values["effort"]), optimize=int(values["effort"]) >= 9 and n_frames == 1)
        used.update(mode="palette 256 colours (lossy)" if values.get("palette") else "lossless", effort=values["effort"])
    elif p == "GIF":
        used.update(colors=values["colors"], dither=values["dither"])
    elif p == "TIFF":
        comp = _TIFF_COMPRESSION[values["compression"]]
        if comp:
            kw["compression"] = comp
        used.update(compression=values["compression"])
    elif p == "JPEG2000":
        if values["lossless"]:
            used.update(mode="lossless")
        else:
            kw.update(irreversible=True, quality_mode="rates", quality_layers=[_jp2_ratio(int(values["quality"]))])
            used.update(mode="lossy", quality=values["quality"])
    elif p == "TGA":
        if values.get("rle"):
            kw["compression"] = "tga_rle"
        used.update(rle=bool(values.get("rle")))
    return kw, used


def _loop_kwargs(t: "Target", loop: int | None) -> tuple[dict, int | None]:
    """Keep 'loops forever' vs 'plays N times'. Returns (kwargs, loop value expected on re-read)."""
    if t.pillow == "GIF":
        return ({}, None) if loop is None else ({"loop": loop}, loop)
    value = 1 if loop is None else loop          # WebP/APNG/AVIF: 0 = forever, n = play n times
    return {"loop": value}, value


@dataclass
class Rendered:
    data: bytes
    width: int
    height: int
    frames: int
    used: dict


def render(stage: Stage, idx: list[int], decoded: Decoded, s: Settings, t: "Target", values: dict, scale: float,
           meta_kw: dict) -> Rendered:
    images: list[Image.Image] = []
    tr_index = None
    for i in idx:
        size, _ = frame_size(stage.frames[i], s, scale)
        im = geometry(stage.frames[i], size, t, s, stage.alpha_mode)
        im, tr = _encode_prep(im, t, s, values, stage.alpha_mode)
        tr_index = tr if tr is not None else tr_index
        # Pillow encoders fall back to im.info for icc_profile/exif/xmp; only explicit choices may be written.
        im.info = {k: v for k, v in im.info.items() if k == "transparency"}
        images.append(im)
    kw, used = save_kwargs(t, values, len(images))
    kw.update(meta_kw)
    if stage.icc_out and t.icc:
        kw["icc_profile"] = stage.icc_out
    if t.pillow == "ICO":
        side = images[0].width
        kw["sizes"] = [(z, z) for z in (16, 24, 32, 48, 64, 128, 256) if z <= side] or [(side, side)]
        used["sizes"] = [z[0] for z in kw["sizes"]]
    if len(images) > 1:
        kw.update(save_all=True, append_images=images[1:])
        if t.animation:
            loop_kw, _ = _loop_kwargs(t, decoded.loop)
            kw.update(duration=[stage.durations[i] for i in idx], **loop_kw)
            if t.pillow == "GIF":
                kw["disposal"] = 2
            if t.pillow == "PNG":
                kw["disposal"] = 1
    if t.pillow == "GIF" and tr_index is not None:
        kw["transparency"] = tr_index
    buf = io.BytesIO()
    try:
        images[0].save(buf, format=t.pillow, **kw)
    except (OSError, ValueError, KeyError) as exc:
        raise ConvertError("encode_failed", f"The {t.pillow} encoder failed: {exc}") from exc
    return Rendered(buf.getvalue(), images[0].width, images[0].height, len(images), used)


def _knob(fid: str, values: dict) -> str | None:
    knob = TARGET_KNOB.get(fid)
    if knob == "quality" and values.get("lossless"):
        return None
    return knob


def produce(stage: Stage, idx: list[int], decoded: Decoded, s: Settings, t: "Target", fid: str,
            meta_kw: dict, deadline: float) -> tuple[Rendered, dict | None]:
    """Encode once, or run a bounded best-effort search for the size target."""
    import time
    values = dict(s.values)
    first = render(stage, idx, decoded, s, t, values, 1.0, meta_kw)
    goal = s.target.bytes
    if not goal:
        return first, None
    attempts = 1
    best = first
    info: dict = {"bytes": goal, "met": len(first.data) <= goal, "attempts": 1, "stopped_early": False,
                  "changed": [], "message": ""}
    if info["met"]:
        info["message"] = "The chosen settings already fit; nothing was lowered."
        return first, info

    def budget_left() -> bool:
        return attempts < MAX_TARGET_ATTEMPTS and time.time() < deadline

    knob = _knob(fid, values)
    if knob is None and "effort" in values:
        hi_effort = max(c["max"] for c in CONTROLS[fid] if c["id"] == "effort")
        if values["effort"] < hi_effort and budget_left():
            v2 = {**values, "effort": hi_effort}
            r = render(stage, idx, decoded, s, t, v2, 1.0, meta_kw)
            attempts += 1
            if len(r.data) < len(best.data):
                best, values = r, v2
                info["changed"].append(f"compression effort raised to {hi_effort} (same pixels)")
    if knob and len(best.data) > goal:
        start = int(values[knob])
        floor = s.target.min_quality if knob == "quality" else 16
        floor = min(floor, start)
        low = render(stage, idx, decoded, s, t, {**values, knob: floor}, 1.0, meta_kw)
        attempts += 1
        if len(low.data) <= goal:
            lo, hi, fit = floor, start - 1, low          # find the highest value that still fits
            while lo < hi and budget_left():
                mid = (lo + hi + 1) // 2
                r = render(stage, idx, decoded, s, t, {**values, knob: mid}, 1.0, meta_kw)
                attempts += 1
                if len(r.data) <= goal:
                    lo, fit = mid, r
                else:
                    hi = mid - 1
            best = fit
            values = {**values, knob: lo}
            info["changed"].append(f"{knob} lowered from {start} to {lo}")
        else:
            if len(low.data) < len(best.data):
                best = low
                values = {**values, knob: floor}
                info["changed"].append(f"{knob} lowered from {start} to the lowest allowed value {floor}")
    if len(best.data) > goal and s.target.allow_resize and budget_left():
        # The user allowed smaller dimensions: search the largest scale that fits, at the current settings.
        base_values = dict(s.values) if knob is None else {**values, knob: int(s.values[knob])}
        small = render(stage, idx, decoded, s, t, base_values, 0.05, meta_kw)
        attempts += 1
        if len(small.data) <= goal:
            lo, hi, fit = 0.05, 1.0, small
            while hi - lo > 0.01 and budget_left():
                mid = (lo + hi) / 2
                r = render(stage, idx, decoded, s, t, base_values, mid, meta_kw)
                attempts += 1
                if len(r.data) <= goal:
                    lo, fit = mid, r
                else:
                    hi = mid
            best = fit
            info["changed"] = [c for c in info["changed"] if not c.startswith(knob or "~")]
            info["changed"].append(f"dimensions reduced to {fit.width}×{fit.height} ({lo * 100:.0f}% of the requested size) "
                                   f"at your chosen settings")
            values = base_values
            info["scale"] = round(lo, 3)
    info["attempts"] = attempts
    info["stopped_early"] = not budget_left() and len(best.data) > goal
    info["met"] = len(best.data) <= goal
    info["final_bytes"] = len(best.data)
    if info["met"]:
        info["message"] = "Best effort: " + "; ".join(info["changed"]) + "."
    else:
        why = []
        if knob and not s.target.allow_resize:
            why.append(f"even at the lowest allowed {knob} the file is larger than the target")
        elif not knob:
            why.append(f"{t.pillow} is lossless here, so quality cannot be lowered")
        if s.target.allow_resize:
            why.append("even at 5% of the size the target was not reached" if not info["stopped_early"] else "the search hit its time/attempt limit")
        suggestion = "Tick “Allow smaller dimensions” to let the search shrink the image" if not s.target.allow_resize else \
            "Choose a lossy format or a larger target"
        info["message"] = f"{'; '.join(why).capitalize()}. The smallest result was kept. {suggestion}."
    best.used = {**best.used, **{k: v for k, v in values.items() if k in best.used or k == knob}}
    return best, info


def encode(decoded: Decoded, s: Settings, caps_features: dict, work: Path, stem: str, deadline: float | None = None) -> dict:
    """Produce output file(s) + previews. Returns a report dict; raises ConvertError."""
    import time
    fid = s.format
    t = TARGETS.get(fid)
    if t is None:
        raise ConvertError("unsupported_output", f"{fid} is not an available output format.")
    deadline = deadline or (time.time() + 3600)
    can_animate = t.animation and caps_features.get("animation_write", t.animation)
    can_multipage = t.multipage and caps_features.get("multipage_write", t.multipage)
    multi = len(decoded.frames) > 1 or decoded.total_frames > 1
    if multi:
        keeps = (decoded.kind == "animated" and (can_animate or can_multipage)) or (decoded.kind == "multipage" and can_multipage)
        if s.frames == "keep" and not keeps:
            raise ConvertError("frames_choice_required",
                               f"{t.pillow} cannot store {decoded.total_frames} {'frames' if decoded.kind == 'animated' else 'pages'}. "
                               "Choose 'first only' or 'separate files'.")

    stage = stage_a(decoded, s, t, caps_features)
    notes = stage.notes
    if multi and s.frames == "keep" and decoded.kind == "animated" and not can_animate and can_multipage:
        notes.append("animation frames stored as pages (timing is not kept)")
    _, resize_notes = frame_size(stage.frames[0], s, 1.0)
    notes += resize_notes
    if t.square:
        size0, _ = frame_size(stage.frames[0], s, 1.0)
        if size0[0] != size0[1] or max(size0) > t.max_icon:
            notes.append(f"fitted to a square canvas (max {t.max_icon} px)")
        if t.pillow == "ICNS" and max(size0) < 1024:
            notes.append("ICNS stores sizes up to 1024 px; larger slots are upscaled")
    meta_kw: dict = {}
    meta_summary = _metadata_kwargs(t, decoded, s, meta_kw)
    if t.icc and stage.icc_out:
        meta_summary["icc"] = "original profile embedded"
    elif decoded.icc:
        meta_summary["icc"] = "converted to sRGB; no profile embedded (colours look the same)" if not _is_srgb(decoded.icc) \
            else "sRGB source; profile not embedded (sRGB is the default)"
    else:
        meta_summary["icc"] = "none in source (treated as sRGB)"

    out_dir = work / "out"
    out_dir.mkdir(exist_ok=True)
    units: list[tuple[list[int], str]] = []
    n = len(stage.frames)
    if s.frames == "separate" and n > 1:
        digits = len(str(n))
        units = [([i], f"{stem}_{i + 1:0{digits}d}{t.ext}") for i in range(n)]
        notes.append(f"{n} {'frames' if decoded.kind == 'animated' else 'pages'} saved as separate files")
    else:
        if s.frames == "first" and decoded.total_frames > 1:
            notes.append(f"only the first of {decoded.total_frames} {'frames' if decoded.kind == 'animated' else 'pages'} kept (your choice)")
        units = [(list(range(n)), f"{stem}{t.ext}")]

    outputs = []
    for idx, name in units:
        rendered, target_info = produce(stage, idx, decoded, s, t, fid, meta_kw, deadline)
        dest = out_dir / name
        dest.write_bytes(rendered.data)
        anim = len(idx) > 1 and t.animation
        outputs.append({
            "path": dest, "settings": rendered.used, "target": target_info,
            "expected_frames": len(idx),
            "expected_duration": sum(stage.durations[i] for i in idx) if anim else None,
            "expected_loop": _loop_kwargs(t, decoded.loop)[1] if anim else "skip",
            "width": rendered.width, "height": rendered.height,
        })

    previews = make_previews(stage, s, t, outputs[0], work, scale=(outputs[0]["target"] or {}).get("scale", 1.0))
    return {"target": fid, "outputs": outputs, "notes": list(dict.fromkeys(notes)), "has_alpha_in": stage.has_alpha,
            "alpha_out": stage.has_alpha and stage.alpha_mode != "none",
            "icc_embedded": bool(stage.icc_out and t.icc), "metadata": meta_summary, "previews": previews,
            "source_has_gps": decoded.has_gps, "high_bit_depth": decoded.high_bit_depth}


def make_previews(stage: Stage, s: Settings, t: "Target", first_out: dict, work: Path, scale: float) -> dict:
    """before.png = source pixels (oriented, sRGB) at the output geometry; after = the real output."""
    pdir = work / "out" / ".preview"
    pdir.mkdir(exist_ok=True)
    size, _ = frame_size(stage.frames[0], s, scale)
    before = geometry(stage.preview_src or stage.frames[0], size, t, s, "full" if stage.has_alpha else "none")
    out_path: Path = first_out["path"]
    try:
        with Image.open(out_path) as o:
            o.seek(0)
            after = o.convert("RGBA")
    except MemoryError:
        raise
    except Exception as exc:
        if s.format == "heic":
            return {"before": None, "after": None, "after_is_output": False, "width": 0, "height": 0, "scaled": False}
        raise ConvertError("verify_failed", f"The {t.pillow} output could not be decoded again: {exc}") from exc
    scaled = max(after.size) > PREVIEW_MAX
    if scaled:
        f = PREVIEW_MAX / max(after.size)
        after = after.resize((round(after.width * f), round(after.height * f)), Image.Resampling.LANCZOS)
    if before.size != after.size:
        before = _resize(before.convert("RGBA"), after.size)
    before.save(pdir / "before.png", compress_level=3)
    use_file = (s.format in BROWSER_PREVIEWABLE) and not scaled
    if not use_file:
        after.save(pdir / "after.png", compress_level=3)
    return {"before": "before.png", "after": None if use_file else "after.png", "after_is_output": use_file,
            "width": after.width, "height": after.height, "scaled": scaled}


# ======================================================================= verify

def verify_output(path: Path, fid: str, expected_frames: int, expected_duration: int | None,
                  expected_loop="skip") -> dict:
    t = TARGETS[fid]
    s = sniff_file(path)
    detected = s.format_id
    if detected == "unknown":  # formats without magic numbers: confirm with Pillow
        try:
            with Image.open(path) as im:
                from ..capabilities.images import PILLOW_TO_ID
                detected = PILLOW_TO_ID.get(im.format or "", "unknown")
        except Exception:
            detected = "unknown"
    checks = {"type": detected == fid, "extension": path.suffix == t.ext}
    try:
        with Image.open(path) as im:
            im.load()
            frames = getattr(im, "n_frames", 1) or 1
            total = 0
            if frames > 1 and t.animation:
                for f in ImageSequence.Iterator(im):
                    total += int(f.info.get("duration", 0) or 0)
            im.seek(0)
            loop = im.info.get("loop")
            alpha_pixels = ("A" in im.getbands() or "transparency" in im.info) and \
                im.convert("RGBA").getchannel("A").getextrema()[0] < 255
            info = {"width": im.width, "height": im.height, "mode": im.mode, "frames": frames, "has_alpha": alpha_pixels,
                    "loop": loop}
    except Exception as exc:
        if fid == "heic":  # no Pillow decoder: structural + FFmpeg HEVC check
            from .heif_tools import check_heic
            ok, detail, dims = check_heic(path)
            if not ok:
                raise ConvertError("verify_failed", f"The HEIC output failed verification: {detail}") from exc
            return {"width": dims[0], "height": dims[1], "mode": "?", "frames": 1, "has_alpha": False, "loop": None,
                    "detected": detected, "mime": t.mime, "extension": t.ext,
                    "checks": {**checks, "frames": True, "decoded_by": "FFmpeg HEVC"}}
        raise ConvertError("verify_failed", f"The output could not be decoded again: {exc}") from exc
    if t.animation and expected_duration:
        # Encoders may merge identical consecutive frames; the total running time must match.
        checks["frames"] = abs(total - expected_duration) <= 10 * expected_frames
    else:
        checks["frames"] = frames == expected_frames
    if expected_loop != "skip" and t.pillow in ("GIF", "PNG", "WEBP"):
        checks["loop"] = loop == expected_loop
    if not all(checks.values()):
        failed = [k for k, v in checks.items() if not v]
        raise ConvertError("verify_failed", f"Output check failed ({', '.join(failed)}): detected {detected}, {frames} frame(s), loop {loop}.")
    return {**info, "detected": detected, "mime": t.mime, "extension": t.ext, "checks": checks}


def zip_outputs(paths: list[Path], dest: Path) -> Path:
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_STORED) as zf:
        for p in paths:
            zf.write(p, arcname=p.name)
    return dest
