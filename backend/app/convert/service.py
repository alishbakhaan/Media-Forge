"""Runs an image conversion job in a sandboxed worker process and records the result."""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

import logging

from .. import proc

log = logging.getLogger("mediaforge.convert")
from ..config import BACKEND_DIR, Settings
from ..store import Store
from .image_ops import LOSSLESS_SOURCES, TARGETS
from .settings import SettingsError, parse


BG_REMOVAL_FORMATS = {"png", "webp", "avif", "tiff"}  # outputs with full transparency


class ServiceError(Exception):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def safe_stem(name: str) -> str:
    stem = Path(name).stem if name else "image"
    stem = re.sub(r"[^\w.\- ]+", "_", stem).strip(" ._")[:80]
    return stem or "image"


def convert_image(store: Store, item_id: str, raw: dict, caps: dict, settings: Settings) -> dict:
    meta = store.read_meta(item_id)
    if not meta:
        raise ServiceError("not_found", "This upload has expired or does not exist. Upload the file again.", 404)
    report = meta["report"]
    if report["kind"] != "image":
        raise ServiceError("bad_request", "Only images can be converted in this version.", 400)
    if not report.get("within_limits", True):
        raise ServiceError("limit_exceeded", "This file is over the configured limits.", 413)

    fmt = str(raw.get("format", "")).lower()
    offered = {o["id"]: o for o in report["outputs"]}
    if fmt not in offered or fmt not in caps["images"]["writable"] or fmt not in TARGETS:
        raise ServiceError("unsupported_output", f"'{fmt}' is not a verified output format for this file.", 400)
    choice = offered[fmt]
    info = report["image"]

    frames = raw.get("frames")
    if info.get("frames", 1) > 1:
        allowed = [o["id"] for o in choice.get("frame_options", [])]
        if frames not in allowed:
            raise ServiceError("frames_choice_required",
                               f"This {('animation' if info.get('animated') else 'document')} has {info['frames']} "
                               f"{'frames' if info.get('animated') else 'pages'}. Choose one of: {', '.join(allowed)}.", 400)
    else:
        frames = "keep"
    options = {k: raw.get(k) for k in ("format", "preset", "settings", "transparency", "background", "resize", "target",
                                       "metadata", "icc", "remove_background") if raw.get(k) is not None}
    if options.get("remove_background"):
        if not (caps["images"].get("remove_background") or {}).get("verified"):
            raise ServiceError("unsupported", "Background removal is not available on this server.", 400)
        if not (choice.get("transparency") is None or choice["transparency"]["mode"] == "full") or fmt not in BG_REMOVAL_FORMATS:
            raise ServiceError("bad_request", f"{choice['label']} cannot store transparency, so the background cannot be removed. "
                                              "Choose PNG or WebP.", 400)
        if info.get("frames", 1) > 1 and frames != "first":
            raise ServiceError("bad_request", "Background removal works on still images. Choose “first frame only”.", 400)
    options["format"] = fmt
    options["frames"] = frames
    try:  # validate here too so bad requests fail fast with a 400
        parse(options, lossless_source=report["format"]["id"] in LOSSLESS_SOURCES, max_dim=TARGETS[fmt].max_dim)
    except SettingsError as exc:
        raise ServiceError("bad_request", str(exc), 400) from exc

    row = next(f for f in caps["images"]["formats"] if f["id"] == report["format"]["id"])
    target_row = next(f for f in caps["images"]["formats"] if f["id"] == fmt)
    item = store.item_dir(item_id)
    oid = store.new_id()
    out_dir = item / "outputs" / oid
    out_dir.mkdir(parents=True)
    limits = settings.limits
    job = {
        "input": str(item / "input.bin"),
        "source_format": report["format"]["id"],
        "backend": row["read"]["backend"],
        "options": options,
        "timeout_s": limits.image_timeout_s,
        "features": target_row["features"],
        "limits": {"max_image_megapixels": limits.max_image_megapixels, "max_decoded_megapixels": limits.max_decoded_megapixels,
                   "probe_timeout_s": limits.probe_timeout_s},
        "work": str(out_dir),
        "stem": safe_stem(meta["name"]),
    }
    job_path = out_dir / "job.json"
    job_path.write_text(json.dumps(job))
    try:
        res = proc.run([sys.executable, "-m", "app.convert.worker", str(job_path)], cwd=BACKEND_DIR,
                       timeout=limits.image_timeout_s, memory_mb=limits.worker_memory_mb, cpu_s=limits.image_timeout_s)
    except proc.ProcessTimeout as exc:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise ServiceError("timeout", f"Conversion took longer than {limits.image_timeout_s}s and was stopped.") from exc
    try:
        data = json.loads(res.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        shutil.rmtree(out_dir, ignore_errors=True)
        log.warning("worker exited %s without a result: %s", res.returncode, res.err_text(1500))
        if b"MemoryError" in res.stderr or res.returncode < 0:
            raise ServiceError("limit_exceeded", "Conversion exceeded the memory or CPU limit and was stopped.")
        raise ServiceError("internal", "The converter stopped unexpectedly.", 500)
    if "error" in data:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise ServiceError(data["error"]["code"], data["error"]["message"])

    # Keep only the deliverables.
    for p in out_dir.iterdir():
        if p.name not in ("out",):
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)

    base = f"/api/items/{item_id}/outputs/{oid}"
    files = []
    for f in data["files"]:
        f.pop("path", None)
        f["url"] = f"{base}/{f['name']}"
        files.append(f)
    original = meta["size_bytes"]
    total = sum(f["size_bytes"] for f in files)
    pv = data.get("previews") or {}
    previews = {
        "before": f"{base}/.preview/{pv['before']}" if pv.get("before") else None,
        "after": files[0]["url"] if pv.get("after_is_output") else (f"{base}/.preview/{pv['after']}" if pv.get("after") else None),
        "width": pv.get("width"), "height": pv.get("height"), "scaled": pv.get("scaled", False),
    }
    larger = len(files) == 1 and total > original
    result = {
        "id": oid,
        "format": fmt,
        "label": target_row["label"],
        "files": files,
        "notes": data["notes"],
        "alpha_in": data["alpha_in"],
        "alpha_out": data["alpha_out"],
        "icc_embedded": data["icc_embedded"],
        "original_size": original,
        "output_size": total,
        "change_percent": round((total - original) / original * 100, 1) if original else None,
        "options": job["options"],
        "settings": data.get("settings"),
        "metadata": data.get("metadata"),
        "target": files[0].get("target") if len(files) == 1 else _combine_targets(files),
        "previews": previews,
        "larger_than_original": larger,
        "original_url": f"/api/items/{item_id}/original",
    }
    if "bundle" in data:
        b = data["bundle"]
        b.pop("path", None)
        b["url"] = f"{base}/{b['name']}"
        result["bundle"] = b
    (out_dir / "result.json").write_text(json.dumps(result))
    return result


def _combine_targets(files: list[dict]) -> dict | None:
    ts = [f.get("target") for f in files if f.get("target")]
    if not ts:
        return None
    met = sum(1 for t in ts if t["met"])
    return {"bytes": ts[0]["bytes"], "met": met == len(ts), "per_file": True,
            "message": f"Target applied to each file separately: met for {met} of {len(ts)} files."}


def output_file(store: Store, item_id: str, oid: str, name: str) -> tuple[Path, str] | None:
    out = store.output_dir(item_id, oid)
    if not out or not (out / "result.json").exists():
        return None
    if name in (".preview/before.png", ".preview/after.png", ".preview/poster.jpg"):
        p = out / "out" / name
        return (p, "image/jpeg" if name.endswith(".jpg") else "image/png") if p.is_file() else None
    result = json.loads((out / "result.json").read_text())
    for f in result["files"]:
        if f["name"] == name:
            return out / "out" / name, f["mime"]
    b = result.get("bundle")
    if b and b["name"] == name:
        return out / "out" / name, b["mime"]
    return None
