# MediaForge

Web app for image/video conversion and compression plus PDF tools, built milestone by milestone.
Formats are only offered after they pass tests on the machine the app runs on.

| Milestone | Status |
|---|---|
| 1. Interface + detected format capabilities | done |
| 2. Image conversion | done |
| 3. Image compression, quality controls, resizing | done — [milestone-3-status.md](milestone-3-status.md) |
| 4. Video conversion (background jobs) | **done — see [milestone-4-status.md](milestone-4-status.md)** |
| 5. Video compression · 6. Batch processing · 7. PDF tools · 8. Reliability/deployment | not started |

PDFs are currently analysed only (pages/encryption).

**Interface redesign, stage 1 (12 Oct 2026):** the frontend was rebuilt around three tabs with a simple
upload → select output → convert → download flow. The backend is unchanged. Not built yet, and labelled "Coming soon"
in the interface: background removal, every document tool, MP4 ↔ GIF, video compression. Screenshots: `design-review/stage-1/`.

---

## Run it

Requirements: Python 3.11+, Node 18+, FFmpeg + ffprobe, ImageMagick (optional, only for camera RAW).

```bash
# 1. System tools
brew install python@3.13 node ffmpeg imagemagick libraw              # macOS
sudo apt install python3 python3-venv nodejs npm ffmpeg imagemagick  # Ubuntu/Debian 24.04+

# 2. Unzip and enter the project
unzip mediaforge-m4.zip && cd mediaforge

# 3. Python packages (virtual environment recommended)
python3 -m venv .venv && source .venv/bin/activate
python3 -m pip install -r backend/requirements.txt

# 4. Frontend: the ZIP already contains the built frontend/dist; rebuild only after changing frontend/src
(cd frontend && npm install && npm run build)

# 5. Test corpus: keeps samples/real, generates samples/derived, damaged and synthetic with FFmpeg
python3 scripts/build_samples.py

# 6. Start → http://127.0.0.1:8000   (PORT=8010 ./run.sh for another port)
./run.sh
```

The first start runs the capability self-tests (~3 minutes: every video container × codec pair is converted and
checked for A/V sync); the page shows "detecting" until they finish. Results are cached in `var/cache` until the code,
the toolchain or `samples/` change. Useful commands, from `backend/` (venv active):

```bash
python3 -m app.cli capabilities             # verified formats (--refresh to re-test, --json for everything)
python3 -m app.cli inspect path/to/file     # what the UI shows for one file
python3 -m unittest discover -s tests -v    # 106 tests (~4 min); Chromium checks run if Playwright is installed
```

Optional browser tests: `pip install playwright && playwright install chromium`.

Frontend development: `cd frontend && npm run dev` rebuilds on save; reload the browser.

### Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `MF_MAX_UPLOAD_MB` | 500 | per-file upload limit |
| `MF_MAX_IMAGE_MEGAPIXELS` | 100 | per image/frame, checked from the header before decoding |
| `MF_MAX_DECODED_MEGAPIXELS` | 400 | all frames of one job together |
| `MF_IMAGE_TIMEOUT_S` | 120 | conversion worker is killed after this; target-size search uses ≤70 % of it |
| `MF_WORKER_MEMORY_MB` | 3072 | address-space cap per conversion worker |
| `MF_MAX_CONCURRENT_JOBS` / `MF_MAX_CONCURRENT_PROBES` | 2 / 4 | image conversions / file inspections |
| `MF_MAX_VIDEO_SECONDS`, `MF_MAX_VIDEO_WIDTH/HEIGHT`, `MF_MAX_PDF_PAGES` | 3600, 7680/4320, 500 | checked at upload; over-limit videos can be inspected but not converted |
| `MF_MAX_CONCURRENT_VIDEO_JOBS` | 1 | FFmpeg conversions running at once |
| `MF_MAX_QUEUED_JOBS` | 10 | waiting video jobs; more → HTTP 429 |
| `MF_VIDEO_TIMEOUT_S` | 3600 | a video job is stopped (FFmpeg killed, files deleted) after this |
| `MF_MAX_JOB_DISK_MB` | 8192 | output size cap per video job, checked while FFmpeg writes |
| `MF_MIN_FREE_DISK_MB` | 1024 | jobs are refused at submit, and stopped (files deleted) while running, when free space falls below this |
| `MF_PROBE_TIMEOUT_S` / `MF_PROCESS_TIMEOUT_S` | 30 / 900 | |
| `MF_FILE_TTL_MINUTES` | 60 | uploads and outputs are deleted after this (or on DELETE) |
| `MF_WORK_DIR`, `MF_CACHE_DIR`, `MF_SAMPLES_DIR` | `var/work`, `var/cache`, `samples` | |
| `MF_REQUIRE_REAL_SAMPLES` | 1 | 0 = advertise on self-tests alone (not recommended) |

