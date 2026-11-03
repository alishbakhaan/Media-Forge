"""Inspect an uploaded file: detect its real type from content, open it with the backend
that would process it, and report details plus the output formats that are valid for it.
"""
from __future__ import annotations

import json
import re
from fractions import Fraction
from pathlib import Path

from PIL import Image, ImageCms, ImageSequence

from . import proc
from .capabilities import images as img_caps
from .capabilities import video as vid_caps
from .config import Limits
from .sniff import Sniff, sniff_file

Image.MAX_IMAGE_PIXELS = None

try:  # optional; only used when capability detection verified pillow-heif decoding
    import pillow_heif  # type: ignore
    pillow_heif.register_heif_opener()
except Exception:
    pass  # replaced by the explicit, configurable limit in _inspect_pillow


class InspectError(Exception):
    """An error with a user-facing message and a stable code."""

    def __init__(self, code: str, message: str, status: int = 422, detected: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.detected = detected


def _ext(name: str) -> str:
    suffix = Path(name).suffix.lower()
    return suffix if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix or "") else ""


RAW_CODER = {"dng": "DNG", "cr2": "CR2", "cr3": "CR3", "nef": "NEF", "arw": "ARW", "orf": "ORF",
             "rw2": "RW2", "raf": "RAF", "pef": "PEF"}


# ======================================================================= images

def _icc_description(data: bytes | None) -> str | None:
    if not data:
        return None
    try:
        import io
        prof = ImageCms.ImageCmsProfile(io.BytesIO(data))
        return (ImageCms.getProfileDescription(prof) or "").strip() or "embedded profile"
    except Exception:
        return "embedded profile (unreadable)"


_BIT_DEPTH = {"1": 1, "L": 8, "P": 8, "RGB": 8, "RGBA": 8, "CMYK": 8, "YCbCr": 8, "LA": 8, "PA": 8,
              "I;16": 16, "I;16B": 16, "I;16L": 16, "I": 32, "F": 32, "RGBa": 8, "La": 8}

_ORIENTATION = {1: "normal", 2: "mirrored", 3: "rotated 180°", 4: "mirrored vertically", 5: "mirrored + rotated 90° CW",
                6: "rotated 90° CW", 7: "mirrored + rotated 90° CCW", 8: "rotated 90° CCW"}


def _inspect_pillow(path: Path, limits: Limits) -> dict:
    max_px = limits.max_image_megapixels * 1_000_000
    # Opening only parses the header. Our own pixel limit (checked before any decode) replaces
    # Pillow's global decompression-bomb threshold, which would be racy to change per request.
    try:
        im = Image.open(path)
    except Exception as exc:
        raise InspectError("damaged", f"The image could not be opened: {exc}", 422) from exc
    with im:
        width, height = im.size
        over_limit = width * height > max_px
        if not over_limit:
            try:
                im.load()
            except Exception as exc:
                raise InspectError("damaged", f"The image is damaged or truncated ({exc}).", 422) from exc
        try:
            frames = getattr(im, "n_frames", 1) or 1
        except Exception as exc:  # Pillow raises IndexError/EOFError/OSError on truncation
            raise InspectError("damaged", f"The image is damaged: its frames could not be read ({exc}).", 422) from exc
        pil_format = im.format or ""
        animated = frames > 1 and pil_format in ("GIF", "PNG", "WEBP", "AVIF")
        multipage = frames > 1 and not animated
        bands = im.getbands()
        has_alpha = "A" in bands or "a" in bands or "transparency" in im.info
        transparent_pixels = False
        if (has_alpha or frames > 1) and not over_limit:
            try:
                transparent_pixels = any(
                    fr.convert("RGBA").getchannel("A").getextrema()[0] < 255
                    for _, fr in zip(range(64), ImageSequence.Iterator(im)))
                im.seek(0)
            except Exception:
                transparent_pixels = has_alpha
            has_alpha = has_alpha or transparent_pixels
        gps = False
        try:
            exif = im.getexif()
            orientation = exif.get(0x0112)
            exif_tags = len(exif)
            gps = 0x8825 in exif and bool(exif.get_ifd(0x8825))
        except Exception:
            orientation, exif_tags = None, 0
        xmp_data = im.info.get("xmp") or im.info.get("XML:com.adobe.xmp") or b""
        if isinstance(xmp_data, str):
            xmp_data = xmp_data.encode("utf-8", "replace")
        gps = gps or b"GPSLatitude" in xmp_data
        dpi = im.info.get("dpi")
        bit_depth = _BIT_DEPTH.get(im.mode)
        if pil_format == "PNG":  # Pillow narrows 16-bit RGB(A) to 8-bit modes; read the true depth from IHDR
            with open(path, "rb") as fh:
                head = fh.read(26)
            if len(head) == 26 and head[12:16] == b"IHDR":
                bit_depth = head[24]
        elif pil_format == "TIFF":
            try:
                bps = im.tag_v2.get(258)
                bit_depth = max(bps) if isinstance(bps, tuple) else int(bps or bit_depth or 8)
            except Exception:
                pass
        info = {
            "backend": "pillow",
            "pillow_format": pil_format,
            "width": width,
            "height": height,
            "megapixels": round(width * height / 1e6, 2),
            "mode": im.mode,
            "bit_depth": bit_depth,
            "has_alpha": has_alpha,
            "transparent_pixels": transparent_pixels,
            "frames": frames,
            "animated": animated,
            "multipage": multipage,
            "loop": im.info.get("loop") if animated else None,
            "icc_profile": _icc_description(im.info.get("icc_profile")),
            "exif_orientation": orientation,
            "orientation_label": _ORIENTATION.get(orientation) if orientation else None,
            "display_size": [height, width] if orientation in (5, 6, 7, 8) else [width, height],
            "exif_tags": exif_tags,
            "gps": gps,
            "xmp": bool(im.info.get("xmp") or im.info.get("XML:com.adobe.xmp")),
            "dpi": [round(float(d), 1) for d in dpi] if dpi else None,
        }
    if over_limit:
        info["limit_violations"] = [f"{info['megapixels']} MP exceeds the {limits.max_image_megapixels} MP limit"]
    return info


