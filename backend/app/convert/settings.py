"""User-facing conversion settings: presets, per-format controls, resize, target size, metadata.

Everything here is pure data + validation (no image work), so the same definitions are sent to the
UI (which only shows the controls that apply to the chosen format) and enforced by the worker.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


class SettingsError(ValueError):
    pass


PRESETS = ("best", "balanced", "smallest")
PRESET_LABELS = {"best": "Best quality", "balanced": "Balanced", "smallest": "Smallest file"}

# ---------------------------------------------------------------------------------------------------
# Per-format controls. "kind" tells the UI how to explain them:
#   quality   lossy: lower = smaller file, visible loss
#   effort    lossless compression effort: same pixels, smaller file, slower
#   toggle / choice / range  format-specific options
# Presets give the starting value of every control; editing any control switches the UI to "Custom".
# ---------------------------------------------------------------------------------------------------
CONTROLS: dict[str, list[dict]] = {
    "jpeg": [
        {"id": "quality", "kind": "quality", "label": "Quality (lossy)", "min": 1, "max": 100,
         "presets": {"best": 92, "balanced": 82, "smallest": 65}},
        {"id": "progressive", "kind": "toggle", "label": "Progressive (loads gradually, often slightly smaller)",
         "presets": {"best": False, "balanced": True, "smallest": True}},
        {"id": "subsampling", "kind": "choice", "label": "Colour detail (chroma subsampling)",
         "options": [{"id": "444", "label": "4:4:4 full colour detail"}, {"id": "422", "label": "4:2:2"},
                     {"id": "420", "label": "4:2:0 smallest (standard for photos)"}],
         "presets": {"best": "444", "balanced": "420", "smallest": "420"}},
    ],
    "webp": [
        {"id": "lossless", "kind": "toggle", "label": "Lossless (exact pixels)",
         "presets": {"best": "source", "balanced": False, "smallest": False}},
        {"id": "quality", "kind": "quality", "label": "Quality (lossy)", "min": 1, "max": 100, "when": {"lossless": False},
         "presets": {"best": 90, "balanced": 80, "smallest": 62}},
        {"id": "effort", "kind": "effort", "label": "Encoder effort (higher = smaller, slower)", "min": 0, "max": 6,
         "presets": {"best": 6, "balanced": 4, "smallest": 6}},
    ],
    "avif": [
        {"id": "quality", "kind": "quality", "label": "Quality (lossy)", "min": 1, "max": 100,
         "presets": {"best": 85, "balanced": 65, "smallest": 45}},
        {"id": "effort", "kind": "effort", "label": "Encoder effort (higher = smaller, slower)", "min": 0, "max": 10,
         "presets": {"best": 6, "balanced": 4, "smallest": 6}},
    ],
    "heic": [
        {"id": "quality", "kind": "quality", "label": "Quality (lossy)", "min": 1, "max": 100,
         "presets": {"best": 90, "balanced": 75, "smallest": 55}},
    ],
    "png": [
        {"id": "effort", "kind": "effort", "label": "Compression effort (lossless: same pixels, smaller file, slower)",
         "min": 0, "max": 9, "presets": {"best": 9, "balanced": 6, "smallest": 9}},
        {"id": "palette", "kind": "toggle", "label": "Reduce to 256 colours (lossy, keeps transparency)",
         "presets": {"best": False, "balanced": False, "smallest": True}},
    ],
    "gif": [
        {"id": "colors", "kind": "range", "label": "Number of colours (fewer = smaller, lossy)", "min": 2, "max": 256,
         "presets": {"best": 256, "balanced": 256, "smallest": 128}},
        {"id": "dither", "kind": "toggle", "label": "Dithering (smoother gradients, larger file)",
         "presets": {"best": True, "balanced": True, "smallest": False}},
    ],
    "tiff": [
        {"id": "compression", "kind": "choice", "label": "Compression (all lossless)",
         "options": [{"id": "lzw", "label": "LZW"}, {"id": "deflate", "label": "Deflate (ZIP)"}, {"id": "none", "label": "None"}],
         "presets": {"best": "lzw", "balanced": "deflate", "smallest": "deflate"}},
    ],
    "jpeg2000": [
        {"id": "lossless", "kind": "toggle", "label": "Lossless (exact pixels)",
         "presets": {"best": True, "balanced": False, "smallest": False}},
        {"id": "quality", "kind": "quality", "label": "Quality (lossy)", "min": 1, "max": 100, "when": {"lossless": False},
         "presets": {"best": 90, "balanced": 75, "smallest": 55}},
    ],
    "tga": [
        {"id": "rle", "kind": "toggle", "label": "RLE compression (lossless)",
         "presets": {"best": False, "balanced": True, "smallest": True}},
    ],
}
NO_CONTROLS_NOTE = "This format has no compression settings; file size depends only on dimensions."
FORMAT_NOTES = {"avif": "Lossless AVIF is not implemented in this app; AVIF output is always lossy."}

# The knob a target-size search may lower, per format (never format, dimensions or frames).
TARGET_KNOB = {"jpeg": "quality", "webp": "quality", "avif": "quality", "heic": "quality", "jpeg2000": "quality", "gif": "colors"}
MIN_QUALITY_DEFAULT = 30


def preset_values(fid: str, preset: str, lossless_source: bool) -> dict:
    vals = {}
    for c in CONTROLS.get(fid, []):
        v = c["presets"][preset]
        if v == "source":
            v = lossless_source
        vals[c["id"]] = v
    return vals


def controls_for(fid: str, lossless_source: bool) -> dict:
    """What the UI needs to render the settings for one output format."""
    ctrls = CONTROLS.get(fid, [])
    return {
        "controls": [{k: v for k, v in c.items() if k != "presets"} for c in ctrls],
        "presets": {p: preset_values(fid, p, lossless_source) for p in PRESETS},
        "note": FORMAT_NOTES.get(fid) or (None if ctrls else NO_CONTROLS_NOTE),
        "target_knob": TARGET_KNOB.get(fid),
    }


@dataclass
class Resize:
    mode: str = "none"            # none | width | height | percent | fit (width+height)
    width: int | None = None
    height: int | None = None
    percent: float | None = None
    keep_aspect: bool = True
    allow_upscale: bool = False

    def compute(self, w: int, h: int) -> tuple[int, int, list[str]]:
        """Return the new size. Upscaling is clamped unless explicitly allowed."""
        notes: list[str] = []
        if self.mode == "none":
            return w, h, notes
        if self.mode == "percent":
            s = (self.percent or 100) / 100
            nw, nh = w * s, h * s
        elif self.mode == "width":
            nw = self.width
            nh = h * nw / w if self.keep_aspect else h
        elif self.mode == "height":
            nh = self.height
            nw = w * nh / h if self.keep_aspect else w
        else:  # fit
            if self.keep_aspect:
                s = min(self.width / w, self.height / h)
                nw, nh = w * s, h * s
            else:
                nw, nh = self.width, self.height
        nw, nh = max(1, round(nw)), max(1, round(nh))
        if not self.allow_upscale and (nw > w or nh > h):
            if self.keep_aspect or self.mode == "percent":
                s = min(w / nw, h / nh)
                nw, nh = max(1, round(nw * s)), max(1, round(nh * s))
            else:
                nw, nh = min(nw, w), min(nh, h)
            notes.append("not enlarged beyond the original size (upscaling is off)")
        return nw, nh, notes


@dataclass
class Target:
    bytes: int | None = None
    allow_resize: bool = False
    min_quality: int = MIN_QUALITY_DEFAULT


@dataclass
class Settings:
    format: str
    preset: str = "best"
    values: dict = field(default_factory=dict)   # control id -> value (validated)
    frames: str = "keep"
    transparency: str = "keep"
    background: str = "#ffffff"
    resize: Resize = field(default_factory=Resize)
    target: Target = field(default_factory=Target)
    metadata: str = "remove_gps"   # keep | remove_gps | remove_all
    icc: str = "keep"              # keep | srgb
    remove_background: bool = False  # AI cut-out of the main subject (see convert/bgremove.py)

    def public(self) -> dict:
        return asdict(self)


def _bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if v in (0, 1):
        return bool(v)
    raise SettingsError("expected true/false")


def _int(v, lo: int, hi: int, name: str) -> int:
    try:
        iv = int(v)
    except (TypeError, ValueError):
        raise SettingsError(f"{name} must be a whole number")
    if not lo <= iv <= hi:
        raise SettingsError(f"{name} must be between {lo} and {hi}")
    return iv


def parse(raw: dict, lossless_source: bool, max_dim: int = 65535) -> Settings:
    if not isinstance(raw, dict):
        raise SettingsError("expected a JSON object")
    fid = str(raw.get("format", "")).lower()
    preset = str(raw.get("preset") or "best")
    if preset not in PRESETS:
        raise SettingsError(f"unknown preset {preset!r}")
    values = preset_values(fid, preset, lossless_source)
    overrides = raw.get("settings") or {}
    if not isinstance(overrides, dict):
        raise SettingsError("settings must be an object")
    defs = {c["id"]: c for c in CONTROLS.get(fid, [])}
    for key, v in overrides.items():
        c = defs.get(key)
        if c is None:
            raise SettingsError(f"'{key}' does not apply to this format")
        if c["kind"] in ("quality", "effort", "range"):
            values[key] = _int(v, c["min"], c["max"], c["label"])
        elif c["kind"] == "toggle":
            values[key] = _bool(v)
        elif c["kind"] == "choice":
            if v not in [o["id"] for o in c["options"]]:
                raise SettingsError(f"invalid choice for {c['label']}")
            values[key] = v

    frames = str(raw.get("frames") or "keep")
    if frames not in ("keep", "first", "separate"):
        raise SettingsError(f"unknown frames option {frames!r}")
    transparency = str(raw.get("transparency") or "keep")
    if transparency not in ("keep", "flatten"):
        raise SettingsError(f"unknown transparency option {transparency!r}")
    bg = str(raw.get("background") or "#ffffff").lower()
    if not (len(bg) == 7 and bg[0] == "#" and all(c in "0123456789abcdef" for c in bg[1:])):
        raise SettingsError("background must be a colour like #ffffff")

    r = raw.get("resize") or {}
    if not isinstance(r, dict):
        raise SettingsError("resize must be an object")
    mode = str(r.get("mode") or "none")
    if mode not in ("none", "width", "height", "percent", "fit"):
        raise SettingsError(f"unknown resize mode {mode!r}")
    resize = Resize(mode=mode, keep_aspect=_bool(r.get("keep_aspect", True)), allow_upscale=_bool(r.get("allow_upscale", False)))
    if mode in ("width", "fit"):
        resize.width = _int(r.get("width"), 1, max_dim, "Width")
    if mode in ("height", "fit"):
        resize.height = _int(r.get("height"), 1, max_dim, "Height")
    if mode == "percent":
        try:
            resize.percent = float(r.get("percent"))
        except (TypeError, ValueError):
            raise SettingsError("Percent must be a number")
        if not 1 <= resize.percent <= 1000:
            raise SettingsError("Percent must be between 1 and 1000")
    if mode == "height" and not resize.keep_aspect:
        pass

    t = raw.get("target") or {}
    if not isinstance(t, dict):
        raise SettingsError("target must be an object")
    target = Target()
    if t.get("bytes") not in (None, "", 0):
        target.bytes = _int(t.get("bytes"), 1024, 4 * 1024 ** 3, "Target size")
        target.allow_resize = _bool(t.get("allow_resize", False))
        target.min_quality = _int(t.get("min_quality", MIN_QUALITY_DEFAULT), 1, 100, "Lowest quality")

    metadata = str(raw.get("metadata") or "remove_gps")
    if metadata not in ("keep", "remove_gps", "remove_all"):
        raise SettingsError(f"unknown metadata option {metadata!r}")
    icc = str(raw.get("icc") or "keep")
    if icc not in ("keep", "srgb"):
        raise SettingsError(f"unknown colour profile option {icc!r}")
    remove_background = _bool(raw.get("remove_background", False))
    return Settings(fid, preset, values, frames, transparency, bg, resize, target, metadata, icc, remove_background)
