"""Milestone 4 tests: video conversion through the HTTP API and the background job queue.

Independent-source tests use files made by other software (samples/real). Round-trip tests use clips
derived from that real footage by FFmpeg (samples/derived) or synthetic probes (samples/synthetic), and
re-read our outputs with FFmpeg/ffprobe — the same toolchain — so they are labelled round-trip.

Run from backend/:  python3 -m unittest tests.test_milestone4 -v
"""
from __future__ import annotations

import json
import os
import re
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

BACKEND = Path(__file__).resolve().parent.parent
SAMPLES = BACKEND.parent / "samples"
sys.path.insert(0, str(BACKEND))

from app.video.probe import probe  # noqa: E402
from app.video.selftest import signal_stats  # noqa: E402

REAL_CLIP = "real/mp4/example-movie.mp4"   # real H.264 file from TeX Live (no audio track)
HAVE = (SAMPLES / REAL_CLIP).exists() and (SAMPLES / "derived/clip.mov").exists()


class Server:
    def __init__(self, **env):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.work = Path(tempfile.mkdtemp(prefix="mf-m4-"))
        e = {**os.environ, "MF_WORK_DIR": str(self.work), **{k: str(v) for k, v in env.items()}}
        self.p = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(self.port)], cwd=BACKEND,
                                  env=e, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.base = f"http://127.0.0.1:{self.port}"
        for _ in range(1200):
            try:
                with urllib.request.urlopen(self.base + "/api/capabilities") as r:
                    if r.status == 200:
                        return
            except Exception:
                pass
            time.sleep(0.25)
        raise RuntimeError("server did not start")

    def close(self):
        self.p.terminate()
        self.p.wait(15)
        shutil.rmtree(self.work, ignore_errors=True)

    def req(self, method, path, body=None, headers=None, raw=False):
        r = urllib.request.Request(self.base + path, data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(r, timeout=600) as resp:
                data = resp.read()
                return resp.status, (data if raw else json.loads(data)), resp.headers
        except urllib.error.HTTPError as e:
            data = e.read()
            return e.code, (data if raw else json.loads(data)), e.headers

    def upload(self, rel: str) -> tuple[int, dict]:
        status, body, _ = self.req("POST", "/api/uploads", (SAMPLES / rel).read_bytes(),
                                   {"X-Filename": urllib.parse.quote(Path(rel).name)})
        return status, body

    def submit(self, item: str, **opts) -> tuple[int, dict]:
        status, body, _ = self.req("POST", f"/api/items/{item}/video/jobs", json.dumps(opts).encode(),
                                   {"Content-Type": "application/json"})
        return status, body

    def plan(self, item: str, **opts) -> tuple[int, dict]:
        status, body, _ = self.req("POST", f"/api/items/{item}/video/plan", json.dumps(opts).encode(),
                                   {"Content-Type": "application/json"})
        return status, body

    def wait(self, job_id: str, timeout: float = 600) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            _, j, _ = self.req("GET", f"/api/jobs/{job_id}")
            if j["state"] not in ("queued", "running", "verifying"):
                return j
            time.sleep(0.3)
        raise AssertionError("job did not finish")

    def convert(self, item: str, **opts) -> dict:
        status, j = self.submit(item, **opts)
        assert status in (200, 202), j
        return self.wait(j["id"])

    def download(self, url: str, dest: Path) -> Path:
        status, data, headers = self.req("GET", urllib.parse.quote(url), raw=True)
        assert status == 200
        dest.write_bytes(data)
        return dest


def measure_sync(path: Path) -> float | None:
    """Seconds between the white flash and the start of the beep, as decoded by FFmpeg."""
    out = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(path), "-map", "0:v:0", "-vf",
                          "signalstats,metadata=print:key=lavfi.signalstats.YAVG", "-map", "0:a:0", "-af",
                          "silencedetect=n=-30dB:d=0.05", "-f", "null", "-"], capture_output=True, text=True).stderr
    flash = pts = None
    for line in out.splitlines():
        m = re.search(r"pts_time:([\d.]+)", line)
        if m:
            pts = float(m.group(1))
        m = re.search(r"YAVG=([\d.]+)", line)
        if m and float(m.group(1)) > 128 and flash is None:
            flash = pts
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", out)]
    return None if flash is None or not ends else ends[0] - flash


