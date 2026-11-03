"""Capability registry: runs the self-tests once, caches the result per toolchain version."""
from __future__ import annotations

import hashlib
import importlib.metadata as md
import json
import threading
import time
from pathlib import Path

from .. import proc
from . import images, pdf, samples, video

SCHEMA_VERSION = 6  # bump when detection logic changes


def _pkg_version(name: str) -> str | None:
    try:
        return md.version(name)
    except md.PackageNotFoundError:
        return None


def _pkg_license(name: str) -> str | None:
    try:
        meta = md.metadata(name)
    except md.PackageNotFoundError:
        return None
    return meta.get("License-Expression") or (meta.get("License") or "").splitlines()[0][:60] or None


def _samples_signature(samples_dir: Path | None) -> str:
    if not samples_dir or not samples_dir.is_dir():
        return "no-samples"
    entries = sorted(f"{p.relative_to(samples_dir)}:{p.stat().st_size}:{int(p.stat().st_mtime)}"
                     for p in samples_dir.rglob("*") if p.is_file())
    return hashlib.sha256("\n".join(entries).encode()).hexdigest()[:12]


def _code_signature() -> str:
    """Detection results depend on this code; any edit re-runs the self-tests."""
    app_dir = Path(__file__).resolve().parent.parent
    files = sorted([*app_dir.glob("capabilities/*.py"), *app_dir.glob("video/*.py"), app_dir / "convert" / "image_ops.py",
                    app_dir / "fixtures.py", app_dir / "sniff.py"])
    h = hashlib.sha256()
    for f in files:
        h.update(f.read_bytes())
    return h.hexdigest()[:12]


def _fingerprint(samples_dir: Path | None = None, require_real: bool = True) -> str:
    parts = [str(SCHEMA_VERSION), _code_signature(), _samples_signature(samples_dir), f"require_real={require_real}"]
    for pkg in ("pillow", "pillow-heif", "pikepdf", "pypdfium2", "tifffile", "onnxruntime"):
        parts.append(f"{pkg}={_pkg_version(pkg)}")
    from ..convert import bgremove
    parts.append(f"bg-model={bgremove.model_path().exists()}")
    for cmd in (["ffmpeg"] if proc.which("ffmpeg") else None, proc.imagemagick("convert")):
        if cmd:
            res = proc.run([*cmd, "-version"], timeout=15)
            parts.append(res.stdout.decode("utf-8", "replace").splitlines()[0] if res.stdout else cmd[0])
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def licenses(caps: dict) -> list[dict]:
    rows = []
    for pkg, role in (("starlette", "web framework"), ("uvicorn", "ASGI server"), ("pillow", "image codecs"),
                      ("pillow-heif", "HEIC/HEIF codec"), ("pikepdf", "PDF editing"), ("pypdfium2", "PDF rendering"),
                      ("tifffile", "DNG test sample (dev)"), ("numpy", "DNG test sample (dev)")):
        ver = _pkg_version(pkg)
        if ver:
            rows.append({"name": pkg, "version": ver, "license": _pkg_license(pkg), "role": role, "source": "package metadata"})
    ff = caps["video"].get("ffmpeg")
    if ff:
        rows.append({"name": "FFmpeg", "version": ff["version"], "license": ff["license"], "role": "video, SVG rasterising",
                     "source": "ffmpeg -version configuration"})
    im = caps["images"]["backends"].get("imagemagick")
    if im:
        rows.append({"name": "ImageMagick", "version": im["version"], "license": "ImageMagick License (Apache-2.0 style)",
                     "role": "camera RAW decode (LibRaw: LGPL-2.1 / CDDL-1.0)", "source": "known licence"})
    return rows