def _inspect_imagemagick(path: Path, coder: str, limits: Limits) -> dict:
    cmd = proc.imagemagick("identify")
    if not cmd:
        raise InspectError("unsupported", "ImageMagick is not installed.", 415)
    try:
        res = proc.run([*cmd, "-ping", "-format", "%w %h %n %[channels]\\n", f"{coder}:{path}"], timeout=limits.probe_timeout_s)
    except proc.ProcessTimeout as exc:
        raise InspectError("timeout", "Reading the image took too long.", 422) from exc
    first = res.stdout.decode("utf-8", "replace").strip().splitlines()[:1]
    if not res.ok or not first:
        raise InspectError("damaged", "The image could not be decoded (damaged or unsupported variant).", 422)
    parts = first[0].split()
    width, height = int(parts[0]), int(parts[1])
    info = {"backend": "imagemagick", "width": width, "height": height, "megapixels": round(width * height / 1e6, 2),
            "mode": parts[3] if len(parts) > 3 else None, "has_alpha": len(parts) > 3 and "a" in parts[3].lower(),
            "frames": 1, "animated": False, "multipage": False, "icc_profile": None, "exif_orientation": None,
            "display_size": [width, height]}
    if width * height > limits.max_image_megapixels * 1_000_000:
        info["limit_violations"] = [f"{info['megapixels']} MP exceeds the {limits.max_image_megapixels} MP limit"]
    return info


def _inspect_svg(path: Path, limits: Limits) -> dict:
    text = path.read_bytes()[:2_000_000].decode("utf-8", "replace")
    warnings_ = []
    if re.search(r"""(?:xlink:)?href\s*=\s*["'](?:https?:|file:|//)""", text):
        warnings_.append("References to external files/URLs are not loaded.")
    if "<script" in text.lower():
        warnings_.append("Scripts inside the SVG are ignored.")
    out = path.parent / "svg-preview.png"
    try:
        res = proc.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "svg_pipe",
                        "-i", str(path), "-frames:v", "1", str(out)], timeout=limits.probe_timeout_s)
    except proc.ProcessTimeout as exc:
        raise InspectError("timeout", "Rasterising the SVG took too long.", 422) from exc
    if not res.ok or not out.exists():
        raise InspectError("damaged", "The SVG could not be rendered (invalid or unsupported SVG).", 422)
    with Image.open(out) as im:
        w, h = im.size
        alpha = "A" in im.getbands()
    return {"backend": "ffmpeg-librsvg", "width": w, "height": h, "megapixels": round(w * h / 1e6, 2), "vector": True,
            "has_alpha": alpha, "frames": 1, "animated": False, "multipage": False, "icc_profile": None,
            "exif_orientation": None, "display_size": [w, h], "warnings": warnings_}


