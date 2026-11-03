"""Command-line helpers.

    python -m app.cli capabilities [--refresh] [--json]
    python -m app.cli inspect FILE
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .capabilities import Registry
from .config import load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capabilities", help="detect and print format support")
    c.add_argument("--refresh", action="store_true", help="ignore the cache and re-run all self-tests")
    c.add_argument("--json", action="store_true")
    i = sub.add_parser("inspect", help="inspect one file like the web UI does")
    i.add_argument("file", type=Path)
    args = parser.parse_args(argv)

    settings = load_settings()
    caps = Registry(settings.cache_dir, settings.work_dir, settings.samples_dir, settings.require_real_samples).detect(force=getattr(args, "refresh", False))

    if args.cmd == "inspect":
        import shutil
        from . import proc
        from .inspect import InspectError, inspect_file
        with proc.scratch_dir(settings.work_dir, prefix="cli-") as work:
            target = work / "input.bin"
            shutil.copyfile(args.file, target)
            try:
                print(json.dumps(inspect_file(target, args.file.name, caps, settings.limits), indent=2))
            except InspectError as exc:
                print(json.dumps({"error": exc.code, "message": exc.message}, indent=2))
                return 2
        return 0

    if args.json:
        print(json.dumps(caps, indent=2))
        return 0
    img, vid = caps["images"], caps["video"]
    label = {f["id"]: f["label"] for f in img["formats"]}
    print(f"Detected in {caps['detect_seconds']}s (cached: {caps.get('from_cache')})")
    print("Images  read :", ", ".join(label[x] for x in img["readable"]))
    print("Images  write:", ", ".join(label[x] for x in img["writable"]))
    for f in img["formats"]:
        if f["priority"] and not (f["read"] and f["read"]["verified"] and f["write"]):
            for n in f["notes"]:
                print(f"  ! {f['label']}: {n}")
    print("Video   read :", ", ".join(vid["readable"]))
    print("Video   write:", ", ".join(vid["writable"]))
    for c in vid["containers"]:
        if c["write"]["verified"]:
            print(f"  {c['label']:<26} video: {', '.join(v['id'] for v in c['write']['video_codecs'])} | audio: {', '.join(a['id'] for a in c['write']['audio_codecs'])}")
    print("PDF libs     :", ", ".join(f"{k} {v.get('version')} ({'ok' if v.get('verified') else 'FAILED'})" for k, v in caps["pdf"]["libraries"].items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
