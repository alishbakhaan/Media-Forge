"""ffprobe wrapper: normalised description of a video file.

Reports what the user sees (displayed size after rotation and pixel aspect ratio), what is inside
(streams, chapters, attachments), and properties that change how it must be converted
(HDR transfer, bit depth, variable frame rate, rotation).
"""
from __future__ import annotations

import json
import re
from fractions import Fraction
from pathlib import Path

from .. import proc

TEXT_SUBS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}
BITMAP_SUBS = {"dvd_subtitle", "hdmv_pgs_subtitle", "dvb_subtitle", "xsub"}
HDR_TRANSFERS = {"smpte2084": "HDR10 (PQ)", "arib-std-b67": "HLG"}

# Demuxer to force for each sniffed container, so FFmpeg never guesses a different (e.g. playlist) format.
DEMUXER = {"mp4": "mov", "mov": "mov", "3gp": "mov", "mkv": "matroska", "webm": "matroska", "avi": "avi",
           "mpeg": "mpeg", "mpegts": "mpegts", "ogv": "ogg", "flv": "flv", "asf": "asf", "mxf": "mxf"}

SAFE_INPUT = ["-protocol_whitelist", "file", "-enable_drefs", "0"]


def input_args(path: Path, container: str | None) -> list[str]:
    args = ["-protocol_whitelist", "file"]
    fmt = DEMUXER.get(container or "")
    if fmt:
        args += ["-f", fmt]
        if fmt == "mov":
            args += ["-enable_drefs", "0"]
    return args + ["-i", str(path)]


def _frac(v: str | None) -> Fraction | None:
    try:
        if not v or v in ("0/0", "N/A"):
            return None
        f = Fraction(v.replace(":", "/"))
        return f if f > 0 else None
    except (ValueError, ZeroDivisionError):
        return None


def _rotation(stream: dict) -> int:
    for sd in stream.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                return int(round(float(sd["rotation"]))) % 360
            except (TypeError, ValueError):
                return 0
    rot = (stream.get("tags") or {}).get("rotate")
    try:
        return int(rot) % 360 if rot else 0
    except ValueError:
        return 0


def _bit_depth(stream: dict) -> int:
    pf = stream.get("pix_fmt") or ""
    m = re.search(r"p(9|10|12|14|16)(le|be)?$", pf)
    if m:
        return int(m.group(1))
    try:
        return int(stream.get("bits_per_raw_sample") or 8)
    except ValueError:
        return 8


def detect_vfr(path: Path, container: str | None, timeout: float, max_frames: int = 400) -> dict:
    """Look at real frame timestamps; a container's nominal rate is not enough to tell CFR from VFR."""
    res = proc.run(["ffprobe", "-v", "error", *input_args(path, container)[:-2], "-select_streams", "v:0",
                    "-show_entries", "packet=pts_time,duration_time", "-read_intervals", f"%+#{max_frames}",
                    "-of", "csv=p=0", str(path)], timeout=timeout)
    pts = []
    for line in res.stdout.decode("utf-8", "replace").splitlines():
        try:
            pts.append(float(line.split(",")[0]))
        except (ValueError, IndexError):
            continue
    pts.sort()
    deltas = [round(b - a, 4) for a, b in zip(pts, pts[1:]) if b > a]
    if len(deltas) < 10:
        return {"vfr": False, "checked_frames": len(pts)}
    deltas.sort()
    lo, hi = deltas[len(deltas) // 20], deltas[-len(deltas) // 20 - 1]
    return {"vfr": hi > lo * 1.5 + 0.002, "checked_frames": len(pts), "min_frame_s": lo, "max_frame_s": hi}


def probe(path: Path, container: str | None, timeout: float = 30, vfr_check: bool = True) -> dict:
    res = proc.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", "-show_chapters",
                    *input_args(path, container)[:-2], str(path)], timeout=timeout)
    if not res.ok:
        raise ValueError(res.err_text(300).replace(str(path), "file"))
    data = json.loads(res.stdout or b"{}")
    fmt = data.get("format", {})
    streams = []
    for s in data.get("streams", []):
        disp = s.get("disposition") or {}
        tags = s.get("tags") or {}
        e = {"index": s.get("index"), "type": s.get("codec_type"), "codec": s.get("codec_name"),
             "codec_label": s.get("codec_long_name"), "codec_tag": s.get("codec_tag_string"), "profile": s.get("profile"),
             "language": tags.get("language"), "title": tags.get("title"), "default": bool(disp.get("default")),
             "bit_rate": int(s["bit_rate"]) if str(s.get("bit_rate", "")).isdigit() else None,
             "start_time": float(s["start_time"]) if s.get("start_time") not in (None, "N/A") else None,
             "duration": float(s["duration"]) if s.get("duration") not in (None, "N/A") else None}
        if e["type"] == "video":
            w, h = s.get("width") or 0, s.get("height") or 0
            sar = _frac(s.get("sample_aspect_ratio")) or Fraction(1)
            rot = _rotation(s)
            disp_w, disp_h = round(w * sar), h
            if rot in (90, 270):
                disp_w, disp_h = disp_h, disp_w
            transfer = s.get("color_transfer")
            e.update({
                "width": w, "height": h, "sar": f"{sar.numerator}:{sar.denominator}", "rotation": rot,
                "display_width": disp_w, "display_height": disp_h,
                "fps": round(float(_frac(s.get("avg_frame_rate")) or _frac(s.get("r_frame_rate")) or 0), 3) or None,
                "r_frame_rate": s.get("r_frame_rate"), "avg_frame_rate": s.get("avg_frame_rate"),
                "pix_fmt": s.get("pix_fmt"), "bit_depth": _bit_depth(s), "level": s.get("level"),
                "color_transfer": transfer, "color_primaries": s.get("color_primaries"), "color_space": s.get("color_space"),
                "color_range": s.get("color_range"),
                "hdr": HDR_TRANSFERS.get(transfer or ""),
                "attached_pic": bool(disp.get("attached_pic")),
            })
        elif e["type"] == "audio":
            e.update({"channels": s.get("channels"), "channel_layout": s.get("channel_layout"),
                      "sample_rate": int(s["sample_rate"]) if str(s.get("sample_rate", "")).isdigit() else None})
        elif e["type"] == "subtitle":
            c = e["codec"] or ""
            e["subtitle_kind"] = "text" if c in TEXT_SUBS else ("bitmap" if c in BITMAP_SUBS else "other")
        elif e["type"] == "attachment":
            e["filename"] = tags.get("filename")
            e["mimetype"] = tags.get("mimetype")
        streams.append(e)
    duration = float(fmt["duration"]) if fmt.get("duration") not in (None, "N/A") else None
    video = [s for s in streams if s["type"] == "video" and not s.get("attached_pic")]
    info = {
        "format_name": fmt.get("format_name"), "format_label": fmt.get("format_long_name"),
        "duration": duration, "size": int(fmt["size"]) if str(fmt.get("size", "")).isdigit() else None,
        "bit_rate": int(fmt["bit_rate"]) if str(fmt.get("bit_rate", "")).isdigit() else None,
        "streams": streams, "chapters": len(data.get("chapters") or []),
        "format_tags": {k: str(v)[:200] for k, v in (fmt.get("tags") or {}).items()},
        "counts": {t: sum(1 for s in streams if s["type"] == t) for t in ("video", "audio", "subtitle", "attachment", "data")},
        "primary_video": video[0] if video else None,
    }
    if video and vfr_check:
        info["vfr"] = detect_vfr(path, container, timeout)
    return info