@unittest.skipUnless(HAVE, "run scripts/build_samples.py first")
class VideoConversionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = Server()
        cls.tmp = Path(tempfile.mkdtemp(prefix="mf-m4dl-"))
        _, caps, _ = cls.s.req("GET", "/api/capabilities")
        cls.caps = caps
        cls.items: dict[str, dict] = {}

    @classmethod
    def tearDownClass(cls):
        cls.s.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def up(self, rel: str) -> dict:
        if rel not in self.items:
            status, body = self.s.upload(rel)
            self.assertEqual(status, 200, body)
            self.items[rel] = body
        return self.items[rel]

    def done(self, rel: str, **opts) -> dict:
        j = self.s.convert(self.up(rel)["id"], **opts)
        self.assertEqual(j["state"], "done", j.get("error"))
        return j["result"]

    def out_info(self, res: dict) -> dict:
        f = res["files"][0]
        p = self.s.download(f["url"], self.tmp / f"{res['id']}{Path(f['name']).suffix}")
        from app.sniff import sniff_file
        return probe(p, sniff_file(p).format_id, timeout=60) | {"_path": p}

    # ---------------------------------------------------------------- independent source (real file)
    def test_real_clip_inspect(self):
        v = self.up(REAL_CLIP)["video"]
        self.assertEqual((v["display_width"], v["display_height"]), (1280, 720))
        self.assertEqual(v["codec"], "h264")
        self.assertFalse(v["has_audio"])
        self.assertAlmostEqual(v["duration"], 60.97, delta=0.1)
        self.assertEqual(self.up(REAL_CLIP)["format"]["id"], "mp4")

    def test_real_clip_remux_and_reencode(self):
        res = self.done(REAL_CLIP, container="mkv")
        self.assertEqual(res["plan"]["mode"], "remux")
        info = self.out_info(res)
        self.assertAlmostEqual(info["duration"], 60.97, delta=0.3)
        self.assertEqual(info["counts"]["audio"], 0)
        res = self.done(REAL_CLIP, container="avi")  # H.264 cannot be copied into AVI → MPEG-4 Part 2
        self.assertEqual(res["plan"]["video"]["action"], "encode")
        self.assertIn("cannot hold h264", res["plan"]["video"]["reason"])
        info = self.out_info(res)
        self.assertEqual((info["primary_video"]["display_width"], info["primary_video"]["display_height"]), (1280, 720))
        self.assertAlmostEqual(info["duration"], 60.97, delta=0.3)

    # ---------------------------------------------------------------- every advertised output (round-trip)
    def test_every_advertised_container(self):
        src = probe(SAMPLES / "derived/clip.mov", "mov")
        for cid in self.caps["video"]["writable"]:
            with self.subTest(cid):
                res = self.done("derived/clip.mov", container=cid)
                info = self.out_info(res)
                v = info["primary_video"]
                self.assertEqual((v["display_width"], v["display_height"]), (1280, 720))
                self.assertLessEqual(abs(info["duration"] - src["duration"]), max(0.3, 0.02 * src["duration"]))
                self.assertEqual(info["counts"]["audio"], 1)
                self.assertTrue(all(res["files"][0]["checks"].values()))
                status, _, headers = self.s.req("GET", urllib.parse.quote(res["files"][0]["url"]), raw=True)
                self.assertEqual(headers["Content-Type"], res["files"][0]["mime"])

    def test_av_sync_every_container(self):
        base = measure_sync(SAMPLES / "synthetic/sync.mov")
        self.assertIsNotNone(base)
        results = {}
        for cid in self.caps["video"]["writable"]:
            for force in (False, True):
                res = self.done("synthetic/sync.mov", container=cid, force_reencode=force, confirm_drops=True)
                off = measure_sync(self.out_info(res)["_path"])
                results[(cid, res["plan"]["mode"])] = off
                with self.subTest(container=cid, mode=res["plan"]["mode"]):
                    self.assertIsNotNone(off)
                    self.assertLessEqual(abs(off - base), 0.045, f"A/V offset changed by {(off - base) * 1000:.0f} ms")
                    if force:
                        self.assertEqual(res["plan"]["mode"], "encode")
        print("\nA/V sync (ms change):", {k: round((v - base) * 1000, 1) for k, v in results.items()})

    def caps_default(self, cid):
        return next(c for c in self.caps["video"]["containers"] if c["id"] == cid)["write"]["default_video"]

    # ---------------------------------------------------------------- orientation, aspect, VFR, no audio
    def test_rotation(self):
        res = self.done("derived/rotated-90.mp4", container="mp4")
        self.assertEqual(res["plan"]["mode"], "remux")
        v = self.out_info(res)["primary_video"]
        self.assertEqual((v["display_width"], v["display_height"], v["rotation"]), (720, 1280, 90))
        for cid in ("webm", "avi", "mpeg"):
            with self.subTest(cid):
                res = self.done("derived/rotated-90.mp4", container=cid)
                v = self.out_info(res)["primary_video"]
                self.assertEqual(v["rotation"], 0)
                self.assertEqual((v["display_width"], v["display_height"]), (720, 1280))

    def test_anamorphic_aspect_and_no_upscale(self):
        src = probe(SAMPLES / "derived/anamorphic.mp4", "mp4")["primary_video"]
        aspect = src["display_width"] / src["display_height"]
        for cid in ("mp4", "webm", "avi", "mkv"):
            with self.subTest(cid):
                res = self.done("derived/anamorphic.mp4", container=cid, video_codec=self.caps_default(cid))
                v = self.out_info(res)["primary_video"]
                self.assertAlmostEqual(v["display_width"] / v["display_height"], aspect, delta=0.02)
                self.assertLessEqual(v["display_width"], src["display_width"] + 1)
                self.assertLessEqual(v["display_height"], src["display_height"] + 1)
                self.assertEqual(self.out_info(res)["counts"]["audio"], 0)  # source has no audio

    def test_variable_frame_rate(self):
        src = probe(SAMPLES / "derived/vfr.mp4", "mp4")
        self.assertTrue(src["vfr"]["vfr"])
        self.assertTrue(self.up("derived/vfr.mp4")["video"]["vfr"])
        for cid, keeps_vfr in (("mkv", True), ("webm", True), ("avi", False)):
            with self.subTest(cid):
                res = self.done("derived/vfr.mp4", container=cid)
                info = self.out_info(res)
                self.assertLessEqual(abs(info["duration"] - src["duration"]), 0.3)
                out_vfr = probe(info["_path"], {"mkv": "mkv", "webm": "webm", "avi": "avi"}[cid])["vfr"]["vfr"]
                self.assertEqual(out_vfr, keeps_vfr)
                if not keeps_vfr:
                    self.assertTrue(any("constant" in n for n in res["notes"]))

    # ---------------------------------------------------------------- tracks
    def test_multitrack_requires_confirmation(self):
        item = self.up("derived/multitrack-full.mkv")["id"]
        status, plan = self.s.plan(item, container="mp4")
        self.assertEqual(status, 200)
        self.assertTrue(any("attachment" in d for d in plan["dropped"]))
        self.assertEqual([s["codec"] for s in plan["subtitles"]], ["mov_text"])
        status, err = self.s.submit(item, container="mp4")
        self.assertEqual((status, err["error"]["code"]), (400, "confirm_drops"))
        res = self.done("derived/multitrack-full.mkv", container="mp4", confirm_drops=True)
        info = self.out_info(res)
        self.assertEqual(info["counts"]["audio"], 2)
        self.assertEqual(info["counts"]["subtitle"], 1)
        self.assertEqual(info["chapters"], 2)
        self.assertEqual([s.get("language") for s in info["streams"] if s["type"] == "audio"], ["eng", "fra"])
        res = self.done("derived/multitrack-full.mkv", container="mkv")  # keeps everything, no confirmation needed
        self.assertEqual(res["plan"]["dropped"], [])
        info = self.out_info(res)
        self.assertEqual((info["counts"]["audio"], info["counts"]["subtitle"], info["counts"]["attachment"], info["chapters"]),
                         (2, 1, 1, 2))
        status, plan = self.s.plan(item, container="flv")
        self.assertTrue(any("only one audio track" in d for d in plan["dropped"]))
        status, plan = self.s.plan(item, container="mkv", audio_tracks=[1], subtitles=False)
        self.assertTrue(any("you unchecked it" in d for d in plan["dropped"]))
        self.assertTrue(any("you chose not to keep subtitles" in d for d in plan["dropped"]))

    # ---------------------------------------------------------------- HDR / bit depth
    def test_hdr_requires_choice_and_preserves(self):
        item = self.up("derived/hdr10-pq.mp4")
        self.assertEqual(item["video"]["hdr"], "HDR10 (PQ)")
        status, err = self.s.submit(item["id"], container="mp4")
        self.assertEqual(err["error"]["code"], "hdr_choice_required")
        status, err = self.s.submit(item["id"], container="avi", hdr="preserve")
        self.assertEqual(err["error"]["code"], "hdr_not_preservable")
        for cid, vc in (("mp4", "hevc"), ("mkv", "av1"), ("webm", "vp9")):
            with self.subTest(f"{cid}/{vc}"):
                res = self.done("derived/hdr10-pq.mp4", container=cid, video_codec=vc, hdr="preserve")
                v = self.out_info(res)["primary_video"]
                self.assertEqual(v["color_transfer"], "smpte2084")
                self.assertEqual(v["color_primaries"], "bt2020")
                self.assertGreaterEqual(v["bit_depth"], 10)
        res = self.done("derived/hdr-hlg.mp4", container="mkv", hdr="preserve")
        self.assertEqual(res["plan"]["mode"], "remux")
        self.assertEqual(self.out_info(res)["primary_video"]["color_transfer"], "arib-std-b67")
        for cid, vc in (("webm", "vp9"), ("mp4", "av1"), ("mov", "hevc")):
            with self.subTest(f"hlg re-encode {cid}/{vc}"):
                res = self.done("derived/hdr-hlg.mp4", container=cid, video_codec=vc, hdr="preserve", force_reencode=True)
                v = self.out_info(res)["primary_video"]
                self.assertEqual(v["color_transfer"], "arib-std-b67")
                self.assertEqual(v["color_primaries"], "bt2020")
                self.assertGreaterEqual(v["bit_depth"], 10)

    def test_hdr_tonemap_not_washed_out(self):
        for rel in ("derived/hdr10-pq.mp4", "derived/hdr-hlg.mp4"):
            with self.subTest(rel):
                res = self.done(rel, container="mp4", hdr="tonemap")
                info = self.out_info(res)
                v = info["primary_video"]
                self.assertEqual(v["bit_depth"], 8)
                self.assertIn(v["color_transfer"], (None, "bt709"))
                naive = self.tmp / "naive.mp4"
                subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(SAMPLES / rel), "-vf", "format=yuv420p",
                                "-c:v", "libx264", str(naive)], check=True)
                y_tm, s_tm = signal_stats(info["_path"])
                y_nv, s_nv = signal_stats(naive)
                self.assertGreater(s_tm, s_nv * 1.5, f"tone-mapped saturation {s_tm:.1f} vs naive {s_nv:.1f}")
                self.assertGreater(y_tm, 40)

    def test_10bit_sdr(self):
        item = self.up("derived/sdr-10bit.mp4")
        self.assertEqual(item["video"]["bit_depth"], 10)
        self.assertIsNone(item["video"]["hdr"])
        res = self.done("derived/sdr-10bit.mp4", container="webm")
        self.assertEqual(self.out_info(res)["primary_video"]["bit_depth"], 10)
        res = self.done("derived/sdr-10bit.mp4", container="mp4", video_codec="h264")
        self.assertEqual(self.out_info(res)["primary_video"]["bit_depth"], 8)
        self.assertTrue(any("converted to 8-bit" in n for n in res["notes"]))

    # ---------------------------------------------------------------- metadata
    def test_video_location_metadata(self):
        tags = lambda res: self.out_info(res)["format_tags"]  # noqa: E731
        t = tags(self.done("derived/location.mov", container="mp4"))
        self.assertNotIn("location", t)
        self.assertEqual(t.get("title"), "Location test")
        t = tags(self.done("derived/location.mov", container="mp4", metadata="keep"))
        self.assertIn("location", t)
        t = tags(self.done("derived/location.mov", container="mp4", metadata="remove_all"))
        self.assertNotIn("title", t)
        self.assertNotIn("location", t)

    # ---------------------------------------------------------------- errors, duplicates, cancellation
    def test_damaged_inputs(self):
        status, body = self.s.upload("damaged/truncated.mp4")
        self.assertEqual((status, body["error"]["code"]), (422, "damaged"))
        status, body = self.s.upload("damaged/cut-faststart.mp4")
        self.assertEqual(status, 200)
        j = self.s.convert(body["id"], container="webm")
        self.assertEqual(j["state"], "failed")
        self.assertIn(j["error"]["code"], ("verify_failed", "ffmpeg_failed"))
        self.assertFalse((self.s.work / "items" / body["id"] / "outputs" / j["id"]).exists())

    def test_duplicate_submission(self):
        item = self.up("derived/clip.mov")["id"]
        s1, j1 = self.s.submit(item, container="ogv")
        s2, j2 = self.s.submit(item, container="ogv")
        self.assertEqual(j1["id"], j2["id"])
        self.assertTrue(j2["duplicate"])
        self.s.wait(j1["id"])
        s3, j3 = self.s.submit(item, container="ogv")
        self.assertEqual(j3["id"], j1["id"])  # finished result is reused, not re-encoded

    def test_cancel_stops_ffmpeg_and_cleans_up(self):
        item = self.up(REAL_CLIP)["id"]
        status, j = self.s.submit(item, container="mkv", video_codec="hevc")
        self.assertEqual(status, 202)
        for _ in range(200):
            _, j = self.s.req("GET", f"/api/jobs/{j['id']}")[:2]
            if j["state"] == "running" and j["progress"] > 0.01:
                break
            time.sleep(0.1)
        self.assertEqual(j["state"], "running")
        self.assertGreater(j["progress"], 0)
        out_dir = self.s.work / "items" / item / "outputs" / j["id"]
        self.assertTrue(out_dir.exists())
        _, c, _ = self.s.req("POST", f"/api/jobs/{j['id']}/cancel")
        self.assertEqual(c["state"], "cancelled")
        time.sleep(0.5)
        self.assertFalse(out_dir.exists())
        ps = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
        self.assertNotIn(j["id"], ps, "FFmpeg must not keep running after cancel")

    def test_delete_upload_cancels_running_job(self):
        status, body = self.s.upload(REAL_CLIP)   # own upload: the shared cached one must survive
        self.assertEqual(status, 200, body)
        item = body["id"]
        j = _job_until_running(self.s, item, container="mkv", video_codec="hevc")
        status, body, _ = self.s.req("DELETE", f"/api/items/{item}")
        self.assertEqual(status, 200)
        _, j2, _ = self.s.req("GET", f"/api/jobs/{j['id']}")
        self.assertEqual(j2["state"], "cancelled")
        self.assertFalse((self.s.work / "items" / item).exists())
        ps = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
        self.assertNotIn(j["id"], ps)

    def test_preview_and_poster(self):
        res = self.done("derived/clip.mov", container="webm")
        self.assertEqual(res["preview"]["mime"], "video/webm")
        self.assertIn("vp09", res["preview"]["codecs"])
        status, data, headers = self.s.req("GET", res["preview"]["poster"], raw=True)
        self.assertEqual((status, headers["Content-Type"]), (200, "image/jpeg"))


