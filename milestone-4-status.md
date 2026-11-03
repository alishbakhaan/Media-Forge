# Milestone 4 status: video conversion

Date: 5 Oct 2026. Tests: **106 passing** (the earlier milestones' tests plus 24 new video tests and 2 new Chromium video tests).
**State: complete; paused for review.** The package was checked from a clean unzip: samples → run.sh → self-tests (146 pairs) → a conversion.

## Completed

| Requirement | Implementation |
|---|---|
| Upload → inspect → choose → convert → download | Video card with container chips, codec menus, track/HDR/metadata choices, a server-side plan, a background job and a verified result. |
| Inspection | Shows duration, displayed resolution (after rotation and SAR) and stored size, file size, container, video codec and bit depth, frame rate, VFR (from real timestamps), audio codecs, dynamic range, every stream, chapters and attachments. |
| Only tested combinations | 146 container × video × audio pairs passed real-clip conversion, decode and A/V sync tests; only those pairs are offered. 5 pairs were withdrawn (see "Bugs found"). |
| Job queue | In-memory queue with real FFmpeg progress, speed, ETA and cancel (process group killed, partial files deleted). Duplicate submissions return the existing job. The queue is bounded (429 when full). Deleting an upload cancels its jobs. |
| Result | Output details, size change (larger output flagged, original downloadable), verification badge, an in-page player when `canPlayType` accepts the exact codec string, otherwise a poster frame and a download. |
| Remux vs re-encode | Decided per stream, with the reason shown. "Always re-encode" is optional. |
| Nothing dropped silently | Extra audio tracks, subtitles, chapters, attachments and extra video streams that the target cannot hold are listed. Conversion requires an explicit confirmation. |
| Orientation and aspect | Rotation is baked in on re-encode or kept on remux. Non-square pixels are made square by shrinking one axis. The output is never upscaled; this is checked on every output. |
| Timing | VFR is kept where supported, with standard/constant frame rate for MPEG-PS, AVI and FLV (disclosed). Videos without audio are handled. Measured A/V sync change is 0–0.2 ms for all containers, except AVI at −10 ms. |
| HDR / high bit depth | HDR10 and HLG are detected, and the user must choose. **Keep HDR** is tested on MP4 (HEVC, AV1, VP9), MOV (HEVC), MKV (HEVC, AV1, VP9) and WebM (VP9, AV1). **Convert to SDR** uses Mobius tone mapping; its saturation is 18.9 against 7.1 for a naive conversion. A naive (washed-out) conversion is never offered. 10-bit SDR sources are disclosed. |
| Limits | Upload size, duration, resolution, processing time, per-job disk, free disk (checked at submit **and while running**), concurrency and queue length. |
| Safety | FFmpeg reads only local files (`-protocol_whitelist file`), uses the demuxer of the detected container, has external references disabled for MP4/MOV (`-enable_drefs 0`), and takes arguments as a list. Each job has its own isolated directory. |
| Output verification | Checks the detected container, codecs, streams, displayed size, rotation, HDR/SDR signalling, duration, start alignment and location removal, then fully decodes the output (`-xerror`). |
| UI/doc fixes | "Lossless AVIF is not implemented in this app". Image metadata options now say exactly what is kept or removed (MakerNote removed with location; TIFF keeps only the main EXIF block). The UI states the channels that are never copied. |

## Bugs found by the tests

1. **Ogg + Theora is +367 ms out of A/V sync** with every audio codec. Theora was withdrawn and Ogg now defaults to VP8.
2. **AVI with H.264 + AC-3 or PCM** was −61 ms or −67 ms out of sync. These pairs were withdrawn.
3. Copying H.264 into AVI produced a malformed bitstream; H.264 copy into AVI was removed.
4. MP2 audio rejected 256k at low sample rates; it now uses 192k at 48 kHz.
5. Writing TIFF with camera EXIF crashed, because Pillow cannot write EXIF sub-IFDs. Those sub-blocks are now dropped for TIFF, and the UI discloses this.
6. Fixed during this checkpoint:
   - Deleting an upload did not stop its running FFmpeg job. It now does, and a test covers it.
   - The mobile layout was 11 px wider than the screen. Fixed.

## Unverified / limitations

- No real HDR camera footage; the HDR clips are made with FFmpeg from a real photo.
- No real phone or camera MOV, MKV, WebM or AVI files. The only independent video is one real MP4 (no audio); the other video samples are FFmpeg-made from real content.
- H.264/AAC playback in a browser is unverified, because the test Chromium build cannot play it. The UI falls back to a poster and a download.
- Jobs are kept in memory. A server restart loses queued and running jobs; finished files remain until they expire.
- Stopping a running job when free disk space runs low is implemented but not exercised by a test. The check at submit is tested.
- There are no quality or size controls for video yet; those belong to Milestone 5.
- HEIC: **Unavailable in this environment.** Camera RAW: **Unverified — real camera samples needed.**

## Next

Milestone 5 (video compression) after your review. See `HANDOFF.md`.
