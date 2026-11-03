"""Milestone 2 tests: real image conversions through the HTTP API, downloaded and re-checked.

Run from backend/:  python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

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
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import replace
from pathlib import Path

from PIL import Image, ImageChops, ImageOps, ImageStat

BACKEND = Path(__file__).resolve().parent.parent
SAMPLES = BACKEND.parent / "samples"
sys.path.insert(0, str(BACKEND))

from app import fixtures, proc  # noqa: E402
from app.capabilities import Registry  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.convert.image_ops import TARGETS  # noqa: E402
from app.convert.service import ServiceError, convert_image  # noqa: E402
from app.inspect import inspect_file  # noqa: E402
from app.sniff import sniff_bytes  # noqa: E402
from app.store import Store  # noqa: E402

SETTINGS = load_settings()
CAPS = Registry(SETTINGS.cache_dir, SETTINGS.work_dir, SAMPLES, True).detect()
WRITABLE = CAPS["images"]["writable"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def mean_diff(a: Image.Image, b: Image.Image) -> float:
    return sum(ImageStat.Stat(ImageChops.difference(a.convert("RGB"), b.convert("RGB"))).mean) / 3


@unittest.skipUnless((SAMPLES / "real").is_dir(), "run scripts/build_samples.py first")
class ApiConversionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = _free_port()
        cls.work = Path(tempfile.mkdtemp(prefix="mf-m2-"))
        env = {**os.environ, "MF_WORK_DIR": str(cls.work)}
        cls.server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(cls.port)],
                                      cwd=BACKEND, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.base = f"http://127.0.0.1:{cls.port}"
        for _ in range(300):
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

    # -- helpers ---------------------------------------------------------------
    def request(self, method, path, data=None, headers=None):
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def upload(self, path: Path | None = None, data: bytes | None = None, name: str | None = None) -> tuple[int, dict]:
        body = data if data is not None else path.read_bytes()
        status, raw, _ = self.request("POST", "/api/uploads", body, {"X-Filename": urllib.parse.quote(name or path.name)})
        return status, json.loads(raw)

    def convert(self, item: str, **opts) -> tuple[int, dict]:
        status, raw, _ = self.request("POST", f"/api/items/{item}/convert", json.dumps(opts).encode(), {"Content-Type": "application/json"})
        return status, json.loads(raw)

    def download(self, url: str) -> tuple[bytes, dict]:
        status, raw, headers = self.request("GET", url)
        self.assertEqual(status, 200, url)
        return raw, headers

    def ok_upload(self, rel: str) -> dict:
        status, body = self.upload(SAMPLES / rel)
        self.assertEqual(status, 200, body)
        return body

    def check_download(self, f: dict, fid: str) -> Image.Image:
        data, headers = self.download(f["url"])
        t = TARGETS[fid]
        self.assertEqual(headers["Content-Type"].split(";")[0], t.mime)
        self.assertIn(f'filename="{f["name"]}"', headers["Content-Disposition"])
        self.assertTrue(f["name"].endswith(t.ext))
        self.assertEqual(len(data), f["size_bytes"])
        sniffed = sniff_bytes(data[:8192]).format_id
        if fid not in ("tga", "pcx", "sgi", "pnm"):  # no magic number: Pillow below confirms instead
            self.assertEqual(sniffed, fid)
        im = Image.open(io.BytesIO(data))
        im.load()
        return im

    # -- tests ---------------------------------------------------------------------
    def test_photo_to_every_verified_format(self):
        body = self.ok_upload("real/jpeg/retina.jpg")
        self.assertEqual({o["id"] for o in body["outputs"]}, set(WRITABLE))
        src = Image.open(SAMPLES / "real/jpeg/retina.jpg")
        for fid in WRITABLE:
            with self.subTest(fid):
                status, res = self.convert(body["id"], format=fid)
                self.assertEqual(status, 200, res)
                f = res["files"][0]
                im = self.check_download(f, fid)
                if fid not in ("ico", "icns"):
                    self.assertEqual(im.size, src.size)
                    self.assertLess(mean_diff(im, src), 6, "colours drifted")
                self.assertEqual(res["original_size"], (SAMPLES / "real/jpeg/retina.jpg").stat().st_size)
                self.assertEqual(res["output_size"], f["size_bytes"])

    def test_heic_and_raw_labels(self):
        with tempfile.TemporaryDirectory() as d:
            heic = fixtures.write_heic(Path(d) / "a.heic", Path(d))
            status, body = self.upload(heic)
            self.assertEqual(status, 415)
            self.assertIn("Unavailable in this environment", body["error"]["message"])
            dng = fixtures.write_linear_dng(Path(d) / "a.dng")
            status, body = self.upload(dng)
            self.assertEqual(status, 415)
            self.assertIn("Unverified—real camera samples needed", body["error"]["message"])
        rows = {f["id"]: f for f in CAPS["images"]["formats"]}
        self.assertEqual(rows["heic"]["status_label"], "Unavailable in this environment")
        self.assertEqual(rows["raw"]["status_label"], "Unverified—real camera samples needed")
        self.assertNotIn("heic", WRITABLE)

    def test_orientation_applied(self):
        body = self.ok_upload("derived/rotated-exif6-landscape.jpg")
        expected = ImageOps.exif_transpose(Image.open(SAMPLES / "derived/rotated-exif6-landscape.jpg"))
        for fid in ("png", "jpeg", "webp", "bmp"):
            with self.subTest(fid):
                _, res = self.convert(body["id"], format=fid)
                im = self.check_download(res["files"][0], fid)
                self.assertEqual(im.size, (400, 600))
                self.assertLess(mean_diff(im, expected), 4)
                self.assertIn(im.getexif().get(0x0112), (None, 1), "orientation tag must be reset")

    def test_cmyk_icc_colour_management(self):
        body = self.ok_upload("derived/cmyk-fogra39.jpg")
        ref = Image.open(SAMPLES / "derived/cmyk-fogra39-reference.png")
        naive = Image.open(SAMPLES / "derived/cmyk-fogra39.jpg").convert("RGB")
        for fid in ("png", "gif", "bmp"):
            with self.subTest(fid):
                _, res = self.convert(body["id"], format=fid)
                im = self.check_download(res["files"][0], fid)
                limit = 9 if fid == "gif" else 6  # GIF adds palette error
                self.assertLess(mean_diff(im, ref), limit)
                self.assertLess(mean_diff(im, ref), mean_diff(naive, ref))
                self.assertIn("CMYK converted to sRGB using the embedded profile", res["notes"])

    def test_icc_profile_kept_or_baked(self):
        body = self.ok_upload("derived/rotated-exif6-icc.jpg")  # sRGB profile embedded
        _, res = self.convert(body["id"], format="png")
        im = self.check_download(res["files"][0], "png")
        self.assertTrue(im.info.get("icc_profile"))
        self.assertTrue(res["icc_embedded"])
        _, res = self.convert(body["id"], format="bmp")
        self.assertFalse(res["icc_embedded"])

    def test_adobe_rgb_profile(self):
        """rocket.jpg (real file) carries an Adobe RGB (1998) profile."""
        from PIL import ImageCms
        src_path = SAMPLES / "real/jpeg/rocket.jpg"
        src = Image.open(src_path)
        icc = src.info["icc_profile"]
        self.assertIn("Adobe RGB", ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(icc))))
        body = self.ok_upload("real/jpeg/rocket.jpg")
        # Formats that store profiles keep it byte-for-byte.
        for fid in ("png", "webp", "tiff", "jpeg", "avif"):
            with self.subTest(fid):
                _, res = self.convert(body["id"], format=fid)
                im = self.check_download(res["files"][0], fid)
                self.assertEqual(im.info.get("icc_profile"), icc)
        # Formats that cannot store profiles get pixels converted to sRGB instead of silently shifting colours.
        expected = ImageCms.profileToProfile(src.convert("RGB"), ImageCms.ImageCmsProfile(io.BytesIO(icc)),
                                             ImageCms.createProfile("sRGB"), outputMode="RGB")
        for fid in ("bmp", "gif", "qoi"):
            with self.subTest(fid):
                _, res = self.convert(body["id"], format=fid)
                im = self.check_download(res["files"][0], fid)
                self.assertIsNone(im.info.get("icc_profile"))
                self.assertLess(mean_diff(im, expected), 3 if fid != "gif" else 8)
                self.assertLess(mean_diff(im, expected), mean_diff(src.convert("RGB"), expected))

    def test_transparency_kept_or_background(self):
        body = self.ok_upload("real/png/basn6a16.png")
        src = Image.open(SAMPLES / "real/png/basn6a16.png").convert("RGBA")
        clear = next((x, y) for y in range(src.height) for x in range(src.width) if src.getpixel((x, y))[3] == 0)
        for fid in ("png", "webp", "avif", "tiff"):
            with self.subTest(fid):
                _, res = self.convert(body["id"], format=fid)
                im = self.check_download(res["files"][0], fid).convert("RGBA")
                self.assertEqual(im.getpixel(clear)[3], 0)
                self.assertTrue(res["alpha_out"])
        for fid in ("jpeg", "bmp"):
            with self.subTest(fid):
                _, res = self.convert(body["id"], format=fid, background="#ff0000")
                im = self.check_download(res["files"][0], fid).convert("RGB")
                r, g, b = im.getpixel(clear)
                self.assertGreater(r, 230)
                self.assertLess(g + b, 40)
                self.assertTrue(any("#ff0000" in n for n in res["notes"]))

    def test_gif_transparency(self):
        body = self.ok_upload("real/png/basn6a16.png")
        gif = next(o for o in body["outputs"] if o["id"] == "gif")
        self.assertEqual(gif["transparency"]["mode"], "binary")
        self.assertIn("1-bit", gif["transparency"]["message"])
        src = Image.open(SAMPLES / "real/png/basn6a16.png").convert("RGBA")
        _, res = self.convert(body["id"], format="gif", transparency="keep", background="#00ff00")
        im = self.check_download(res["files"][0], "gif")
        self.assertIn("transparency", im.info)
        rgba = im.convert("RGBA")
        for (x, y) in [(0, 0), (src.width - 1, src.height - 1), (src.width // 2, src.height // 2)]:
            self.assertEqual(rgba.getpixel((x, y))[3] == 0, src.getpixel((x, y))[3] < 128)
        _, res = self.convert(body["id"], format="gif", transparency="flatten", background="#00ff00")
        im = self.check_download(res["files"][0], "gif")
        self.assertNotIn("transparency", im.info)
        self.assertEqual(im.convert("RGBA").getchannel("A").getextrema(), (255, 255))

    def test_animation_options(self):
        body = self.ok_upload("real/gif/no_time_for_that_tiny.gif")
        n = body["image"]["frames"]
        jpeg = next(o for o in body["outputs"] if o["id"] == "jpeg")
        self.assertEqual([f["id"] for f in jpeg["frame_options"]], ["first", "separate"])
        webp = next(o for o in body["outputs"] if o["id"] == "webp")
        self.assertEqual(webp["frame_options"][0]["id"], "keep")
        # No silent discard: a format that cannot animate refuses without an explicit choice.
        status, res = self.convert(body["id"], format="jpeg")
        self.assertEqual(status, 400)
        self.assertEqual(res["error"]["code"], "frames_choice_required")
        src = Image.open(SAMPLES / "real/gif/no_time_for_that_tiny.gif")
        src_total = sum(f.info.get("duration", 0) for f in __import__("PIL.ImageSequence", fromlist=["x"]).Iterator(src))
        for fid in ("webp", "png", "gif", "avif"):
            with self.subTest(fid):
                _, res = self.convert(body["id"], format=fid, frames="keep")
                im = self.check_download(res["files"][0], fid)
                total = 0
                for i in range(im.n_frames):
                    im.seek(i)
                    total += im.info.get("duration", 0)
                self.assertGreater(im.n_frames, 1)
                self.assertLessEqual(abs(total - src_total), 10 * n)
        _, res = self.convert(body["id"], format="jpeg", frames="first")
        self.assertEqual(len(res["files"]), 1)
        self.assertTrue(any("first of" in x for x in res["notes"]))
        _, res = self.convert(body["id"], format="png", frames="separate")
        self.assertEqual(len(res["files"]), n)
        data, headers = self.download(res["bundle"]["url"])
        self.assertEqual(headers["Content-Type"], "application/zip")
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.namelist()
            self.assertEqual(len(names), n)
            for name in names:
                Image.open(io.BytesIO(z.read(name))).load()

    def test_multipage_options(self):
        body = self.ok_upload("real/tiff/multipage.tif")
        n = body["image"]["frames"]
        self.assertGreater(n, 1)
        tiff = next(o for o in body["outputs"] if o["id"] == "tiff")
        self.assertEqual(tiff["frame_options"][0]["id"], "keep")
        _, res = self.convert(body["id"], format="tiff", frames="keep")
        self.assertEqual(self.check_download(res["files"][0], "tiff").n_frames, n)
        status, res = self.convert(body["id"], format="png")
        self.assertEqual(res["error"]["code"], "frames_choice_required")
        _, res = self.convert(body["id"], format="png", frames="separate")
        self.assertEqual(len(res["files"]), n)

    def test_damaged_input_during_conversion(self):
        # Page 1 intact (so the upload is accepted), page 2 corrupt.
        body = self.ok_upload("damaged/tiff-bad-page2.tif")
        status, res = self.convert(body["id"], format="tiff", frames="keep")
        self.assertEqual(status, 422)
        self.assertEqual(res["error"]["code"], "damaged")
        status, res = self.convert(body["id"], format="png", frames="first")  # page 1 alone still converts
        self.assertEqual(status, 200)
        # A truncated animation is rejected at upload with a clear message, not a crash.
        data = (SAMPLES / "real/gif/no_time_for_that_tiny.gif").read_bytes()
        status, body = self.upload(data=data[: int(len(data) * 0.6)], name="cut.gif")
        self.assertIn(status, (200, 422), body)
        if status == 422:
            self.assertEqual(body["error"]["code"], "damaged")
        for rel in ("damaged/invalid-zlib.png", "damaged/not-really.jpg"):
            status, body = self.upload(SAMPLES / rel)
            self.assertIn(status, (415, 422))

    def test_rejects_unverified_or_unknown_output(self):
        body = self.ok_upload("real/jpeg/rocket.jpg")
        for fmt in ("heic", "svg", "exe", "../../png"):
            status, res = self.convert(body["id"], format=fmt)
            self.assertEqual(status, 400)
        status, res = self.convert("0" * 32, format="png")
        self.assertEqual(status, 404)
        status, _, _ = self.request("POST", f"/api/items/{body['id']}/convert", b"not json", {"Content-Type": "application/json"})
        self.assertEqual(status, 400)

    def test_svg_and_unicode_name(self):
        status, body = self.upload(SAMPLES / "real/svg/folder-home.svg", name="dossier été.svg")
        self.assertEqual(status, 200)
        _, res = self.convert(body["id"], format="png")
        f = res["files"][0]
        self.assertEqual(f["name"], "dossier été.png")
        self.check_download_unicode(f)

    def check_download_unicode(self, f):
        status, raw, headers = self.request("GET", urllib.parse.quote(f["url"]))
        self.assertEqual(status, 200)
        self.assertIn("filename*=utf-8''", headers["Content-Disposition"].lower())

    def test_delete_removes_files(self):
        body = self.ok_upload("real/jpeg/rocket.jpg")
        _, res = self.convert(body["id"], format="png")
        item_dir = self.work / "items" / body["id"]
        self.assertTrue(item_dir.exists())
        self.request("DELETE", f"/api/items/{body['id']}")
        self.assertFalse(item_dir.exists())
        status, _, _ = self.request("GET", res["files"][0]["url"])
        self.assertEqual(status, 404)

    def test_failed_upload_leaves_nothing(self):
        before = set((self.work / "items").iterdir())
        self.upload(SAMPLES / "damaged/not-really.jpg")
        self.assertEqual(set((self.work / "items").iterdir()), before)


@unittest.skipUnless((SAMPLES / "real").is_dir(), "run scripts/build_samples.py first")
class LimitTests(unittest.TestCase):
    """Calls the conversion service directly with tightened limits."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mf-lim-"))
        self.store = Store(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def add(self, path: Path, settings) -> str:
        item_id = self.store.new_id()
        d = self.store.item_dir(item_id, create=True)
        shutil.copyfile(path, d / "input.bin")
        report = inspect_file(d / "input.bin", path.name, CAPS, settings.limits)
        self.store.write_meta(item_id, {"name": path.name, "size_bytes": path.stat().st_size, "report": report})
        return item_id

    def big_png(self, size=(6000, 6000)) -> Path:
        p = self.root / "big.png"
        Image.effect_noise(size, 60).convert("RGB").save(p, compress_level=1)
        return p

    def test_decoded_pixel_limit(self):
        s = replace(SETTINGS, limits=replace(SETTINGS.limits, max_decoded_megapixels=1))
        item = self.add(SAMPLES / "real/png/astronaut.png", s)  # 512×512 → fine
        convert_image(self.store, item, {"format": "webp"}, CAPS, s)
        item = self.add(SAMPLES / "real/jpeg/hubble_deep_field.jpg", s)  # 1000×872 single frame → fine
        convert_image(self.store, item, {"format": "webp"}, CAPS, s)
        s2 = replace(SETTINGS, limits=replace(SETTINGS.limits, max_decoded_megapixels=1))
        item = self.add(SAMPLES / "derived/animated.apng", s2)
        frames = self.store.read_meta(item)["report"]["image"]
        if frames["width"] * frames["height"] * frames["frames"] > 1_000_000:
            with self.assertRaises(ServiceError) as cm:
                convert_image(self.store, item, {"format": "webp", "frames": "keep"}, CAPS, s2)
            self.assertEqual(cm.exception.code, "limit_exceeded")

    def test_timeout_kills_worker(self):
        s = replace(SETTINGS, limits=replace(SETTINGS.limits, image_timeout_s=1))
        item = self.add(self.big_png(), s)
        t0 = time.time()
        with self.assertRaises(ServiceError) as cm:
            convert_image(self.store, item, {"format": "avif"}, CAPS, s)
        self.assertIn(cm.exception.code, ("timeout", "limit_exceeded"))
        self.assertLess(time.time() - t0, 10)
        outputs = self.store.item_dir(item) / "outputs"
        self.assertEqual(list(outputs.iterdir()) if outputs.exists() else [], [], "failed job must leave no files")

    def test_memory_limit(self):
        s = replace(SETTINGS, limits=replace(SETTINGS.limits, worker_memory_mb=200))
        item = self.add(self.big_png(), s)
        with self.assertRaises(ServiceError) as cm:
            convert_image(self.store, item, {"format": "png"}, CAPS, s)
        self.assertEqual(cm.exception.code, "limit_exceeded")

    def test_unique_dirs_and_sweeper(self):
        s = SETTINGS
        item = self.add(SAMPLES / "real/jpeg/rocket.jpg", s)
        a = convert_image(self.store, item, {"format": "png"}, CAPS, s)
        b = convert_image(self.store, item, {"format": "png"}, CAPS, s)
        self.assertNotEqual(a["id"], b["id"])
        d = self.store.item_dir(item)
        old = time.time() - 3600 * 5
        os.utime(d, (old, old))
        self.assertEqual(proc.sweep_expired(self.store.root, 3600), 1)
        self.assertFalse(d.exists())


if __name__ == "__main__":
    unittest.main()


class PillowHeifIsNotTrustedTests(unittest.TestCase):
    """Installing pillow-heif must not by itself enable HEIC: decode and encode are checked separately."""

    def test_broken_install_enables_nothing(self):
        from app.capabilities import images as img_caps
        with tempfile.TemporaryDirectory() as d:
            pkg = Path(d) / "pillow_heif"
            pkg.mkdir()
            # A package that imports fine but provides no working codec (like a wheel without plugins).
            (pkg / "__init__.py").write_text("__version__ = '0.0-fake'\ndef register_heif_opener():\n    pass\n"
                                             "def libheif_info():\n    return {'decoders': {}, 'encoders': {}}\n")
            fixture = fixtures.write_heic(Path(d) / "s.heic", Path(d))
            sys.path.insert(0, d)
            sys.modules.pop("pillow_heif", None)
            try:
                read, write, notes = img_caps._pillow_heif_checks(fixture)
            finally:
                sys.path.remove(d)
                sys.modules.pop("pillow_heif", None)
            self.assertIsNone(read)
            self.assertIsNone(write)
            self.assertTrue(any("decode failed" in n for n in notes), notes)
            self.assertTrue(any("encode" in n for n in notes), notes)
