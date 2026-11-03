# Milestone 3 status — image compression, quality controls, resizing

Date: 5 Oct 2026 · Tests: **80 passing** (32 Milestone 1 + 20 Milestone 2 + 25 Milestone 3 incl. 4 HEIC-gating + 3 Chromium); all 55 pre-existing tests still pass.

## Completed

| Requirement | Implementation |
|---|---|
| Presets | Best quality / Balanced / Smallest file per format; editing a control switches to "Custom". |
| Format-specific controls | Lossy *quality* and lossless *effort* are separate controls with different explanations; only controls that apply to the chosen format (and mode, e.g. WebP lossless hides quality) are shown; formats without settings say so. |
| Resize | Width, height, percentage, or both (fit/exact). Aspect locked by default, upscaling off by default (clamped with a note). Uses displayed orientation; premultiplied-alpha resampling. |
| Target size | Bounded search (≤16 encodes, ≤70 % of the worker timeout) over quality (or GIF colours) down to a user floor. Never changes format, frames or (unless allowed) dimensions. Reports met/not met, final size, settings, what changed, and suggests enabling resizing. |
| Before/after preview | Wipe comparison + 100 % view; "before" keeps the source transparency; "after" is the actual output file. |
| Size comparison | Original vs output size and % change; larger outputs flagged with a link to the original, which is kept until expiry/delete. |
| Metadata | Keep / remove GPS (default) / remove all. Orientation always baked into pixels and reset in EXIF *and* XMP. |
| Colour profiles | Keep (embed where supported, otherwise convert to sRGB) or convert to sRGB without a profile; explained in the UI. |
| 16-bit disclosure | Shown before converting on every output option and in the result. |
| Milestone 2 behaviour | Transparency/background, animation timing **and loop count** (now verified), frame count, multipage, ZIP of separate files, output verification, limits, cleanup — all retested. |
| HEIC gating | Read and write verified independently. Write is checked by our own HEIF parser + FFmpeg's HEVC decoder, so it no longer needs a HEIC reader. Tested with stand-in packages: decode-only, encode-only, both, neither. |
| Docs | `README.md` is the single current reference; this file is the milestone report. |

## Verification results

Real photo `retina.jpg` (1411×1411, 263 KB):

| Format | Best | Balanced | Smallest |
|---|---|---|---|
| JPEG | 284 KB (q92, 4:4:4 → larger than source, flagged) | 128 KB | 81 KB |
| WebP | 96 KB | 53 KB | 34 KB |
| AVIF | 120 KB | 61 KB | 30 KB |

Real transparent/graphic `logo.png` (500×500, 176 KB): PNG 156 / 163 / 98 KB (Smallest = 256-colour palette, lossy, labelled), WebP 101 / 15 / 11 KB.

Target size on `retina.jpg`, JPEG Balanced:

| Target | Allow smaller dimensions | Result |
|---|---|---|
| 60 KB | no | **met** — 58 KB, quality 82 → 47, 1411×1411, 8 encodes |
| 8 KB | no | **not met** — 42 KB at the quality floor 30, dimensions unchanged, user told to allow smaller dimensions |
| 8 KB | yes | **met** — 8 KB at 217×217, quality kept at 82, 10 encodes |

Other tested cases (all through the HTTP API with downloads re-checked): lossless PNG effort 0 vs 9 gives identical pixels and a smaller file; lossless WebP is pixel-identical; resize matrix of 9 cases (incl. clamped upscaling); EXIF-rotated resize; no dark fringes on resized alpha edges; 24-frame GIF → WebP/GIF/PNG with compression + 200 % resize keeps frames, timing and loop; multipage TIFF with Deflate + 50 % resize keeps pages; already-compressed q50 JPEG becomes larger and the original downloads byte-identical; unreachable targets for lossless PNG and animated WebP keep dimensions and all frames; GPS removed from JPEG/PNG/WebP/AVIF/TIFF while camera make stays; "remove all" leaves no EXIF/XMP; Adobe RGB → sRGB conversion within 2/255 of a reference transform; invalid settings rejected with 400.

## Bugs found by the new tests and fixed

1. **Pillow embedded colour profiles the user asked to drop:** PNG/TIFF/WebP encoders fall back to the image's own `info`, so "convert to sRGB" still wrote an sRGB profile. Encoders now get only explicit metadata.
2. **TIFF→TIFF output was corrupt:** a TIFF's "EXIF" is its own IFD (Compression, StripOffsets…), and copying it into the new file broke decoding. Storage tags are now stripped; the corrupt output was caught by verification, not shipped.
3. **"Before" preview showed the flattened image** for formats without alpha; it now shows the source transparency.
4. Unexpected worker exceptions now return a clean error instead of a bare crash.

## Limitations

- HEIC: **Unavailable in this environment** (no HEVC decoder plugin for libheif, no encoder, pillow-heif cannot be installed). The pillow-heif code path is tested only with stand-in packages, not the real library.
- Camera RAW: **Unverified — real camera samples needed.**
- Target size applies per file when frames/pages are saved separately; with dimensions allowed to shrink, the search keeps your chosen quality and reduces size only.
- The search is bounded; on very large or animated images it may stop early (reported as such).
- Lossless AVIF is not implemented in this app (AVIF output is always lossy); JPEG 2000 lossy quality maps to a compression ratio (not a perceptual scale).
- XMP is kept only for JPEG, PNG, WebP and AVIF; TIFF/HEIC outputs keep EXIF but not XMP.
- 16-bit-per-channel images are reduced to 8-bit; ICNS upscales small images into its larger icon slots.
- Preview "before" for ICO/ICNS shows the padded square canvas, matching the output geometry.
- Real-file coverage for WebP, AVIF, BMP and video containers other than MP4 is still FFmpeg-made content.
- Tested on Linux only (Python 3.13, Chromium); not yet on macOS.
