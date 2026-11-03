"""Milestone 3 tests: presets, format controls, resizing, target size, metadata, previews, HEIC gating.

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
from pathlib import Path

from PIL import Image, ImageChops, ImageCms, ImageDraw, ImageOps, ImageSequence, ImageStat

BACKEND = Path(__file__).resolve().parent.parent
SAMPLES = BACKEND.parent / "samples"
sys.path.insert(0, str(BACKEND))

from app import fixtures  # noqa: E402
from app.sniff import sniff_bytes  # noqa: E402


def mean_diff(a: Image.Image, b: Image.Image) -> float:
    return sum(ImageStat.Stat(ImageChops.difference(a.convert("RGB"), b.convert("RGB"))).mean) / 3


@unittest.skipUnless((SAMPLES / "real").is_dir(), "run scripts/build_samples.py first")
class M3Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            cls.port = s.getsockname()[1]
        cls.work = Path(tempfile.mkdtemp(prefix="mf-m3-"))
        cls.server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(cls.port)], cwd=BACKEND,
                                      env={**os.environ, "MF_WORK_DIR": str(cls.work)}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.base = f"http://127.0.0.1:{cls.port}"
        for _ in range(400):
            try:
                with urllib.request.urlopen(cls.base + "/api/capabilities") as r:
                    if r.status == 200:
                        break
            except Exception:
                pass
            time.sleep(0.25)
        cls.ids: dict[str, dict] = {}

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        cls.server.wait(10)
        shutil.rmtree(cls.work, ignore_errors=True)

    def req(self, method, path, data=None, headers=None):
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(r, timeout=300) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def upload(self, rel: str | None = None, data: bytes | None = None, name: str = "x.png") -> dict:
        key = rel or name
        if rel and key in self.ids:
            return self.ids[key]
        body = (SAMPLES / rel).read_bytes() if rel else data
        status, raw, _ = self.req("POST", "/api/uploads", body, {"X-Filename": urllib.parse.quote(Path(rel).name if rel else name)})
        self.assertEqual(status, 200, raw[:300])
        up = json.loads(raw)
        if rel:
            self.ids[key] = up
        return up

    def convert(self, up: dict, expect: int = 200, **opts) -> dict:
        status, raw, _ = self.req("POST", f"/api/items/{up['id']}/convert", json.dumps(opts).encode(), {"Content-Type": "application/json"})
        self.assertEqual(status, expect, raw[:400])
        return json.loads(raw)

    def fetch(self, url: str) -> bytes:
        status, raw, _ = self.req("GET", urllib.parse.quote(url))
        self.assertEqual(status, 200, url)
        return raw

    def image(self, res: dict, i: int = 0) -> Image.Image:
        im = Image.open(io.BytesIO(self.fetch(res["files"][i]["url"])))
        im.load()
        return im

    # ---------------------------------------------------------------- presets & controls
    def test_presets_order_sizes(self):
        up = self.upload("real/jpeg/retina.jpg")
        for fid in ("jpeg", "webp", "avif"):
            with self.subTest(fid):
                sizes = [self.convert(up, format=fid, preset=p)["output_size"] for p in ("best", "balanced", "smallest")]
                self.assertGreater(sizes[0], sizes[1])
                self.assertGreater(sizes[1], sizes[2])
        res = self.convert(up, format="jpeg", preset="balanced")
        self.assertEqual(res["files"][0]["settings"]["quality"], 82)

    def test_only_relevant_controls(self):
        up = self.upload("real/png/logo.png")
        outs = {o["id"]: o["settings"] for o in up["outputs"]}
        ids = lambda f: [c["id"] for c in outs[f]["controls"]]  # noqa: E731
        self.assertEqual(ids("jpeg"), ["quality", "progressive", "subsampling"])
        self.assertEqual(ids("png"), ["effort", "palette"])
        self.assertNotIn("quality", ids("png"))
        self.assertEqual(ids("bmp"), [])
        self.assertTrue(outs["bmp"]["note"])
        q = next(c for c in outs["webp"]["controls"] if c["id"] == "quality")
        self.assertEqual(q["when"], {"lossless": False})
        self.assertEqual({c["kind"] for c in outs["png"]["controls"]}, {"effort", "toggle"})
        self.assertEqual(outs["webp"]["presets"]["best"]["lossless"], True)  # lossless source → lossless WebP at Best

    def test_lossless_effort_keeps_pixels(self):
        up = self.upload("real/png/astronaut.png")
        src = Image.open(SAMPLES / "real/png/astronaut.png").convert("RGB")
        lo = self.convert(up, format="png", settings={"effort": 0, "palette": False})
        hi = self.convert(up, format="png", settings={"effort": 9, "palette": False})
        self.assertEqual(mean_diff(self.image(lo), src), 0)
        self.assertEqual(mean_diff(self.image(hi), src), 0)
        self.assertLess(hi["output_size"], lo["output_size"])
        wl = self.convert(up, format="webp", settings={"lossless": True, "effort": 6})
        self.assertEqual(mean_diff(self.image(wl), src), 0)
        pal = self.convert(up, format="png", settings={"palette": True})
        self.assertGreater(mean_diff(self.image(pal), src), 0)  # palette reduction is lossy, and labelled so
        self.assertIn("lossy", pal["files"][0]["settings"]["mode"])

    def test_invalid_settings_rejected(self):
        up = self.upload("real/jpeg/rocket.jpg")
        self.convert(up, 400, format="jpeg", settings={"quality": 0})
        self.convert(up, 400, format="png", settings={"quality": 50})
        self.convert(up, 400, format="jpeg", resize={"mode": "width", "width": 0})
        self.convert(up, 400, format="jpeg", resize={"mode": "sideways"})
        self.convert(up, 400, format="jpeg", target={"bytes": 10})
        self.convert(up, 400, format="jpeg", metadata="sell")

    # ---------------------------------------------------------------- resize
    def test_resize_dimensions(self):
        up = self.upload("real/jpeg/rocket.jpg")  # 640 × 427
        cases = [
            ({"mode": "width", "width": 320}, (320, 214)),   # 427 × 0.5 = 213.5 → rounds to 214
            ({"mode": "height", "height": 100}, (150, 100)),
            ({"mode": "percent", "percent": 50}, (320, 214)),
            ({"mode": "fit", "width": 200, "height": 200}, (200, 133)),
            ({"mode": "fit", "width": 200, "height": 200, "keep_aspect": False}, (200, 200)),
            ({"mode": "width", "width": 320, "keep_aspect": False}, (320, 427)),
            ({"mode": "percent", "percent": 200}, (640, 427)),                       # upscaling off by default
            ({"mode": "percent", "percent": 200, "allow_upscale": True}, (1280, 854)),
            ({"mode": "width", "width": 2000}, (640, 427)),
        ]
        for spec, expected in cases:
            with self.subTest(spec):
                res = self.convert(up, format="webp", resize=spec)
                self.assertEqual((res["files"][0]["width"], res["files"][0]["height"]), expected)
                self.assertEqual(self.image(res).size, expected)
                if expected == (640, 427) and spec.get("percent", 0) != 100:
                    self.assertTrue(any("upscaling is off" in n for n in res["notes"]))

    def test_resize_uses_displayed_orientation(self):
        up = self.upload("derived/gps-photo.jpg")  # stored 600×400, EXIF orientation 6 → displayed 400×600
        res = self.convert(up, format="png", resize={"mode": "width", "width": 200})
        self.assertEqual(self.image(res).size, (200, 300))

    def test_resize_transparent_no_dark_fringe(self):
        im = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
        ImageDraw.Draw(im).ellipse([40, 40, 360, 360], fill=(255, 0, 0, 255))
        buf = io.BytesIO()
        im.save(buf, "PNG")
        up = self.upload(data=buf.getvalue(), name="circle.png")
        out = self.image(self.convert(up, format="png", resize={"mode": "percent", "percent": 13})).convert("RGBA")
        edge = [out.getpixel((x, y)) for x in range(out.width) for y in range(out.height) if 0 < out.getpixel((x, y))[3] < 255]
        self.assertTrue(edge)
        self.assertGreaterEqual(min(p[0] for p in edge), 200, "semi-transparent edge pixels must stay red, not darken")

    # ---------------------------------------------------------------- animation & multipage
    def test_animation_preserved_with_compression_and_resize(self):
        up = self.upload("real/gif/no_time_for_that_tiny.gif")
        src = Image.open(SAMPLES / "real/gif/no_time_for_that_tiny.gif")
        src_total = sum(f.info.get("duration", 0) for f in ImageSequence.Iterator(src))
        src_loop = Image.open(SAMPLES / "real/gif/no_time_for_that_tiny.gif").info.get("loop")
        for fid, extra in (("webp", {"preset": "smallest"}), ("gif", {"settings": {"colors": 32, "dither": False}}),
                           ("png", {"preset": "balanced"})):
            with self.subTest(fid):
                res = self.convert(up, format=fid, frames="keep", resize={"mode": "percent", "percent": 200, "allow_upscale": True}, **extra)
                out = self.image(res)
                total = sum(f.info.get("duration", 0) for f in ImageSequence.Iterator(out))
                self.assertGreater(out.n_frames, 1)
                self.assertLessEqual(abs(total - src_total), 10 * src.n_frames)
                self.assertEqual(out.size, (src.width * 2, src.height * 2))
                self.assertTrue(res["files"][0]["checks"].get("loop", True))
                if fid == "gif":
                    self.assertEqual(out.info.get("loop"), src_loop)

    def test_multipage_with_resize(self):
        up = self.upload("real/tiff/multipage.tif")
        n = up["image"]["frames"]
        res = self.convert(up, format="tiff", frames="keep", preset="smallest", resize={"mode": "percent", "percent": 50})
        out = self.image(res)
        self.assertEqual(out.n_frames, n)
        self.assertEqual(res["files"][0]["settings"]["compression"], "deflate")
        res = self.convert(up, format="png", frames="separate", resize={"mode": "percent", "percent": 50})
        self.assertEqual(len(res["files"]), n)
        self.assertIn("bundle", res)

    def test_tiff_metadata_does_not_copy_storage_tags(self):
        # Regression: a TIFF's own IFD (Compression, StripOffsets…) was copied as "EXIF" and corrupted new TIFFs.
        up = self.upload("damaged/tiff-bad-page2.tif")  # its first page is fine and it carries camera EXIF
        for fid, settings in (("tiff", {"compression": "deflate"}), ("tiff", {"compression": "none"}), ("jpeg", {})):
            with self.subTest(fid=fid, settings=settings):
                res = self.convert(up, format=fid, frames="first", metadata="keep", settings=settings)
                im = self.image(res)
                self.assertEqual(im.size, (640, 427))
                if fid == "jpeg":  # a TIFF's own IFD legitimately has these; a JPEG's EXIF must not
                    ex = im.getexif()
                    self.assertNotIn(273, ex)
                    self.assertNotIn(259, ex)

    # ---------------------------------------------------------------- size comparison & originals
    def test_already_compressed_gets_larger_and_original_kept(self):
        up = self.upload("real/jpeg/video-001.q50.444.jpeg")
        res = self.convert(up, format="jpeg", preset="best")
        self.assertTrue(res["larger_than_original"])
        self.assertGreater(res["change_percent"], 0)
        self.assertEqual(self.fetch(res["original_url"]), (SAMPLES / "real/jpeg/video-001.q50.444.jpeg").read_bytes())

    # ---------------------------------------------------------------- target size
    def test_target_reachable_by_quality(self):
        up = self.upload("real/jpeg/retina.jpg")
        res = self.convert(up, format="jpeg", preset="balanced", target={"bytes": 60_000})
        t = res["target"]
        self.assertTrue(t["met"])
        self.assertLessEqual(res["output_size"], 60_000)
        self.assertLess(res["files"][0]["settings"]["quality"], 82)
        self.assertEqual((res["files"][0]["width"], res["files"][0]["height"]), (1411, 1411))
        self.assertLessEqual(t["attempts"], 16)
        self.assertIn("best effort", t["message"].lower())

    def test_target_unreachable_without_resize(self):
        up = self.upload("real/jpeg/retina.jpg")
        res = self.convert(up, format="jpeg", target={"bytes": 8_000})
        self.assertFalse(res["target"]["met"])
        self.assertIn("Allow smaller dimensions", res["target"]["message"])
        self.assertEqual((res["files"][0]["width"], res["files"][0]["height"]), (1411, 1411))   # dimensions untouched
        self.assertEqual(sniff_bytes(self.fetch(res["files"][0]["url"])[:64]).format_id, "jpeg")  # format untouched
        res = self.convert(up, format="jpeg", target={"bytes": 8_000, "allow_resize": True})
        self.assertTrue(res["target"]["met"])
        self.assertLessEqual(res["output_size"], 8_000)
        self.assertLess(res["files"][0]["width"], 1411)
        self.assertTrue(any("dimensions reduced" in c for c in res["target"]["changed"]))

    def test_target_lossless_and_animation(self):
        up = self.upload("real/png/astronaut.png")
        res = self.convert(up, format="png", settings={"palette": False}, target={"bytes": 20_000})
        self.assertFalse(res["target"]["met"])
        self.assertIn("lossless", res["target"]["message"])
        self.assertEqual(self.image(res).size, (512, 512))
        up = self.upload("real/gif/no_time_for_that_tiny.gif")
        frames = up["image"]["frames"]
        res = self.convert(up, format="webp", frames="keep", target={"bytes": 1024})
        self.assertFalse(res["target"]["met"])
        self.assertGreater(self.image(res).n_frames, 1)  # animation never sacrificed for size
        self.assertLessEqual(abs(self.image(res).n_frames - frames), frames)

    def test_target_respects_time_limit(self):
        # Many attempts on a big animated job must stop at the attempt/time bound, not run away.
        up = self.upload("real/jpeg/hubble_deep_field.jpg")
        t0 = time.time()
        res = self.convert(up, format="avif", preset="best", target={"bytes": 2_000, "allow_resize": True})
        self.assertLess(time.time() - t0, 120)
        self.assertLessEqual(res["target"]["attempts"], 16)

    # ---------------------------------------------------------------- metadata & colour
    def test_metadata_controls(self):
        up = self.upload("derived/gps-photo.jpg")
        self.assertTrue(up["image"]["gps"])
        expected = ImageOps.exif_transpose(Image.open(SAMPLES / "derived/gps-photo.jpg"))
        for mode in ("keep", "remove_gps", "remove_all"):
            with self.subTest(mode):
                res = self.convert(up, format="jpeg", metadata=mode)
                im = self.image(res)
                ex = im.getexif()
                self.assertEqual(im.size, (400, 600))
                self.assertLess(mean_diff(im, expected), 4)
                self.assertIn(ex.get(0x0112), (None, 1))
                gps = dict(ex.get_ifd(0x8825)) if 0x8825 in ex else {}
                xmp = im.info.get("xmp") or b""
                if mode == "keep":
                    self.assertTrue(gps)
                    self.assertIn(b"GPSLatitude", xmp)
                    self.assertIn(b'tiff:Orientation="1"', xmp)  # XMP orientation updated too
                    self.assertEqual(ex.get(0x010F), "SampleCam")
                elif mode == "remove_gps":
                    self.assertFalse(gps)
                    self.assertEqual(xmp, b"")
                    self.assertEqual(ex.get(0x010F), "SampleCam")
                else:
                    self.assertEqual(len(ex), 0)
                    self.assertEqual(xmp, b"")
                    self.assertNotIn(b"SampleCam", self.fetch(res["files"][0]["url"]))

    def test_location_channels_with_camera_exif(self):
        """GPS sub-IFD, MakerNote and XMP are the location channels handled; checked per output format."""
        up = self.upload("derived/exif-full.jpg")
        for fid in ("jpeg", "png", "webp", "avif", "tiff"):
            for mode in ("keep", "remove_gps", "remove_all"):
                with self.subTest(fmt=fid, mode=mode):
                    res = self.convert(up, format=fid, metadata=mode)
                    data = self.fetch(res["files"][0]["url"])
                    ex = Image.open(io.BytesIO(data)).getexif()
                    gps = bool(ex.get_ifd(0x8825)) if 0x8825 in ex else False
                    has_mn = b"VENDOR-MAKERNOTE-DATA" in data
                    if mode == "keep" and fid != "tiff":
                        self.assertTrue(gps and has_mn)
                    else:
                        self.assertFalse(gps)
                        self.assertFalse(has_mn)
                    if mode != "remove_all":
                        self.assertEqual(ex.get(0x010F), "SampleCam")
                    if fid == "tiff" and mode == "keep":
                        self.assertIn("omits EXIF sub-blocks", res["metadata"]["exif"])

    def test_gps_removed_in_png_and_webp_too(self):
        up = self.upload("derived/gps-photo.jpg")
        for fid in ("png", "webp", "avif", "tiff"):
            with self.subTest(fid):
                res = self.convert(up, format=fid)  # default: remove GPS
                data = self.fetch(res["files"][0]["url"])
                ex = Image.open(io.BytesIO(data)).getexif()
                self.assertFalse(dict(ex.get_ifd(0x8825)) if 0x8825 in ex else {})
                self.assertNotIn(b"GPSLatitude", data)

    def test_colour_profile_options(self):
        src = Image.open(SAMPLES / "real/jpeg/rocket.jpg")
        icc = src.info["icc_profile"]
        expected = ImageCms.profileToProfile(src.convert("RGB"), ImageCms.ImageCmsProfile(io.BytesIO(icc)),
                                             ImageCms.createProfile("sRGB"), outputMode="RGB")
        up = self.upload("real/jpeg/rocket.jpg")
        res = self.convert(up, format="png", icc="srgb", metadata="remove_all")
        im = self.image(res)
        self.assertIsNone(im.info.get("icc_profile"))
        self.assertLess(mean_diff(im, expected), 2)
        self.assertIn("sRGB", res["metadata"]["icc"])
        res = self.convert(up, format="png", icc="keep", metadata="remove_all")
        self.assertEqual(self.image(res).info.get("icc_profile"), icc)  # colour data is not personal metadata

    def test_16bit_disclosed(self):
        up = self.upload("real/png/basn6a16.png")
        self.assertEqual(up["image"]["bit_depth"], 16)
        for o in up["outputs"]:
            self.assertIn("16-bit per channel will be converted to 8-bit", o["notes"])
        res = self.convert(up, format="png")
        self.assertIn("16-bit per channel source converted to 8-bit per channel", res["notes"])

    # ---------------------------------------------------------------- previews & regressions
    def test_previews(self):
        up = self.upload("real/png/basn6a16.png")
        for fid in ("webp", "tiff", "jpeg"):
            with self.subTest(fid):
                res = self.convert(up, format=fid, background="#00ff00", resize={"mode": "percent", "percent": 300, "allow_upscale": True})
                p = res["previews"]
                before = Image.open(io.BytesIO(self.fetch(p["before"])))
                after = Image.open(io.BytesIO(self.fetch(p["after"])))
                self.assertEqual(before.size, (96, 96))
                self.assertEqual(after.size, (96, 96))
                self.assertEqual(before.convert("RGBA").getchannel("A").getextrema()[0], 0)  # before shows the real transparency
                if fid == "tiff":
                    self.assertTrue(p["after"].endswith(".preview/after.png"))
                else:
                    self.assertEqual(p["after"], res["files"][0]["url"])
        status, _, _ = self.req("GET", f"/api/items/{up['id']}/outputs/{res['id']}/.preview/../../input.bin")
        self.assertIn(status, (404,))

    def test_transparency_and_background_still_work(self):
        up = self.upload("real/png/basn6a16.png")
        src = Image.open(SAMPLES / "real/png/basn6a16.png").convert("RGBA")
        clear = next((x, y) for y in range(32) for x in range(32) if src.getpixel((x, y))[3] == 0)
        res = self.convert(up, format="jpeg", background="#ff0000", preset="smallest")
        r, g, b = self.image(res).convert("RGB").getpixel(clear)
        self.assertGreater(r, 200)
        self.assertLess(g + b, 80)
        res = self.convert(up, format="webp", preset="smallest")
        self.assertEqual(self.image(res).convert("RGBA").getpixel(clear)[3], 0)


def _fake_pillow_heif(dirpath: Path, decode: bool, encode: bool) -> None:
    """A stand-in 'pillow_heif' with only a decoder, only an encoder, or neither.
    The decoder/encoder are real (FFmpeg HEVC + our HEIF box code), so the checks are genuine."""
    code = f'''
__version__ = "0.0-test"
DECODE, ENCODE = {decode!r}, {encode!r}
from pathlib import Path
import io, tempfile
from PIL import Image, ImageFile

def libheif_info():
    return {{"decoders": {{"HEVC": DECODE}}, "encoders": {{"HEVC": ENCODE}}}}

class _HeifFile(ImageFile.ImageFile):
    format = "HEIF"
    format_description = "test HEIF"
    def _open(self):
        from app.convert.heif_tools import parse_primary, to_annexb
        from app import proc
        data = self.fp.read()
        info = parse_primary(data)
        with tempfile.TemporaryDirectory() as d:
            es = Path(d) / "x.hevc"; es.write_bytes(to_annexb(info["hvcC"], info["payload"]))
            out = Path(d) / "x.png"
            proc.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "hevc", "-i", str(es), "-frames:v", "1", str(out)], timeout=30)
            img = Image.open(out); img.load(); img = img.convert("RGB")
        self._decoded = img
        self._size = img.size
        self._mode = "RGB"
        self.tile = []
    def load(self):
        if self._im is None:
            self.im = self._decoded.im
        return Image.Image.load(self)

def _accept(prefix):
    return prefix[4:8] == b"ftyp" and prefix[8:12] in (b"heic", b"mif1", b"heix")

def _save(im, fp, filename):
    from app import fixtures
    with tempfile.TemporaryDirectory() as d:
        p = fixtures.write_heic(Path(d) / "o.heic", Path(d), image=im)
        fp.write(p.read_bytes())

def register_heif_opener():
    if DECODE:
        Image.register_open("HEIF", _HeifFile, _accept)
    if ENCODE:
        Image.register_save("HEIF", _save)
'''
    pkg = dirpath / "pillow_heif"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(code)


class HeicGatingTests(unittest.TestCase):
    """Reading and writing are verified separately; neither requires the other."""

    def check(self, decode: bool, encode: bool):
        from app.capabilities import images as img_caps
        with tempfile.TemporaryDirectory() as d:
            _fake_pillow_heif(Path(d), decode, encode)
            fixture = fixtures.write_heic(Path(d) / "s.heic", Path(d))
            sys.path.insert(0, d)
            sys.modules.pop("pillow_heif", None)
            try:
                return img_caps._pillow_heif_checks(fixture)
            finally:
                sys.path.remove(d)
                sys.modules.pop("pillow_heif", None)
                Image.OPEN.pop("HEIF", None)
                Image.SAVE.pop("HEIF", None)
                if "HEIF" in Image.ID:
                    Image.ID.remove("HEIF")

    def test_decode_only(self):
        read, write, notes = self.check(decode=True, encode=False)
        self.assertTrue(read and read.verified, notes)
        self.assertIsNone(write)

    def test_encode_only(self):
        read, write, notes = self.check(decode=False, encode=True)
        self.assertIsNone(read)
        self.assertTrue(write and write.verified, notes)

    def test_both(self):
        read, write, notes = self.check(decode=True, encode=True)
        self.assertTrue(read and write, notes)

    def test_neither(self):
        read, write, notes = self.check(decode=False, encode=False)
        self.assertIsNone(read)
        self.assertIsNone(write)


if __name__ == "__main__":
    unittest.main()
