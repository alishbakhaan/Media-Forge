"""Video self-tests run at capability detection, through the same planner/FFmpeg arguments that real
conversions use. Every container × codec combination that will be offered is encoded from a real clip
(with real audio), verified with ffprobe and decoded. HDR preservation and HDR→SDR tone mapping are
tested on an HDR10 clip made from real images (no real HDR camera footage is available here)."""
from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import proc
from .plan import HDR_PRESERVE_CODECS, HDR_PRESERVE_CONTAINERS, RULES, Choice, PlanError, build_plan, ffmpeg_args
from .probe import probe
from .verify import VerifyError, verify


def make_test_clip(real_video: Path, real_audio: Path | None, out: Path) -> bool:
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", "3", "-t", "1.2", "-i", str(real_video)]
    if real_audio:
        cmd += ["-t", "1.2", "-i", str(real_audio), "-map", "0:v:0", "-map", "1:a:0"]
    cmd += ["-vf", "scale=640:-2", "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-c:a", "flac", "-ar", "48000",
            "-ac", "2", "-shortest", str(out)]
    return proc.run(cmd, timeout=120).ok


def make_sync_clip(out: Path) -> bool:
    """Synthetic A/V sync probe: a white flash and a 1 kHz beep both start at exactly t = 2.0 s."""
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
           "color=c=black:s=320x240:r=30:d=4,drawbox=x=0:y=0:w=320:h=240:color=white:t=fill:enable='between(t,2,2.1)'",
           "-f", "lavfi", "-i", "aevalsrc='if(between(t,2,2.1),0.8*sin(2*PI*1000*t),0)':s=48000:d=4",
           "-c:v", "libx264", "-bf", "3", "-c:a", "flac", "-shortest", str(out)]
    return proc.run(cmd, timeout=60).ok


def measure_sync(path: Path) -> float | None:
    """Seconds from the flash to the start of the beep, as decoded (None if either is not found)."""
    import re
    res = proc.run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(path), "-map", "0:v:0", "-vf",
                    "signalstats,metadata=print:key=lavfi.signalstats.YAVG", "-map", "0:a:0", "-af",
                    "silencedetect=n=-30dB:d=0.05", "-f", "null", "-"], timeout=120)
    text = res.stderr.decode("utf-8", "replace")
    flash = pts = None
    for line in text.splitlines():
        m = re.search(r"pts_time:([\d.]+)", line)
        if m:
            pts = float(m.group(1))
        m = re.search(r"YAVG=([\d.]+)", line)
        if m and float(m.group(1)) > 128 and flash is None:
            flash = pts
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", text)]
    return None if flash is None or not ends else ends[0] - flash


SYNC_TOLERANCE_S = 0.045  # about one video frame at 25–30 fps plus one audio frame


def make_hdr_clip(real_image: Path, out: Path, transfer: str = "smpte2084") -> bool:
    """HDR10 (PQ) or HLG clip from a real photo: SDR → BT.2020 + PQ/HLG, 10-bit HEVC, tagged."""
    zs = (f"zscale=transferin=bt709:primariesin=bt709:matrixin=bt709:rangein=tv:transfer={transfer}:"
          f"primaries=bt2020:matrix=bt2020nc:range=tv:npl=203,format=yuv420p10le")
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-loop", "1", "-i", str(real_image), "-t", "1.2",
           "-vf", f"scale=512:512,zoompan=z='1+0.003*on':d=1:s=512x512:fps=25,{zs}",
           "-c:v", "libx265", "-preset", "ultrafast",
           "-x265-params", f"log-level=error:colorprim=bt2020:transfer={transfer}:colormatrix=bt2020nc",
           "-color_primaries", "bt2020", "-color_trc", transfer, "-colorspace", "bt2020nc", "-tag:v", "hvc1", str(out)]
    return proc.run(cmd, timeout=120).ok