class Registry:
    def __init__(self, cache_dir: Path, work_dir: Path, samples_dir: Path | None = None, require_real: bool = True):
        self.cache_dir = cache_dir
        self.work_dir = work_dir
        self.samples_dir = samples_dir
        self.require_real = require_real
        self._data: dict | None = None
        self._error: str | None = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def status(self) -> str:
        if self._data is not None:
            return "ready"
        if self._error:
            return "error"
        return "detecting"

    @property
    def error(self) -> str | None:
        return self._error

    def get(self) -> dict | None:
        return self._data

    def start(self, force: bool = False) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, args=(force,), name="capability-detect", daemon=True)
            self._thread.start()

    def wait(self, timeout: float | None = None) -> dict | None:
        if self._thread:
            self._thread.join(timeout)
        return self._data

    def _run(self, force: bool) -> None:
        try:
            self._data = self.detect(force)
        except Exception as exc:  # surfaced through /api/capabilities
            self._error = f"{type(exc).__name__}: {exc}"

    def detect(self, force: bool = False) -> dict:
        fp = _fingerprint(self.samples_dir, self.require_real)
        cache_file = self.cache_dir / f"capabilities-{fp}.json"
        if not force and cache_file.exists():
            try:
                data = json.loads(cache_file.read_text())
                data["from_cache"] = True
                return data
            except (OSError, json.JSONDecodeError):
                pass
        started = time.time()
        with proc.scratch_dir(self.work_dir, prefix="caps-") as work:
            (work / "img").mkdir()
            (work / "vid").mkdir()
            (work / "pdf").mkdir()
            img = images.detect(work / "img")
            vid = video.detect(work / "vid")
            pdfs = pdf.detect(work / "pdf")
            (work / "samples").mkdir()
            sample_files = samples.collect(self.samples_dir) if self.samples_dir else []
            image_rows = {r["id"]: r for r in img["formats"]}
            video_rows = {c["id"]: c for c in vid.get("containers", [])}
            inputs = samples.verify_inputs(sample_files, image_rows, video_rows, work / "samples")
            (work / "img-out").mkdir()
            img_out = samples.verify_image_outputs(sample_files, img["formats"], work / "img-out")
            vid_out = samples.verify_video_outputs(sample_files, vid.get("containers", []), video.BY_ID, work / "samples") if vid.get("available") else {}
            caps_partial = {"images": img, "video": vid, "pdf": pdfs}
            samples.apply_policy(caps_partial, inputs, img_out, vid_out, self.require_real)
            # Background removal: offered only after it cuts a real photo out cleanly here.
            from ..convert import bgremove
            cut_photo = next((f["path"] for f in sample_files if f["kind"] == "image" and f["tier"] == "real"
                              and "astronaut" in f["path"].name), None) or next(
                (f["path"] for f in sample_files if f["kind"] == "image" and f["tier"] == "real" and f["format"] in ("png", "jpeg")), None)
            img["remove_background"] = bgremove.selftest(cut_photo)
            # Video conversions: every offered container × codec pair, HDR paths (Milestone 4).
            if vid.get("available"):
                from ..video import selftest
                real_vid = next((f["path"] for f in sample_files if f["kind"] == "video" and f["tier"] == "real"), None)
                wav = next(iter(sorted((self.samples_dir / "real" / "audio-source").glob("*.wav"))), None) \
                    if self.samples_dir and (self.samples_dir / "real" / "audio-source").is_dir() else None
                photo = next((f["path"] for f in sample_files if f["kind"] == "image" and f["format"] == "png" and f["tier"] == "real"
                              and "astronaut" in f["path"].name), None) or next(
                    (f["path"] for f in sample_files if f["kind"] == "image" and f["tier"] == "real" and f["format"] in ("png", "jpeg")), None)
                vt = work / "video-selftest"
                vt.mkdir()
                if real_vid and selftest.make_test_clip(real_vid, wav, vt / "clip.mkv"):
                    hdr_clip = vt / "hdr.mp4" if photo and selftest.make_hdr_clip(photo, vt / "hdr.mp4") else None
                    sync_clip = vt / "sync.mkv" if selftest.make_sync_clip(vt / "sync.mkv") else None
                    selftest.apply(vid, selftest.run_selftests(vid["containers"], vt / "clip.mkv", hdr_clip, vt, sync_clip))
                else:
                    for c in vid["containers"]:
                        if c["write"]["verified"]:
                            c["write"]["verified"] = False
                            c["notes"] = c["notes"] + ["Withdrawn: no real clip available for the conversion test."]
                    vid["writable"] = []
        data = {
            "fingerprint": fp,
            "detected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "detect_seconds": round(time.time() - started, 2),
            "images": img,
            "video": vid,
            "pdf": pdfs,
            "real_samples": {
                "dir": str(self.samples_dir) if self.samples_dir else None,
                "files": sum(1 for f in sample_files if f["tier"] == "real"),
                "derived_files": sum(1 for f in sample_files if f["tier"] == "derived"),
                "policy": "real-or-derived sample required; HEIC/HEIF/RAW need a real file" if self.require_real else "self-tests only (MF_REQUIRE_REAL_SAMPLES=0)",
                "unrecognised": [f["rel"] for f in sample_files if f["kind"] == "unknown"],
            },
        }
        data["licenses"] = licenses(data)
        for old in self.cache_dir.glob("capabilities-*.json"):
            old.unlink(missing_ok=True)
        cache_file.write_text(json.dumps(data, indent=1))
        data["from_cache"] = False
        return data
