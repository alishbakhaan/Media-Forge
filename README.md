# Verification samples

Formats are only advertised after files here pass. Detection re-runs automatically when this folder changes.

- `real/` — files made by other software. **Add your own here** (any sub-folder name):
  - iPhone/Android **HEIC** photos → `real/heic/`
  - **Camera RAW** files (CR2, CR3, NEF, ARW, DNG, RAF, ORF, RW2…) → `real/raw/`
  - Phone/camera **MOV/MP4**, screen recordings, **MKV/WebM/AVI** downloads → `real/video/`
  HEIC/HEIF and RAW are never advertised from synthetic or re-encoded files.
- `derived/` — real content re-encoded by FFmpeg, used where no real file is available. Built by `scripts/build_samples.py`.
- `damaged/` — broken/encrypted files used by the tests only.
