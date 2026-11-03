"""Animated GIF ↔ video, run through the same job queue as other video conversions.

* video → GIF: a trimmed part of the video, at a chosen width and frame rate, with a per-clip 256-colour palette.
  GIF has no sound, subtitles or chapters; the plan says so.
* animated GIF → MP4: every frame keeps its own display time; transparent areas are filled with a chosen colour
  (MP4 has no transparency). H.264 4:2:0 needs even sizes, so odd sizes are padded by one pixel with that colour.
"""
from __future__ import annotations

import re
from pathlib import Path

from PIL import Image

from .. import proc
from .plan import TONEMAP, PlanError
from .probe import input_args, probe

GIF_WIDTHS = (240, 320, 480, 640, 800)
GIF_FPS = (5, 8, 10, 12, 15, 20, 25)
MAX_GIF_SECONDS = 30.0
DEFAULT_GIF_SECONDS = 10.0
MAX_GIF_FRAMES = 3000
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
PALETTE = "split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle"


def _float(raw: dict, key: str, default: float | None) -> float | None:
    val = raw.get(key)
    if val in (None, ""):
        return default
    try:
        return float(val)
    except (TypeError, ValueError) as exc:
        raise PlanError("bad_request", f"{key} must be a number.") from exc


# ------------------------------------------------------------------ video → GIF
def plan_to_gif(info: dict, raw: dict, caps_video: dict) -> dict:
    if not (caps_video.get("gif") or {}).get("to_gif", {}).get("verified"):
        raise PlanError("unsupported_output", "Video to GIF did not pass its test on this server, so it is not offered.")
    v = info.get("primary_video")
    if not v:
        raise PlanError("unsupported", "This file has no video stream.")
    duration = info.get("duration") or 0.0
    start = max(0.0, _float(raw, "start", 0.0))
    end = _float(raw, "end", None)
    if end is None:
        end = duration if duration <= 15 else min(duration, start + DEFAULT_GIF_SECONDS)
    end = min(end, duration) if duration else end
    if end - start < 0.1:
        raise PlanError("bad_request", "Choose an end time after the start time.")
    if end - start > MAX_GIF_SECONDS + 0.01:
        raise PlanError("bad_request", f"A GIF can be at most {MAX_GIF_SECONDS:.0f} seconds long here. Shorten the part you picked.")
    dw, dh = v["display_width"], v["display_height"]
    width = int(_float(raw, "width", min(480, dw)))
    width = max(16, min(width, dw))  # never enlarged
    height = max(1, round(width * dh / dw))
    src_fps = v.get("fps") or 25
    fps = int(_float(raw, "fps", 12))
    if fps not in GIF_FPS:
        raise PlanError("bad_request", f"Frame rate must be one of {', '.join(map(str, GIF_FPS))}.")
    fps = min(fps, max(1, round(src_fps)))
    pre = ""
    notes = ["GIF is a silent picture format: no sound" +
             (", subtitles" if info["counts"].get("subtitle") else "") + (" or chapters" if info.get("chapters") else "") + "."]
    if v.get("hdr"):
        if not caps_video.get("hdr", {}).get("tonemap"):
            raise PlanError("hdr_unsupported", "This HDR video cannot be converted to GIF here: HDR→SDR conversion did not pass its test.")
        pre = TONEMAP + ","
        notes.append(f"{v['hdr']} converted to standard colours.")
    notes.append(f"{width} × {height}, {fps} frames per second, up to 256 colours per frame.")
    spec = {"kind": "to_gif", "start": round(start, 3), "end": round(end, 3), "duration": round(end - start, 3),
            "width": width, "height": height, "fps": fps, "filters": f"{pre}fps={fps},scale={width}:{height}:flags=lanczos,setsar=1,{PALETTE}"}
    public = {"container": "gif", "mode": "encode", "video": {"action": "encode", "codec": "gif", "reason": "frames redrawn as GIF"},
              "audio": [], "subtitles": [], "chapters": {}, "attachments": [], "dropped": [], "notes": notes, "hdr": None,
              "metadata": {"summary": "GIF stores no file information"}, "expected": {"duration": spec["duration"]},
              "gif": {k: spec[k] for k in ("start", "end", "duration", "width", "height", "fps")},
              "limits": {"max_seconds": MAX_GIF_SECONDS, "widths": [w for w in GIF_WIDTHS if w < dw] + [dw], "fps": list(GIF_FPS)}}
    return {"public": public, "spec": spec}