_ALPHA_KIND = {"gif": "binary"}


def _image_outputs(caps: dict, info: dict, source_fmt: str = "") -> list[dict]:
    """Verified outputs plus the explicit choices each one needs for this particular input."""
    outputs = []
    n = info.get("frames", 1)
    noun = "frames" if info.get("animated") else "pages"
    transparent = info.get("transparent_pixels", info.get("has_alpha"))
    for fmt in caps["images"]["formats"]:
        if not (fmt.get("write") and fmt["write"].get("verified")):
            continue
        feats = fmt["features"]
        notes: list[str] = []
        frame_options: list[dict] = []
        if n > 1:
            if info.get("animated") and feats["animation_write"]:
                frame_options.append({"id": "keep", "label": f"Keep animation ({n} frames)"})
            elif info.get("animated") and feats["multipage_write"]:
                frame_options.append({"id": "keep", "label": f"Keep all {n} frames as pages (timing is lost)"})
            elif info.get("multipage") and feats["multipage_write"]:
                frame_options.append({"id": "keep", "label": f"Keep all {n} pages"})
            frame_options.append({"id": "first", "label": f"First {noun[:-1]} only (discards {n - 1})"})
            frame_options.append({"id": "separate", "label": f"Each {noun[:-1]} as a separate file ({n} files, ZIP)"})
            notes.append(f"keeps all {noun}" if frame_options[0]["id"] == "keep" else f"cannot store {n} {noun}: choose first or separate files")
        alpha = None
        if transparent:
            kind = _ALPHA_KIND.get(fmt["id"], "full" if feats["alpha_write"] else "none")
            if kind == "full":
                alpha = {"mode": "full", "message": "Transparency is preserved."}
            elif kind == "binary":
                alpha = {"mode": "binary", "message": "GIF has 1-bit transparency: each pixel is either fully transparent or "
                         "fully opaque. Pixels under 50% opacity become transparent; soft or semi-transparent edges are "
                         "blended onto the background colour and may look jagged. Choose 'flatten' for a solid background."}
                notes.append("1-bit transparency only")
            else:
                alpha = {"mode": "none", "message": f"{fmt['label']} cannot store transparency. Transparent areas will be "
                         "filled with the background colour you choose."}
                notes.append("transparency replaced by background colour")
        if fmt["id"] == "gif":
            notes.append("reduced to 256 colours")
        if fmt["id"] == "ico":
            notes.append("padded to square" + (", downscaled to 256×256" if max(info.get("width", 0), info.get("height", 0)) > 256 else ""))
        if fmt["id"] == "icns":
            notes.append("padded to square; macOS icon sizes up to 1024 px")
        if (info.get("bit_depth") or 8) > 8:
            notes.append(f"{info['bit_depth']}-bit per channel will be converted to 8-bit")
        from .convert.image_ops import LOSSLESS_SOURCES
        from .convert.settings import controls_for
        outputs.append({"id": fmt["id"], "label": fmt["label"], "extension": fmt["extensions"][0],
                        "priority": fmt["priority"], "lossy": feats["lossy"], "lossless": feats["lossless"],
                        "notes": notes, "frame_options": frame_options, "transparency": alpha,
                        "settings": controls_for(fmt["id"], source_fmt in LOSSLESS_SOURCES),
                        "metadata_support": {"exif": fmt["id"] in ("jpeg", "png", "webp", "avif", "tiff", "heic"),
                                             "icc": fmt["id"] in ("jpeg", "png", "webp", "avif", "tiff", "heic"),
                                             "xmp": fmt["id"] in ("jpeg", "png", "webp", "avif")}})
    return outputs


