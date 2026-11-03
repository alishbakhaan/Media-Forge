"""Verification against real files (samples/real) and real content re-encoded by another
encoder (samples/derived). Input and output support are verified separately:

* input  — every sample is fully decoded by the backend that would process it;
* output — real content (a photo, a transparent logo, an animated GIF, a real video clip)
           is encoded into every candidate output format and decoded back.

Advertising policy (``apply_policy``): a format is advertised for reading only if its
self-test passed AND at least one real or derived sample decoded. HEIC/HEIF and camera
RAW additionally require a *real* sample (synthetic or re-encoded files do not count).
A format is advertised for writing only if its self-test passed AND real content
round-tripped through it.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

from PIL import Image

from .. import proc
from ..sniff import sniff_bytes, sniff_file

REAL_ONLY = {"heic", "heif", "raw"}
SKIP_DIRS = {"audio-source"}
RAW_CODER = {"dng": "DNG", "cr2": "CR2", "cr3": "CR3", "nef": "NEF", "arw": "ARW", "orf": "ORF", "rw2": "RW2", "raf": "RAF", "pef": "PEF"}


def collect(samples_dir: Path) -> list[dict]:
    files = []
    for tier in ("real", "derived"):
        base = samples_dir / tier
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.name.startswith(".") or path.suffix.lower() in (".md", ".txt"):
                continue
            if any(part in SKIP_DIRS for part in path.relative_to(base).parts):
                continue
            s = sniff_file(path)
            if s.kind == "unknown":
                try:
                    Image.init()
                    with Image.open(path) as im:
                        from .images import PILLOW_TO_ID
                        fid = PILLOW_TO_ID.get(im.format or "")
                    if fid:
                        s = type(s)("image", fid)
                except Exception:
                    pass
            files.append({"path": path, "rel": str(path.relative_to(samples_dir)), "tier": tier,
                          "kind": s.kind, "format": s.format_id, "detail": s.detail})
    return files


# ------------------------------------------------------------------ input checks

def _decode_image(path: Path, fmt: str, detail: str, backend: str, work: Path, timeout: float) -> tuple[bool, str]:
    if backend.startswith("pillow"):
        try:
            with Image.open(path) as im:
                frames = getattr(im, "n_frames", 1) or 1
                for i in range(min(frames, 64)):
                    im.seek(i)
                    im.load()
                return True, f"{im.width}x{im.height} {im.mode}, {frames} frame(s)"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"[:200]
    out = work / (path.stem + "-decoded.png")
    if backend == "ffmpeg-librsvg":
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "svg_pipe", "-i", str(path), "-frames:v", "1", str(out)]
    elif backend.startswith("imagemagick"):
        coder = RAW_CODER.get(detail, "DNG") if fmt == "raw" else fmt.upper()
        im = proc.imagemagick("convert")
        if not im:
            return False, "ImageMagick not installed"
        cmd = [*im, f"{coder}:{path}", "-strip", f"png:{out}"]
    else:
        return False, f"no decoder ({backend})"
    try:
        res = proc.run(cmd, timeout=timeout)
    except proc.ProcessTimeout as exc:
        return False, str(exc)
    if not res.ok or not out.exists():
        return False, res.err_text(200).replace(str(path.parent) + "/", "")
    with Image.open(out) as im:
        im.load()
        return True, f"{im.width}x{im.height} {im.mode}"


def _decode_video(path: Path, timeout: float) -> tuple[bool, str]:
    res = proc.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)], timeout=timeout)
    if not res.ok:
        return False, res.err_text(200)
    info = json.loads(res.stdout)
    streams = info.get("streams", [])
    v = [s for s in streams if s.get("codec_type") == "video"]
    if not v:
        return False, "no video stream"
    dec = proc.run(["ffmpeg", "-nostdin", "-v", "error", "-t", "3", "-i", str(path), "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-"], timeout=timeout)
    if not dec.ok:
        return False, "decode failed: " + dec.err_text(160)
    kinds = [s.get("codec_type") for s in streams]
    return True, f"{v[0].get('codec_name')} {v[0].get('width')}x{v[0].get('height')}, streams: {', '.join(kinds)}"


def _decode_pdf(path: Path) -> tuple[bool, str]:
    try:
        import pikepdf
        import pypdfium2 as pdfium
        with pikepdf.open(path) as pdf:
            n = len(pdf.pages)
        doc = pdfium.PdfDocument(str(path))
        img = doc[0].render(scale=0.5).to_pil()
        doc.close()
        return True, f"{n} pages, page 1 rendered {img.width}x{img.height}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:200]


def verify_inputs(files: list[dict], image_rows: dict, video_rows: dict, work: Path, timeout: float = 60) -> dict:
    """Returns {kind: {format_id: {"real": {...}, "derived": {...}}}}."""
    results: dict = {"image": {}, "video": {}, "pdf": {}}
    for f in files:
        kind, fmt = f["kind"], f["format"]
        if kind == "image":
            row = image_rows.get(fmt)
            backend = (row or {}).get("read", {}) or {}
            if not backend.get("verified"):
                ok, msg = False, "no working decoder (self-test failed)"
            else:
                ok, msg = _decode_image(f["path"], fmt, f["detail"], backend["backend"], work, timeout)
        elif kind == "video":
            ok, msg = _decode_video(f["path"], timeout)
        elif kind == "pdf":
            ok, msg = _decode_pdf(f["path"])
        else:
            continue
        bucket = results[kind].setdefault(fmt, {}).setdefault(f["tier"], {"passed": 0, "failed": 0, "files": [], "errors": [], "subtypes": []})
        bucket["passed" if ok else "failed"] += 1
        bucket["files"].append(f["rel"])
        if not ok:
            bucket["errors"].append(f"{f['rel']}: {msg}")
        if f["detail"] and ok and f["detail"] not in bucket["subtypes"]:
            bucket["subtypes"].append(f["detail"])
    return results


# ------------------------------------------------------------------ output checks

def _pick(files: list[dict], fmt: str, prefer: str | None = None) -> Path | None:
    cands = [f for f in files if f["kind"] == "image" and f["format"] == fmt and f["tier"] == "real"]
    if prefer:
        named = [f for f in cands if prefer in f["path"].name]
        cands = named or cands
    return cands[0]["path"] if cands else None


def _pick_transparent(files: list[dict]) -> Path | None:
    """A real PNG whose alpha channel actually varies (an all-opaque alpha proves nothing)."""
    for f in files:
        if f["kind"] == "image" and f["format"] == "png" and f["tier"] == "real":
            try:
                with Image.open(f["path"]) as im:
                    a = im.convert("RGBA").getchannel("A")
                    lo, hi = a.getextrema()
                    if lo < 250 and im.width >= 16:
                        return f["path"]
            except Exception:
                continue
    return None


FFMPEG_IMAGE_DECODE = {"jpeg", "png", "webp", "gif", "bmp", "tiff", "jpeg2000", "qoi", "tga", "pnm", "pcx", "sgi", "dds", "avif"}
_BIG_LIMITS = {"max_image_megapixels": 400, "max_decoded_megapixels": 2000, "probe_timeout_s": 60}


def _independent_decode(path: Path, work: Path, timeout: float = 60) -> tuple[bool, str]:
    """Decode our output with FFmpeg (a different decoder from Pillow) and compare the pixels."""
    ref_png = work / (path.name + ".ffmpeg.png")
    res = proc.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
                    "-frames:v", "1", "-pix_fmt", "rgba", str(ref_png)], timeout=timeout)
    if not res.ok or not ref_png.exists():
        return False, "FFmpeg could not decode it: " + res.err_text(160).replace(str(path.parent) + "/", "")
    from PIL import ImageChops, ImageStat
    with Image.open(path) as ours, Image.open(ref_png) as theirs:
        a = ours.convert("RGBA")
        b = theirs.convert("RGBA")
        if a.size != b.size:
            return False, f"FFmpeg decoded {b.size}, Pillow {a.size}"
        # Compare colour only where pixels are visible.
        bg = Image.new("RGBA", a.size, (255, 255, 255, 255))
        diff = ImageChops.difference(Image.alpha_composite(bg, a).convert("RGB"), Image.alpha_composite(bg, b).convert("RGB"))
        mean = sum(ImageStat.Stat(diff).mean) / 3
    if mean > 4:
        return False, f"FFmpeg's pixels differ from Pillow's (mean difference {mean:.1f}/255)"
    return True, f"FFmpeg decodes it identically (mean difference {mean:.2f}/255)"


def verify_image_outputs(files: list[dict], formats: list[dict], work: Path) -> dict:
    """Convert real files through the real conversion pipeline into every candidate format."""
    from ..convert import image_ops as ops

    photo_p = _pick(files, "jpeg", "retina") or _pick(files, "jpeg")
    alpha_p = _pick_transparent(files)
    anim_p = _pick(files, "gif", "no_time")
    multi_p = _pick(files, "tiff", "multipage.tif")
    if not photo_p:
        return {"_error": "no real JPEG photo in samples/real to use as source"}
    results = {}
    for row in formats:
        fid = row["id"]
        if fid not in ops.TARGETS or not (row.get("write") and row["write"].get("verified")):
            continue
        feats = row["features"]
        checks, errors = [], []
        tdir = work / f"out-{fid}"
        tdir.mkdir(exist_ok=True)

        def run(src: Path, src_fmt: str, label: str, **opt) -> dict | None:
            sub = tdir / label.replace(" ", "-")
            sub.mkdir(exist_ok=True)
            try:
                from ..convert import settings as cs
                o = cs.parse({"format": fid, "metadata": "keep", **opt}, lossless_source=src_fmt in ops.LOSSLESS_SOURCES)
                dec = ops.decode(src, src_fmt, "pillow", o, _BIG_LIMITS, sub)
                rep = ops.encode(dec, o, feats, sub, "sample")
                out = rep["outputs"][0]
                v = ops.verify_output(out["path"], fid, out["expected_frames"], out["expected_duration"], out["expected_loop"])
                checks.append(f"{label}: {v['width']}×{v['height']}, {v['frames']} frame(s), {out['path'].stat().st_size // 1024 or 1} KB")
                return {"path": out["path"], "verify": v, "report": rep}
            except Exception as exc:
                errors.append(f"{label}: {getattr(exc, 'message', exc)}"[:220])
                return None

        base = run(photo_p, "jpeg", "real photo")
        independent = None
        if base:
            if fid in FFMPEG_IMAGE_DECODE:
                ok, msg = _independent_decode(base["path"], tdir)
                independent = {"decoder": "FFmpeg", "passed": ok, "detail": msg}
            else:
                independent = {"decoder": None, "passed": None, "detail": "no independent decoder available on this machine"}
        alpha_ok = anim_ok = multi_ok = None
        if alpha_p and (feats["alpha_write"] or fid == "gif"):
            r = run(alpha_p, "png", "real transparent PNG", transparency="keep")
            if r:
                with Image.open(r["path"]) as im:
                    alpha_ok = im.convert("RGBA").getchannel("A").getextrema()[0] < 128
                (checks if alpha_ok else errors).append("transparency kept" + (" (1-bit)" if fid == "gif" else "") if alpha_ok else "transparency lost")
            else:
                alpha_ok = False
        if anim_p and feats["animation_write"]:
            r = run(anim_p, "gif", "real animated GIF", frames="keep")
            anim_ok = r is not None
        if multi_p and feats["multipage_write"]:
            r = run(multi_p, "tiff", "real multipage TIFF", frames="keep")
            multi_ok = r is not None and r["verify"]["frames"] > 1
        results[fid] = {"passed": base is not None, "alpha_ok": alpha_ok, "animation_ok": anim_ok, "multipage_ok": multi_ok,
                        "independent": independent, "checks": checks, "errors": errors}
    return results


def verify_video_outputs(files: list[dict], containers: list[dict], container_meta: dict, work: Path, timeout: float = 120) -> dict:
    src = next((f["path"] for f in files if f["kind"] == "video" and f["tier"] == "real"), None)
    if src is None:
        return {"_error": "no real video in samples/real"}
    audio_dir = src.parents[1] / "audio-source"
    wavs = sorted(audio_dir.glob("*.wav")) if audio_dir.is_dir() else []
    wav = wavs[0] if wavs else None
    from .video import AUDIO_CODECS, VIDEO_CODECS, _encode_args  # local import to avoid cycles
    results = {}
    for c in containers:
        if not c["write"]["verified"]:
            continue
        meta = container_meta[c["id"]]
        vcodec, acodec = c["write"]["default_video"], c["write"]["default_audio"]
        venc = next(v["encoder"] for v in c["write"]["video_codecs"] if v["id"] == vcodec)
        aenc = next(a["encoder"] for a in c["write"]["audio_codecs"] if a["id"] == acodec)
        out = work / f"real-{c['id']}{c['extensions'][0]}"
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-t", "2", "-i", str(src)]
        if wav:
            cmd += ["-t", "2", "-i", str(wav), "-map", "0:v:0", "-map", "1:a:0"]
        cmd += ["-c:v", venc, *_encode_args(vcodec, venc)]
        if wav:
            cmd += ["-c:a", aenc, "-ac", "2"]
        cmd += [*meta.extra_args, "-f", meta.muxer, str(out)]
        try:
            res = proc.run(cmd, timeout=timeout)
            ok = res.ok and out.exists()
            msg = "" if ok else res.err_text(200)
            if ok:
                ok, msg = _decode_video(out, timeout)
        except proc.ProcessTimeout as exc:
            ok, msg = False, str(exc)
        results[c["id"]] = {"passed": ok, "checks": [f"real clip → {VIDEO_CODECS[vcodec]['label']}"
                                                     + (f" + {AUDIO_CODECS[acodec]['label']}" if wav else "") + f": {msg}"] if ok else [],
                            "errors": [] if ok else [msg]}
    return results


# ------------------------------------------------------------------ policy

def apply_policy(caps: dict, inputs: dict, img_out: dict, vid_out: dict, require_real: bool) -> None:
    """Mutates caps: attaches evidence and withdraws anything that did not pass."""
    for row in caps["images"]["formats"]:
        fid = row["id"]
        ev = inputs["image"].get(fid, {})
        real, derived = ev.get("real"), ev.get("derived")
        row["real_samples"] = _summary(real, derived)
        if row.get("read") and row["read"].get("verified") and require_real:
            ok = bool(real and real["passed"]) or (fid not in REAL_ONLY and bool(derived and derived["passed"]))
            if not ok:
                row["read_selftest"] = row["read"]
                row["read"] = None
                need = "a real file (synthetic and re-encoded files do not count)" if fid in REAL_ONLY else "a real or derived sample"
                row["notes"].append(f"Not advertised for input: needs {need} in samples/ that decodes.")
        if fid == "raw" and real and real["subtypes"]:
            row["notes"].append("Verified RAW types: " + ", ".join(t.upper() for t in real["subtypes"]))
        if real and real["failed"]:
            row["notes"].append(f"{real['failed']} real file(s) failed to decode: " + "; ".join(real["errors"][:3]))
        out = img_out.get(fid)
        row["write_evidence"] = out
        if out and out["passed"]:
            if out.get("alpha_ok") is False:
                row["features"]["alpha_write"] = False
                row["notes"].append("Transparency not preserved with real content; it will be replaced by a background colour.")
            elif out.get("alpha_ok") and fid == "gif":
                row["features"]["alpha_write"] = True
                row["features"]["alpha_binary"] = True
            if out.get("animation_ok") is False:
                row["features"]["animation_write"] = False
                row["notes"].append("Animation not preserved with real content; choose first frame or separate files.")
            if out.get("multipage_ok") is False:
                row["features"]["multipage_write"] = False
                row["notes"].append("Multiple pages not preserved with real content.")
        if row.get("write") and row["write"].get("verified") and require_real:
            if not (out and out["passed"]):
                row["write_selftest"] = row["write"]
                row["write"] = None
                row["notes"].append("Not advertised for output: real-content round trip failed: "
                                    + ("; ".join(out["errors"][:2]) if out else img_out.get("_error", "not run")))
    for row in caps["images"]["formats"]:
        readable = bool(row.get("read") and row["read"].get("verified"))
        writable = bool(row.get("write") and row["write"].get("verified"))
        if row["id"] in ("heic", "heif") and not (readable or writable):
            row["status_label"] = ("Decoding works, but a real HEIC sample is needed to enable input"
                                   if row.get("read_selftest") else "Unavailable in this environment")
        elif row["id"] == "raw" and not readable:
            row["status_label"] = "Unverified—real camera samples needed"
        elif not readable and not writable:
            row["status_label"] = "Not verified on this server"
    caps["images"]["readable"] = [r["id"] for r in caps["images"]["formats"] if r.get("read") and r["read"].get("verified")]
    caps["images"]["writable"] = [r["id"] for r in caps["images"]["formats"] if r.get("write") and r["write"].get("verified")]

    for c in caps["video"].get("containers", []):
        ev = inputs["video"].get(c["id"], {})
        real, derived = ev.get("real"), ev.get("derived")
        c["real_samples"] = _summary(real, derived)
        if c["read"]["verified"] and require_real and not ((real and real["passed"]) or (derived and derived["passed"])):
            c["read"]["verified"] = False
            c["read"]["withheld"] = "no real or derived sample"
            c["notes"] = c["notes"] + ["Not advertised for input: no real or derived sample in samples/."]
        out = vid_out.get(c["id"])
        c["write_evidence"] = out
        if c["write"]["verified"] and require_real and not (out and out["passed"]):
            c["write"]["verified"] = False
            c["write"]["video_codecs"] = []
            c["write"]["audio_codecs"] = []
            c["notes"] = c["notes"] + ["Not advertised for output: real-clip encode failed."]
    caps["video"]["readable"] = [c["id"] for c in caps["video"].get("containers", []) if c["read"]["verified"]]
    caps["video"]["writable"] = [c["id"] for c in caps["video"].get("containers", []) if c["write"]["verified"]]

    pdf_ev = inputs["pdf"].get("pdf", {})
    caps["pdf"]["real_samples"] = _summary(pdf_ev.get("real"), pdf_ev.get("derived"))


def _summary(real: dict | None, derived: dict | None) -> dict:
    real = real or {}
    derived = derived or {}
    return {
        "passed": real.get("passed", 0),
        "failed": real.get("failed", 0),
        "derived_passed": derived.get("passed", 0),
        "derived_failed": derived.get("failed", 0),
        "files": real.get("files", []),
        "derived_files": derived.get("files", []),
        "errors": real.get("errors", []) + derived.get("errors", []),
        "subtypes": real.get("subtypes", []),
    }