---

## Architecture

```
frontend/   React 19 + esbuild → frontend/dist (served by the backend)
  src/App.jsx            three tabs (Convert Images / Convert Document / Convert Videos); footer link to Technical details
  src/ImagePage.jsx      upload → format → convert → download; compression, target size, resize, advanced in collapsible sections
  src/VideoPage.jsx      same flow for video: server plan, HDR and "not included" choices, job progress + cancel, result + player
  src/DocumentPage.jsx   document tool cards + focused tool screen (all "Coming soon"; PDF upload is checked only)
  src/DetailsPage.jsx    Formats (FormatsPanel.jsx) and System (SystemPanel.jsx), moved out of the main navigation
  src/ui.jsx, icons.jsx, useUpload.js   shared upload card, collapsible sections, notices, icons, one-file upload state
  src/Compare.jsx        before/after wipe comparison with 100 % view
backend/    Python ASGI app (Starlette + uvicorn)
  app/main.py            HTTP API
  app/capabilities/      self-tests + real-sample verification → what this machine can read/write
  app/sniff.py           file type from magic bytes (extensions never trusted)
  app/inspect.py         opens the file with the real backend; details + valid outputs + controls
  app/convert/settings.py  presets, per-format controls, resize/target/metadata options + validation
  app/convert/image_ops.py decode → orient → colour → transparency → resize → encode → verify → previews
  app/convert/heif_tools.py HEIF parser + FFmpeg HEVC check (independent HEIC verification)
  app/convert/worker.py  sandboxed worker process (timeout, memory cap)
  app/video/probe.py     ffprobe → displayed size (rotation + SAR), HDR, bit depth, VFR, tracks, chapters, attachments
  app/video/plan.py      per-container rules; copy vs re-encode per stream with reasons; what is dropped; FFmpeg args
  app/video/jobs.py      background queue: progress, cancel, duplicate detection, time/disk limits
  app/video/verify.py    output checks before download (container, streams, size, duration, sync, full decode)
  app/video/selftest.py  every container × codec pair on a real clip + content-based A/V sync + HDR tests
  app/store.py           per-upload directories with expiry
  app/proc.py            safe subprocess runner, sweeper
samples/    real/ (files from other software — add your own), derived/, damaged/
scripts/build_samples.py
```

Starlette is used instead of FastAPI because the build environment could not reach PyPI; Starlette is the framework
FastAPI is built on, and the API layer (`app/main.py`) is small enough to port later.

### API

| Method | Path | |
|---|---|---|
| GET | `/api/capabilities` | verified formats, evidence, limits, licences (503 while detecting) |
| POST | `/api/uploads` | raw body + `X-Filename`; stores and inspects; returns report + `id` |
| POST | `/api/items/{id}/convert` | JSON options (below); runs the worker; returns result |
| GET | `/api/items/{id}/outputs/{oid}/{name}` | download (correct MIME, attachment, UTF-8 names) |
| GET | `/api/items/{id}/outputs/{oid}/.preview/{before,after}.png` | comparison previews |
| GET | `/api/items/{id}/original` | the uploaded file (kept until expiry/delete) |
| DELETE | `/api/items/{id}` | delete upload and all outputs now (cancels its video jobs) |
| POST | `/api/items/{id}/video/plan` | video options (below) → what will be copied, re-encoded or dropped, and why |
| POST | `/api/items/{id}/video/jobs` | queue a video conversion → 202 `{job}`; identical request → 200 with the existing job |
| GET | `/api/jobs/{job}` | state (`queued`/`running`/`done`/`failed`/`cancelled`), percent, speed, ETA, result or error |
| POST | `/api/jobs/{job}/cancel` | stop FFmpeg and delete partial output |
| POST | `/api/inspect` | stateless inspection (nothing stored) |

