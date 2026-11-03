# MediaForge handoff (end of Milestone 4, 5 Oct 2026)

Resume here tomorrow. Main reference: `README.md`. Milestone reports: `milestone-3-status.md`, `milestone-4-status.md`.

## State

- Milestones 1–4 are done. **106 tests pass.** The work is paused for your review of Milestone 4.
- The package `mediaforge-m4.zip` was checked from a clean unzip: build_samples → run.sh → capabilities (10 video containers, 146 codec pairs) → one real conversion.
  - Python packages came from the system install, because PyPI and npm are blocked in the build sandbox. `pip install -r backend/requirements.txt` itself was not exercised there.
- Stack: Python 3.13, Starlette + uvicorn, Pillow 12.3, FFmpeg 6.1 (GPL build), ImageMagick 6 (RAW fallback only), pikepdf + pypdfium2, React 19 + esbuild.

## Completed features

**M1 Interface and capabilities**
- Files / Formats / System tabs.
- Read and write support is detected separately on the running machine.
- The Formats tab shows round-trip and independent-file evidence in separate columns.

**M2 Image conversion**
- 14 readable and 16 writable image formats.
- File type is detected from content.
- Orientation and colour are handled, and transparency is kept or flattened onto a chosen background.
- Animated and multipage inputs need an explicit choice.
- Every output is re-read and checked.

**M3 Image compression**
- Presets and per-format controls (lossy quality separate from lossless effort).
- Resize without upscaling.
- Best-effort target size.
- Before/after wipe comparison.
- Metadata options: keep / remove location / remove all.
- Colour profile options.
- 16-bit sources disclosed.

**M4 Video conversion**
- Inspection: displayed size, codecs, streams, VFR, HDR, chapters, attachments.
- Only the 146 container/codec pairs that passed real-clip, decode and A/V-sync tests are offered.
- Plan shows copy vs re-encode per stream, with reasons.
- Anything the output can't hold must be confirmed before it is dropped.
- HDR: keep it (tested paths only) or tone-map to SDR.
- Rotation and aspect ratio are kept; no upscaling.
- Background job queue: progress, ETA, cancel, duplicate protection.
- Limits: duration, resolution, time, per-job disk, free disk, concurrency, queue length.
- Output is verified and fully decoded before download.
- In-page player when the browser can play the file, otherwise a poster frame and a download.
- Deleting an upload cancels its jobs.

## Known limitations

- HEIC: **Unavailable in this environment.**
- Camera RAW: **Unverified — real camera samples needed.**
- Video real-file coverage:
  - The only independent file is one MP4 with no audio.
  - No real phone or camera MOV, MKV, WebM or AVI files.
  - No real HDR footage; the HDR clips are made with FFmpeg.
- Withdrawn for A/V sync: Ogg+Theora (+367 ms); AVI with H.264 + AC-3 or PCM (about −61 to −67 ms).
- Browser playback of H.264/AAC is unverified: the test Chromium build lacks those codecs. The UI falls back to a poster frame and a download.
- Jobs live in memory, so a restart loses queued and running jobs. Finished files stay until they expire (60 min).
- Stopping a running job on low disk is implemented but untested; refusing a new job on low disk is tested.
- Image metadata:
  - IPTC, JPEG comments and PNG text are never copied.
  - TIFF output keeps only the main EXIF block.
  - Lossless AVIF is not implemented in this app.
- Tested on Linux only, not macOS.

## Remaining milestones

5. **Video compression:**
   - quality/CRF presets, bitrate, resolution and frame-rate reduction
   - target file size (two-pass, best effort, never promised)
   - audio bitrate
   - before/after comparison
6. **Batch processing:**
   - many files with shared settings, per-file status and errors
   - ZIP download
   - built on the M4 job queue
7. **PDF tools:**
   - merge, split, rotate, compress, images↔PDF, page extraction
   - backends already verified: pikepdf, pypdfium2
8. **Reliability and deployment:**
   - persistent job store across restarts
   - Docker image, auth/rate limiting, logging/metrics, macOS check, licence notice for the GPL FFmpeg

## Open questions for the review

- Is the M4 UI, and its requirement to confirm before dropping anything, acceptable?
- Could you provide real phone or camera videos (MOV, HDR, multi-track) and iPhone HEIC files to close the unverified items?
