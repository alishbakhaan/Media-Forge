"""HTTP API (Starlette; see README for why not FastAPI yet)."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from urllib.parse import unquote

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import proc
from .capabilities import Registry
from .config import load_settings
from .convert.service import ServiceError, convert_image, output_file
from .inspect import InspectError, inspect_file
from .store import Store
from .video.jobs import JobError, JobManager

log = logging.getLogger("mediaforge")
settings = load_settings()
registry = Registry(settings.cache_dir, settings.work_dir, settings.samples_dir, settings.require_real_samples)
_probe_slots = asyncio.Semaphore(settings.limits.max_concurrent_probes)
_job_slots = asyncio.Semaphore(settings.limits.max_concurrent_jobs)
store = Store(settings.work_dir)
jobs = JobManager(settings, store)


class _TooLarge(Exception):
    pass


async def _receive(request: Request, target) -> int:
    """Stream the raw request body to `target`, enforcing the upload limit."""
    limit = settings.limits.max_upload_bytes
    received = 0
    with open(target, "wb") as fh:
        async for chunk in request.stream():
            received += len(chunk)
            if received > limit:
                raise _TooLarge()
            fh.write(chunk)
    return received


def _error(code: str, message: str, status: int, **extra) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, **extra}}, status_code=status)


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "capabilities": registry.status})


async def capabilities(request: Request) -> JSONResponse:
    if registry.status == "detecting":
        return JSONResponse({"status": "detecting"}, status_code=503, headers={"Retry-After": "2"})
    if registry.status == "error":
        return _error("capability_detection_failed", registry.error or "unknown error", 500)
    data = dict(registry.get() or {})
    data["status"] = "ready"
    data["limits"] = settings.limits.public()
    return JSONResponse(data)


async def inspect(request: Request) -> JSONResponse:
    """Body = raw file bytes. Header X-Filename = URL-encoded original name (display only)."""
    caps = registry.get()
    if caps is None:
        return _error("not_ready", "The server is still detecting its capabilities. Try again in a few seconds.", 503)
    limit = settings.limits.max_upload_bytes
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        return _error("too_large", f"File exceeds the {settings.limits.max_upload_mb} MB upload limit.", 413)
    name = unquote(request.headers.get("x-filename", "upload"))[:255] or "upload"

    async with _probe_slots:
        with proc.scratch_dir(settings.work_dir, prefix="inspect-") as work:
            target = work / "input.bin"  # fixed name: the user's file name never touches disk or argv
            try:
                received = await _receive(request, target)
            except _TooLarge:
                return _error("too_large", f"File exceeds the {settings.limits.max_upload_mb} MB upload limit.", 413)
            if received == 0:
                return _error("empty", "No file data received.", 400)
            try:
                report = await run_in_threadpool(inspect_file, target, name, caps, settings.limits)
            except InspectError as exc:
                return _error(exc.code, exc.message, exc.status, detected=exc.detected, name=name, size_bytes=received)
            except proc.ProcessTimeout:
                return _error("timeout", "Analysing the file took too long.", 422, name=name)
            except Exception:  # never leak tracebacks to clients
                log.exception("inspect failed")
                return _error("internal", "Unexpected error while analysing the file.", 500, name=name)
    return JSONResponse(report)


async def upload(request: Request) -> JSONResponse:
    """Store an upload for conversion and return its inspection report plus an item id."""
    caps = registry.get()
    if caps is None:
        return _error("not_ready", "The server is still detecting its capabilities. Try again in a few seconds.", 503)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > settings.limits.max_upload_bytes:
        return _error("too_large", f"File exceeds the {settings.limits.max_upload_mb} MB upload limit.", 413)
    name = unquote(request.headers.get("x-filename", "upload"))[:255] or "upload"
    item_id = store.new_id()
    item = store.item_dir(item_id, create=True)
    keep = False
    try:
        async with _probe_slots:
            try:
                received = await _receive(request, item / "input.bin")
            except _TooLarge:
                return _error("too_large", f"File exceeds the {settings.limits.max_upload_mb} MB upload limit.", 413)
            if received == 0:
                return _error("empty", "No file data received.", 400)
            try:
                report = await run_in_threadpool(inspect_file, item / "input.bin", name, caps, settings.limits)
            except InspectError as exc:
                return _error(exc.code, exc.message, exc.status, detected=exc.detected, name=name, size_bytes=received)
            except proc.ProcessTimeout:
                return _error("timeout", "Analysing the file took too long.", 422, name=name)
            except Exception:
                log.exception("upload inspect failed")
                return _error("internal", "Unexpected error while analysing the file.", 500, name=name)
        store.write_meta(item_id, {"name": name, "size_bytes": received, "report": report})
        keep = True
        return JSONResponse({**report, "id": item_id, "expires_in_minutes": settings.limits.file_ttl_minutes})
    finally:
        if not keep:
            store.delete(item_id)


async def convert(request: Request) -> JSONResponse:
    caps = registry.get()
    if caps is None:
        return _error("not_ready", "The server is still starting.", 503)
    item_id = request.path_params["item_id"]
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError
    except Exception:
        return _error("bad_request", "Expected a JSON object.", 400)
    async with _job_slots:
        try:
            result = await run_in_threadpool(convert_image, store, item_id, body, caps, settings)
        except ServiceError as exc:
            return _error(exc.code, exc.message, exc.status)
        except Exception:
            log.exception("convert failed")
            return _error("internal", "Unexpected error during conversion.", 500)
    return JSONResponse(result)


async def _json_body(request: Request) -> dict | None:
    try:
        body = await request.json()
        return body if isinstance(body, dict) else None
    except Exception:
        return None


async def doc_tool(request: Request) -> JSONResponse:
    """Body: {"items": [upload ids, in order], "options": {...}}. Runs one document tool and returns its result."""
    from .docs import DocError, run_tool
    body = await _json_body(request)
    if body is None or not isinstance(body.get("items"), list) or not all(isinstance(i, str) for i in body["items"]):
        return _error("bad_request", "Expected {items: [...], options: {...}}.", 400)
    async with _job_slots:
        try:
            result = await run_in_threadpool(run_tool, store, request.path_params["tool"], body["items"], body.get("options") or {})
        except DocError as exc:
            return _error(exc.code, exc.message, exc.status)
        except Exception:
            log.exception("document tool failed")
            return _error("internal", "Unexpected error while processing the document.", 500)
    return JSONResponse(result)


async def video_plan(request: Request) -> JSONResponse:
    caps = registry.get()
    body = await _json_body(request)
    if caps is None or body is None:
        return _error("bad_request", "Expected a JSON object.", 400)
    try:
        _, _, _, planned = await run_in_threadpool(jobs.plan, request.path_params["item_id"], body, caps)
    except JobError as exc:
        return _error(exc.code, exc.message, exc.status, **exc.details)
    planned.pop("_plan", None)
    planned.pop("_gif", None)
    return JSONResponse(planned)


async def video_submit(request: Request) -> JSONResponse:
    caps = registry.get()
    body = await _json_body(request)
    if caps is None or body is None:
        return _error("bad_request", "Expected a JSON object.", 400)
    jobs.caps = caps
    try:
        job, duplicate = await run_in_threadpool(jobs.submit, request.path_params["item_id"], body, caps)
    except JobError as exc:
        return _error(exc.code, exc.message, exc.status, **exc.details)
    data = jobs.get(job.id)
    data["duplicate"] = duplicate
    return JSONResponse(data, status_code=200 if duplicate else 202)


async def job_status(request: Request) -> JSONResponse:
    data = jobs.get(request.path_params["job_id"])
    return JSONResponse(data) if data else _error("not_found", "Unknown job.", 404)


async def job_cancel(request: Request) -> JSONResponse:
    data = await run_in_threadpool(jobs.cancel, request.path_params["job_id"])
    return JSONResponse(data) if data else _error("not_found", "Unknown job.", 404)


async def download(request: Request):
    found = output_file(store, request.path_params["item_id"], request.path_params["output_id"], request.path_params["name"])
    if not found or not found[0].is_file():
        return _error("not_found", "This file has expired or does not exist.", 404)
    path, mime = found
    return FileResponse(path, media_type=mime, filename=path.name, headers={"Cache-Control": "no-store"})


async def preview(request: Request):
    name = ".preview/" + request.path_params["name"]
    found = output_file(store, request.path_params["item_id"], request.path_params["output_id"], name)
    if not found:
        return _error("not_found", "Preview not available.", 404)
    return FileResponse(found[0], media_type=found[1], headers={"Cache-Control": "no-store"})


async def original(request: Request):
    """The uploaded file stays available until it expires or is deleted (e.g. when an output came out larger)."""
    item_id = request.path_params["item_id"]
    meta = store.read_meta(item_id)
    d = store.item_dir(item_id)
    if not meta or not d:
        return _error("not_found", "This upload has expired or does not exist.", 404)
    from .capabilities.images import BY_ID as IMG
    mime = "application/octet-stream"
    fid = meta["report"]["format"]["id"]
    if meta["report"]["kind"] == "image" and fid in IMG:
        mime = IMG[fid].mime
    return FileResponse(d / "input.bin", media_type=mime, filename=meta["name"], headers={"Cache-Control": "no-store"})


async def delete_item(request: Request) -> JSONResponse:
    item_id = request.path_params["item_id"]
    await run_in_threadpool(jobs.cancel_item, item_id)
    return JSONResponse({"deleted": store.delete(item_id)})


def _sweep_all(ttl: float) -> int:
    removed = proc.sweep_expired(store.root, ttl)
    for entry in settings.work_dir.iterdir():
        if entry != store.root and entry.is_dir() and entry.name.startswith(("inspect-", "caps-", "cli-", "test-", "job-")):
            removed += proc.sweep_expired_one(entry, ttl)
    return removed


async def _sweeper() -> None:
    ttl = settings.limits.file_ttl_minutes * 60
    while True:
        removed = await run_in_threadpool(_sweep_all, ttl)
        if removed:
            log.info("removed %d expired work items", removed)
        await asyncio.sleep(min(300, max(30, ttl / 4)))


@contextlib.asynccontextmanager
async def lifespan(app: Starlette):
    registry.start(force=os.environ.get("MF_REFRESH_CAPABILITIES") == "1")
    jobs.start()
    task = asyncio.create_task(_sweeper())
    try:
        yield
    finally:
        task.cancel()
        jobs.stop()


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'",
        )
        return response


routes = [
    Route("/api/health", health),
    Route("/api/capabilities", capabilities),
    Route("/api/inspect", inspect, methods=["POST"]),
    Route("/api/uploads", upload, methods=["POST"]),
    Route("/api/items/{item_id}", delete_item, methods=["DELETE"]),
    Route("/api/items/{item_id}/convert", convert, methods=["POST"]),
    Route("/api/items/{item_id}/original", original),
    Route("/api/docs/{tool}", doc_tool, methods=["POST"]),
    Route("/api/items/{item_id}/video/plan", video_plan, methods=["POST"]),
    Route("/api/items/{item_id}/video/jobs", video_submit, methods=["POST"]),
    Route("/api/jobs/{job_id}", job_status),
    Route("/api/jobs/{job_id}/cancel", job_cancel, methods=["POST"]),
    Route("/api/items/{item_id}/outputs/{output_id}/.preview/{name}", preview),
    Route("/api/items/{item_id}/outputs/{output_id}/{name}", download),
]
if settings.frontend_dist.is_dir():
    routes.append(Mount("/", StaticFiles(directory=settings.frontend_dist, html=True), name="frontend"))
else:
    async def _no_frontend(request: Request) -> HTMLResponse:
        return HTMLResponse("<p>Frontend not built. Run <code>npm run build</code> in <code>frontend/</code>.</p>")
    routes.append(Route("/", _no_frontend))

app = Starlette(routes=routes, lifespan=lifespan, middleware=[Middleware(SecurityHeaders)])
