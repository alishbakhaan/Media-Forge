"""Decide, stream by stream, what a video conversion will do, and build the FFmpeg command.

The plan is shown to the user before converting: which streams are copied (remuxed), re-encoded
(and why), converted (e.g. subtitles) or dropped. Anything dropped needs an explicit confirmation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from .probe import BITMAP_SUBS, TEXT_SUBS, input_args


class PlanError(ValueError):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details or {}


@dataclass(frozen=True)
class ContainerRules:
    muxer: str
    ext: str
    mime: str
    copy_video: frozenset          # codecs that may be stream-copied into this container
    copy_audio: frozenset
    multi_audio: bool = True
    text_subs: str | None = None   # codec text subtitles are converted to (None = unsupported)
    bitmap_subs: bool = False
    chapters: bool = False
    attachments: bool = False
    rotation_metadata: bool = False  # keeps display-matrix rotation when remuxing
    vfr: bool = True                 # can store variable frame rate
    standard_fps: bool = False       # MPEG-1/2 program streams need a standard frame rate
    extra: tuple = ()


RULES: dict[str, ContainerRules] = {
    "mp4": ContainerRules("mp4", ".mp4", "video/mp4", frozenset({"h264", "hevc", "av1", "vp9", "mpeg4"}),
                          frozenset({"aac", "mp3", "opus", "ac3", "alac"}), text_subs="mov_text", chapters=True,
                          rotation_metadata=True, extra=("-movflags", "+faststart")),
    "mov": ContainerRules("mov", ".mov", "video/quicktime", frozenset({"h264", "hevc", "prores", "mpeg4", "mjpeg"}),
                          frozenset({"aac", "alac", "pcm_s16le", "pcm_s24le", "mp3"}), text_subs="mov_text", chapters=True,
                          rotation_metadata=True, extra=("-movflags", "+faststart")),
    "mkv": ContainerRules("matroska", ".mkv", "video/x-matroska",
                          frozenset({"h264", "hevc", "av1", "vp9", "vp8", "mpeg4", "mpeg2video", "ffv1", "prores", "theora", "mjpeg"}),
                          frozenset({"aac", "opus", "vorbis", "flac", "mp3", "ac3", "eac3", "pcm_s16le", "pcm_s24le", "alac"}),
                          text_subs="keep", bitmap_subs=True, chapters=True, attachments=True, rotation_metadata=True),
    "webm": ContainerRules("webm", ".webm", "video/webm", frozenset({"vp9", "vp8", "av1"}), frozenset({"opus", "vorbis"}),
                           text_subs="webvtt", chapters=True),
    "avi": ContainerRules("avi", ".avi", "video/x-msvideo", frozenset({"mpeg4", "mjpeg"}),
                          frozenset({"mp3", "ac3", "pcm_s16le"}), vfr=False),
    "mpeg": ContainerRules("mpeg", ".mpg", "video/mpeg", frozenset({"mpeg2video", "mpeg1video"}), frozenset({"mp2", "ac3", "mp3"}),
                           vfr=False, standard_fps=True),
    "mpegts": ContainerRules("mpegts", ".ts", "video/mp2t", frozenset({"h264", "hevc", "mpeg2video"}),
                             frozenset({"aac", "mp2", "ac3", "mp3"})),
    "ogv": ContainerRules("ogg", ".ogv", "video/ogg", frozenset({"theora", "vp8"}), frozenset({"vorbis", "opus", "flac"})),
    "flv": ContainerRules("flv", ".flv", "video/x-flv", frozenset({"h264"}), frozenset({"aac", "mp3"}), multi_audio=False,
                          vfr=False),
    "3gp": ContainerRules("3gp", ".3gp", "video/3gpp", frozenset({"h264", "mpeg4"}), frozenset({"aac"}),
                          rotation_metadata=True, extra=("-movflags", "+faststart")),
}

# Encoder settings used for conversions in Milestone 4 (quality controls arrive in Milestone 5).
VIDEO_ENC = {
    "h264": {"libx264": ["-preset", "medium", "-crf", "20"]},
    "hevc": {"libx265": ["-preset", "medium", "-crf", "23", "-x265-params", "log-level=error"]},
    "av1": {"libsvtav1": ["-preset", "8", "-crf", "32"], "libaom-av1": ["-cpu-used", "6", "-crf", "32", "-b:v", "0", "-row-mt", "1"]},
    "vp9": {"libvpx-vp9": ["-crf", "32", "-b:v", "0", "-deadline", "good", "-cpu-used", "4", "-row-mt", "1"]},
    "vp8": {"libvpx": ["-crf", "10", "-b:v", "4M", "-deadline", "good", "-cpu-used", "4"]},
    "mpeg4": {"mpeg4": ["-q:v", "3"]},
    "mpeg2video": {"mpeg2video": ["-q:v", "3"]},
    "mpeg1video": {"mpeg1video": ["-q:v", "3"]},
    "prores": {"prores_ks": ["-profile:v", "2"]},
    "mjpeg": {"mjpeg": ["-q:v", "3"]},
    "ffv1": {"ffv1": ["-level", "3"]},
    "theora": {"libtheora": ["-q:v", "7"]},
}
PIX_FMT_8 = {"prores": "yuv422p10le", "mjpeg": "yuvj420p", "ffv1": None}
HIGH_DEPTH_OK = {"hevc": "yuv420p10le", "av1": "yuv420p10le", "vp9": "yuv420p10le", "prores": "yuv422p10le", "ffv1": None}
AUDIO_ENC = {
    "aac": ["-b:a", "192k"], "mp3": ["-b:a", "192k"], "opus": ["-b:a", "128k"], "vorbis": ["-q:a", "5"], "flac": [],
    "alac": [], "pcm_s16le": [], "ac3": ["-b:a", "384k"], "mp2": ["-b:a", "192k", "-ar", "48000"], "wmav2": ["-b:a", "192k"],
}
STEREO_ONLY = {"mp3", "mp2"}
LOCATION_TAG = re.compile(r"location|gps|iso6709|©xyz|xyz", re.I)
STANDARD_FPS = [Fraction(24000, 1001), Fraction(24), Fraction(25), Fraction(30000, 1001), Fraction(30), Fraction(50),
                Fraction(60000, 1001), Fraction(60)]
# "Reduce file size": stronger compression per codec (higher CRF / q), never touching lossless codecs.
QUALITY = {"high": "High quality", "balanced": "Balanced", "small": "Smallest file"}
QUALITY_ARGS = {
    "h264": {"-crf": {"high": "21", "balanced": "25", "small": "29"}},
    "hevc": {"-crf": {"high": "24", "balanced": "28", "small": "32"}},
    "av1": {"-crf": {"high": "32", "balanced": "38", "small": "44"}},
    "vp9": {"-crf": {"high": "33", "balanced": "38", "small": "43"}},
    "vp8": {"-crf": {"high": "12", "balanced": "20", "small": "30"}, "-b:v": {"high": "3M", "balanced": "1500k", "small": "800k"}},
    "mpeg4": {"-q:v": {"high": "4", "balanced": "6", "small": "9"}},
    "mpeg2video": {"-q:v": {"high": "4", "balanced": "6", "small": "9"}},
    "mpeg1video": {"-q:v": {"high": "4", "balanced": "6", "small": "9"}},
    "mjpeg": {"-q:v": {"high": "4", "balanced": "7", "small": "11"}},
    "theora": {"-q:v": {"high": "6", "balanced": "5", "small": "3"}},
}
SMALL_AUDIO = {"aac": "128k", "mp3": "128k", "opus": "96k", "ac3": "192k", "mp2": "128k"}
MAX_HEIGHTS = (2160, 1440, 1080, 720, 480, 360)
HDR_PRESERVE_CODECS = {"hevc", "av1", "vp9"}
HDR_PRESERVE_CONTAINERS = {"mp4", "mov", "mkv", "webm"}
TONEMAP = ("zscale=t=linear:npl=203,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=mobius:desat=0,"
           "zscale=t=bt709:m=bt709:r=tv,format=yuv420p")


@dataclass
class Choice:
    container: str
    video_codec: str = "auto"          # "copy", "auto" (copy when possible) or a codec id
    audio_codec: str = "auto"
    audio_tracks: list[int] | None = None   # stream indexes to keep (None = all the container can hold)
    subtitles: bool = True
    chapters: bool = True
    hdr: str | None = None             # "preserve" | "tonemap" (required for HDR input)
    confirm_drops: bool = False
    metadata: str = "remove_location"  # keep | remove_location | remove_all
    force_reencode: bool = False       # re-encode even when copying would work
    quality: str | None = None         # "Reduce file size": high | balanced | small
    max_height: int | None = None      # "Reduce file size": shorter side at most this many pixels

    @classmethod
    def parse(cls, raw: dict) -> "Choice":
        if not isinstance(raw, dict):
            raise PlanError("bad_request", "Expected a JSON object.")
        tracks = raw.get("audio_tracks")
        if tracks is not None and (not isinstance(tracks, list) or not all(isinstance(t, int) for t in tracks)):
            raise PlanError("bad_request", "audio_tracks must be a list of stream indexes.")
        hdr = raw.get("hdr")
        if hdr not in (None, "preserve", "tonemap"):
            raise PlanError("bad_request", "hdr must be 'preserve' or 'tonemap'.")
        meta = raw.get("metadata") or "remove_location"
        if meta not in ("keep", "remove_location", "remove_all"):
            raise PlanError("bad_request", "metadata must be keep, remove_location or remove_all.")
        quality = raw.get("quality") or None
        if quality is not None and quality not in QUALITY:
            raise PlanError("bad_request", "quality must be high, balanced or small.")
        max_height = raw.get("max_height") or None
        if max_height is not None and max_height not in MAX_HEIGHTS:
            raise PlanError("bad_request", f"max_height must be one of {', '.join(map(str, MAX_HEIGHTS))}.")
        return cls(str(raw.get("container", "")).lower(), str(raw.get("video_codec") or "auto"),
                   str(raw.get("audio_codec") or "auto"), tracks, bool(raw.get("subtitles", True)),
                   bool(raw.get("chapters", True)), hdr, bool(raw.get("confirm_drops", False)), meta,
                   bool(raw.get("force_reencode", False)), quality, max_height)


@dataclass
class Plan:
    container: str
    video: dict
    audio: list[dict] = field(default_factory=list)
    subtitles: list[dict] = field(default_factory=list)
    chapters: dict = field(default_factory=dict)
    attachments: list[dict] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    mode: str = "encode"                # "remux" when every kept stream is copied
    expected: dict = field(default_factory=dict)
    filters: list[str] = field(default_factory=list)
    hdr: dict | None = None
    metadata: dict = field(default_factory=dict)

    def public(self) -> dict:
        return {k: getattr(self, k) for k in ("container", "video", "audio", "subtitles", "chapters", "attachments",
                                                "dropped", "notes", "mode", "expected", "hdr", "metadata")}


def _nearest_std_fps(fps: float) -> Fraction:
    return min(STANDARD_FPS, key=lambda f: abs(float(f) - fps))


def build_plan(info: dict, container_caps: dict, choice: Choice) -> Plan:
    """container_caps: the verified capability row for the target container."""
    cid = choice.container
    r = RULES.get(cid)
    if r is None or not container_caps.get("write", {}).get("verified"):
        raise PlanError("unsupported_output", f"'{cid}' is not a verified output container.")
    vcodecs = {v["id"]: v for v in container_caps["write"]["video_codecs"]}
    acodecs = {a["id"]: a for a in container_caps["write"]["audio_codecs"]}
    v = info.get("primary_video")
    if not v:
        raise PlanError("unsupported", "This file has no video stream.")

    plan = Plan(container=cid, video={})
    # ---------------------------------------------------------------- video
    reasons: list[str] = []
    hdr_kind = v.get("hdr")
    if hdr_kind and not choice.hdr:
        raise PlanError("hdr_choice_required", f"This video is {hdr_kind}. Choose 'preserve HDR' or 'convert to SDR'.",
                        {"hdr": hdr_kind})
    if choice.video_codec not in ("auto", "copy") and choice.video_codec not in vcodecs:
        raise PlanError("unsupported_output", f"Video codec '{choice.video_codec}' is not verified for {cid}.")
    copy_ok = v["codec"] in r.copy_video
    if not copy_ok:
        reasons.append(f"{cid.upper()} cannot hold {v['codec']} video")
    if copy_ok and v.get("rotation") and not r.rotation_metadata:
        copy_ok = False
        reasons.append(f"{cid.upper()} cannot store rotation metadata, so the rotation is applied to the pixels")
    if copy_ok and hdr_kind and choice.hdr == "tonemap":
        copy_ok = False
        reasons.append("converting HDR to SDR requires re-encoding")
    if copy_ok and hdr_kind and choice.hdr == "preserve" and cid not in HDR_PRESERVE_CONTAINERS:
        copy_ok = False
    if copy_ok and info.get("vfr", {}).get("vfr") and not r.vfr:
        copy_ok = False
        reasons.append(f"{cid.upper()} needs a constant frame rate")
    if copy_ok and r.standard_fps and v.get("fps") and abs(float(_nearest_std_fps(v["fps"])) - v["fps"]) > 0.01:
        copy_ok = False
        reasons.append("MPEG program streams need a standard frame rate")
    if choice.force_reencode and choice.video_codec == "copy":
        raise PlanError("bad_request", "Choose either 'copy' or 're-encode', not both.")
    if copy_ok and choice.force_reencode:
        copy_ok = False
        reasons.append("you chose to re-encode")
    short_side = min(v["display_width"], v["display_height"])
    shrink_to = choice.max_height if choice.max_height and short_side > choice.max_height else None
    if copy_ok and (choice.quality or shrink_to):
        copy_ok = False
        reasons.append("reducing the file size requires re-encoding")
    if choice.video_codec == "copy" and not copy_ok:
        raise PlanError("copy_not_possible", "The video cannot be copied without re-encoding: " + "; ".join(reasons) + ".")
    want_copy = copy_ok and choice.video_codec in ("auto", "copy", v["codec"])
    filters: list[str] = []
    if want_copy:
        plan.video = {"index": v["index"], "action": "copy", "codec": v["codec"], "encoder": None,
                      "reason": "copied unchanged (no quality loss)"}
    else:
        vc = choice.video_codec if choice.video_codec not in ("auto", "copy") else container_caps["write"]["default_video"]
        if vc not in vcodecs:
            raise PlanError("unsupported_output", f"No verified video encoder for {cid}.")
        enc = vcodecs[vc]["encoder"]
        if choice.video_codec not in ("auto", "copy") and copy_ok and vc != v["codec"]:
            reasons.append(f"you chose {vcodecs[vc]['label']}")
        pix = None
        if hdr_kind:
            if choice.hdr == "preserve":
                tested_hdr = container_caps.get("_hdr_preserve")
                if vc not in HDR_PRESERVE_CODECS or cid not in HDR_PRESERVE_CONTAINERS or \
                        (tested_hdr is not None and [cid, vc] not in tested_hdr):
                    raise PlanError("hdr_not_preservable",
                                    f"HDR can only be preserved as HEVC, AV1 or VP9 in MP4/MOV/MKV/WebM. "
                                    f"Choose one of those, or convert to SDR.", {"hdr": hdr_kind})
                pix = "yuv420p10le"
                plan.hdr = {"kind": hdr_kind, "action": "preserve", "transfer": v["color_transfer"],
                            "primaries": v.get("color_primaries"), "matrix": v.get("color_space")}
            else:
                if container_caps.get("_hdr_tonemap") is False:
                    raise PlanError("hdr_unsupported", "HDR→SDR conversion did not pass its test on this server, so it is not "
                                                       "offered. Use 'preserve HDR' or another tool.", {"hdr": hdr_kind})
                filters.append(TONEMAP)
                plan.hdr = {"kind": hdr_kind, "action": "tonemap"}
                plan.notes.append(f"{hdr_kind} converted to SDR (BT.709) with Mobius tone mapping.")
        elif v.get("bit_depth", 8) > 8:
            if vc in HIGH_DEPTH_OK:
                pix = HIGH_DEPTH_OK[vc]
                plan.notes.append(f"{v['bit_depth']}-bit video kept at high bit depth.")
            else:
                plan.notes.append(f"{v['bit_depth']}-bit video converted to 8-bit ({vcodecs[vc]['label']} is encoded as 8-bit here).")
        if pix is None:
            pix = PIX_FMT_8.get(vc, "yuv420p")
        # Square pixels for re-encodes: shrink one axis rather than upscale the other.
        sar = Fraction(v.get("sar", "1:1").replace(":", "/")) if v.get("sar") else Fraction(1)
        if sar != 1:
            if sar > 1:
                filters.append(f"scale=iw:trunc(ih/{float(sar):.6f}/2)*2,setsar=1")
            else:
                filters.append(f"scale=trunc(iw*{float(sar):.6f}/2)*2:ih,setsar=1")
            plan.notes.append(f"non-square pixels ({v['sar']}) converted to square pixels by reducing one dimension; "
                              "displayed aspect ratio unchanged")
        if shrink_to:
            s = shrink_to / short_side
            nw, nh = max(2, round(v["display_width"] * s / 2) * 2), max(2, round(v["display_height"] * s / 2) * 2)
            filters.append(f"scale={nw}:{nh}")
            plan.notes.append(f"resized to {nw} × {nh} to reduce the file size")
        if pix and pix.startswith("yuv420") or pix == "yuvj420p":
            filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")  # 4:2:0 needs even sizes (≤1 px trimmed, never enlarged)
        if v.get("rotation"):
            plan.notes.append(f"rotation ({v['rotation']}°) applied to the pixels")
        if r.standard_fps and v.get("fps"):
            std = _nearest_std_fps(v["fps"])
            if abs(float(std) - v["fps"]) > 0.01 or info.get("vfr", {}).get("vfr"):
                filters.append(f"fps={std.numerator}/{std.denominator}")
                plan.notes.append(f"frame rate set to {float(std):.3f} fps (MPEG requires a standard rate); timing and sync kept")
        elif not r.vfr and info.get("vfr", {}).get("vfr"):
            plan.notes.append(f"variable frame rate stored as constant {v['fps']} fps ({cid.upper()} has no VFR); frames are "
                              "duplicated or dropped to keep sync")
        plan.video = {"index": v["index"], "action": "encode", "codec": vc, "encoder": enc, "pix_fmt": pix,
                      "reason": "; ".join(reasons) if reasons else "re-encoded to the chosen codec",
                      "args": list(VIDEO_ENC.get(vc, {}).get(enc, []))}
        if choice.quality:
            args = plan.video["args"]
            for flag, levels in QUALITY_ARGS.get(vc, {}).items():
                if flag in args:
                    args[args.index(flag) + 1] = levels[choice.quality]
                else:
                    args += [flag, levels[choice.quality]]
            if vc in QUALITY_ARGS:
                plan.notes.append(f"video re-encoded at '{QUALITY[choice.quality]}' to reduce the file size")
            else:
                plan.notes.append(f"{vcodecs[vc]['label']} has no quality setting; only resizing can make it smaller")
        if vc == "hevc" and cid in ("mp4", "mov"):
            plan.video["args"] += ["-tag:v", "hvc1"]
        if hdr_kind and choice.hdr == "preserve":
            plan.video["args"] += ["-color_primaries", v.get("color_primaries") or "bt2020",
                                   "-color_trc", v["color_transfer"], "-colorspace", v.get("color_space") or "bt2020nc"]
            if vc == "hevc":
                plan.video["args"][plan.video["args"].index("-x265-params") + 1] += (
                    f":colorprim={v.get('color_primaries') or 'bt2020'}:transfer={v['color_transfer']}"
                    f":colormatrix={v.get('color_space') or 'bt2020nc'}:hdr10-opt=1")
            if vc == "vp9":
                plan.video["args"] += ["-profile:v", "2"]
    plan.filters = filters

    # ---------------------------------------------------------------- audio
    audio_streams = [s for s in info["streams"] if s["type"] == "audio"]
    wanted = [s for s in audio_streams if choice.audio_tracks is None or s["index"] in choice.audio_tracks]
    for s in audio_streams:
        if s not in wanted:
            plan.dropped.append(f"audio track #{s['index']} ({s['codec']}{', ' + s['language'] if s.get('language') else ''}) — you unchecked it")
    if not r.multi_audio and len(wanted) > 1:
        default = next((s for s in wanted if s.get("default")), wanted[0])
        for s in wanted:
            if s is not default:
                plan.dropped.append(f"audio track #{s['index']} ({s['codec']}{', ' + s['language'] if s.get('language') else ''}) "
                                    f"— {cid.upper()} holds only one audio track")
        wanted = [default]
    if choice.audio_codec not in ("auto", "copy") and choice.audio_codec not in acodecs:
        raise PlanError("unsupported_output", f"Audio codec '{choice.audio_codec}' is not verified for {cid}.")
    for s in wanted:
        a_copy = s["codec"] in r.copy_audio and choice.audio_codec in ("auto", "copy", s["codec"]) and not choice.force_reencode
        if choice.audio_codec == "copy" and s["codec"] not in r.copy_audio:
            raise PlanError("copy_not_possible", f"{cid.upper()} cannot hold {s['codec']} audio; it must be re-encoded.")
        if a_copy:
            plan.audio.append({"index": s["index"], "action": "copy", "codec": s["codec"], "reason": "copied unchanged",
                               "language": s.get("language"), "channels": s.get("channels")})
        else:
            ac = choice.audio_codec if choice.audio_codec not in ("auto", "copy") else container_caps["write"]["default_audio"]
            if ac not in acodecs:
                raise PlanError("unsupported_output", f"No verified audio encoder for {cid}.")
            why = (f"{cid.upper()} cannot hold {s['codec']} audio" if s["codec"] not in r.copy_audio
                   else "you chose to re-encode" if choice.force_reencode else f"you chose {acodecs[ac]['label']}")
            entry = {"index": s["index"], "action": "encode", "codec": ac, "encoder": acodecs[ac]["encoder"], "reason": why,
                     "language": s.get("language"), "channels": s.get("channels"), "args": list(AUDIO_ENC.get(ac, []))}
            if choice.quality == "small" and ac in SMALL_AUDIO and "-b:a" in entry["args"]:
                entry["args"][entry["args"].index("-b:a") + 1] = SMALL_AUDIO[ac]
            if ac in STEREO_ONLY and (s.get("channels") or 2) > 2:
                entry["args"] += ["-ac", "2"]
                plan.notes.append(f"audio track #{s['index']} downmixed to stereo ({acodecs[ac]['label']} is stereo-only)")
            plan.audio.append(entry)
    if not audio_streams:
        plan.notes.append("the source has no audio; the output will have none")

    # ---------------------------------------------------------------- subtitles / chapters / attachments
    for s in (x for x in info["streams"] if x["type"] == "subtitle"):
        label = f"subtitle #{s['index']} ({s['codec']}{', ' + s['language'] if s.get('language') else ''})"
        if not choice.subtitles:
            plan.dropped.append(f"{label} — you chose not to keep subtitles")
        elif s["codec"] in BITMAP_SUBS:
            if r.bitmap_subs:
                plan.subtitles.append({"index": s["index"], "action": "copy", "codec": s["codec"]})
            else:
                plan.dropped.append(f"{label} — {cid.upper()} cannot store image-based subtitles")
        elif s["codec"] in TEXT_SUBS and r.text_subs:
            target = s["codec"] if r.text_subs == "keep" and s["codec"] != "mov_text" else ("srt" if r.text_subs == "keep" else r.text_subs)
            action = "copy" if target == s["codec"] else "convert"
            plan.subtitles.append({"index": s["index"], "action": action, "codec": target,
                                   "reason": None if action == "copy" else f"converted to {target} (text and timing kept; styling may be simplified)"})
        else:
            plan.dropped.append(f"{label} — {cid.upper()} cannot store subtitles")
    if info.get("chapters"):
        if not choice.chapters:
            plan.dropped.append(f"{info['chapters']} chapters — you chose not to keep them")
            plan.chapters = {"count": info["chapters"], "action": "drop"}
        elif r.chapters:
            plan.chapters = {"count": info["chapters"], "action": "keep"}
        else:
            plan.chapters = {"count": info["chapters"], "action": "drop"}
            plan.dropped.append(f"{info['chapters']} chapters — {cid.upper()} cannot store chapters")
    for s in (x for x in info["streams"] if x["type"] == "attachment"):
        name = s.get("filename") or f"#{s['index']}"
        if r.attachments:
            plan.attachments.append({"index": s["index"], "action": "copy", "filename": name})
        else:
            plan.dropped.append(f"attachment {name} ({s.get('mimetype') or 'file'}) — only MKV stores attachments")
    for s in (x for x in info["streams"] if x["type"] == "data"):
        plan.dropped.append(f"data stream #{s['index']} ({s.get('codec_tag') or s.get('codec') or 'data'}) — not converted")
    for s in (x for x in info["streams"] if x["type"] == "video" and x["index"] != v["index"]):
        what = "cover image" if s.get("attached_pic") else "extra video stream"
        plan.dropped.append(f"{what} #{s['index']} — only the main video stream is converted")

    # Only offer combinations the self-test actually converted on this machine.
    tested = container_caps["write"].get("tested_pairs")
    if tested is not None:
        enc_v = {x.split("+")[0] for x in tested}
        enc_a = {x.split("+")[1] for x in tested}
        vc = plan.video["codec"]
        for a in plan.audio:
            if vc in enc_v and a["codec"] in enc_a and f"{vc}+{a['codec']}" not in tested:
                raise PlanError("untested_combination", f"{vc} video with {a['codec']} audio in {cid.upper()} has not passed "
                                                        "the conversion test on this server. Choose another codec.")

    # ---------------------------------------------------------------- container metadata
    tags = info.get("format_tags") or {}
    loc_keys = [k for k in tags if LOCATION_TAG.search(k)]
    if choice.metadata == "remove_all":
        plan.metadata = {"mode": "remove_all", "removed": sorted(tags), "kept": [],
                         "summary": "file-level metadata (title, dates, device, location…) removed; track languages kept"}
    elif choice.metadata == "remove_location":
        plan.metadata = {"mode": "remove_location", "removed": loc_keys, "kept": [k for k in tags if k not in loc_keys],
                         "summary": ("location tags removed: " + ", ".join(loc_keys)) if loc_keys else "no location tags in the source"}
    else:
        plan.metadata = {"mode": "keep", "removed": [], "kept": list(tags),
                         "summary": "file-level metadata copied where the container supports it" +
                                    (" (includes location)" if loc_keys else "")}
    plan.expected["no_location_tags"] = choice.metadata != "keep"

    plan.mode = "remux" if plan.video["action"] == "copy" and all(a["action"] == "copy" for a in plan.audio) else "encode"
    disp_w, disp_h = v["display_width"], v["display_height"]
    plan.expected = {
        "video_codec": plan.video["codec"], "audio": [a["codec"] for a in plan.audio],
        "subtitles": len(plan.subtitles), "chapters": plan.chapters.get("action") == "keep",
        "display_width": disp_w, "display_height": disp_h, "duration": info.get("duration"),
        "hdr_transfer": v["color_transfer"] if plan.hdr and plan.hdr["action"] == "preserve" else None,
    }
    if plan.video["action"] == "encode" and v.get("rotation"):
        plan.expected["rotation_baked"] = True
    return plan


def ffmpeg_args(plan: Plan, src: Path, container: str, out: Path) -> list[str]:
    r = RULES[plan.container]
    args = ["ffmpeg", "-nostdin", "-hide_banner", "-y", "-loglevel", "error", "-progress", "pipe:1", "-nostats",
            *input_args(src, container), "-map", f"0:{plan.video['index']}"]
    for a in plan.audio:
        args += ["-map", f"0:{a['index']}"]
    for s in plan.subtitles:
        args += ["-map", f"0:{s['index']}"]
    for t in plan.attachments:
        args += ["-map", f"0:{t['index']}"]
    if plan.video["action"] == "copy":
        args += ["-c:v", "copy"]
    else:
        if plan.filters:
            args += ["-vf", ",".join(plan.filters)]
        args += ["-c:v", plan.video["encoder"], *plan.video["args"]]
        if plan.video.get("pix_fmt"):
            args += ["-pix_fmt", plan.video["pix_fmt"]]
    for i, a in enumerate(plan.audio):
        if a["action"] == "copy":
            args += [f"-c:a:{i}", "copy"]
        else:
            args += [f"-c:a:{i}", a["encoder"], *_per_stream(a["args"], i)]
            if plan.container == "flv" and a["codec"] == "mp3":
                args += [f"-ar:a:{i}", "44100"]  # FLV stores MP3 only at 44.1/22.05/11.025 kHz
    for i, s in enumerate(plan.subtitles):
        args += [f"-c:s:{i}", "copy" if s["action"] == "copy" else s["codec"]]
    if plan.attachments:
        args += ["-c:t", "copy"]
    if plan.metadata.get("mode") == "remove_all":
        args += ["-map_metadata:g", "-1"]
    else:
        args += ["-map_metadata", "0"]
        for k in plan.metadata.get("removed", []):
            args += ["-metadata", f"{k}="]
    args += ["-map_chapters", "0" if plan.chapters.get("action") == "keep" else "-1"]
    if not r.vfr:
        args += ["-fps_mode", "cfr"]
    args += [*r.extra, "-f", r.muxer, str(out)]
    return args


def _per_stream(args: list[str], i: int) -> list[str]:
    out = []
    for x in args:
        if x in ("-b:a", "-q:a"):
            out.append(f"{x}:{i}")
        elif x in ("-ac", "-ar"):
            out.append(f"{x}:a:{i}")
        else:
            out.append(x)
    return out
