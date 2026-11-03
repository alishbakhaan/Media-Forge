"""Background video jobs: a bounded queue, worker threads, live FFmpeg progress, cancellation,
time/disk limits, duplicate-submission protection and verification before download.

Jobs live in memory; their files live in the upload's directory (items/<id>/outputs/<job>/).
A server restart loses queued/running jobs (finished outputs stay downloadable) — see README.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .. import proc
from ..config import Settings
from ..store import Store
from . import gif
from .plan import RULES, Choice, PlanError, build_plan, ffmpeg_args
from .probe import probe
from .verify import VerifyError, browser_codecs, poster, verify

ACTIVE = ("queued", "running", "verifying")


class JobError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, details: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.status, self.details = code, message, status, details or {}


@dataclass
class Job:
    id: str
    item_id: str
    key: str
    container: str
    plan: dict
    choice: dict
    src_info: dict
    source_container: str | None
    created: float = field(default_factory=time.time)
    state: str = "queued"
    progress: float = 0.0
    speed: float | None = None
    started: float | None = None
    finished: float | None = None
    error: dict | None = None
    result: dict | None = None
    cancel_requested: bool = False
    proc: subprocess.Popen | None = None
    kind: str = "video"                 # "video" | "to_gif" | "from_gif" (then `choice` holds the GIF spec)

    def public(self, queue_pos: int | None = None) -> dict:
        eta = None
        if self.state == "running" and self.started and self.progress > 0.02:
            el = time.time() - self.started
            eta = round(el / self.progress - el, 1)
        return {"id": self.id, "item_id": self.item_id, "state": self.state, "progress": round(self.progress, 4),
                "eta_s": eta, "speed": self.speed, "queue_position": queue_pos, "container": self.container,
                "plan": self.plan, "error": self.error, "result": self.result,
                "created": self.created, "started": self.started, "finished": self.finished}


class JobManager:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self.jobs: dict[str, Job] = {}
        self.by_key: dict[str, str] = {}
        self.queue: deque[str] = deque()
        self.cv = threading.Condition()
        self.threads: list[threading.Thread] = []
        self.stopping = False

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        for i in range(self.settings.limits.max_concurrent_video_jobs):
            t = threading.Thread(target=self._worker, name=f"video-worker-{i}", daemon=True)
            t.start()
            self.threads.append(t)

    def stop(self) -> None:
        with self.cv:
            self.stopping = True
            self.cv.notify_all()
        for job in list(self.jobs.values()):
            if job.state in ACTIVE:
                self._kill(job)

    # ------------------------------------------------------------------ API
    def plan(self, item_id: str, raw: dict, caps: dict) -> tuple[dict, dict, Choice, dict]:
        meta = self.store.read_meta(item_id)
        if not meta:
            raise JobError("not_found", "This upload has expired or does not exist. Upload the file again.", 404)
        report = meta["report"]
        if report["kind"] == "image" and report["format"]["id"] == "gif":
            item = self.store.item_dir(item_id)
            try:
                planned = gif.plan_from_gif(item / "input.bin", raw, caps["video"])
            except PlanError as exc:
                raise JobError(exc.code, exc.message, 400) from exc
            return meta, {"duration": planned["spec"]["duration"]}, None, planned["public"] | {"_gif": planned["spec"]}
        if report["kind"] != "video":
            raise JobError("bad_request", "This item is not a video.", 400)
        if not report.get("within_limits", True):
            raise JobError("limit_exceeded", "This video is over the configured limits: " +
                           "; ".join(report["video"].get("limit_violations", [])), 413)
        try:
            choice = Choice.parse(raw)
        except PlanError as exc:
            raise JobError(exc.code, exc.message, 400) from exc
        cont = next((c for c in caps["video"]["containers"] if c["id"] == choice.container), None)
        if cont is None and choice.container != "gif":
            raise JobError("unsupported_output", f"'{choice.container}' is not a verified output container.", 400)
        src_info = meta.get("video_probe") or report["video"]["probe"]
        if choice.container == "gif":
            try:
                planned = gif.plan_to_gif(src_info, raw, caps["video"])
            except PlanError as exc:
                raise JobError(exc.code, exc.message, 400) from exc
            return meta, src_info, None, planned["public"] | {"_gif": planned["spec"]}
        try:
            plan = build_plan(src_info, cont, choice)
        except PlanError as exc:
            raise JobError(exc.code, exc.message, 400, exc.details) from exc
        return meta, src_info, choice, plan.public() | {"_plan": plan}

    def submit(self, item_id: str, raw: dict, caps: dict) -> tuple[Job, bool]:
        meta, src_info, choice, planned = self.plan(item_id, raw, caps)
        spec = planned.pop("_gif", None)
        if spec is None:
            plan = planned.pop("_plan")
            if plan.dropped and not choice.confirm_drops:
                raise JobError("confirm_drops", "Some streams or data cannot be kept in this format. Review the list and confirm.",
                               400, {"dropped": plan.dropped})
        stored = spec if spec is not None else choice.__dict__
        key = hashlib.sha256(json.dumps([item_id, stored], sort_keys=True).encode()).hexdigest()
        limits = self.settings.limits
        with self.cv:
            existing = self.jobs.get(self.by_key.get(key, ""))
            if existing and existing.state in (*ACTIVE, "done"):
                return existing, True
            if sum(1 for j in self.jobs.values() if j.state == "queued") >= limits.max_queued_jobs:
                raise JobError("queue_full", "Too many conversions are waiting. Try again when one finishes.", 429)
            free = shutil.disk_usage(self.store.root).free
            if free < limits.min_free_disk_mb * 1024 * 1024:
                raise JobError("low_disk", "The server is low on disk space; conversions are paused.", 507)
            job = Job(uuid.uuid4().hex, item_id, key, planned["container"], planned, stored, src_info,
                      meta["report"]["format"]["id"], kind=spec["kind"] if spec else "video")
            self.jobs[job.id] = job
            self.by_key[key] = job.id
            self.queue.append(job.id)
            self.cv.notify()
        return job, False

    def get(self, job_id: str) -> dict | None:
        job = self.jobs.get(job_id)
        if not job:
            return None
        pos = None
        with self.cv:
            if job.state == "queued" and job.id in self.queue:
                pos = list(self.queue).index(job.id) + 1
        return job.public(pos)

    def cancel(self, job_id: str) -> dict | None:
        job = self.jobs.get(job_id)
        if not job:
            return None
        with self.cv:
            if job.state == "queued":
                self.queue.remove(job.id)
                job.state, job.finished = "cancelled", time.time()
                self._cleanup(job)
                return job.public()
            if job.state in ("running", "verifying"):
                job.cancel_requested = True
        if job.state in ("running", "verifying"):
            self._kill(job)
            for _ in range(100):
                if job.state not in ("running", "verifying"):
                    break
                time.sleep(0.05)
        return job.public()

    def cancel_item(self, item_id: str) -> int:
        """Cancel every queued or running job of an upload (used before the upload is deleted)."""
        ids = [j.id for j in list(self.jobs.values()) if j.item_id == item_id and j.state in (*ACTIVE,)]
        for jid in ids:
            self.cancel(jid)
        return len(ids)

    # ------------------------------------------------------------------ internals
    def _out_dir(self, job: Job) -> Path:
        return self.store.root / job.item_id / "outputs" / job.id

    def _cleanup(self, job: Job) -> None:
        shutil.rmtree(self._out_dir(job), ignore_errors=True)
        self.by_key.pop(job.key, None)

    def _kill(self, job: Job) -> None:
        p = job.proc
        if p and p.poll() is None:
            proc.kill_tree(p.pid, graceful=True)
            try:
                p.wait(3)
            except subprocess.TimeoutExpired:
                proc.kill_tree(p.pid)

    def _worker(self) -> None:
        while True:
            with self.cv:
                while not self.queue and not self.stopping:
                    self.cv.wait()
                if self.stopping:
                    return
                job = self.jobs[self.queue.popleft()]
                job.state, job.started = "running", time.time()
            try:
                self._run(job)
            except JobError as exc:
                self._fail(job, exc.code, exc.message)
            except Exception as exc:  # never kill the worker thread
                self._fail(job, "internal", f"Unexpected error ({type(exc).__name__}).")

    def _fail(self, job: Job, code: str, message: str) -> None:
        if job.cancel_requested:
            job.state, job.error = "cancelled", None
        else:
            job.state, job.error = "failed", {"code": code, "message": message}
        job.finished = time.time()
        self._cleanup(job)

    def _run(self, job: Job) -> None:
        limits = self.settings.limits
        item_dir = self.store.item_dir(job.item_id)
        if not item_dir:
            raise JobError("not_found", "The upload expired before the conversion started.")
        meta = self.store.read_meta(job.item_id)
        out_dir = self._out_dir(job)
        (out_dir / "out").mkdir(parents=True)
        stem = _stem(meta["name"])
        plan = None
        if job.kind == "to_gif":
            mime, out = "image/gif", out_dir / "out" / f"{stem}.gif"
            cmd = gif.to_gif_args(job.choice, item_dir / "input.bin", job.source_container, out)
        elif job.kind == "from_gif":
            mime, out = "video/mp4", out_dir / "out" / f"{stem}.mp4"
            cmd = gif.from_gif_args(job.choice, item_dir / "input.bin", out)
        else:
            rules = RULES[job.container]
            mime, out = rules.mime, out_dir / "out" / f"{stem}{rules.ext}"
            plan = build_plan(job.src_info, self._caps_row(job.container), Choice(**job.choice))
            cmd = ffmpeg_args(plan, item_dir / "input.bin", job.source_container, out)
        log = open(out_dir / "ffmpeg.log", "wb")
        job.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log, cwd=out_dir,
                                    env=proc._child_env(), **proc.group_kwargs())
        duration = job.choice.get("duration") if job.kind != "video" else job.src_info.get("duration") or 0
        state = {"out_us": 0, "speed": None}
        reader = threading.Thread(target=_read_progress, args=(job.proc.stdout, state), daemon=True)
        reader.start()
        disk_limit = limits.max_job_disk_mb * 1024 * 1024
        min_free = limits.min_free_disk_mb * 1024 * 1024
        deadline = job.started + limits.video_timeout_s
        reason = None
        while job.proc.poll() is None:
            time.sleep(0.25)
            if duration:
                job.progress = min(0.99, state["out_us"] / 1e6 / duration)
            job.speed = state["speed"]
            if job.cancel_requested:
                reason = "cancelled"
            elif time.time() > deadline:
                reason = "timeout"
            elif out.exists() and out.stat().st_size > disk_limit:
                reason = "disk"
            elif shutil.disk_usage(out_dir).free < min_free:
                reason = "low_disk"
            if reason:
                self._kill(job)
                break
            os.utime(item_dir)  # keep the upload alive while it is being converted
        job.proc.wait()
        reader.join(2)
        log.close()
        if reason == "cancelled" or job.cancel_requested:
            raise JobError("cancelled", "Cancelled.")
        if reason == "timeout":
            raise JobError("timeout", f"Conversion took longer than {limits.video_timeout_s}s and was stopped.")
        if reason == "disk":
            raise JobError("disk_limit", f"The output grew beyond the {limits.max_job_disk_mb} MB per-job disk limit and was stopped.")
        if reason == "low_disk":
            raise JobError("low_disk", "The server ran low on disk space, so the conversion was stopped and its files deleted.")
        if job.proc.returncode != 0 or not out.exists():
            err = (out_dir / "ffmpeg.log").read_bytes().decode("utf-8", "replace").strip().splitlines()
            msg = err[-1] if err else f"exit code {job.proc.returncode}"
            raise JobError("ffmpeg_failed", "FFmpeg could not convert this file: " + msg.replace(str(item_dir), "")[:300])
        job.state = "verifying"
        job.progress = 1.0
        remaining = max(30.0, deadline - time.time())
        try:
            if job.kind == "to_gif":
                ver = gif.verify_gif(out, job.choice, timeout=remaining)
                ver["info"] = None
            elif job.kind == "from_gif":
                ver, info = gif.verify_from_gif(out, job.choice, timeout=remaining)
                ver["info"] = info
                ver["details"]["streams"] = info["streams"]
            else:
                ver = verify(out, job.container, plan.public(), job.src_info, timeout=remaining)
        except (VerifyError, PlanError) as exc:
            raise JobError("verify_failed", exc.message) from exc
        except proc.ProcessTimeout as exc:
            raise JobError("timeout", "Verifying the output took too long.") from exc
        if job.cancel_requested:
            raise JobError("cancelled", "Cancelled.")
        pdir = out_dir / "out" / ".preview"
        pdir.mkdir()
        is_gif = job.kind == "to_gif"
        has_poster = not is_gif and poster(out, pdir / "poster.jpg", ver["details"].get("duration"), timeout=60)
        base = f"/api/items/{job.item_id}/outputs/{job.id}"
        size = out.stat().st_size
        original = meta["size_bytes"]
        file_entry = {"name": out.name, "url": f"{base}/{out.name}", "size_bytes": size, "mime": mime,
                      "checks": ver["checks"], "audio": [], "subtitles": [], "chapters": 0,
                      **{k: v for k, v in ver["details"].items() if k != "streams"}}
        public = job.plan if plan is None else plan.public()
        result = {
            "id": job.id, "kind": "gif" if is_gif else "video", "format": job.container,
            "label": "GIF" if is_gif else self._caps_row(job.container)["label"],
            "files": [file_entry], "plan": public, "notes": public["notes"],
            "original_size": original, "output_size": size,
            "change_percent": round((size - original) / original * 100, 1) if original else None,
            "larger_than_original": size > original, "original_url": f"/api/items/{job.item_id}/original",
            "streams": ver["details"].get("streams", []),
            "preview": {"url": file_entry["url"], "mime": mime, "image": is_gif,
                        "codecs": "" if is_gif else browser_codecs(ver["info"]),
                        "poster": f"{base}/.preview/poster.jpg" if has_poster else None},
            "elapsed_s": round(time.time() - job.started, 1),
        }
        for p in out_dir.iterdir():
            if p.name != "out":
                p.unlink(missing_ok=True) if p.is_file() else shutil.rmtree(p, ignore_errors=True)
        (out_dir / "result.json").write_text(json.dumps(result))
        job.result, job.state, job.finished = result, "done", time.time()

    def _caps_row(self, container: str) -> dict:
        return next(c for c in self.caps["video"]["containers"] if c["id"] == container)

    caps: dict = {}


def _read_progress(stream, state: dict) -> None:
    for raw in iter(stream.readline, b""):
        line = raw.decode("utf-8", "replace").strip()
        if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
            try:
                state["out_us"] = max(state["out_us"], int(line.split("=", 1)[1]))
            except ValueError:
                pass
        elif line.startswith("speed="):
            try:
                state["speed"] = float(line.split("=", 1)[1].rstrip("x"))
            except ValueError:
                pass


def _stem(name: str) -> str:
    import re
    stem = Path(name).stem if name else "video"
    stem = re.sub(r"[^\w.\- ]+", "_", stem).strip(" ._")[:80]
    return stem or "video"