def inspect_image(path: Path, sniff: Sniff, caps: dict, limits: Limits) -> dict:
    fmt_id = sniff.format_id
    meta = img_caps.BY_ID.get(fmt_id)
    row = next((f for f in caps["images"]["formats"] if f["id"] == fmt_id), None)
    label = meta.label if meta else fmt_id.upper()
    if not row or not (row.get("read") and row["read"].get("verified")):
        status = (row or {}).get("status_label") or "reading this format is not verified on this server"
        raise InspectError("unsupported", f"{label}: {status}.", 415, detected=label)
    backend = row["read"]["backend"]
    if backend.startswith("pillow"):
        info = _inspect_pillow(path, limits)
    elif backend == "ffmpeg-librsvg":
        info = _inspect_svg(path, limits)
    elif backend.startswith("imagemagick"):
        coder = RAW_CODER.get(sniff.detail, "DNG") if fmt_id == "raw" else fmt_id.upper()
        info = _inspect_imagemagick(path, coder, limits)
        if fmt_id == "raw":
            info["raw_type"] = (sniff.detail or "raw").upper()
    else:  # pragma: no cover
        raise InspectError("unsupported", f"No decoder for {label}.", 415)
    # Pillow may identify a better-specific format (e.g. MPO is a JPEG variant).
    return {"kind": "image", "format": {"id": fmt_id, "label": label, "detail": sniff.detail or None},
            "image": info, "outputs": _image_outputs(caps, info, fmt_id)}


# ======================================================================= video

def _fps(rate: str | None) -> float | None:
    try:
        f = Fraction(rate) if rate and rate != "0/0" else None
        return round(float(f), 3) if f else None
    except (ValueError, ZeroDivisionError):
        return None


def _rotation(stream: dict) -> int | None:
    for sd in stream.get("side_data_list", []) or []:
        if "rotation" in sd:
            try:
                return int(sd["rotation"])
            except (TypeError, ValueError):
                return None
    rot = (stream.get("tags") or {}).get("rotate")
    return int(rot) if rot and rot.lstrip("-").isdigit() else None


_NOT_VIDEO_FORMATS = {"tty", "image2", "png_pipe", "jpeg_pipe", "svg_pipe", "webp_pipe", "gif", "bmp_pipe", "tiff_pipe"}