def to_gif_args(spec: dict, src: Path, container: str | None, out: Path) -> list[str]:
    inp = input_args(src, container)
    return ["ffmpeg", "-nostdin", "-hide_banner", "-y", "-loglevel", "error", "-progress", "pipe:1", "-nostats",
            *inp[:-2], "-ss", str(spec["start"]), "-t", str(spec["duration"]), *inp[-2:],
            "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", spec["filters"], "-loop", "0", "-f", "gif", str(out)]


def verify_gif(out: Path, spec: dict, timeout: float) -> dict:
    checks: dict[str, bool] = {}
    try:
        with Image.open(out) as im:
            checks["container"] = im.format == "GIF"
            checks["size"] = im.size == (spec["width"], spec["height"])
            frames, total = getattr(im, "n_frames", 1), 0
            for i in range(frames):
                im.seek(i)
                im.load()
                total += im.info.get("duration", 0)
            checks["loops"] = im.info.get("loop") == 0
    except Exception:  # noqa: BLE001 - anything unreadable is a failed check
        checks["container"] = False
        frames, total = 0, 0
    checks["duration"] = abs(total / 1000 - spec["duration"]) <= max(0.25, 2 / spec["fps"])
    dec = proc.run(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(out), "-f", "null", "-"], timeout=timeout)
    checks["decodes"] = dec.ok
    failed = [k for k, ok in checks.items() if not ok]
    if failed:
        raise PlanError("verify_failed", "Output check failed: " + ", ".join(failed))
    return {"checks": checks, "details": {"width": spec["width"], "height": spec["height"], "frames": frames,
                                          "duration": round(total / 1000, 3), "fps": spec["fps"]}}


# ------------------------------------------------------------------ animated GIF → MP4
def gif_facts(path: Path) -> dict:
    """Frame count, display time as browsers show it (delays under 20 ms count as 100 ms) and transparency."""
    with Image.open(path) as im:
        if im.format != "GIF":
            raise PlanError("bad_request", "This is not a GIF.")
        frames = getattr(im, "n_frames", 1)
        if frames < 2:
            raise PlanError("not_animated", "This GIF has only one frame. Convert it on the Convert Images tab instead.")
        if frames > MAX_GIF_FRAMES:
            raise PlanError("limit_exceeded", f"This GIF has {frames} frames; the limit here is {MAX_GIF_FRAMES}.")
        total, transparent = 0, False
        for i in range(frames):
            im.seek(i)
            d = im.info.get("duration", 0) or 0
            total += d if d >= 20 else 100
            if not transparent and ("transparency" in im.info or im.mode in ("RGBA", "LA", "PA")):
                transparent = im.convert("RGBA").getchannel("A").getextrema()[0] < 255
        return {"frames": frames, "duration": round(total / 1000, 3), "width": im.width, "height": im.height,
                "transparent": transparent, "loop": im.info.get("loop")}


def plan_from_gif(path: Path, raw: dict, caps_video: dict) -> dict:
    if not (caps_video.get("gif") or {}).get("from_gif", {}).get("verified"):
        raise PlanError("unsupported_output", "GIF to video did not pass its test on this server, so it is not offered.")
    if str(raw.get("container") or "mp4").lower() != "mp4":
        raise PlanError("unsupported_output", "An animated GIF can be converted to MP4.")
    bg = raw.get("background") or "#ffffff"
    if not HEX.match(bg):
        raise PlanError("bad_request", "background must be a colour like #ffffff.")
    facts = gif_facts(path)
    w, h = facts["width"] + facts["width"] % 2, facts["height"] + facts["height"] % 2
    notes = [f"Timing kept: {facts['frames']} frames over {facts['duration']:.2f} s.", "No sound (GIFs have none)."]
    if facts["transparent"]:
        notes.append(f"Transparent areas filled with {bg.lower()} — MP4 cannot store transparency.")
    if (w, h) != (facts["width"], facts["height"]):
        notes.append(f"Padded to {w} × {h} (MP4 needs even sizes).")
    notes.append("MP4 plays once; most players can repeat it.")
    color = "0x" + bg[1:]
    spec = {"kind": "from_gif", "background": bg.lower(), "width": w, "height": h, **{k: facts[k] for k in ("frames", "duration", "transparent")},
            "filters": (f"format=rgba,split[a][b];[a]drawbox=c={color}@1:t=fill:replace=1[bg];[bg][b]overlay=format=auto,"
                        f"pad=ceil(iw/2)*2:ceil(ih/2)*2:color={color},format=yuv420p")}
    public = {"container": "mp4", "mode": "encode", "video": {"action": "encode", "codec": "h264", "reason": "GIF frames encoded as H.264"},
              "audio": [], "subtitles": [], "chapters": {}, "attachments": [], "dropped": [], "notes": notes, "hdr": None,
              "metadata": {"summary": "no file information to copy"}, "expected": {"duration": facts["duration"]},
              "gif_in": {k: spec[k] for k in ("frames", "duration", "transparent", "width", "height", "background")}}
    return {"public": public, "spec": spec}


