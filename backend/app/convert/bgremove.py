"""Background removal: IS-Net (general use) through onnxruntime, entirely on this computer.

Model: isnet-general-use.onnx (Apache-2.0), from the rembg project's releases, md5 fc16ebd8b0c10d971d3513d564d01e29,
kept in backend/models/ (MF_MODELS_DIR overrides). Pre/post-processing matches rembg's DisSession. The model
predicts how likely each pixel is to belong to the main subject; that becomes the alpha channel. It works best on
a clear subject (people, products, animals); busy scenes or several subjects may need touching up.
"""
from __future__ import annotations

import hashlib
import os
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image

MODEL_NAME = "isnet-general-use.onnx"
MODEL_MD5 = "fc16ebd8b0c10d971d3513d564d01e29"
SIZE = 1024


def model_path() -> Path:
    base = Path(os.environ.get("MF_MODELS_DIR") or Path(__file__).resolve().parents[2] / "models")
    return base / MODEL_NAME


def available() -> tuple[bool, str]:
    """The runtime is importable and the model file is present and intact."""
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False, "onnxruntime is not installed"
    p = model_path()
    if not p.is_file():
        return False, f"model file missing: {p.name} (see README: Background removal)"
    h = hashlib.md5()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest() != MODEL_MD5:
        return False, "model file is damaged (checksum mismatch)"
    return True, "IS-Net general-use model (onnxruntime, runs locally)"


@lru_cache(maxsize=1)
def _session():
    import onnxruntime as ort
    opts = ort.SessionOptions()
    opts.log_severity_level = 3
    return ort.InferenceSession(str(model_path()), opts, providers=["CPUExecutionProvider"])


def mask(im: Image.Image) -> Image.Image:
    """Greyscale mask the size of `im`: 255 = subject, 0 = background."""
    rgb = im.convert("RGB").resize((SIZE, SIZE), Image.Resampling.LANCZOS)
    a = np.asarray(rgb, dtype=np.float32)
    a = a / max(float(a.max()), 1e-6) - 0.5
    x = np.expand_dims(a.transpose(2, 0, 1), 0).astype(np.float32)
    sess = _session()
    pred = sess.run(None, {sess.get_inputs()[0].name: x})[0][0, 0]
    lo, hi = float(pred.min()), float(pred.max())
    pred = (pred - lo) / max(hi - lo, 1e-6)
    return Image.fromarray((pred * 255).astype("uint8"), mode="L").resize(im.size, Image.Resampling.LANCZOS)


def cutout(im: Image.Image) -> Image.Image:
    """RGBA copy of `im` whose background is transparent (any existing transparency is kept too)."""
    m = mask(im)
    rgba = im.convert("RGBA")
    if im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info:
        from PIL import ImageChops
        m = ImageChops.multiply(m, rgba.getchannel("A"))
    rgba.putalpha(m)
    return rgba


def selftest(photo: Path | None) -> dict:
    """Offered only if a real photo comes out with a clear subject and a cleared background."""
    ok, detail = available()
    if not ok:
        return {"verified": False, "detail": detail}
    if not photo or not photo.exists():
        return {"verified": False, "detail": "no real photo sample to test with"}
    try:
        with Image.open(photo) as src:
            src.thumbnail((512, 512))
            out = cutout(src)
        hist = out.getchannel("A").histogram()
        total = sum(hist)
        clear, solid = sum(hist[:16]) / total, sum(hist[240:]) / total
        if clear < 0.05 or solid < 0.05:
            return {"verified": False, "detail": f"test photo gave {clear:.0%} background / {solid:.0%} subject"}
        return {"verified": True, "detail": f"{detail}; test photo: {clear:.0%} background removed, {solid:.0%} subject kept"}
    except Exception as exc:  # noqa: BLE001 - a broken runtime must not break detection
        return {"verified": False, "detail": f"{type(exc).__name__}: {exc}"}
