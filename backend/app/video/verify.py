"""Check a finished video before it is offered: container, streams, displayed size, duration,
A/V offset, HDR tags, and a full decode."""
from __future__ import annotations

from pathlib import Path

from .. import proc
from ..sniff import sniff_file
from .probe import probe

SNIFF_OK = {"mp4": {"mp4"}, "mov": {"mov", "mp4"}, "3gp": {"3gp", "mp4"}, "mkv": {"mkv"}, "webm": {"webm"}, "avi": {"avi"},
            "mpeg": {"mpeg"}, "mpegts": {"mpegts"}, "ogv": {"ogv"}, "flv": {"flv"}}
SUB_NAMES = {"srt": "subrip"}


class VerifyError(Exception):
    def __init__(self, message: str, checks: dict):
        super().__init__(message)
        self.message, self.checks = message, checks


def _offset(info: dict) -> float | None:
    v = info.get("primary_video")
    a = next((s for s in info["streams"] if s["type"] == "audio"), None)
    if not v or not a or v.get("start_time") is None or a.get("start_time") is None:
        return None
    return a["start_time"] - v["start_time"]


def verify(out: Path, container: str, plan: dict, src_info: dict, timeout: float) -> dict:
    checks: dict[str, bool] = {}
    details: dict = {}
    s = sniff_file(out)
    checks["container"] = s.format_id in SNIFF_OK.get(container, {container})
    try:
        info = probe(out, s.format_id if s.format_id != "unknown" else None, timeout=timeout, vfr_check=False)
    except ValueError as exc:
        raise VerifyError(f"ffprobe could not read the output: {exc}", {"probe": False}) from exc
    v = info.get("primary_video")
    exp = plan["expected"]
    checks["video_stream"] = bool(v) and v["codec"] == exp["video_codec"]
    if v:
        ew, eh = exp["display_width"], exp["display_height"]
        w, h = v["display_width"], v["display_height"]
        aspect_ok = abs((w / h) - (ew / eh)) <= 0.01 * (ew / eh) + 2 / eh
        checks["display_aspect"] = aspect_ok
        checks["no_upscale"] = w <= ew + 1 and h <= eh + 1
        if exp.get("rotation_baked"):
            checks["rotation_applied"] = v.get("rotation", 0) == 0
        details.update(display_width=w, display_height=h, codec=v["codec"], pix_fmt=v.get("pix_fmt"),
                       bit_depth=v.get("bit_depth"), fps=v.get("fps"), rotation=v.get("rotation"),
                       color_transfer=v.get("color_transfer"))
        if exp.get("hdr_transfer"):
            checks["hdr_preserved"] = v.get("color_transfer") == exp["hdr_transfer"] and (v.get("bit_depth") or 8) >= 10
        if plan.get("hdr") and plan["hdr"]["action"] == "tonemap":
            checks["sdr_output"] = v.get("color_transfer") in (None, "bt709", "unknown") and (v.get("bit_depth") or 8) == 8
    audio = [x for x in info["streams"] if x["type"] == "audio"]
    checks["audio_streams"] = [a["codec"] for a in audio] == exp["audio"]
    subs = [x for x in info["streams"] if x["type"] == "subtitle"]
    checks["subtitle_streams"] = len(subs) == exp["subtitles"]
    if exp.get("chapters"):
        checks["chapters"] = info["chapters"] > 0
    src_dur, out_dur = src_info.get("duration"), info.get("duration")
    if src_dur and out_dur:
        tol = max(0.3, 0.02 * src_dur)
        checks["duration"] = abs(out_dur - src_dur) <= tol
        details["duration"] = out_dur
    off_in, off_out = _offset(src_info), _offset(info)
    if off_in is not None and off_out is not None:
        details["av_offset_change_ms"] = round((off_out - off_in) * 1000, 1)
        # Container start-time bookkeeping (encoder delay, B-frames) legitimately moves this by tens of ms;
        # content-based sync is measured in the test-suite. This only catches gross desync.
        checks["av_start_offset"] = abs(off_out - off_in) <= 0.25
    if exp.get("no_location_tags"):
        from .plan import LOCATION_TAG
        checks["location_removed"] = not any(LOCATION_TAG.search(k) for k in info.get("format_tags", {}))
    dec = proc.run(["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-xerror", "-i", str(out), "-map", "0:v:0",
                    "-map", "0:a?", "-f", "null", "-"], timeout=timeout)
    checks["decodes"] = dec.ok
    details.update(size_bytes=out.stat().st_size, container=s.format_id, audio=[a["codec"] for a in audio],
                   subtitles=[x["codec"] for x in subs], chapters=info["chapters"], streams=info["streams"])
    failed = [k for k, ok in checks.items() if not ok]
    if failed:
        raise VerifyError("Output check failed: " + ", ".join(failed) +
                          (f" ({dec.err_text(200)})" if "decodes" in failed else ""), checks)
    return {"checks": checks, "details": details, "info": info}


def browser_codecs(info: dict) -> str:
    """RFC 6381 codec string used by the browser's canPlayType() to decide whether to show a player."""
    out = []
    for s in info["streams"]:
        c = s.get("codec")
        if s["type"] == "video" and not s.get("attached_pic"):
            if c == "h264":
                prof = {"Baseline": "42", "Constrained Baseline": "42", "Main": "4D", "High": "64"}.get(s.get("profile") or "", "64")
                out.append(f"avc1.{prof}00{int(s.get('level') or 40):02X}")
            elif c == "hevc":
                out.append("hvc1.1.6.L93.B0")
            elif c == "av1":
                out.append(f"av01.0.08M.{10 if (s.get('bit_depth') or 8) > 8 else 8:02d}")
            elif c == "vp9":
                out.append(f"vp09.{'02' if (s.get('bit_depth') or 8) > 8 else '00'}.10.{10 if (s.get('bit_depth') or 8) > 8 else 8:02d}")
            elif c in ("vp8", "theora"):
                out.append(c)
            else:
                out.append(c or "unknown")
        elif s["type"] == "audio":
            out.append({"aac": "mp4a.40.2", "mp3": "mp3", "opus": "opus", "vorbis": "vorbis", "flac": "flac", "ac3": "ac-3",
                        "alac": "alac"}.get(c, c or "unknown"))
    return ", ".join(dict.fromkeys(out))


def poster(out: Path, dest: Path, duration: float | None, timeout: float) -> bool:
    t = min(1.0, (duration or 0) / 2)
    res = proc.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(out), "-frames:v", "1",
                    "-vf", "scale='min(640,iw)':-2", str(dest)], timeout=timeout)
    return res.ok and dest.exists()