def _job_until_running(s: Server, item: str, **opts) -> dict:
    status, j = s.submit(item, **opts)
    assert status == 202, j
    for _ in range(200):
        _, j, _ = s.req("GET", f"/api/jobs/{j['id']}")
        if j["state"] != "queued":
            break
        time.sleep(0.05)
    return j


@unittest.skipUnless(HAVE, "run scripts/build_samples.py first")
class VideoLimitTests(unittest.TestCase):
    def test_duration_limit(self):
        s = Server(MF_MAX_VIDEO_SECONDS=10)
        try:
            status, body = s.upload(REAL_CLIP)
            self.assertFalse(body["within_limits"])
            status, err = s.submit(body["id"], container="mkv")
            self.assertEqual((status, err["error"]["code"]), (413, "limit_exceeded"))
        finally:
            s.close()

    def test_resolution_limit(self):
        s = Server(MF_MAX_VIDEO_WIDTH=640, MF_MAX_VIDEO_HEIGHT=640)
        try:
            status, body = s.upload("derived/clip.mov")
            self.assertFalse(body["within_limits"])
            self.assertEqual(s.submit(body["id"], container="mkv")[0], 413)
        finally:
            s.close()

    def test_processing_timeout(self):
        s = Server(MF_VIDEO_TIMEOUT_S=1)
        try:
            _, body = s.upload(REAL_CLIP)
            _, j = s.submit(body["id"], container="webm")
            j = s.wait(j["id"])
            self.assertEqual((j["state"], j["error"]["code"]), ("failed", "timeout"))
            self.assertFalse((s.work / "items" / body["id"] / "outputs" / j["id"]).exists())
        finally:
            s.close()

    def test_disk_limit(self):
        s = Server(MF_MAX_JOB_DISK_MB=1)
        try:
            _, body = s.upload(REAL_CLIP)
            _, j = s.submit(body["id"], container="mov", video_codec="prores")
            j = s.wait(j["id"])
            self.assertEqual((j["state"], j["error"]["code"]), ("failed", "disk_limit"))
            self.assertFalse((s.work / "items" / body["id"] / "outputs" / j["id"]).exists())
        finally:
            s.close()

    def test_concurrency_and_queue(self):
        s = Server(MF_MAX_CONCURRENT_VIDEO_JOBS=1, MF_MAX_QUEUED_JOBS=1)
        try:
            _, body = s.upload(REAL_CLIP)
            a = _job_until_running(s, body["id"], container="mkv", video_codec="hevc")
            self.assertEqual(a["state"], "running")
            status, b = s.submit(body["id"], container="webm")
            self.assertEqual((status, b["state"], b["queue_position"]), (202, "queued", 1))
            status, c = s.submit(body["id"], container="ogv")
            self.assertEqual((status, c["error"]["code"]), (429, "queue_full"))
            _, bc, _ = s.req("POST", f"/api/jobs/{b['id']}/cancel")
            self.assertEqual(bc["state"], "cancelled")
            _, ac, _ = s.req("POST", f"/api/jobs/{a['id']}/cancel")
            self.assertEqual(ac["state"], "cancelled")
        finally:
            s.close()

    def test_low_disk_refused(self):
        s = Server(MF_MIN_FREE_DISK_MB=10_000_000)
        try:
            _, body = s.upload("derived/clip.mov")
            status, err = s.submit(body["id"], container="mkv")
            self.assertEqual((status, err["error"]["code"]), (507, "low_disk"))
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
