"""Safe subprocess execution and isolated working directories.

Rules enforced here:
* commands are argument lists, never shell strings;
* stdin is closed, output is captured and size-capped;
* every call has a timeout and the whole process group is killed on expiry;
* user-supplied file names never reach the command line (files are stored
  under fixed names such as ``input.bin`` inside a private temp directory).
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

from .config import BACKEND_DIR

_MAX_CAPTURE = 4 * 1024 * 1024
POLICY_DIR = BACKEND_DIR / "app" / "imagemagick"


class ProcessTimeout(RuntimeError):
    pass


@dataclass
class ProcResult:
    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def err_text(self, limit: int = 600) -> str:
        return self.stderr.decode("utf-8", "replace").strip()[-limit:]


def _child_env() -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        # ImageMagick: use our restrictive policy and hard resource caps.
        "MAGICK_CONFIGURE_PATH": str(POLICY_DIR),
        "MAGICK_MEMORY_LIMIT": os.environ.get("MF_MAGICK_MEMORY", "512MiB"),
        "MAGICK_MAP_LIMIT": os.environ.get("MF_MAGICK_MAP", "1GiB"),
        "MAGICK_DISK_LIMIT": os.environ.get("MF_MAGICK_DISK", "2GiB"),
        "MAGICK_TIME_LIMIT": os.environ.get("MF_MAGICK_TIME", "120"),
        "MAGICK_THREAD_LIMIT": os.environ.get("MF_MAGICK_THREADS", "2"),
    }
    if IS_WINDOWS:  # Windows programs need these to start at all
        for k in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "PATHEXT", "COMSPEC", "LOCALAPPDATA"):
            if k in os.environ:
                env[k] = os.environ[k]
    return env


IS_WINDOWS = sys.platform == "win32"


def group_kwargs() -> dict:
    """Popen arguments that put a child in its own process group, so it can be stopped with its children."""
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_tree(pid: int, graceful: bool = False) -> None:
    """Stop a process started with group_kwargs() and everything it started."""
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
        return
    try:
        os.killpg(pid, signal.SIGTERM if graceful else signal.SIGKILL)
    except ProcessLookupError:
        pass


def _limiter(memory_mb: int | None, cpu_s: float | None):
    if not memory_mb and not cpu_s:
        return None

    def apply() -> None:  # runs in the child before exec
        import resource
        if memory_mb and sys.platform != "darwin":
            limit = memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        if cpu_s:
            sec = int(cpu_s) + 1
            resource.setrlimit(resource.RLIMIT_CPU, (sec, sec + 1))
    return apply


def run(cmd: Sequence[str], *, timeout: float, cwd: Path | None = None, memory_mb: int | None = None,
        cpu_s: float | None = None, env_extra: dict[str, str] | None = None) -> ProcResult:
    if isinstance(cmd, (str, bytes)):
        raise TypeError("commands must be argument lists")
    env = _child_env()
    if env_extra:
        env.update(env_extra)
    proc = subprocess.Popen(
        list(cmd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
        env=env,
        # Own process group so we can kill children too. Windows has no rlimits: there the timeout is the only cap.
        **group_kwargs(),
        **({} if IS_WINDOWS else {"preexec_fn": _limiter(memory_mb, cpu_s)}),
    )
    try:
        if sys.platform == "darwin" and memory_mb:
            # macOS rejects RLIMIT_AS. Enforce a sampled resident-memory
            # budget across the worker process group instead (not a hard cap).
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(cmd, timeout)
                try:
                    out, err = proc.communicate(timeout=min(0.25, remaining))
                    break
                except subprocess.TimeoutExpired:
                    usage = subprocess.run(
                        ["/bin/ps", "-axo", "pgid=,rss="],
                        capture_output=True, text=True, timeout=2, check=True,
                    )
                    rss_kb = sum(int(fields[1]) for line in usage.stdout.splitlines()
                                 if len(fields := line.split()) == 2
                                 and fields[0] == str(proc.pid))
                    if rss_kb > memory_mb * 1024:
                        kill_tree(proc.pid)
                        out, err = proc.communicate()
                        return ProcResult(proc.returncode, out[:_MAX_CAPTURE],
                                          b"MemoryError: worker process group exceeded memory limit")
        else:
            out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc.pid)
        proc.communicate()
        raise ProcessTimeout(f"{Path(cmd[0]).name} exceeded {timeout:.0f}s")
    except Exception:
        # Fail closed if resource monitoring fails; never leave a worker running.
        kill_tree(proc.pid)
        proc.communicate()
        raise
    return ProcResult(proc.returncode, out[:_MAX_CAPTURE], err[:_MAX_CAPTURE])


def which(name: str) -> str | None:
    return shutil.which(name)


def imagemagick(tool: str) -> list[str] | None:
    """Command prefix for an ImageMagick tool: IM7 ('magick convert'-style) or IM6 ('convert')."""
    if shutil.which("magick"):
        return ["magick"] if tool == "convert" else ["magick", tool]
    if IS_WINDOWS:  # Windows ships an unrelated convert.exe (disk conversion); only trust "magick" there
        return None
    exe = shutil.which(tool)
    return [exe] if exe else None


# ---------------------------------------------------------------- work dirs

@contextmanager
def scratch_dir(root: Path, prefix: str = "job-") -> Iterator[Path]:
    """A private directory that is always removed afterwards."""
    root.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix=f"{prefix}{uuid.uuid4().hex[:8]}-", dir=root))
    os.chmod(path, 0o700)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def sweep_expired(root: Path, ttl_seconds: float) -> int:
    """Delete leftovers (e.g. after a crash) older than the TTL. Returns count removed."""
    if not root.exists():
        return 0
    removed = 0
    cutoff = time.time() - ttl_seconds
    for entry in root.iterdir():
        try:
            if entry.stat().st_mtime < cutoff:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
                removed += 1
        except FileNotFoundError:
            continue
    return removed


def sweep_expired_one(path: Path, ttl_seconds: float) -> int:
    """Remove a single scratch directory if it is older than the TTL."""
    try:
        if path.stat().st_mtime < time.time() - ttl_seconds:
            shutil.rmtree(path, ignore_errors=True)
            return 1
    except FileNotFoundError:
        pass
    return 0