def from_gif_args(spec: dict, src: Path, out: Path) -> list[str]:
    return ["ffmpeg", "-nostdin", "-hide_banner", "-y", "-loglevel", "error", "-progress", "pipe:1", "-nostats",
            "-protocol_whitelist", "file", "-f", "gif", "-i", str(src), "-vf", spec["filters"], "-fps_mode", "passthrough",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-an", "-movflags", "+faststart", "-f", "mp4", str(out)]


def verify_from_gif(out: Path, spec: dict, timeout: float) -> tuple[dict, dict]:
    info = probe(out, "mp4", timeout=timeout, vfr_check=False)
    v = info.get("primary_video") or {}
    pk = proc.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries", "stream=nb_read_packets",
                   "-of", "csv=p=0", str(out)], timeout=timeout)
    try:
        frames = int(pk.stdout.decode().strip() or 0)
    except ValueError:
        frames = 0
    # The last frame's display time is not always stored in MP4, so allow up to one frame of difference.
    longest = max(0.1, spec["duration"] / max(1, spec["frames"]) * 2)
    checks = {"video_stream": v.get("codec") == "h264", "size": (v.get("width"), v.get("height")) == (spec["width"], spec["height"]),
              "frames": frames == spec["frames"], "no_audio": info["counts"]["audio"] == 0,
              "duration": abs((info.get("duration") or 0) - spec["duration"]) <= longest}
    dec = proc.run(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(out), "-f", "null", "-"], timeout=timeout)
    checks["decodes"] = dec.ok
    failed = [k for k, ok in checks.items() if not ok]
    if failed:
        raise PlanError("verify_failed", "Output check failed: " + ", ".join(failed))
    details = {"codec": "h264", "display_width": v.get("width"), "display_height": v.get("height"), "bit_depth": 8,
               "duration": info.get("duration"), "frames": frames, "audio": [], "subtitles": [], "chapters": 0}
    return {"checks": checks, "details": details}, info


# ------------------------------------------------------------------ capability self-test
def selftest(work: Path, timeout: float = 60) -> dict:
    """Both directions on tiny real encodes; a direction is offered only if its output passes the same checks."""
    result = {"to_gif": {"verified": False, "detail": ""}, "from_gif": {"verified": False, "detail": ""}}
    clip = work / "gif-src.mp4"
    ok = proc.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=25:duration=1",
                   "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)], timeout=timeout).ok
    try:
        if not ok:
            raise PlanError("selftest", "could not make a test clip")
        spec = {"start": 0.0, "duration": 1.0, "width": 120, "height": 68, "fps": 10,
                "filters": f"fps=10,scale=120:68:flags=lanczos,setsar=1,{PALETTE}"}
        out = work / "gif-out.gif"
        res = proc.run([a for a in to_gif_args(spec, clip, "mp4", out) if a not in ("-progress", "pipe:1")], timeout=timeout)
        if not res.ok:
            raise PlanError("selftest", res.err_text(200))
        verify_gif(out, spec, timeout)
        result["to_gif"] = {"verified": True, "detail": "1 s clip → 10 frames, size and timing checked"}
    except PlanError as exc:
        result["to_gif"]["detail"] = exc.message
    try:
        frames = []
        for i in range(3):
            im = Image.new("RGBA", (31, 21), (0, 0, 0, 0))
            im.paste((40 * i, 120, 200, 255), (2 + i * 5, 2, 14 + i * 5, 18))
            frames.append(im)
        src = work / "gif-in.gif"
        frames[0].save(src, save_all=True, append_images=frames[1:], duration=[40, 80, 120], loop=0, disposal=2)
        planned = plan_from_gif(src, {"background": "#ff0000"}, {"gif": {"from_gif": {"verified": True}}})
        out = work / "gif-in.mp4"
        res = proc.run([a for a in from_gif_args(planned["spec"], src, out) if a not in ("-progress", "pipe:1")], timeout=timeout)
        if not res.ok:
            raise PlanError("selftest", res.err_text(200))
        verify_from_gif(out, planned["spec"], timeout)
        result["from_gif"] = {"verified": True, "detail": "3-frame transparent GIF with 40/80/120 ms frames → MP4, frames and timing checked"}
    except PlanError as exc:
        result["from_gif"]["detail"] = exc.message
    except ValueError as exc:
        result["from_gif"]["detail"] = str(exc)
    return result