Convert options: `format`, `preset` (`best`/`balanced`/`smallest`), `settings` (per-format control values),
`frames` (`keep`/`first`/`separate`), `transparency` (`keep`/`flatten`), `background` (`#rrggbb`),
`resize` (`mode`: `none`/`width`/`height`/`percent`/`fit`, `width`, `height`, `percent`, `keep_aspect`, `allow_upscale`),
`target` (`bytes`, `allow_resize`, `min_quality`), `metadata` (`keep`/`remove_gps`/`remove_all`), `icc` (`keep`/`srgb`).

Video options: `container`, `video_codec` / `audio_codec` (`auto` = copy when possible, `copy`, or a codec id),
`force_reencode`, `audio_tracks` (stream indexes to keep), `subtitles`, `chapters`, `hdr` (`preserve`/`tonemap`,
required for HDR input), `metadata` (`keep`/`remove_location`/`remove_all`), `confirm_drops` (required when anything
would be discarded).

---

## How formats are verified

Nothing is advertised because a library claims support.

1. **Round-trip self-test** — each format is encoded and decoded on this machine; output bytes are sniffed to
   confirm the encoder wrote the right format (ImageMagick 6 "succeeds" at writing HEIC but writes PNG — caught).
   Every video container × video codec × audio codec pair is converted from a real clip with real audio, probed, fully
   decoded and checked for A/V sync with a flash/beep probe (±45 ms). Failed pairs are withdrawn; if a container's
   default pair fails, the next passing pair becomes the default.
2. **Independent input test** — every file in `samples/real` (from other software) and `samples/derived` (real
   content re-encoded by FFmpeg) is fully decoded. HEIC/HEIF and camera RAW require a file in `samples/real`.
3. **Output test through the real pipeline** — a real photo, a real transparent PNG, a real animated GIF and a real
   multipage TIFF are converted into every candidate format and read back; FFmpeg (an independent decoder) must
   decode our output with the same pixels. The test suite also opens outputs in Chromium.

The Formats page (footer → Technical details) shows round-trip and independent results in separate columns.

### Current results on the reference machine

**Images — read (14):** JPEG, PNG, WebP, AVIF, GIF, BMP, TIFF, ICO, SVG (rasterised), JPEG 2000, QOI, TGA, PNM, PCX
**Images — write (16):** JPEG, PNG, WebP, AVIF, GIF (1-bit transparency), BMP, TIFF, ICO, JPEG 2000, QOI, TGA, PNM, PCX, DDS, SGI, ICNS
**HEIC/HEIF:** Unavailable in this environment (libheif has no HEVC decoder plugin; no HEIC encoder; pillow-heif not installable).
**Camera RAW:** Unverified — real camera samples needed (LibRaw decodes a synthetic DNG only).
**Video — read (11):** MP4, MOV, MKV, WebM, AVI, MPEG-PS, MPEG-TS, Ogg, FLV, 3GP, WMV/ASF · **write (10):** same minus WMV/ASF.
146 container/codec pairs pass; 5 are withdrawn for A/V sync (Ogg + Theora +367 ms with every audio codec; AVI +
H.264 with AC-3 −61 ms or PCM −67 ms). HDR keep: MP4/MOV/MKV/WebM with 10-bit HEVC, AV1 or VP9 (MOV: HEVC only);
HDR → SDR tone mapping verified.
**PDF:** pikepdf, pypdfium2 and Pillow verified; tools arrive in Milestone 7.

### Enabling HEIC and RAW

HEIC reading and writing are gated **independently**:

- *Read* is enabled when a decoder (pillow-heif, or ImageMagick with a working libheif HEVC plugin) decodes an
  HEIC built by a different encoder (FFmpeg/libx265), **and** a real HEIC in `samples/real/heic/` decodes.
- *Write* is enabled when pillow-heif's output parses as HEIF and its HEVC data decodes in FFmpeg — no HEIC reader is
  needed — **and** real content round-trips through the conversion pipeline.

Install `pillow-heif` (`python3 -m pip install pillow-heif`; its wheels bundle x265, GPL-2.0), add real iPhone HEICs to
`samples/real/heic/` and real camera files to `samples/real/raw/`, then restart. Installing alone enables nothing.

---

## Image features (Milestones 2–3)

- **Presets:** Best quality, Balanced, Smallest file. Each sets the format's controls; editing any control switches to
  "Custom".