def inspect_video(path: Path, sniff: Sniff, caps: dict, limits: Limits) -> dict:
    try:
        res = proc.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                       timeout=limits.probe_timeout_s)
    except proc.ProcessTimeout as exc:
        raise InspectError("timeout", "Analysing the video took too long.", 422) from exc
    if not res.ok:
        msg = re.sub(r"\[[^\]]*@ 0x[0-9a-f]+\]\s*", "", res.err_text(200)).replace(str(path) + ": ", "").replace(str(path), "file")
        raise InspectError("damaged", f"The video could not be read; it may be damaged or incomplete. ({msg})", 422)
    data = json.loads(res.stdout or b"{}")
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    if fmt.get("format_name") in _NOT_VIDEO_FORMATS:
        raise InspectError("unsupported", "This file is not a supported video.", 415)
    out_streams = []
    encrypted = False
    for s in streams:
        tag = (s.get("codec_tag_string") or "").lower()
        if tag in ("encv", "enca", "drms", "drmi") or "encryption" in json.dumps(s.get("side_data_list", [])).lower():
            encrypted = True
        disp = s.get("disposition") or {}
        entry = {"index": s.get("index"), "type": s.get("codec_type"), "codec": s.get("codec_name"),
                 "codec_label": s.get("codec_long_name"), "profile": s.get("profile"),
                 "language": (s.get("tags") or {}).get("language"), "default": bool(disp.get("default")),
                 "bit_rate": int(s["bit_rate"]) if str(s.get("bit_rate", "")).isdigit() else None}
        if s.get("codec_type") == "video":
            entry.update({"width": s.get("width"), "height": s.get("height"), "fps": _fps(s.get("avg_frame_rate")) or _fps(s.get("r_frame_rate")),
                          "pix_fmt": s.get("pix_fmt"), "rotation": _rotation(s), "attached_pic": bool(disp.get("attached_pic")),
                          "color_space": s.get("color_space"), "color_transfer": s.get("color_transfer")})
        elif s.get("codec_type") == "audio":
            entry.update({"channels": s.get("channels"), "channel_layout": s.get("channel_layout"),
                          "sample_rate": int(s["sample_rate"]) if str(s.get("sample_rate", "")).isdigit() else None})
        out_streams.append(entry)
    if encrypted:
        raise InspectError("encrypted", "This video is DRM-protected/encrypted and cannot be converted.", 422)
    video_streams = [s for s in out_streams if s["type"] == "video" and not s.get("attached_pic")]
    if not video_streams:
        kinds = sorted({s["type"] for s in out_streams if s["type"]})
        msg = "No video stream found" + (f" (contains: {', '.join(kinds)})" if kinds else "") + ". Audio-only files are not supported."
        raise InspectError("unsupported", msg, 415)
    v0 = video_streams[0]
    if not v0.get("codec") or v0["codec"] == "none":
        raise InspectError("unsupported", "The video codec in this file is not supported by this server's FFmpeg.", 415)
    duration = float(fmt["duration"]) if fmt.get("duration") not in (None, "N/A") else None
    violations = []
    if duration and duration > limits.max_video_seconds:
        violations.append(f"duration {duration:.0f}s exceeds the {limits.max_video_seconds}s limit")
    if (v0.get("width") or 0) > limits.max_video_width or (v0.get("height") or 0) > limits.max_video_height:
        violations.append(f"resolution {v0.get('width')}×{v0.get('height')} exceeds {limits.max_video_width}×{limits.max_video_height}")
    container_id = sniff.format_id if sniff.format_id in vid_caps.BY_ID else None
    meta = vid_caps.BY_ID.get(container_id) if container_id else None
    info = {
        "container": {"id": container_id, "label": meta.label if meta else fmt.get("format_long_name"), "ffprobe_name": fmt.get("format_name")},
        "duration": round(duration, 3) if duration else None,
        "bit_rate": int(fmt["bit_rate"]) if str(fmt.get("bit_rate", "")).isdigit() else None,
        "streams": out_streams,
        "counts": {t: sum(1 for s in out_streams if s["type"] == t) for t in ("video", "audio", "subtitle", "data")},
        "has_audio": any(s["type"] == "audio" for s in out_streams),
        "width": v0.get("width"), "height": v0.get("height"), "fps": v0.get("fps"), "rotation": v0.get("rotation"),
    }
    # Full probe: displayed size, HDR, bit depth, VFR, chapters, attachments (used by the converter too).
    from .video.plan import Choice, PlanError, build_plan
    from .video.probe import probe as vprobe
    try:
        full = vprobe(path, container_id, timeout=limits.probe_timeout_s)
    except ValueError as exc:
        raise InspectError("damaged", f"The video could not be read; it may be damaged or incomplete. ({exc})", 422) from exc
    pv = full["primary_video"] or {}
    if (pv.get("display_width") or 0) > max(limits.max_video_width, limits.max_video_height) or \
            (pv.get("display_height") or 0) > max(limits.max_video_width, limits.max_video_height):
        pass  # coded-size check above already applies the limit
    info.update({
        "display_width": pv.get("display_width"), "display_height": pv.get("display_height"), "sar": pv.get("sar"),
        "codec": pv.get("codec"), "pix_fmt": pv.get("pix_fmt"), "bit_depth": pv.get("bit_depth"), "hdr": pv.get("hdr"),
        "color_transfer": pv.get("color_transfer"), "color_primaries": pv.get("color_primaries"),
        "vfr": (full.get("vfr") or {}).get("vfr", False), "chapters": full["chapters"],
        "attachments": full["counts"]["attachment"], "size": full.get("size"), "probe": full,
        "audio_codecs": [x["codec"] for x in full["streams"] if x["type"] == "audio"],
    })
    if violations:
        info["limit_violations"] = violations
    outputs = []
    for c in caps["video"]["containers"]:
        w = c["write"]
        if not w["verified"]:
            continue
        try:
            preview = build_plan(full, c, Choice(c["id"], hdr="preserve" if pv.get("hdr") else None))
            mode, reason, drops = preview.mode, preview.video["reason"], len(preview.dropped)
        except PlanError as exc:
            mode, reason, drops = "encode", exc.message, 0
        notes = ["remux: no re-encoding" if mode == "remux" else "re-encode"]
        if drops:
            notes.append(f"{drops} item(s) cannot be kept")
        outputs.append({"id": c["id"], "label": c["label"], "extension": c["extensions"][0], "priority": c["priority"],
                        "video_codecs": [{"id": v["id"], "label": v["label"]} for v in w["video_codecs"]],
                        "audio_codecs": [{"id": a["id"], "label": a["label"]} for a in w["audio_codecs"]] if info["has_audio"] else [],
                        "default_video": w["default_video"], "default_audio": w["default_audio"] if info["has_audio"] else None,
                        "remux_possible": mode == "remux", "video_reason": reason, "notes": notes})
    label = meta.label if meta else (fmt.get("format_long_name") or "Video")
    return {"kind": "video", "format": {"id": container_id or fmt.get("format_name"), "label": label, "detail": sniff.detail or None},
            "video": info, "outputs": outputs}


