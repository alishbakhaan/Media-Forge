"""Runtime configuration. Every limit can be overridden with an environment variable."""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BACKEND_DIR.parent


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:  # fail loudly on bad config
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be positive, got {value}")
    return value


@dataclass(frozen=True)
class Limits:
    max_upload_mb: int
    max_image_megapixels: int
    max_video_seconds: int
    max_video_width: int
    max_video_height: int
    max_pdf_pages: int
    probe_timeout_s: int
    process_timeout_s: int
    file_ttl_minutes: int
    max_concurrent_probes: int
    max_decoded_megapixels: int
    image_timeout_s: int
    worker_memory_mb: int
    max_concurrent_jobs: int
    max_concurrent_video_jobs: int
    max_queued_jobs: int
    video_timeout_s: int
    max_job_disk_mb: int
    min_free_disk_mb: int

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    def public(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Settings:
    limits: Limits
    work_dir: Path
    cache_dir: Path
    frontend_dist: Path
    samples_dir: Path
    require_real_samples: bool
    imagemagick_policy_dir: Path = field(default=BACKEND_DIR / "app" / "imagemagick")


def load_settings() -> Settings:
    work_dir = Path(os.environ.get("MF_WORK_DIR", PROJECT_DIR / "var" / "work")).resolve()
    cache_dir = Path(os.environ.get("MF_CACHE_DIR", PROJECT_DIR / "var" / "cache")).resolve()
    limits = Limits(
        max_upload_mb=_int("MF_MAX_UPLOAD_MB", 500),
        max_image_megapixels=_int("MF_MAX_IMAGE_MEGAPIXELS", 100),
        max_video_seconds=_int("MF_MAX_VIDEO_SECONDS", 3600),
        max_video_width=_int("MF_MAX_VIDEO_WIDTH", 7680),
        max_video_height=_int("MF_MAX_VIDEO_HEIGHT", 4320),
        max_pdf_pages=_int("MF_MAX_PDF_PAGES", 500),
        probe_timeout_s=_int("MF_PROBE_TIMEOUT_S", 30),
        process_timeout_s=_int("MF_PROCESS_TIMEOUT_S", 900),
        file_ttl_minutes=_int("MF_FILE_TTL_MINUTES", 60),
        max_concurrent_probes=_int("MF_MAX_CONCURRENT_PROBES", 4),
        max_decoded_megapixels=_int("MF_MAX_DECODED_MEGAPIXELS", 400),
        image_timeout_s=_int("MF_IMAGE_TIMEOUT_S", 120),
        worker_memory_mb=_int("MF_WORKER_MEMORY_MB", 3072),
        max_concurrent_jobs=_int("MF_MAX_CONCURRENT_JOBS", 2),
        max_concurrent_video_jobs=_int("MF_MAX_CONCURRENT_VIDEO_JOBS", 1),
        max_queued_jobs=_int("MF_MAX_QUEUED_JOBS", 10),
        video_timeout_s=_int("MF_VIDEO_TIMEOUT_S", 3600),
        max_job_disk_mb=_int("MF_MAX_JOB_DISK_MB", 8192),
        min_free_disk_mb=_int("MF_MIN_FREE_DISK_MB", 1024),
    )
    frontend_dist = Path(os.environ.get("MF_FRONTEND_DIST", PROJECT_DIR / "frontend" / "dist")).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    samples_dir = Path(os.environ.get("MF_SAMPLES_DIR", PROJECT_DIR / "samples")).resolve()
    require_real = os.environ.get("MF_REQUIRE_REAL_SAMPLES", "1") != "0"
    return Settings(limits=limits, work_dir=work_dir, cache_dir=cache_dir, frontend_dist=frontend_dist,
                    samples_dir=samples_dir, require_real_samples=require_real)