def _run(plan, src: Path, src_container: str, out: Path, src_info: dict) -> tuple[bool, str]:
    cmd = [a for a in ffmpeg_args(plan, src, src_container, out) if a not in ("-progress", "pipe:1")]
    try:
        res = proc.run(cmd, timeout=300)
    except proc.ProcessTimeout as exc:
        return False, str(exc)
    if not res.ok or not out.exists():
        return False, res.err_text(240)
    try:
        verify(out, plan.container, plan.public(), src_info, timeout=120)
    except VerifyError as exc:
        return False, exc.message
    return True, "ok"


def apply(caps_video: dict, results: dict) -> None:
    """Withdraw anything that failed: codecs with no passing pair, containers whose default pair failed."""
    for c in caps_video.get("containers", []):
        res = results["combos"].get(c["id"])
        if not res or not c["write"]["verified"]:
            continue
        passed = [k for k, v in res.items() if v["passed"]]
        c["write"]["tested_pairs"] = passed
        c["write"]["failed_pairs"] = {k: v["detail"] for k, v in res.items() if not v["passed"]}
        ok_v = {k.split("+")[0] for k in passed}
        ok_a = {k.split("+")[1] for k in passed}
        c["write"]["video_codecs"] = [v for v in c["write"]["video_codecs"] if v["id"] in ok_v]
        c["write"]["audio_codecs"] = [a for a in c["write"]["audio_codecs"] if a["id"] in ok_a]
        if f"{c['write']['default_video']}+{c['write']['default_audio']}" not in passed:
            old = f"{c['write']['default_video']}+{c['write']['default_audio']}"
            # Pick the first passing pair (in preference order) as the new default, or withdraw the container.
            order = [f"{v['id']}+{a['id']}" for v in c["write"]["video_codecs"] for a in c["write"]["audio_codecs"]]
            new = next((p for p in order if p in passed), None)
            if new:
                c["write"]["default_video"], c["write"]["default_audio"] = new.split("+")
                c["notes"] = c["notes"] + [f"Default changed from {old} to {new}: {res[old]['detail'] if old in res else 'not tested'}."]
            else:
                c["write"]["verified"] = False
                c["notes"] = c["notes"] + ["Withdrawn: no codec pair passed the real-clip conversion test."]
    for c in caps_video.get("containers", []):
        c["_hdr_preserve"] = results["hdr"]["preserve"]
        c["_hdr_tonemap"] = bool(results["hdr"]["tonemap"])
    caps_video["writable"] = [c["id"] for c in caps_video.get("containers", []) if c["write"]["verified"]]
    caps_video["selftest"] = {"clip": results["clip"], "sync": results.get("sync")}
    caps_video["hdr"] = results["hdr"]