# ======================================================================= pdf

def inspect_pdf(path: Path, caps: dict, limits: Limits) -> dict:
    import pikepdf
    try:
        pdf = pikepdf.open(path)
    except pikepdf.PasswordError as exc:
        raise InspectError("encrypted", "This PDF is password-protected. Remove the password and upload it again.", 422,
                           detected="PDF") from exc
    except pikepdf.PdfError as exc:
        raise InspectError("damaged", "The PDF is damaged and could not be repaired.", 422, detected="PDF") from exc
    with pdf:
        pages = len(pdf.pages)
        sizes = []
        for page in list(pdf.pages)[:5]:
            box = [float(x) for x in page.mediabox]
            sizes.append({"width_pt": round(box[2] - box[0], 1), "height_pt": round(box[3] - box[1], 1),
                          "rotate": int(page.obj.get("/Rotate", 0))})
        info = {"pages": pages, "version": pdf.pdf_version, "encrypted": pdf.is_encrypted,
                "owner_restrictions": pdf.is_encrypted, "first_pages": sizes}
    if pages == 0:
        raise InspectError("damaged", "The PDF has no pages.", 422, detected="PDF")
    if pages > limits.max_pdf_pages:
        info["limit_violations"] = [f"{pages} pages exceeds the {limits.max_pdf_pages}-page limit"]
    ops = [{"id": o["id"], "label": o["label"], "available": False, "milestone": o["milestone"],
            "backend_ready": o["backend_ready"]} for o in caps["pdf"]["operations"]]
    return {"kind": "pdf", "format": {"id": "pdf", "label": "PDF", "detail": f"version {info['version']}"},
            "pdf": info, "outputs": [], "pdf_operations": ops}


# ======================================================================= entry

def inspect_file(path: Path, original_name: str, caps: dict, limits: Limits) -> dict:
    size = path.stat().st_size
    if size == 0:
        raise InspectError("empty", "The file is empty.", 422)
    sniff = sniff_file(path)
    if sniff.kind == "unknown":
        # Formats without a magic number (e.g. TGA): let Pillow's content checks decide.
        try:
            Image.init()
            with Image.open(path) as im:
                fid = img_caps.PILLOW_TO_ID.get(im.format or "")
            if fid:
                sniff = Sniff("image", fid)
        except Exception:
            pass
    if sniff.kind == "unknown":
        from .docs import inspect_docx, is_docx
        if is_docx(path):
            report = inspect_docx(path)
        else:
            raise InspectError("unsupported", "Unrecognised file type. Supported: images, videos and PDFs listed on the Formats page.", 415)
    elif sniff.kind == "image":
        report = inspect_image(path, sniff, caps, limits)
    elif sniff.kind == "video":
        report = inspect_video(path, sniff, caps, limits)
    else:
        report = inspect_pdf(path, caps, limits)

    ext = _ext(original_name)
    expected: list[str] = []
    if report["kind"] == "image":
        expected = img_caps.BY_ID[report["format"]["id"]].extensions if report["format"]["id"] in img_caps.BY_ID else []
    elif report["kind"] == "video" and report["format"]["id"] in vid_caps.BY_ID:
        expected = vid_caps.BY_ID[report["format"]["id"]].extensions
        if report["format"]["id"] in ("mp4", "mov", "3gp"):
            expected = expected + [".mp4", ".m4v", ".mov", ".3gp"]  # same ISO-BMFF family
    elif report["kind"] == "pdf":
        expected = [".pdf"]
    elif report["kind"] == "document":
        expected = [".docx"]
    report.update({
        "name": original_name[:255],
        "size_bytes": size,
        "extension": ext or None,
        "extension_mismatch": bool(ext) and bool(expected) and ext not in expected,
        "expected_extensions": expected,
    })
    detail = report.get(report["kind"], {})
    report["within_limits"] = not detail.get("limit_violations")
    return report
