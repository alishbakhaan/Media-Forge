"""Independent-decoder check: converted files must open in a real browser (Chromium via Playwright)
and show the same pixels Pillow sees. Skipped when Playwright is not installed.

Run from backend/:  python3 -m unittest tests.test_browser -v
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image

BACKEND = Path(__file__).resolve().parent.parent
SAMPLES = BACKEND.parent / "samples"

try:
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover
    async_playwright = None

BROWSER_FORMATS = ["jpeg", "png", "webp", "avif", "gif", "bmp", "ico"]


@unittest.skipUnless(async_playwright and (SAMPLES / "real").is_dir(), "needs Playwright + samples")
class BrowserDecodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            cls.port = s.getsockname()[1]
        cls.work = Path(tempfile.mkdtemp(prefix="mf-br-"))
        cls.server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(cls.port)], cwd=BACKEND,
                                      env={**os.environ, "MF_WORK_DIR": str(cls.work)}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.base = f"http://127.0.0.1:{cls.port}"
        for _ in range(300):
            try:
                urllib.request.urlopen(cls.base + "/api/capabilities")
                break
            except Exception:
                time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        cls.server.wait(10)
        shutil.rmtree(cls.work, ignore_errors=True)

    def _post(self, path, data, headers):
        req = urllib.request.Request(self.base + path, data=data, method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)

    def _convert_all(self, rel: str) -> dict[str, str]:
        up = self._post("/api/uploads", (SAMPLES / rel).read_bytes(), {"X-Filename": urllib.parse.quote(Path(rel).name)})
        urls = {}
        for fid in BROWSER_FORMATS:
            if fid in {o["id"] for o in up["outputs"]}:
                res = self._post(f"/api/items/{up['id']}/convert", json.dumps({"format": fid, "background": "#ffffff"}).encode(),
                                 {"Content-Type": "application/json"})
                urls[fid] = res["files"][0]["url"]
        return urls

    async def _decode_in_browser(self, urls: dict[str, str]) -> dict:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page(bypass_csp=True)
            await page.goto(self.base + "/api/health")
            out = {}
            for fid, url in urls.items():
                out[fid] = await page.evaluate(
                    """async (url) => {
                        const img = new Image();
                        img.src = url;
                        try { await img.decode(); } catch (e) { return {error: String(e)}; }
                        const c = document.createElement('canvas');
                        c.width = img.naturalWidth; c.height = img.naturalHeight;
                        const ctx = c.getContext('2d');
                        ctx.drawImage(img, 0, 0);
                        const pts = [[0.25, 0.25], [0.5, 0.5], [0.75, 0.6]].map(([fx, fy]) =>
                          Array.from(ctx.getImageData(Math.floor(fx * c.width), Math.floor(fy * c.height), 1, 1).data));
                        return {w: img.naturalWidth, h: img.naturalHeight, pts};
                    }""", url)
            await browser.close()
            return out

    def _compare(self, rel: str):
        urls = self._convert_all(rel)
        decoded = asyncio.run(self._decode_in_browser(urls))
        for fid, got in decoded.items():
            with self.subTest(source=rel, format=fid):
                self.assertNotIn("error", got, f"Chromium could not decode {fid}: {got}")
                data = urllib.request.urlopen(self.base + urllib.parse.quote(urls[fid])).read()
                im = Image.open(io.BytesIO(data))
                if fid == "ico":
                    self.assertGreater(got["w"], 0)
                    continue
                im = im.convert("RGBA")
                self.assertEqual((got["w"], got["h"]), im.size)
                for (fx, fy), px in zip([(0.25, 0.25), (0.5, 0.5), (0.75, 0.6)], got["pts"]):
                    ours = im.getpixel((int(fx * im.width), int(fy * im.height)))
                    if ours[3] < 255:  # canvas un-premultiplies, so compare colour only where opaque
                        self.assertLessEqual(abs(ours[3] - px[3]), 2, f"{fid} alpha mismatch {ours} vs {px}")
                        continue
                    self.assertLessEqual(max(abs(a - b) for a, b in zip(ours, px)), 12, f"{fid} pixel mismatch {ours} vs {px}")

    def test_photo_outputs_open_in_chromium(self):
        self._compare("real/jpeg/retina.jpg")

    def test_transparent_outputs_open_in_chromium(self):
        self._compare("real/png/basn6a16.png")  # real alpha gradient; JPEG/BMP get a white background

    def test_rotated_photo_outputs_open_in_chromium(self):
        self._compare("derived/rotated-exif6-landscape.jpg")


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(async_playwright and (SAMPLES / "derived/clip.mov").exists(), "needs Playwright + samples")
class BrowserVideoTests(unittest.TestCase):
    """Independent decoder for video outputs: Chromium must load them, report the right displayed size and
    duration, and our codec string must match what Chromium says it can play."""

    @classmethod
    def setUpClass(cls):
        from tests.test_milestone4 import Server
        cls.s = Server()

    @classmethod
    def tearDownClass(cls):
        cls.s.close()

    def _convert(self, rel, **opts):
        st, up = self.s.upload(rel)
        assert st == 200, up
        j = self.s.convert(up["id"], **opts)
        assert j["state"] == "done", j
        return j["result"]

    async def _load(self, items):
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page(bypass_csp=True)
            await page.goto(self.s.base + "/api/health")
            out = []
            for res in items:
                out.append(await page.evaluate(
                    """async ([url, type]) => {
                        const v = document.createElement('video');
                        const can = v.canPlayType(type);
                        v.muted = true; v.preload = 'auto'; v.src = url;
                        const ok = await new Promise((r) => { v.onloadeddata = () => r(true); v.onerror = () => r(false); setTimeout(() => r(false), 15000); });
                        return {can, ok, w: v.videoWidth, h: v.videoHeight, d: v.duration, err: v.error && v.error.code};
                    }""", [res["preview"]["url"], f'{res["preview"]["mime"]}; codecs="{res["preview"]["codecs"]}"']))
            await browser.close()
            return out

    def test_chromium_plays_web_outputs(self):
        # Playwright's Chromium is an open-source build without H.264/AAC, so only royalty-free outputs can be
        # decoded here; H.264/AAC playback in Chrome/Safari is not verified by this suite.
        cases = [
            ("derived/clip.mov", {"container": "webm"}, (1280, 720)),
            ("derived/clip.mov", {"container": "mp4", "video_codec": "av1", "audio_codec": "opus"}, (1280, 720)),
            ("derived/clip.mov", {"container": "mp4", "video_codec": "vp9", "audio_codec": "opus"}, (1280, 720)),
            ("derived/rotated-90.mp4", {"container": "webm"}, (720, 1280)),
            ("derived/rotated-90.mp4", {"container": "mp4", "video_codec": "av1", "audio_codec": "opus"}, (720, 1280)),
            ("derived/vfr.mp4", {"container": "webm"}, (1280, 720)),
            ("derived/clip.mov", {"container": "ogv"}, (1280, 720)),
        ]
        results = [self._convert(rel, **o) for rel, o, _ in cases]
        loaded = asyncio.run(self._load(results))
        for (rel, opts, dims), res, got in zip(cases, results, loaded):
            with self.subTest(source=rel, **opts):
                self.assertIn(got["can"], ("probably", "maybe"), res["preview"])
                self.assertTrue(got["ok"], got)
                self.assertEqual((got["w"], got["h"]), dims)
                self.assertAlmostEqual(got["d"], res["files"][0]["duration"], delta=0.15)

    def test_chromium_cannot_play_is_reported(self):
        # Our codec string must make canPlayType say "no" where the browser really can't decode, so the UI
        # falls back to the poster + download instead of a broken player.
        for opts in ({"container": "avi"}, {"container": "mp4"}, {"container": "mkv", "video_codec": "hevc"}):
            with self.subTest(**opts):
                res = self._convert("derived/clip.mov", **opts)
                got = asyncio.run(self._load([res]))[0]
                if got["can"] == "":
                    self.assertIsNotNone(res["preview"]["poster"])
                else:
                    self.assertTrue(got["ok"], got)
