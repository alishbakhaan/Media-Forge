"""Per-upload storage with automatic expiry.

    <work>/items/<id>/input.bin          uploaded bytes (fixed name; user names never hit disk)
    <work>/items/<id>/meta.json          original name, size, inspection report
    <work>/items/<id>/outputs/<oid>/     one conversion: out/ files + result.json

IDs are random 128-bit hex. Items are deleted by the sweeper once untouched for the TTL,
or immediately via DELETE /api/items/<id>.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path

_ID = re.compile(r"^[0-9a-f]{32}$")


class Store:
    def __init__(self, root: Path):
        self.root = root / "items"
        self.root.mkdir(parents=True, exist_ok=True)

    def new_id(self) -> str:
        return uuid.uuid4().hex

    def item_dir(self, item_id: str, create: bool = False) -> Path | None:
        if not _ID.match(item_id or ""):
            return None
        path = self.root / item_id
        if create:
            path.mkdir(mode=0o700)
            return path
        if not path.is_dir():
            return None
        now = time.time()
        os.utime(path, (now, now))  # keep alive while in use
        return path

    def write_meta(self, item_id: str, meta: dict) -> None:
        d = self.root / item_id
        (d / "meta.json").write_text(json.dumps(meta))

    def read_meta(self, item_id: str) -> dict | None:
        d = self.item_dir(item_id)
        if not d or not (d / "meta.json").exists():
            return None
        return json.loads((d / "meta.json").read_text())

    def output_dir(self, item_id: str, output_id: str) -> Path | None:
        d = self.item_dir(item_id)
        if not d or not _ID.match(output_id or ""):
            return None
        out = d / "outputs" / output_id
        return out if out.is_dir() else None

    def delete(self, item_id: str) -> bool:
        d = self.item_dir(item_id)
        if not d:
            return False
        shutil.rmtree(d, ignore_errors=True)
        return True
