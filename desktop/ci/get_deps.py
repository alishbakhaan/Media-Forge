"""CI helper: fetch the background-removal model (checked against its published md5) and a static FFmpeg for Windows.

Usage: python desktop/ci/get_deps.py
"""
import hashlib
import io
import json
import re
import shutil
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / "backend" / "models" / "isnet-general-use.onnx"
MODEL_URL = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/isnet-general-use.onnx"
MODEL_MD5 = "fc16ebd8b0c10d971d3513d564d01e29"
FFMPEG_DIR = ROOT / "desktop" / "ffmpeg"


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "mediaforge-ci"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return r.read()


def model() -> None:
    if MODEL.exists() and hashlib.md5(MODEL.read_bytes()).hexdigest() == MODEL_MD5:
        print("model already present")
        return
    data = get(MODEL_URL)
    if hashlib.md5(data).hexdigest() != MODEL_MD5:
        raise SystemExit("model checksum mismatch")
    MODEL.parent.mkdir(parents=True, exist_ok=True)
    MODEL.write_bytes(data)
    print("model", len(data), "bytes, md5 ok")


def ffmpeg_windows() -> None:
    """Static GPL build from BtbN/FFmpeg-Builds (GitHub): newest release branch, not the master snapshot."""
    rel = json.loads(get("https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest"))
    pat = re.compile(r"ffmpeg-n(\d+)\.(\d+)-latest-win64-gpl-\d+\.\d+\.zip$")
    assets = [(tuple(map(int, m.groups())), a) for a in rel["assets"] if (m := pat.search(a["name"]))]
    if not assets:
        raise SystemExit("no Windows FFmpeg release build found")
    _, asset = max(assets, key=lambda x: x[0])
    print("ffmpeg:", asset["name"])
    z = zipfile.ZipFile(io.BytesIO(get(asset["browser_download_url"])))
    FFMPEG_DIR.mkdir(parents=True, exist_ok=True)
    for name in z.namelist():
        if name.endswith(("bin/ffmpeg.exe", "bin/ffprobe.exe")):
            with z.open(name) as src, open(FFMPEG_DIR / Path(name).name, "wb") as dst:
                shutil.copyfileobj(src, dst)
    (FFMPEG_DIR / "FFMPEG-LICENSE.txt").write_text(
        "MediaForge includes FFmpeg (https://ffmpeg.org), used unmodified as separate programs (ffmpeg.exe, ffprobe.exe).\n"
        f"This build is {asset['name']} from https://github.com/BtbN/FFmpeg-Builds, compiled with GPL components,\n"
        "so it is licensed under the GNU General Public License v3 or later: https://www.gnu.org/licenses/gpl-3.0.html\n"
        "Source code: https://ffmpeg.org/download.html and the build scripts at https://github.com/BtbN/FFmpeg-Builds\n")


if __name__ == "__main__":
    model()
    ffmpeg_windows()
