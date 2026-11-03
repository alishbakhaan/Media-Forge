"""Video container / codec capabilities discovered from FFmpeg and verified by test encodes.

Containers (MP4, MKV, …) are kept separate from video codecs (H.264, VP9, …) and audio
codecs (AAC, Opus, …). A container is offered as an output only when at least one
video+audio combination was actually encoded, muxed, probed and decoded here.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .. import proc

VIDEO_CODECS: dict[str, dict] = {
    "h264": {"label": "H.264 / AVC", "encoders": ["libx264"], "args": ["-preset", "ultrafast", "-pix_fmt", "yuv420p"]},
    "hevc": {"label": "H.265 / HEVC", "encoders": ["libx265"], "args": ["-preset", "ultrafast", "-pix_fmt", "yuv420p", "-x265-params", "log-level=error"]},
    "av1": {"label": "AV1", "encoders": ["libsvtav1", "libaom-av1"], "args": {"libsvtav1": ["-preset", "12", "-pix_fmt", "yuv420p"],
                                                                              "libaom-av1": ["-cpu-used", "8", "-row-mt", "1", "-pix_fmt", "yuv420p"]}},
    "vp9": {"label": "VP9", "encoders": ["libvpx-vp9"], "args": ["-deadline", "realtime", "-cpu-used", "8", "-pix_fmt", "yuv420p"]},
    "vp8": {"label": "VP8", "encoders": ["libvpx"], "args": ["-deadline", "realtime", "-cpu-used", "8", "-pix_fmt", "yuv420p"]},
    "mpeg4": {"label": "MPEG-4 Part 2", "encoders": ["mpeg4"], "args": ["-q:v", "5", "-pix_fmt", "yuv420p"]},
    "mpeg2video": {"label": "MPEG-2", "encoders": ["mpeg2video"], "args": ["-q:v", "5", "-pix_fmt", "yuv420p"]},
    "mpeg1video": {"label": "MPEG-1", "encoders": ["mpeg1video"], "args": ["-q:v", "5", "-pix_fmt", "yuv420p"]},
    "prores": {"label": "Apple ProRes", "encoders": ["prores_ks"], "args": ["-profile:v", "0"]},
    "mjpeg": {"label": "Motion JPEG", "encoders": ["mjpeg"], "args": ["-q:v", "5", "-pix_fmt", "yuvj420p"]},
    "ffv1": {"label": "FFV1 (lossless)", "encoders": ["ffv1"], "args": []},
    "theora": {"label": "Theora", "encoders": ["libtheora"], "args": ["-q:v", "5", "-pix_fmt", "yuv420p"]},
    "wmv2": {"label": "Windows Media Video 8", "encoders": ["wmv2"], "args": ["-pix_fmt", "yuv420p"]},
}

AUDIO_CODECS: dict[str, dict] = {
    "aac": {"label": "AAC", "encoders": ["aac"]},
    "mp3": {"label": "MP3", "encoders": ["libmp3lame"]},
    "opus": {"label": "Opus", "encoders": ["libopus"]},
    "vorbis": {"label": "Vorbis", "encoders": ["libvorbis"]},
    "flac": {"label": "FLAC (lossless)", "encoders": ["flac"]},
    "alac": {"label": "ALAC (lossless)", "encoders": ["alac"]},
    "pcm_s16le": {"label": "PCM 16-bit", "encoders": ["pcm_s16le"]},
    "ac3": {"label": "Dolby Digital AC-3", "encoders": ["ac3"]},
    "mp2": {"label": "MPEG Audio Layer II", "encoders": ["mp2"]},
    "wmav2": {"label": "Windows Media Audio 2", "encoders": ["wmav2"]},
}


@dataclass
class Container:
    id: str
    label: str
    extensions: list[str]
    muxer: str
    probe_names: tuple[str, ...]          # substrings of ffprobe format_name
    video: list[str]
    audio: list[str]
    default_video: str
    default_audio: str
    priority: bool = True
    write_offered: bool = True
    extra_args: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


CONTAINERS: list[Container] = [
    Container("mp4", "MP4", [".mp4", ".m4v"], "mp4", ("mp4",), ["h264", "hevc", "av1", "vp9", "mpeg4"], ["aac", "mp3", "opus", "ac3", "alac"], "h264", "aac",
              extra_args=["-movflags", "+faststart"]),
    Container("mov", "QuickTime MOV", [".mov", ".qt"], "mov", ("mov",), ["h264", "hevc", "prores", "mpeg4", "mjpeg"], ["aac", "alac", "pcm_s16le", "mp3"], "h264", "aac"),
    Container("mkv", "Matroska MKV", [".mkv"], "matroska", ("matroska",), ["h264", "hevc", "av1", "vp9", "vp8", "mpeg4", "mpeg2video", "ffv1", "prores"],
              ["aac", "opus", "vorbis", "flac", "mp3", "ac3", "pcm_s16le"], "h264", "aac"),
    Container("webm", "WebM", [".webm"], "webm", ("webm",), ["vp9", "vp8", "av1"], ["opus", "vorbis"], "vp9", "opus"),
    Container("avi", "AVI", [".avi"], "avi", ("avi",), ["mpeg4", "mjpeg", "h264"], ["mp3", "ac3", "pcm_s16le"], "mpeg4", "mp3",
              notes=["Legacy container: no B-frame-friendly timing, limited codec support"]),
    Container("mpeg", "MPEG-PS (.mpg)", [".mpg", ".mpeg", ".m2v", ".vob"], "mpeg", ("mpeg",), ["mpeg2video", "mpeg1video"], ["mp2", "ac3", "mp3"], "mpeg2video", "mp2"),
    # Additional containers discovered from FFmpeg.
    Container("mpegts", "MPEG-TS", [".ts", ".m2ts", ".mts"], "mpegts", ("mpegts",), ["h264", "hevc", "mpeg2video"], ["aac", "mp2", "ac3", "mp3"], "h264", "aac", priority=False),
    Container("ogv", "Ogg Video", [".ogv", ".ogg"], "ogg", ("ogg",), ["theora", "vp8"], ["vorbis", "opus", "flac"], "theora", "vorbis", priority=False),
    Container("flv", "Flash Video", [".flv"], "flv", ("flv",), ["h264"], ["aac", "mp3"], "h264", "aac", priority=False, extra_args=["-ar", "44100"]),
    Container("3gp", "3GP", [".3gp", ".3g2"], "3gp", ("3gp",), ["h264", "mpeg4"], ["aac"], "h264", "aac", priority=False),
    Container("asf", "Windows Media (ASF/WMV)", [".wmv", ".asf"], "asf", ("asf",), ["wmv2"], ["wmav2"], "wmv2", "wmav2", priority=False, write_offered=False,
              notes=["Input only"]),
]
BY_ID = {c.id: c for c in CONTAINERS}

TEST_SIZE = "128x96"


def _list(flag: str, timeout: float) -> str:
    res = proc.run(["ffmpeg", "-hide_banner", flag], timeout=timeout)
    return res.stdout.decode("utf-8", "replace")


def _parse_coders(text: str) -> set[str]:
    names = set()
    for line in text.splitlines():
        m = re.match(r"^\s([VAS][F.][S.][X.][B.][D.])\s+(\S+)", line)
        if m:
            names.add(m.group(2))
    return names


def _parse_formats(text: str) -> set[str]:
    names = set()
    for line in text.splitlines():
        m = re.match(r"^\s+[DE]{1,2}\s+(\S+)", line)
        if m:
            names.update(m.group(1).split(","))
    return names


def ffmpeg_info(timeout: float = 15) -> dict | None:
    if not (proc.which("ffmpeg") and proc.which("ffprobe")):
        return None
    res = proc.run(["ffmpeg", "-hide_banner", "-version"], timeout=timeout)
    text = res.stdout.decode("utf-8", "replace")
    m = re.search(r"ffmpeg version (\S+)", text)
    conf = re.search(r"configuration: (.*)", text)
    config = conf.group(1) if conf else ""
    gpl = "--enable-gpl" in config
    nonfree = "--enable-nonfree" in config
    return {
        "version": m.group(1) if m else "unknown",
        "gpl": gpl,
        "nonfree": nonfree,
        "license": "GPL-2.0-or-later (this build uses --enable-gpl)" if gpl else "LGPL-2.1-or-later",
        "redistributable": not nonfree,
    }


def _encode_args(codec: str, encoder: str) -> list[str]:
    args = VIDEO_CODECS[codec]["args"]
    if isinstance(args, dict):
        args = args.get(encoder, [])
    return list(args)


def _probe(path: Path, timeout: float) -> dict | None:
    res = proc.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], timeout=timeout)
    if not res.ok:
        return None
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return None


def _test_encode(work: Path, c: Container, vcodec: str, vencoder: str, acodec: str, aencoder: str, timeout: float) -> tuple[bool, str]:
    out = work / f"t-{c.id}-{vcodec}-{acodec}{c.extensions[0]}"
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size={TEST_SIZE}:rate=25:duration=0.4",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=0.5",
        "-map", "0:v", "-map", "1:a",
        "-c:v", vencoder, *_encode_args(vcodec, vencoder),
        "-c:a", aencoder, "-ac", "2",
        *c.extra_args,
        "-f", c.muxer, str(out),
    ]
    try:
        res = proc.run(cmd, timeout=timeout)
    except proc.ProcessTimeout as exc:
        return False, str(exc)
    if not res.ok:
        return False, res.err_text(240)
    info = _probe(out, timeout)
    if not info:
        return False, "ffprobe could not read the result"
    fmt_name = info.get("format", {}).get("format_name", "")
    names = {s.get("codec_type"): s.get("codec_name") for s in info.get("streams", [])}
    if not any(p in fmt_name for p in c.probe_names):
        return False, f"muxed as {fmt_name}"
    if names.get("video") != vcodec or names.get("audio") != acodec:
        return False, f"streams came back as {names}"
    dec = proc.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(out), "-f", "null", "-"], timeout=timeout)
    if not dec.ok:
        return False, "decode failed: " + dec.err_text(200)
    return True, ""


# Codecs that can carry more than 8 bits, with the pixel format used to ask for it (see video/plan.py HIGH_DEPTH_OK).
HIGH_DEPTH_PIX = {"hevc": "yuv420p10le", "av1": "yuv420p10le", "vp9": "yuv420p10le", "prores": "yuv422p10le", "ffv1": "yuv420p10le"}


def _test_high_depth(work: Path, vcodec: str, vencoder: str, timeout: float) -> tuple[bool, str]:
    """Ask the encoder for 10-bit and look at what it really wrote. An 8-bit-only build (x265 is often
    compiled that way) accepts the request, quietly falls back to 8-bit and still exits 0."""
    out = work / f"depth-{vcodec}.mkv"
    args = _encode_args(vcodec, vencoder)
    if "-pix_fmt" in args:
        i = args.index("-pix_fmt")
        del args[i:i + 2]
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc2=size={TEST_SIZE}:rate=25:duration=0.4",
           "-c:v", vencoder, *args, "-pix_fmt", HIGH_DEPTH_PIX[vcodec], "-f", "matroska", str(out)]
    try:
        res = proc.run(cmd, timeout=timeout)
    except proc.ProcessTimeout as exc:
        return False, str(exc)
    if not res.ok:
        return False, res.err_text(160)
    info = _probe(out, timeout)
    pix = next((s.get("pix_fmt") or "" for s in (info or {}).get("streams", []) if s.get("codec_type") == "video"), "")
    if re.search(r"p1[0246](le|be)?$", pix):
        return True, pix
    return False, f"asked for {HIGH_DEPTH_PIX[vcodec]}, got {pix or 'nothing'}"


def detect(work: Path, timeout: float = 60) -> dict:
    info = ffmpeg_info()
    if info is None:
        return {"available": False, "ffmpeg": None, "containers": [], "readable": [], "writable": [], "codecs": {"video": [], "audio": []},
                "notes": ["FFmpeg/ffprobe not found on PATH: video features are disabled."]}

    encoders = _parse_coders(_list("-encoders", timeout))
    decoders = _parse_coders(_list("-decoders", timeout))
    muxers = _parse_formats(_list("-muxers", timeout))
    demuxers = _parse_formats(_list("-demuxers", timeout))

    def pick_encoder(table: dict, codec: str) -> str | None:
        return next((e for e in table[codec]["encoders"] if e in encoders), None)

    jobs: dict[tuple[str, str, str], tuple] = {}
    for c in CONTAINERS:
        if c.muxer not in muxers:
            continue
        dv, da = pick_encoder(VIDEO_CODECS, c.default_video), pick_encoder(AUDIO_CODECS, c.default_audio)
        for v in c.video:
            ve = pick_encoder(VIDEO_CODECS, v)
            if ve and da:
                jobs[(c.id, v, c.default_audio)] = (c, v, ve, c.default_audio, da)
        for a in c.audio:
            ae = pick_encoder(AUDIO_CODECS, a)
            if ae and dv:
                jobs[(c.id, c.default_video, a)] = (c, c.default_video, dv, a, ae)

    results: dict[tuple[str, str, str], tuple[bool, str]] = {}
    depth: dict[str, tuple[bool, str]] = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {key: pool.submit(_test_encode, work, *args, timeout) for key, args in jobs.items()}
        depth_futures = {v: pool.submit(_test_high_depth, work, v, pick_encoder(VIDEO_CODECS, v), timeout)
                         for v in HIGH_DEPTH_PIX if pick_encoder(VIDEO_CODECS, v)}
        for key, fut in futures.items():
            try:
                results[key] = fut.result()
            except Exception as exc:  # pragma: no cover - defensive
                results[key] = (False, str(exc))
        for v, fut in depth_futures.items():
            try:
                depth[v] = fut.result()
            except Exception as exc:  # pragma: no cover - defensive
                depth[v] = (False, str(exc))

    containers, readable, writable = [], [], []
    for c in CONTAINERS:
        demuxer_ok = any(d in demuxers for d in (c.muxer, *c.probe_names))
        vids, auds = [], []
        for v in c.video:
            ok, err = results.get((c.id, v, c.default_audio), (False, "encoder not available"))
            vids.append({"id": v, "label": VIDEO_CODECS[v]["label"], "encoder": pick_encoder(VIDEO_CODECS, v), "verified": ok, "error": err or None,
                         "high_depth": depth.get(v, (False, ""))[0]})
        for a in c.audio:
            ok, err = results.get((c.id, c.default_video, a), (False, "encoder not available"))
            auds.append({"id": a, "label": AUDIO_CODECS[a]["label"], "encoder": pick_encoder(AUDIO_CODECS, a), "verified": ok, "error": err or None})
        default_ok = results.get((c.id, c.default_video, c.default_audio), (False, ""))[0]
        # Reading is verified by decoding the sample we just muxed.
        read_ok = default_ok and demuxer_ok
        write_ok = default_ok and c.write_offered
        containers.append({
            "id": c.id, "label": c.label, "extensions": c.extensions, "priority": c.priority,
            "read": {"verified": read_ok, "demuxer": demuxer_ok},
            "write": {
                "verified": write_ok,
                "video_codecs": [v for v in vids if v["verified"]] if write_ok else [],
                "audio_codecs": [a for a in auds if a["verified"]] if write_ok else [],
                "default_video": c.default_video if write_ok else None,
                "default_audio": c.default_audio if write_ok else None,
                "failed": [x for x in vids + auds if not x["verified"]],
            },
            "notes": c.notes,
        })
        if read_ok:
            readable.append(c.id)
        if write_ok:
            writable.append(c.id)

    codecs = {
        "video": [{"id": k, "label": v["label"], "decoder": k in decoders, "encoder": pick_encoder(VIDEO_CODECS, k),
                   "high_depth": depth.get(k, (False, ""))[0]} for k, v in VIDEO_CODECS.items()],
        "audio": [{"id": k, "label": v["label"], "decoder": k in decoders, "encoder": pick_encoder(AUDIO_CODECS, k)} for k, v in AUDIO_CODECS.items()],
    }
    hw = sorted(e for e in encoders if re.search(r"_(nvenc|qsv|vaapi|videotoolbox|amf|v4l2m2m)$", e))
    notes = [f"{VIDEO_CODECS[v]['label']}: the {pick_encoder(VIDEO_CODECS, v)} encoder on this server only writes 8-bit ({why}). "
             f"10-bit and HDR video cannot be re-encoded to {VIDEO_CODECS[v]['label']} here; copying such streams unchanged still works."
             for v, (ok, why) in depth.items() if not ok]
    from ..video import gif
    gif_dir = work / "gif"
    gif_dir.mkdir(exist_ok=True)
    return {
        "available": True,
        "gif": gif.selftest(gif_dir, timeout),
        "ffmpeg": info,
        "containers": containers,
        "readable": readable,
        "writable": writable,
        "codecs": codecs,
        "counts": {"demuxers": len(demuxers), "muxers": len(muxers), "encoders": len(encoders), "decoders": len(decoders)},
        "notes": notes + ([f"Hardware encoders are compiled in but not used (they need matching GPUs/drivers): {', '.join(hw)}"] if hw else []),
    }
