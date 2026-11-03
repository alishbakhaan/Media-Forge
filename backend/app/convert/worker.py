"""Conversion worker, run as a separate process so it can be time- and memory-limited.

    python -m app.convert.worker JOB.json   → prints a JSON result on stdout
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import time

from .image_ops import LOSSLESS_SOURCES, TARGETS, ConvertError, decode, encode, verify_output, zip_outputs
from .settings import SettingsError, parse


def run_image_job(job: dict) -> dict:
    started = time.time()
    try:
        opts = parse(job["options"], lossless_source=job["source_format"] in LOSSLESS_SOURCES,
                     max_dim=TARGETS[job["options"]["format"]].max_dim if job["options"].get("format") in TARGETS else 65535)
    except SettingsError as exc:
        raise ConvertError("bad_request", str(exc)) from exc
    work = Path(job["work"])
    decoded = decode(Path(job["input"]), job["source_format"], job["backend"], opts, job["limits"], work)
    # Leave headroom inside the worker timeout for writing, verifying and previews.
    deadline = started + job.get("timeout_s", 120) * 0.7
    report = encode(decoded, opts, job.get("features", {}), work, job["stem"], deadline=deadline)
    files = []
    for out in report["outputs"]:
        v = verify_output(out["path"], opts.format, out["expected_frames"], out["expected_duration"], out["expected_loop"])
        files.append({"name": out["path"].name, "path": str(out["path"]), "size_bytes": out["path"].stat().st_size,
                      "settings": out["settings"], "target": out["target"], **v})
    result = {
        "kind": "image",
        "format": opts.format,
        "files": files,
        "notes": report["notes"],
        "alpha_in": report["has_alpha_in"],
        "alpha_out": report["alpha_out"],
        "icc_embedded": report["icc_embedded"],
        "metadata": report["metadata"],
        "previews": report["previews"],
        "settings": opts.public(),
        "source": {"frames": decoded.total_frames, "kind": decoded.kind, "orientation": decoded.orientation},
    }
    if len(files) > 1:
        z = zip_outputs([Path(f["path"]) for f in files], work / "out" / f"{job['stem']}-{opts.format}.zip")
        result["bundle"] = {"name": z.name, "path": str(z), "size_bytes": z.stat().st_size, "mime": "application/zip"}
    return result


def main() -> int:
    job = json.loads(Path(sys.argv[1]).read_text())
    try:
        result = run_image_job(job)
    except ConvertError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message}}))
        return 3
    except MemoryError:
        print(json.dumps({"error": {"code": "limit_exceeded", "message": "The image needs more memory than this server allows."}}))
        return 3
    except Exception as exc:  # unexpected: report cleanly instead of a bare crash (details go to the server log)
        import traceback
        traceback.print_exc()
        print(json.dumps({"error": {"code": "internal", "message": f"Conversion failed unexpectedly ({type(exc).__name__})."}}))
        return 3
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
