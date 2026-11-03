#!/usr/bin/env python3
"""Assemble the verification corpus in samples/.

samples/real/      Files made by other software (cameras, phones, apps, test suites).
                   ADD YOUR OWN HERE — especially iPhone HEIC and camera RAW files.
                   HEIC and RAW are only advertised once a file here decodes.
samples/derived/   Real image/video content re-encoded by FFmpeg into formats for which
                   no real file was available (e.g. WebP, AVIF, MKV). Weaker evidence.
samples/damaged/   Deliberately broken/encrypted files, used by the tests only.

This script copies known real files that ship with this Linux image (Go, scikit-image,
pngsuite, TeX Live, LibreOffice test/gallery data) if they exist, then derives the rest.
Missing sources are skipped; nothing is downloaded.

Usage:  python3 scripts/build_samples.py [--samples DIR]
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

GO = Path("/usr/local/go1.25.1/src/image")
SKI = Path("/usr/local/lib/python3.13/dist-packages/skimage/data")
MWE = Path("/usr/share/texlive/texmf-dist/tex/latex/mwe")
GALLERY = Path("/usr/lib/libreoffice/share/gallery")

REAL = {
    "jpeg": [GO / "testdata/video-001.jpeg", GO / "testdata/video-001.progressive.jpeg", GO / "testdata/video-001.cmyk.jpeg",
             GO / "testdata/video-005.gray.jpeg", GO / "testdata/video-001.q50.444.jpeg", SKI / "retina.jpg", SKI / "rocket.jpg",
             SKI / "hubble_deep_field.jpg", MWE / "example-image-a.jpg"],
    "png": [SKI / "astronaut.png", SKI / "logo.png", SKI / "camera.png", GO / "png/testdata/pngsuite/basn6a16.png",
            GO / "png/testdata/pngsuite/basn3p08-trns.png", GO / "png/testdata/pngsuite/basn0g16.png",
            GO / "png/testdata/pngsuite/basn4a08.png", GO / "testdata/video-001.png"],
    "gif": [SKI / "no_time_for_that_tiny.gif", GO / "testdata/video-001.interlaced.gif", GALLERY / "bigapple.gif", GALLERY / "flowers.gif"],
    "tiff": [SKI / "multipage_rgb.tif", SKI / "multipage.tif"],
    "ico": [Path("/usr/share/clang/scan-view-18/share/bugcatcher.ico"),
            Path("/usr/local/lib/python3.13/dist-packages/mkdocs/themes/mkdocs/img/favicon.ico")],
    "svg": [Path("/usr/share/icons/Humanity/places/64/user-trash-full.svg"), Path("/usr/share/icons/Humanity/places/48/stock_music-library.svg"),
            Path("/usr/share/icons/ubuntu-mono-light/places/64/folder-home.svg")],
    "mp4": [MWE / "example-movie.mp4"],  # H.264, no audio track
    "pdf": [Path("/usr/share/texmf/doc/fonts/tex-gyre/qhv-test.pdf"), MWE / "example-image-a4-numbered.pdf", MWE / "example-image-a.pdf"],
    "wav": [GALLERY / "sounds/kongas.wav", GALLERY / "sounds/roll.wav"],  # audio source for derived videos
}
DAMAGED = [GO / "testdata/video-001.progressive.truncated.jpeg", GO / "testdata/video-001.progressive.truncated.png",
           GO / "png/testdata/invalid-crc32.png", GO / "png/testdata/invalid-trunc.png", GO / "png/testdata/invalid-zlib.png"]


def ff(*args: str) -> bool:
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print("  ffmpeg failed:", " ".join(args[-1:]), res.stderr.strip()[-200:])
    return res.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=Path, default=ROOT / "samples")
    args = parser.parse_args()
    real, derived, damaged = (args.samples / d for d in ("real", "derived", "damaged"))
    for d in (real, derived, damaged):
        d.mkdir(parents=True, exist_ok=True)

    copied = 0
    for kind, files in REAL.items():
        for src in files:
            if src.exists():
                dest_dir = real / ("audio-source" if kind == "wav" else kind)
                dest_dir.mkdir(exist_ok=True)
                shutil.copyfile(src, dest_dir / src.name)
                copied += 1
    for src in DAMAGED:
        if src.exists():
            shutil.copyfile(src, damaged / src.name)
    print(f"copied {copied} real files")

    photo = real / "jpeg" / "retina.jpg"
    alpha = real / "png" / "logo.png"
    anim = real / "gif" / "no_time_for_that_tiny.gif"
    movie = real / "mp4" / "example-movie.mp4"
    wav = real / "audio-source" / "kongas.wav"
    wav2 = real / "audio-source" / "roll.wav"

    # ---- images: real content, encoded by FFmpeg's encoders (independent of Pillow's readers)
    if photo.exists():
        ff("-i", str(photo), "-c:v", "libwebp", "-quality", "80", str(derived / "photo.webp"))
        ff("-i", str(photo), "-c:v", "libaom-av1", "-still-picture", "1", "-cpu-used", "8", "-pix_fmt", "yuv420p", "-f", "avif", str(derived / "photo.avif"))
        ff("-i", str(photo), "-c:v", "bmp", str(derived / "photo.bmp"))
        ff("-i", str(photo), "-c:v", "libopenjpeg", str(derived / "photo.jp2"))
        ff("-i", str(photo), "-c:v", "qoi", str(derived / "photo.qoi"))
        ff("-i", str(photo), "-c:v", "targa", str(derived / "photo.tga"))
        ff("-i", str(photo), "-c:v", "ppm", "-f", "image2", str(derived / "photo.ppm"))
        ff("-i", str(photo), "-c:v", "pcx", str(derived / "photo.pcx"))
        ff("-i", str(photo), "-c:v", "sgi", str(derived / "photo.sgi"))
        ff("-i", str(photo), "-c:v", "tiff", "-compression_algo", "lzw", str(derived / "photo-lzw.tiff"))
        # Phone-style JPEG: EXIF orientation 6 + embedded sRGB ICC profile (re-encoded by Pillow).
        from PIL import Image, ImageCms
        with Image.open(photo) as im:
            exif = im.getexif()
            exif[0x0112] = 6
            exif[0x010F] = "SampleMaker"
            icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
            im.convert("RGB").save(derived / "rotated-exif6-icc.jpg", quality=92, exif=exif.tobytes(), icc_profile=icc)
            # Phone-style photo with GPS location in EXIF and XMP, plus camera make/model and orientation 6.
            gps_exif = im.getexif()
            gps_exif[0x0112] = 6
            gps_exif[0x010F] = "SampleCam"
            gps_exif[0x0110] = "Model 1"
            gps = gps_exif.get_ifd(0x8825)
            gps.update({1: "N", 2: (43.0, 39.0, 30.5), 3: "W", 4: (79.0, 23.0, 1.0)})
            xmp = ('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
                   '<rdf:Description xmlns:exif="http://ns.adobe.com/exif/1.0/" xmlns:tiff="http://ns.adobe.com/tiff/1.0/" '
                   'exif:GPSLatitude="43,39.508N" exif:GPSLongitude="79,23.017W" tiff:Orientation="6"/></rdf:RDF></x:xmpmeta>')
            im.convert("RGB").crop((100, 300, 700, 700)).save(derived / "gps-photo.jpg", quality=90, exif=gps_exif.tobytes(),
                                                                xmp=xmp.encode())
            # Camera-style EXIF: main block + Exif sub-IFD (with a MakerNote) + GPS sub-IFD.
            cam = Image.Exif()
            cam[0x010F], cam[0x0110] = "SampleCam", "Model 2"
            sub = cam.get_ifd(0x8769)
            sub[0x9003] = "2024:05:01 10:00:00"
            sub[0x927C] = b"VENDOR-MAKERNOTE-DATA"
            cam.get_ifd(0x8825).update({1: "N", 2: (43.0, 39.0, 30.5), 3: "W", 4: (79.0, 23.0, 1.0)})
            im.convert("RGB").crop((0, 0, 500, 400)).save(derived / "exif-full.jpg", quality=90, exif=cam.tobytes())
            # Non-square, so a wrong orientation shows up in the output dimensions.
            land = im.convert("RGB").crop((100, 300, 700, 700))  # 600×400
            land.save(derived / "rotated-exif6-landscape.jpg", quality=92, exif=exif.tobytes())
            # CMYK JPEG tagged with a real print profile (FOGRA39), as produced by print workflows.
            fogra = Path("/usr/share/texlive/texmf-dist/tex/generic/colorprofiles/FOGRA39L_coated.icc")
            if fogra.exists():
                cmyk = ImageCms.profileToProfile(land, ImageCms.createProfile("sRGB"), str(fogra), outputMode="CMYK")
                cmyk.save(derived / "cmyk-fogra39.jpg", quality=95, icc_profile=fogra.read_bytes())
                land.save(derived / "cmyk-fogra39-reference.png")  # what the colours should look like
    if alpha.exists():
        ff("-i", str(alpha), "-c:v", "libwebp", "-lossless", "1", str(derived / "logo-alpha.webp"))
        ff("-i", str(alpha), "-c:v", "libaom-av1", "-still-picture", "1", "-cpu-used", "8", "-f", "avif", str(derived / "logo-alpha.avif"))
    if anim.exists():
        ff("-i", str(anim), "-c:v", "libwebp_anim", "-loop", "0", "-lossless", "0", str(derived / "animated.webp"))
        ff("-i", str(anim), "-plays", "0", "-f", "apng", str(derived / "animated.apng"))

    # ---- video: the real H.264 clip (+ real audio) remuxed/transcoded into other containers
    if movie.exists() and wav.exists():
        common = ["-t", "4", "-i", str(movie), "-i", str(wav), "-map", "0:v", "-map", "1:a", "-shortest"]
        ff(*common, "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", str(derived / "clip.mov"))
        ff(*common, "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-c:a", "libopus", str(derived / "clip.webm"))
        ff(*common, "-c:v", "mpeg4", "-q:v", "4", "-c:a", "libmp3lame", str(derived / "clip.avi"))
        ff(*common, "-c:v", "mpeg2video", "-q:v", "4", "-c:a", "mp2", "-f", "mpeg", str(derived / "clip.mpg"))
        ff(*common, "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-f", "mpegts", str(derived / "clip.ts"))
        ff(*common, "-c:v", "libtheora", "-q:v", "6", "-c:a", "libvorbis", str(derived / "clip.ogv"))
        ff(*common, "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-ar", "44100", str(derived / "clip.flv"))
        ff(*common, "-c:v", "libx264", "-preset", "veryfast", "-vf", "scale=640:-2", "-c:a", "aac", str(derived / "clip.3gp"))
        ff(*common, "-c:v", "wmv2", "-q:v", "4", "-c:a", "wmav2", str(derived / "clip.wmv"))
        # Multi-stream MKV: HEVC video, two audio tracks (different languages), one subtitle track.
        srt = derived / "subs.srt"
        srt.write_text("1\n00:00:00,500 --> 00:00:02,000\nHello\n\n2\n00:00:02,500 --> 00:00:03,500\nWorld\n")
        ff("-t", "4", "-i", str(movie), "-i", str(wav), "-i", str(wav2), "-i", str(srt),
           "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3:s", "-shortest",
           "-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=error",
           "-c:a", "libopus", "-c:s", "srt", "-metadata:s:a:0", "language=eng", "-metadata:s:a:1", "language=fra",
           str(derived / "multistream.mkv"))
        srt.unlink(missing_ok=True)
        # Phone-style portrait clip: rotation stored as display-matrix metadata, not in pixels.
        ff("-display_rotation", "90", "-i", str(derived / "clip.mov"), "-c", "copy", str(derived / "rotated-90.mp4"))

        # ---- Milestone 4 video cases (all derived from the real clip + real audio) ----
        # Variable frame rate: every frame for 2 s, then every other frame (30 → 15 fps), timestamps kept.
        ff("-t", "4", "-i", str(movie), "-t", "4", "-i", str(wav), "-map", "0:v", "-map", "1:a",
           "-vf", "select='if(lt(t,2),1,not(mod(n,2)))'", "-fps_mode", "vfr", "-c:v", "libx264", "-preset", "veryfast",
           "-c:a", "aac", "-shortest", str(derived / "vfr.mp4"))
        # Anamorphic (non-square pixels): 720×480 stored, 32:27 pixel aspect → displays 16:9.
        ff("-t", "3", "-i", str(movie), "-vf", "scale=720:480,setsar=32/27", "-c:v", "libx264", "-preset", "veryfast",
           str(derived / "anamorphic.mp4"))
        # 10-bit SDR (BT.709) HEVC.
        ff("-t", "3", "-i", str(movie), "-vf", "scale=640:-2", "-c:v", "libx265", "-preset", "ultrafast",
           "-x265-params", "log-level=error", "-pix_fmt", "yuv420p10le", "-color_primaries", "bt709", "-color_trc", "bt709",
           "-colorspace", "bt709", "-tag:v", "hvc1", str(derived / "sdr-10bit.mp4"))
        # Phone-style location metadata in a MOV.
        ff("-i", str(derived / "clip.mov"), "-c", "copy", "-metadata", "location=+43.6589-079.3836/",
           "-metadata", "title=Location test", str(derived / "location.mov"))
        # Everything at once: 2 audio tracks, subtitles, chapters, a font attachment (MKV).
        chap = derived / "chapters.txt"
        chap.write_text(";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1500\ntitle=Intro\n"
                        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=1500\nEND=3500\ntitle=Main\n")
        font = next((p for p in (Path("/usr/share/tesseract-ocr/5/tessdata/pdf.ttf"),) if p.exists()), None)
        if (derived / "multistream.mkv").exists():
            extra = ["-attach", str(font), "-metadata:s:t", "mimetype=font/ttf"] if font else []
            ff("-i", str(derived / "multistream.mkv"), "-i", str(chap), "-map", "0", "-map_metadata", "1", "-map_chapters", "1",
               "-c", "copy", *extra, str(derived / "multitrack-full.mkv"))
        chap.unlink(missing_ok=True)

    # HDR10 and HLG clips made from a real photo (no real HDR camera footage is available here).
    photo_png = real / "png" / "astronaut.png"
    if photo_png.exists():
        sys.path.insert(0, str(ROOT / "backend"))
        from app.video.selftest import make_hdr_clip
        make_hdr_clip(photo_png, derived / "hdr10-pq.mp4", "smpte2084")
        make_hdr_clip(photo_png, derived / "hdr-hlg.mp4", "arib-std-b67")

    # ---- damaged / encrypted
    if movie.exists():
        data = movie.read_bytes()
        (damaged / "truncated.mp4").write_bytes(data[: len(data) // 3])
    pdf_src = real / "pdf" / "example-image-a.pdf"
    if pdf_src.exists():
        import pikepdf
        with pikepdf.open(pdf_src) as pdf:
            pdf.save(damaged / "password.pdf", encryption=pikepdf.Encryption(user="secret", owner="owner"))
        data = pdf_src.read_bytes()
        (damaged / "truncated.pdf").write_bytes(data[:600])
    rocket = real / "jpeg" / "rocket.jpg"
    if rocket.exists():
        # Two-page LZW TIFF whose second page is corrupt: accepted at upload (page 1 is fine),
        # must fail cleanly when all pages are converted.
        import io
        import random
        from PIL import Image
        src = Image.open(rocket).convert("RGB")
        buf = io.BytesIO()
        src.save(buf, "TIFF", save_all=True, append_images=[src.rotate(90, expand=True)], compression="tiff_lzw")
        data = bytearray(buf.getvalue())
        page = Image.open(io.BytesIO(bytes(data)))
        page.seek(1)
        rnd = random.Random(1)
        for off, cnt in zip(page.tag_v2[273], page.tag_v2[279]):
            for i in range(off, off + cnt):
                data[i] = rnd.randrange(256)
        (damaged / "tiff-bad-page2.tif").write_bytes(bytes(data))
    if (derived / "clip.mov").exists():
        # Index at the front (faststart), media cut off: inspects fine, fails part-way through decoding.
        tmp = damaged / "full-faststart.mp4"
        ff("-i", str(derived / "clip.mov"), "-c", "copy", "-movflags", "+faststart", str(tmp))
        if tmp.exists():
            data = tmp.read_bytes()
            (damaged / "cut-faststart.mp4").write_bytes(data[: int(len(data) * 0.6)])
            tmp.unlink()
    # Synthetic A/V sync probe (white flash + 1 kHz beep at exactly t = 2.0 s). Test-only, not used to advertise.
    synth = args.samples / "synthetic"
    synth.mkdir(exist_ok=True)
    ff("-f", "lavfi", "-i", "color=c=black:s=320x240:r=30:d=4,drawbox=x=0:y=0:w=320:h=240:color=white:t=fill:enable='between(t,2,2.1)'",
       "-f", "lavfi", "-i", "aevalsrc='if(between(t,2,2.1),0.8*sin(2*PI*1000*t),0)':s=48000:d=4",
       "-c:v", "libx264", "-bf", "3", "-c:a", "aac", "-shortest", str(synth / "sync.mov"))
    (damaged / "not-really.jpg").write_text("This is a text file pretending to be a JPEG.\n")
    if photo.exists():
        shutil.copyfile(photo, damaged / "photo-named-as.png")  # wrong extension, valid JPEG content

    print(f"derived: {len(list(derived.iterdir()))} files, damaged: {len(list(damaged.iterdir()))} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