- **Format-specific controls, irrelevant ones hidden:**

  | Format | Lossy quality | Lossless effort (same pixels) | Other |
  |---|---|---|---|
  | JPEG | quality 1–100 | — | progressive, chroma subsampling 4:4:4 / 4:2:2 / 4:2:0 |
  | WebP | quality (only when lossy) | encoder effort 0–6 | lossless toggle |
  | AVIF | quality | encoder effort 0–10 | lossless AVIF is not implemented in this app |
  | PNG | — | compression effort 0–9 | "reduce to 256 colours" (lossy, keeps transparency) |
  | GIF | — | — | number of colours 2–256, dithering |
  | TIFF | — | — | compression LZW / Deflate / none (all lossless) |
  | JPEG 2000 | quality (only when lossy) | — | lossless toggle |
  | TGA | — | — | RLE (lossless) |
  | BMP, ICO, QOI, PNM, PCX, DDS, SGI, ICNS | no settings — the UI says so | | |

- **Resize** by width, height, percentage, or width + height (fit inside, or exact when the aspect lock is off).
  Aspect ratio locked by default; enlarging is off unless ticked (requests are clamped and a note says so).
  Sizes refer to the displayed (EXIF-rotated) orientation. Alpha is resampled premultiplied (no dark fringes).
- **Target size (best effort):** a bounded search (≤ 16 encodes, ≤ 70 % of the worker timeout) lowers only the
  format's lossy knob (quality, or GIF colours) down to a user-set floor (default 30). It never changes the format,
  frames, or — unless "Allow smaller dimensions" is ticked — the dimensions. Lossless formats only get maximum effort.
  The result says whether the target was met, the final size, the settings used and what was changed; if not met it
  suggests enabling smaller dimensions or a lossy format. No exact size or reduction is promised.
- **Before/after preview:** wipe comparison at the output's geometry, with an "actual pixels" 100 % view. "Before" is
  the source (oriented, colour-managed, transparency intact); "after" is the real output file (or a PNG decode of it for
  formats browsers cannot show).
- **Size comparison:** original vs output size and percentage change. Larger outputs are flagged, and the original
  stays downloadable until it expires or is deleted.
- **Metadata:** *keep supported metadata* (EXIF incl. MakerNote, and XMP; TIFF output keeps only the main EXIF block
  because Pillow cannot write EXIF sub-blocks) · *remove location* (default; deletes the EXIF GPS block, the camera
  MakerNote — which can hold location — and all XMP; keeps other EXIF) · *remove all EXIF and XMP*. Never copied in any
  mode: IPTC, JPEG comments, PNG text chunks and other format-specific text; this is disclosed in the UI. Orientation is always applied to the pixels and reset to 1 in EXIF and XMP, so removing
  metadata never rotates the image.
- **Colour profiles:** "keep" embeds the original profile when the format supports ICC (JPEG, PNG, WebP, AVIF, TIFF);
  for other formats the pixels are converted to sRGB so colours look the same. "Convert to sRGB" converts the pixels
  and embeds no profile. CMYK is converted to sRGB through its profile. Profiles are colour data, not personal
  metadata, so "remove all metadata" keeps the profile unless "convert to sRGB" is chosen.
- **From Milestone 2 (unchanged):** only verified outputs; every output re-read and checked for type, extension, MIME,
  frame count, total animation time and loop count; transparency kept or replaced by a chosen background colour; GIF
  1-bit transparency explained before converting; animated/multipage inputs require an explicit choice (keep / first /
  separate files as ZIP) when the target cannot keep them; 16-bit sources disclosed before converting.

## Video conversion (Milestone 4)

Upload → inspect → choose container → (optional) codecs, tracks, HDR, metadata → plan → convert in the background →
verified download with preview.

- **Inspection:** duration, displayed resolution (after rotation and pixel aspect ratio) and stored size, file size,
  container, video codec and bit depth, frame rate (VFR detected from real timestamps), audio codecs, dynamic range,
  every stream (language, title, default flag), chapters and attachments.
- **Only tested choices:** containers and codecs come from the self-tests; codec menus list only pairs that passed.
  Each container chip says "remux: no re-encoding" or "re-encode".
