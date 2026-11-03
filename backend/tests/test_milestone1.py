"""Milestone 1 tests: content sniffing, capability verification, file inspection, HTTP API.

Run from backend/:  python3 -m unittest discover -s tests -v
Requires the sample corpus:  python3 ../scripts/build_samples.py
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
ROOT = BACKEND.parent
SAMPLES = ROOT / "samples"
sys.path.insert(0, str(BACKEND))

from app import proc  # noqa: E402
from app.capabilities import Registry  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.inspect import InspectError, inspect_file  # noqa: E402
from app.sniff import sniff_file  # noqa: E402

SETTINGS = load_settings()
CAPS = Registry(SETTINGS.cache_dir, SETTINGS.work_dir, SAMPLES, True).detect()


def inspect(rel: str) -> dict:
    src = SAMPLES / rel
    with proc.scratch_dir(SETTINGS.work_dir, prefix="test-") as work:
        target = work / "input.bin"
        shutil.copyfile(src, target)
        return inspect_file(target, src.name, CAPS, SETTINGS.limits)


def inspect_error(rel: str) -> InspectError:
    try:
        inspect(rel)
    except InspectError as exc:
        return exc
    raise AssertionError(f"{rel} unexpectedly inspected fine")


@unittest.skipUnless((SAMPLES / "real").is_dir(), "run scripts/build_samples.py first")
class SniffTests(unittest.TestCase):
    EXPECT = {
        "real/jpeg/retina.jpg": ("image", "jpeg"), "real/png/logo.png": ("image", "png"), "real/gif/flowers.gif": ("image", "gif"),
        "real/tiff/multipage.tif": ("image", "tiff"), "real/ico/favicon.ico": ("image", "ico"), "real/svg/folder-home.svg": ("image", "svg"),
        "real/mp4/example-movie.mp4": ("video", "mp4"), "real/pdf/qhv-test.pdf": ("pdf", "pdf"),
        "derived/photo.webp": ("image", "webp"), "derived/photo.avif": ("image", "avif"), "derived/photo.bmp": ("image", "bmp"),
        "derived/photo.jp2": ("image", "jpeg2000"), "derived/photo.qoi": ("image", "qoi"), "derived/animated.apng": ("image", "png"),
        "derived/clip.mov": ("video", "mov"), "derived/clip.webm": ("video", "webm"), "derived/multistream.mkv": ("video", "mkv"),
        "derived/clip.avi": ("video", "avi"), "derived/clip.mpg": ("video", "mpeg"), "derived/clip.ts": ("video", "mpegts"),
        "derived/clip.ogv": ("video", "ogv"), "derived/clip.flv": ("video", "flv"), "derived/clip.3gp": ("video", "3gp"),
        "derived/clip.wmv": ("video", "asf"), "damaged/photo-named-as.png": ("image", "jpeg"), "damaged/not-really.jpg": ("unknown", "unknown"),
    }

    def test_magic_bytes(self):
        for rel, (kind, fmt) in self.EXPECT.items():
            with self.subTest(rel):
                s = sniff_file(SAMPLES / rel)
                self.assertEqual((s.kind, s.format_id), (kind, fmt))


@unittest.skipUnless((SAMPLES / "real").is_dir(), "run scripts/build_samples.py first")
class CapabilityTests(unittest.TestCase):
    def fmt(self, fid):
        return next(f for f in CAPS["images"]["formats"] if f["id"] == fid)

    def test_read_and_write_lists_are_separate(self):
        img = CAPS["images"]
        self.assertIn("svg", img["readable"])
        self.assertNotIn("svg", img["writable"])
        self.assertIn("dds", img["writable"])       # Pillow writes it ...
        self.assertNotIn("dds", img["readable"])    # ... but no independent sample proves reading

    def test_priority_formats(self):
        for fid in ("jpeg", "png", "webp", "avif", "gif", "bmp", "tiff", "ico"):
            with self.subTest(fid):
                self.assertIn(fid, CAPS["images"]["readable"])
                self.assertIn(fid, CAPS["images"]["writable"])

    def test_heic_not_advertised_without_working_codec(self):
        for fid in ("heic", "heif"):
            row = self.fmt(fid)
            if row["read"] is None:
                self.assertNotIn(fid, CAPS["images"]["readable"])
            self.assertIsNone(row["write"], "HEIC export must stay off without a verified encoder")

    def test_raw_requires_real_file(self):
        row = self.fmt("raw")
        real_raw = [p for p in (SAMPLES / "real").rglob("*") if p.is_file() and sniff_file(p).format_id == "raw"]
        if not real_raw:
            self.assertNotIn("raw", CAPS["images"]["readable"])
            self.assertTrue(any("real file" in n for n in row["notes"]))

    def test_imagemagick_fake_heic_output_is_detected(self):
        """ImageMagick 6 exits 0 but writes PNG bytes when asked for HEIC; we must not trust exit codes."""
        if not shutil.which("convert"):
            self.skipTest("ImageMagick not installed")
        with tempfile.TemporaryDirectory() as d:
            src, out = Path(d) / "a.png", Path(d) / "a.heic"
            from PIL import Image
            Image.new("RGB", (8, 8)).save(src)
            subprocess.run(["convert", str(src), str(out)], capture_output=True)  # system policy, as a user would
            if out.exists():
                self.assertNotEqual(sniff_file(out).format_id, "heic")

    def test_gif_transparency_is_binary(self):
        # Milestone 2 pipeline keeps 1-bit GIF transparency; full alpha is not claimed.
        self.assertTrue(self.fmt("gif")["features"]["alpha_binary"])

    def test_animation_and_multipage_flags(self):
        for fid in ("gif", "webp", "png", "avif"):
            self.assertTrue(self.fmt(fid)["features"]["animation_write"], fid)
        self.assertTrue(self.fmt("tiff")["features"]["multipage_write"])
        self.assertFalse(self.fmt("jpeg")["features"]["animation_write"])

    def test_every_advertised_format_has_evidence(self):
        for f in CAPS["images"]["formats"]:
            if f["id"] in CAPS["images"]["readable"]:
                ev = f["real_samples"]
                self.assertGreater(ev["passed"] + ev["derived_passed"], 0, f["id"])
            if f["id"] in CAPS["images"]["writable"]:
                self.assertTrue(f["write_evidence"]["passed"], f["id"])

    def test_video_containers_and_codecs(self):
        vid = CAPS["video"]
        for cid in ("mp4", "mov", "mkv", "webm", "avi", "mpeg"):
            self.assertIn(cid, vid["readable"])
            self.assertIn(cid, vid["writable"])
        webm = next(c for c in vid["containers"] if c["id"] == "webm")
        self.assertEqual({v["id"] for v in webm["write"]["video_codecs"]} - {"vp9", "vp8", "av1"}, set())
        self.assertEqual(webm["write"]["default_video"], "vp9")
        self.assertNotIn("asf", vid["writable"])  # input only

    def test_pdf_backends(self):
        for name in ("pillow", "pikepdf", "pypdfium2"):
            self.assertTrue(CAPS["pdf"]["libraries"][name]["verified"], name)
        self.assertTrue(all(not op["implemented"] for op in CAPS["pdf"]["operations"]))


@unittest.skipUnless((SAMPLES / "real").is_dir(), "run scripts/build_samples.py first")
class InspectTests(unittest.TestCase):
    def test_every_corpus_file(self):
        known_bad = {"real/tiff/multipage_rgb.tif"}  # float64 planar scientific TIFF: Pillow cannot read it
        for tier in ("real", "derived"):
            for p in sorted((SAMPLES / tier).rglob("*")):
                rel = str(p.relative_to(SAMPLES))
                if not p.is_file() or p.name.startswith(".") or "audio-source" in rel or p.suffix in (".md", ".txt") or rel in known_bad:
                    continue
                kind = sniff_file(p).kind
                fmt = sniff_file(p).format_id
                if kind == "image" and fmt not in CAPS["images"]["readable"]:
                    continue  # not advertised; covered by the 'unsupported' test
                with self.subTest(rel):
                    try:
                        r = inspect(rel)
                    except InspectError as exc:
                        # Allowed only when the format is deliberately not advertised (e.g. SGI from FFmpeg).
                        self.assertEqual(exc.code, "unsupported")
                        self.assertNotIn(sniff_file(p).format_id, CAPS["images"]["readable"])
                        continue
                    self.assertIn(r["kind"], ("image", "video", "pdf"))
                    if r["kind"] != "pdf":
                        self.assertTrue(r["outputs"], "advertised input must have outputs")

    def test_known_unreadable_tiff_reports_damage_not_crash(self):
        e = inspect_error("real/tiff/multipage_rgb.tif")
        self.assertEqual(e.code, "damaged")

    def test_cmyk_and_gray_jpeg(self):
        self.assertEqual(inspect("real/jpeg/video-001.cmyk.jpeg")["image"]["mode"], "CMYK")
        self.assertEqual(inspect("real/jpeg/video-005.gray.jpeg")["image"]["mode"], "L")

    def test_orientation_and_icc(self):
        r = inspect("derived/rotated-exif6-icc.jpg")["image"]
        self.assertEqual(r["exif_orientation"], 6)
        self.assertEqual(r["display_size"], [r["height"], r["width"]])
        self.assertIn("sRGB", r["icc_profile"])

    def test_transparency_and_16bit(self):
        r = inspect("real/png/basn6a16.png")["image"]
        self.assertTrue(r["has_alpha"])
        out = {o["id"]: o for o in inspect("real/png/basn6a16.png")["outputs"]}
        self.assertEqual(out["jpeg"]["transparency"]["mode"], "none")
        self.assertEqual(out["gif"]["transparency"]["mode"], "binary")
        self.assertEqual(out["webp"]["transparency"]["mode"], "full")

    def test_animated_vs_static_vs_multipage(self):
        a = inspect("real/gif/no_time_for_that_tiny.gif")
        self.assertTrue(a["image"]["animated"])
        outs = {o["id"]: o for o in a["outputs"]}
        self.assertEqual(outs["webp"]["frame_options"][0]["id"], "keep")
        self.assertEqual([o["id"] for o in outs["jpeg"]["frame_options"]], ["first", "separate"])
        self.assertFalse(inspect("real/jpeg/retina.jpg")["image"]["animated"])
        m = inspect("real/tiff/multipage.tif")["image"]
        self.assertTrue(m["multipage"])
        self.assertGreater(m["frames"], 1)
        self.assertTrue(inspect("derived/animated.webp")["image"]["animated"])
        self.assertTrue(inspect("derived/animated.apng")["image"]["animated"])

    def test_svg_rasterised(self):
        r = inspect("real/svg/folder-home.svg")
        self.assertTrue(r["image"]["vector"])
        self.assertNotIn("svg", [o["id"] for o in r["outputs"]])

    def test_only_writable_outputs_offered(self):
        ids = {o["id"] for o in inspect("real/jpeg/retina.jpg")["outputs"]}
        self.assertEqual(ids, set(CAPS["images"]["writable"]))
        self.assertNotIn("heic", ids)

    def test_video_without_audio(self):
        r = inspect("real/mp4/example-movie.mp4")
        self.assertFalse(r["video"]["has_audio"])
        self.assertTrue(all(o["audio_codecs"] == [] for o in r["outputs"]))
        self.assertEqual((r["video"]["width"], r["video"]["height"]), (1280, 720))

    def test_multistream(self):
        r = inspect("derived/multistream.mkv")["video"]
        self.assertEqual(r["counts"], {"video": 1, "audio": 2, "subtitle": 1, "data": 0})
        self.assertEqual([s["language"] for s in r["streams"] if s["type"] == "audio"], ["eng", "fra"])

    def test_rotation_metadata(self):
        self.assertIn(inspect("derived/rotated-90.mp4")["video"]["rotation"], (90, -90))

    def test_pdf(self):
        r = inspect("real/pdf/example-image-a4-numbered.pdf")
        self.assertEqual(r["pdf"]["pages"], 12)
        self.assertFalse(any(op["available"] for op in r["pdf_operations"]))

    def test_errors(self):
        self.assertEqual(inspect_error("damaged/password.pdf").code, "encrypted")
        self.assertEqual(inspect_error("damaged/truncated.pdf").code, "damaged")
        self.assertEqual(inspect_error("damaged/truncated.mp4").code, "damaged")
        self.assertEqual(inspect_error("damaged/not-really.jpg").code, "unsupported")
        for rel in ("damaged/invalid-zlib.png", "damaged/invalid-crc32.png"):
            with self.subTest(rel):
                self.assertIn(inspect_error(rel).code, ("damaged", "unsupported"))

    def test_tolerable_corruption(self):
        # Go's strict decoders reject these; Pillow/libjpeg decode them (missing progressive scans,
        # missing trailing chunk). They are accepted, as most image tools do.
        for rel in ("damaged/video-001.progressive.truncated.jpeg", "damaged/invalid-trunc.png"):
            with self.subTest(rel):
                self.assertEqual(inspect(rel)["kind"], "image")

    def test_extension_mismatch(self):
        r = inspect("damaged/photo-named-as.png")
        self.assertEqual(r["format"]["id"], "jpeg")
        self.assertTrue(r["extension_mismatch"])
        self.assertFalse(inspect("real/jpeg/retina.jpg")["extension_mismatch"])

    def test_resolution_limit(self):
        from dataclasses import replace
        small = replace(SETTINGS.limits, max_image_megapixels=1)
        with proc.scratch_dir(SETTINGS.work_dir) as work:
            shutil.copyfile(SAMPLES / "real/png/astronaut.png", work / "input.bin")  # 512x512 → fine
            self.assertTrue(inspect_file(work / "input.bin", "a.png", CAPS, small)["within_limits"])
            from PIL import Image
            with Image.open(SAMPLES / "real/jpeg/hubble_deep_field.jpg") as im:
                im.resize((1600, 1400)).save(work / "input.bin", format="JPEG")  # 2.24 MP
            r = inspect_file(work / "input.bin", "h.jpg", CAPS, small)
            self.assertFalse(r["within_limits"])


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless((SAMPLES / "real").is_dir(), "run scripts/build_samples.py first")
class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = _free_port()
        cls.work = Path(tempfile.mkdtemp(prefix="mf-api-"))
        env = {**os.environ, "MF_MAX_UPLOAD_MB": "2", "MF_WORK_DIR": str(cls.work)}
        cls.server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(cls.port)],
                                      cwd=BACKEND, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.base = f"http://127.0.0.1:{cls.port}"
        for _ in range(200):
            try:
                with urllib.request.urlopen(cls.base + "/api/capabilities") as r:
                    if r.status == 200:
                        break
            except (urllib.error.URLError, ConnectionError):
                pass
            time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        cls.server.wait(10)
        shutil.rmtree(cls.work, ignore_errors=True)

    def post(self, path: Path, name: str | None = None):
        req = urllib.request.Request(self.base + "/api/inspect", data=path.read_bytes(), method="POST",
                                     headers={"X-Filename": urllib.parse.quote(name or path.name), "Content-Type": "application/octet-stream"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r), r.headers
        except urllib.error.HTTPError as e:
            return e.code, json.load(e), e.headers

    def test_capabilities_endpoint(self):
        with urllib.request.urlopen(self.base + "/api/capabilities") as r:
            data = json.load(r)
            self.assertEqual(data["status"], "ready")
            self.assertEqual(data["limits"]["max_upload_mb"], 2)
            self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")

    def test_inspect_ok_and_cleanup(self):
        status, body, _ = self.post(SAMPLES / "real/jpeg/rocket.jpg", "rocket ü.jpg")
        self.assertEqual(status, 200)
        self.assertEqual(body["name"], "rocket ü.jpg")
        leftovers = [p for p in self.work.iterdir() if p.name.startswith("inspect-")]
        self.assertEqual(leftovers, [], "upload temp dirs must be deleted")

    def test_upload_limit(self):
        status, body, _ = self.post(SAMPLES / "real/png/astronaut.png")  # ~770 KB: under 2 MB
        self.assertEqual(status, 200)
        big = self.work / "big.bin"
        big.write_bytes(b"\0" * (3 * 1024 * 1024))
        status, body, _ = self.post(big)
        self.assertEqual(status, 413)
        self.assertEqual(body["error"]["code"], "too_large")

    def test_unsupported_message(self):
        status, body, _ = self.post(SAMPLES / "damaged/not-really.jpg")
        self.assertEqual(status, 415)
        self.assertIn("Unrecognised", body["error"]["message"])

    def test_frontend_served(self):
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn(b'<div id="root">', r.read())


if __name__ == "__main__":
    unittest.main()
