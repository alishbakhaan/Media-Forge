"""CI smoke test: start the packaged backend and run real conversions through its HTTP API.

Usage: python desktop/ci/smoke.py <path to mediaforge-server(.exe)>
Fails (exit 1) if start-up checks fail or any conversion does not produce a valid file.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = ROOT / "samples"
PORT = 8765
B = f"http://127.0.0.1:{PORT}"


def req(method, path, body=None, raw=None, headers=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    r = urllib.request.Request(B + path, data=data, method=method, headers=headers or {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=600) as x:
            return x.status, x.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def up(path):
    st, b = req("POST", "/api/uploads", raw=Path(path).read_bytes(), headers={"X-Filename": urllib.parse.quote(Path(path).name)})
    assert st == 200, (path, b[:300])
    return json.loads(b)


def job(item, **opts):
    st, b = req("POST", f"/api/items/{item}/video/jobs", opts)
    j = json.loads(b)
    assert st < 400, j
    while j["state"] in ("queued", "running", "verifying"):
        time.sleep(1)
        j = json.loads(req("GET", f"/api/jobs/{j['id']}")[1])
    return j


def main(exe):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows console can't print "→" otherwise
    work = Path(tempfile.mkdtemp(prefix="mf-smoke-"))
    env = {**os.environ, "MF_WORK_DIR": str(work / "work"), "MF_CACHE_DIR": str(work / "cache"),
           "MF_FRONTEND_DIST": str(ROOT / "frontend" / "dist"), "MF_SAMPLES_DIR": str(SAMPLES),
           "PATH": str(ROOT / "desktop" / "ffmpeg") + os.pathsep + os.environ.get("PATH", "")}
    server = subprocess.Popen([exe, "--port", str(PORT)], env=env)
    results, failed = [], 0
    try:
        t0 = time.time()
        while time.time() - t0 < 1500:
            try:
                st, b = req("GET", "/api/capabilities")
                if st in (200, 500):
                    break
            except (urllib.error.URLError, ConnectionError):
                pass
            time.sleep(5)
        caps = json.loads(b)
        assert st == 200, caps
        print(f"start-up checks done in {time.time() - t0:.0f}s; ffmpeg {caps['video']['ffmpeg']['version']}")
        print("image write:", caps["images"]["writable"])
        print("video write:", caps["video"]["writable"])
        print("remove background:", caps["images"]["remove_background"], "| gif:", caps["video"]["gif"])

        def check(name, fn):
            nonlocal failed
            try:
                detail = fn()
                print(f"ok    {name}: {detail}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"FAIL  {name}: {exc}")

        def img(src, **opts):
            u = up(SAMPLES / src)
            st, b = req("POST", f"/api/items/{u['id']}/convert", opts)
            r = json.loads(b)
            assert st == 200, r
            return f"{r['files'][0]['name']} {r['output_size']} B {r['notes']}"

        check("PNG to JPG (image worker)", lambda: img("real/png/logo.png", format="jpeg"))
        check("JPG to WebP", lambda: img("real/jpeg/rocket.jpg", format="webp", preset="smallest"))
        check("iPhone HEIC to JPG", lambda: img("real/heic/retina-apple-sips.heic", format="jpeg"))
        check("Remove background", lambda: img("real/png/astronaut.png", format="png", remove_background=True))

        def vid(src, **opts):
            j = job(up(SAMPLES / src)["id"], **opts)
            assert j["state"] == "done", j.get("error")
            return f"{j['result']['files'][0]['name']} {j['result']['output_size']} B"

        check("MOV to MP4", lambda: vid("derived/clip.mov", container="mp4"))
        check("MOV to WebM (re-encode)", lambda: vid("derived/clip.mov", container="webm"))
        check("MP4 to MP4, smaller", lambda: vid("real/mp4/example-movie.mp4", container="mp4", quality="small", max_height=480))
        check("MP4 to GIF", lambda: vid("real/mp4/example-movie.mp4", container="gif", start=1, end=4))
        check("GIF to MP4", lambda: vid("real/gif/no_time_for_that_tiny.gif", container="mp4"))

        def doc(tool, files, **opts):
            ids = [up(f)["id"] for f in files]
            st, b = req("POST", f"/api/docs/{tool}", {"items": ids, "options": opts})
            r = json.loads(b)
            assert st == 200, r
            return f"{[f['name'] for f in r['files']][:3]} {r['output_size']} B"

        pdf = SAMPLES / "real/pdf/example-image-a4-numbered.pdf"
        check("Merge PDF", lambda: doc("merge", [pdf, SAMPLES / "real/pdf/qhv-test.pdf"]))
        check("Protect PDF", lambda: doc("protect", [pdf], password="secret123"))
        check("PDF to images", lambda: doc("pdf-to-images", [SAMPLES / "real/pdf/example-image-a.pdf"]))
        check("PDF to Word", lambda: doc("pdf-to-word", [SAMPLES / "real/pdf/qhv-test.pdf"]))

        def word():
            from docx import Document
            d = Document()
            d.add_heading("Smoke test", 1)
            d.add_paragraph("Hello from Windows.")
            path = work / "smoke.docx"
            d.save(path)
            return doc("word-to-pdf", [path])

        check("Word to PDF", word)
    finally:
        server.terminate()
        try:
            server.wait(20)
        except subprocess.TimeoutExpired:
            server.kill()
    if failed:
        raise SystemExit(f"{failed} conversion(s) failed")
    print("all conversions passed")


if __name__ == "__main__":
    main(sys.argv[1])