- **Plan before converting:** the server explains, per stream, whether it is copied or re-encoded and why (e.g. "WebM
  cannot hold H.264 video"). "Always re-encode" is available when copying would work.
- **Nothing discarded silently:** extra audio tracks, subtitles (text vs bitmap), chapters, attachments and extra video
  streams the target cannot hold are listed; conversion is refused until the user ticks "I understand these will not be
  in the output". Text subtitles are converted (e.g. SRT → mov_text) where the container needs it.
- **Picture geometry:** rotation is baked into the pixels on re-encode (or kept as metadata on remux); non-square
  pixels are converted to square by shrinking one axis, never enlarging; odd dimensions are trimmed by at most 1 px
  for encoders that need even sizes. Output displayed size is checked against the source (aspect within 1 %, never
  larger).
- **Timing:** timestamps are kept (VFR stays VFR) where the container supports it; MPEG-PS gets the nearest standard
  frame rate and AVI/FLV constant frame rate, with a note. Videos without audio produce outputs without audio.
- **HDR / high bit depth:** HDR10 (PQ) and HLG are detected. The user must choose *keep HDR* (10-bit HEVC/AV1/VP9 in
  MP4/MOV/MKV/WebM, tested paths only) or *convert to SDR* (zscale + Mobius tone mapping to BT.709). Plain conversion
  without tone mapping is not offered because it looks washed out. 10-bit SDR sources are disclosed and converted to
  8-bit only when the chosen codec needs it.
- **Metadata:** remove location tags (default; `location`, GPS, ISO 6709, `©xyz` tag names), remove all file-level
  metadata, or keep what the container supports. Location stored in vendor data streams is never copied because data
  streams are never converted.
- **Jobs:** one FFmpeg at a time by default, a bounded queue, real progress from FFmpeg (`-progress`), speed and ETA,
  cancel (FFmpeg's process group is terminated, partial files deleted), duplicate submissions return the existing job.
- **Output verification before download:** sniffed container, codecs, stream counts, displayed size, rotation, HDR or
  SDR signalling, duration (within max(0.3 s, 2 %)), audio/video start alignment, location removal, and a full decode
  with `-xerror`.
- **Result:** output details, size change (larger outputs flagged; original stays downloadable), an in-page player when
  the browser reports it can play that exact codec string (`canPlayType`), otherwise a poster frame and a download.

## Safety

- File type from content; a JPEG named `.png` is treated as JPEG.
- Uploads stream to a private random-id directory under a fixed name; every conversion gets its own sub-directory;
  failed jobs leave nothing; a sweeper deletes anything older than the TTL; "Clear finished" deletes immediately.
- Conversions run in a separate worker process with a timeout, CPU limit and memory cap.
- FFmpeg/ffprobe inputs: `-protocol_whitelist file`, the demuxer forced to the sniffed container (no playlist or
  concat tricks), `-enable_drefs 0` for MP4/MOV; each video job writes only inside its own directory.
- Subprocesses: argument lists only, closed stdin, timeouts with process-group kill, capped output, minimal env.
  ImageMagick runs under a deny-all policy allowing only RAW/HEIC reading and PNG writing.
- Pixel limits checked from headers before decoding; decoded-pixel limit across frames. Video duration, resolution,
  processing time, per-job disk use, free disk and concurrency are limited (see Configuration).
- Encoders receive only the metadata the user chose (Pillow's fallback to source `info` is blocked).
- CSP, `nosniff`, `X-Frame-Options: DENY`, `no-referrer`.

## Licences (checked Oct 2026)

| Component | Licence | Use |
|---|---|---|
| Pillow 12.3 | MIT-CMU | image codecs |
| FFmpeg 6.1 | GPL-2.0+ for this build (`--enable-gpl`) | video, SVG rasterising, independent decoding checks |
| ImageMagick 6.9 + LibRaw | ImageMagick licence; LibRaw LGPL-2.1/CDDL | camera RAW (fallback only) |
| pikepdf (qpdf) · pypdfium2 (PDFium) | MPL-2.0 (Apache-2.0) · Apache-2.0/BSD-3 | PDF |
| Starlette, uvicorn | BSD-3 | web |
| React, esbuild | MIT | frontend |
| pillow-heif (optional) | BSD-3 code; wheels bundle x265 (GPL-2.0) | HEIC |
| Rejected: PyMuPDF, Ghostscript | AGPL-3.0 | — |

Running as a web service with a GPL FFmpeg is fine; distributing the app bundled with it brings GPL obligations.

## Known limitations

See [milestone-4-status.md](milestone-4-status.md) for the current list (and
[milestone-3-status.md](milestone-3-status.md) for image-specific ones).