def run_selftests(containers: list[dict], clip: Path, hdr_clip: Path | None, work: Path, sync_clip: Path | None = None) -> dict:
    info = probe(clip, "mkv", timeout=60)
    sync_info = probe(sync_clip, "mkv", timeout=60) if sync_clip and sync_clip.exists() else None
    sync_base = measure_sync(sync_clip) if sync_info else None
    jobs = []
    for c in containers:  # every video × audio pair that could be offered
        if not c["write"]["verified"] or c["id"] not in RULES:
            continue
        for v in c["write"]["video_codecs"]:
            for a in c["write"]["audio_codecs"]:
                jobs.append((c, v["id"], a["id"]))

    def one(job):
        c, vc, ac = job
        out = work / f"st-{c['id']}-{vc}-{ac}{RULES[c['id']].ext}"
        try:
            plan = build_plan(info, c, Choice(c["id"], video_codec=vc, audio_codec=ac))
        except PlanError as exc:
            return job, False, exc.message
        ok, msg = _run(plan, clip, "mkv", out, info)
        out.unlink(missing_ok=True)
        if ok and sync_base is not None:
            # Content-based A/V sync: encode the flash/beep probe with the same pair and measure the offset.
            splan = build_plan(sync_info, c, Choice(c["id"], video_codec=vc, audio_codec=ac, force_reencode=True))
            sout = work / f"sync-{c['id']}-{vc}-{ac}{RULES[c['id']].ext}"
            sok, smsg = _run(splan, sync_clip, "mkv", sout, sync_info)
            off = measure_sync(sout) if sok else None
            sout.unlink(missing_ok=True)
            if off is None:
                return job, False, f"A/V sync probe failed: {smsg}"
            drift = off - sync_base
            if abs(drift) > SYNC_TOLERANCE_S:
                return job, False, f"audio/video out of sync by {drift * 1000:+.0f} ms"
            msg = f"ok; A/V sync {drift * 1000:+.1f} ms"
        return job, ok, msg

    combos: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for (c, vc, ac), ok, msg in pool.map(one, jobs):
            combos.setdefault(c["id"], {})[f"{vc}+{ac}"] = {"video": vc, "audio": ac, "passed": ok, "detail": msg}

    hdr = {"preserve": [], "tonemap": False, "detail": {}, "source": "HDR10 clip made from a real photo with FFmpeg"}
    if hdr_clip and hdr_clip.exists():
        hinfo = probe(hdr_clip, "mp4", timeout=60)
        for c in containers:
            if c["id"] not in HDR_PRESERVE_CONTAINERS or not c["write"]["verified"]:
                continue
            for v in c["write"]["video_codecs"]:
                if v["id"] not in HDR_PRESERVE_CODECS:
                    continue
                try:
                    plan = build_plan(hinfo, c, Choice(c["id"], video_codec=v["id"], hdr="preserve"))
                except PlanError as exc:
                    hdr["detail"][f"{c['id']}/{v['id']}"] = exc.message
                    continue
                ok, msg = _run(plan, hdr_clip, "mp4", work / f"hdr-{c['id']}-{v['id']}{RULES[c['id']].ext}", hinfo)
                hdr["detail"][f"{c['id']}/{v['id']}"] = msg
                if ok:
                    hdr["preserve"].append([c["id"], v["id"]])
        mp4 = next((c for c in containers if c["id"] == "mp4" and c["write"]["verified"]), None)
        if mp4:
            plan = build_plan(hinfo, mp4, Choice("mp4", video_codec="h264", hdr="tonemap"))
            out = work / "hdr-tonemap.mp4"
            ok, msg = _run(plan, hdr_clip, "mp4", out, hinfo)
            if ok:
                ok, msg = tonemap_looks_right(hdr_clip, out)
            hdr["tonemap"] = ok
            hdr["detail"]["tonemap"] = msg
    return {"combos": combos, "hdr": hdr, "clip": "1.2 s of a real clip (scaled to 640 px) with real audio",
            "sync": None if sync_base is None else f"flash/beep probe, tolerance ±{SYNC_TOLERANCE_S * 1000:.0f} ms"}


def signal_stats(path: Path) -> tuple[float, float]:
    """Average luma (YAVG) and saturation (SATAVG) over all frames, 8-bit scale."""
    import re
    res = proc.run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(path), "-vf",
                    "format=yuv420p,signalstats,metadata=print:file=-", "-f", "null", "-"], timeout=120)
    text = res.stdout.decode("utf-8", "replace") + res.stderr.decode("utf-8", "replace")
    y = [float(m) for m in re.findall(r"signalstats\.YAVG=([\d.]+)", text)]
    s = [float(m) for m in re.findall(r"signalstats\.SATAVG=([\d.]+)", text)]
    return (sum(y) / len(y) if y else 0.0), (sum(s) / len(s) if s else 0.0)


def tonemap_looks_right(hdr_src: Path, sdr_out: Path) -> tuple[bool, str]:
    """Guard against washed-out output: compare against what a naive (no tone mapping) conversion gives.
    A correct SDR conversion keeps clearly more saturation than the naive one."""
    naive = sdr_out.with_name("naive.mp4")
    proc.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(hdr_src), "-vf", "format=yuv420p", "-c:v", "libx264",
              "-crf", "18", str(naive)], timeout=120)
    y_tm, s_tm = signal_stats(sdr_out)
    y_nv, s_nv = signal_stats(naive)
    naive.unlink(missing_ok=True)
    ok = s_tm > s_nv * 1.8 and y_tm > 40
    return ok, f"tone-mapped saturation {s_tm:.1f} vs naive {s_nv:.1f}; luma {y_tm:.1f}"
