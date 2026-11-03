import { useEffect, useId, useMemo, useRef, useState } from "react";
import { convertItem, formatBytes, formatName, sentence, sizeChange, us } from "./api.js";
import Compare from "./Compare.jsx";
import { IconArrow, IconCheck, IconDownload, IconImage } from "./icons.jsx";
import { Bar, Disclosure, EmptyOutput, FileLine, FormatSelect, Notice, PageHead, SourceCard, Working, WrongKind } from "./ui.jsx";
import { useUpload } from "./useUpload.js";

const PRESETS = [
  { id: "best", label: "Best quality" },
  { id: "balanced", label: "Balanced" },
  { id: "smallest", label: "Smallest file" },
];

// Mirrors backend settings.Resize.compute so the user sees the output size before converting.
export function computeResize(r, w, h) {
  if (r.mode === "none") return { w, h, clamped: false };
  let nw, nh;
  const n = (v) => Number(v) || 0;
  if (r.mode === "percent") { nw = (w * n(r.percent)) / 100; nh = (h * n(r.percent)) / 100; }
  else if (r.mode === "width") { nw = n(r.width); nh = r.keep_aspect ? (h * nw) / w : h; }
  else if (r.mode === "height") { nh = n(r.height); nw = r.keep_aspect ? (w * nh) / h : w; }
  else if (r.keep_aspect) { const s = Math.min(n(r.width) / w, n(r.height) / h); nw = w * s; nh = h * s; }
  else { nw = n(r.width); nh = n(r.height); }
  nw = Math.max(1, Math.round(nw)); nh = Math.max(1, Math.round(nh));
  let clamped = false;
  if (!r.allow_upscale && (nw > w || nh > h)) {
    clamped = true;
    if (r.keep_aspect || r.mode === "percent") { const s = Math.min(w / nw, h / nh); nw = Math.max(1, Math.round(nw * s)); nh = Math.max(1, Math.round(nh * s)); }
    else { nw = Math.min(nw, w); nh = Math.min(nh, h); }
  }
  return { w: nw, h: nh, clamped };
}

const defaultFrames = (o) => (o.frame_options.find((f) => f.id === "keep") ? "keep" : null);

// Upload → select output → convert → download, one image at a time.
export default function ImagePage({ caps, incoming, onMove }) {
  const upload = useUpload(caps.limits.max_upload_mb);
  const [choice, setChoice] = useState(null);
  const selectId = useId();

  // A file uploaded on another tab that turned out to be an image.
  useEffect(() => {
    if (incoming) upload.adopt(incoming.file, incoming.report);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [incoming]);

  const report = upload.phase === "ready" ? upload.report : null;
  const image = report?.kind === "image" ? report : null;
  const usable = Boolean(image && image.within_limits && image.outputs.length);

  const accept = useMemo(
    () => ["image/*", ...caps.images.formats.filter((f) => f.read?.verified).flatMap((f) => f.extensions)].join(","),
    [caps],
  );
  const outputs = useMemo(() => {
    const list = usable ? image.outputs : caps.images.formats.filter((f) => f.write?.verified);
    return list.map((o) => ({ ...o, name: formatName(o.id, o.label) }));
  }, [usable, image, caps]);

  // Until the user picks a format, default to JPG (PNG when the upload already is a JPG).
  const fallback = image?.format.id === "jpeg" ? "png" : "jpeg";
  const format = [choice, fallback].find((id) => outputs.some((o) => o.id === id)) || outputs[0]?.id;
  const out = outputs.find((o) => o.id === format);

  return (
    <div className="page">
      <PageHead lead="Convert" mark={usable ? `${formatName(image.format.id, image.format.label)} to ${out.name}` : "images"}
        sub="Upload an image, choose a format, download the result." />
      <div className="bench">
        <SourceCard upload={upload} noun="image" Icon={IconImage} accept={accept} maxMb={caps.limits.max_upload_mb}>
          {image && <ImageSource file={upload.file} report={image} />}
          {report?.kind === "video" && (
            <WrongKind title="This is a video" text="Videos are converted on their own tab."
              action="Open in Convert Videos" onAction={() => { upload.release(); onMove("videos", upload.file, report); }} />
          )}
          {report?.kind === "pdf" && <WrongKind title="This is a PDF" text="PDFs have their own tools." action="Open Convert Document"
            onAction={() => { upload.reset(); window.location.hash = "#documents"; }} />}
        </SourceCard>

        {usable ? (
          <ImageSession key={image.id} report={image} outputs={outputs} format={format} onFormat={setChoice}
            cutoutOk={Boolean(caps.images.remove_background?.verified)}
            selectId={selectId} onAnother={upload.reset} />
        ) : (
          <>
            <section className="cell cell-format">
              <label className="cell-label" htmlFor={selectId}>Convert to</label>
              <FormatSelect id={selectId} value={format} options={outputs} onChange={setChoice} />
            </section>
            <section className="cell cell-controls">
              <button type="button" className="btn primary big" disabled>Convert to {out?.name} <IconArrow /></button>
              <p className="muted small center">Add an image to start.</p>
            </section>
            <section className="cell cell-result">
              <EmptyOutput Icon={IconImage}>Your converted image will appear here.</EmptyOutput>
            </section>
          </>
        )}
      </div>
    </div>
  );
}

function ImageSource({ file, report }) {
  const [url, setUrl] = useState(null);
  const [broken, setBroken] = useState(false);
  useEffect(() => {
    const u = URL.createObjectURL(file);
    setUrl(u);
    setBroken(false);
    return () => URL.revokeObjectURL(u);
  }, [file]);
  const i = report.image;
  const [w, h] = i.display_size || [i.width, i.height];
  const name = formatName(report.format.id, report.format.label);
  return (
    <>
      <div className="preview checker">
        {url && !broken
          ? <img src={url} alt={`Original image: ${report.name}`} onError={() => setBroken(true)} />
          : <div className="preview-none"><IconImage size={36} /><span>No preview for {name} in this browser</span></div>}
      </div>
      <FileLine name={report.name} meta={[name, formatBytes(report.size_bytes), `${w} × ${h}`]} />
      {report.extension_mismatch && <Notice>The name ends in {report.extension}, but this is really a {name} file.</Notice>}
      {!report.within_limits && <Notice tone="bad">Too large to convert: {(i.limit_violations || []).join("; ")}.</Notice>}
      {i.warnings?.map((x) => <Notice key={x}>{us(x)}</Notice>)}
    </>
  );
}

const CUTOUT_FORMATS = ["png", "webp", "avif", "tiff"];

function ImageSession({ report, outputs, format, onFormat, selectId, onAnother, cutoutOk }) {
  const info = report.image;
  const [srcW, srcH] = info.display_size || [info.width, info.height];
  const out = outputs.find((o) => o.id === format);
  const uid = useId();
  const [preset, setPreset] = useState("best");
  const [custom, setCustom] = useState(false);
  const [values, setValues] = useState(() => ({ ...out.settings.presets.best }));
  const [frames, setFrames] = useState(() => defaultFrames(out));
  const [transparency, setTransparency] = useState("keep");
  const [background, setBackground] = useState("#ffffff");
  const [resize, setResize] = useState({ mode: "none", width: srcW, height: srcH, percent: 50, keep_aspect: true, allow_upscale: false });
  const [target, setTarget] = useState({ enabled: false, amount: 200, unit: "KB", allow_resize: false, min_quality: 30 });
  const [metadata, setMetadata] = useState("remove_gps");
  const [icc, setIcc] = useState("keep");
  const [cutout, setCutout] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null); // { data, sig }
  const resultRef = useRef(null);

  const needsFrames = out.frame_options.length > 0;
  const alpha = out.transparency; // null when the image has no transparent areas
  const controls = out.settings.controls;
  const knob = out.settings.target_knob && !(out.settings.target_knob === "quality" && values.lossless) ? out.settings.target_knob : null;
  const dims = useMemo(() => computeResize(resize, srcW, srcH), [resize, srcW, srcH]);
  const resizeValid = resize.mode === "none" || (dims.w > 0 && dims.h > 0);
  const ready = (!needsFrames || frames) && !busy && resizeValid;

  const canCut = cutoutOk && CUTOUT_FORMATS.includes(out.id) && (!needsFrames || frames === "first");
  const body = { format: out.id, preset, settings: values, background, metadata, icc, ...(canCut && cutout ? { remove_background: true } : {}) };
  if (needsFrames) body.frames = frames;
  if (alpha?.mode === "binary") body.transparency = transparency;
  if (resize.mode !== "none") {
    body.resize = { mode: resize.mode, keep_aspect: resize.keep_aspect, allow_upscale: resize.allow_upscale };
    if (resize.mode === "width" || resize.mode === "fit") body.resize.width = Number(resize.width);
    if (resize.mode === "height" || resize.mode === "fit") body.resize.height = Number(resize.height);
    if (resize.mode === "percent") body.resize.percent = Number(resize.percent);
  }
  if (target.enabled) {
    body.target = {
      bytes: Math.round(Number(target.amount) * (target.unit === "MB" ? 1024 * 1024 : 1024)),
      allow_resize: target.allow_resize,
      min_quality: Number(target.min_quality),
    };
  }
  const sig = JSON.stringify(body);
  const current = result?.sig === sig; // the result on screen was made with exactly these settings

  const choose = (id) => {
    const o = outputs.find((x) => x.id === id);
    if (!o) return;
    onFormat(id);
    setError(null);
    setFrames(defaultFrames(o));
    setTransparency("keep");
    setCustom(false);
    setValues({ ...o.settings.presets[preset] });
  };
  const pickPreset = (p) => {
    setPreset(p);
    setCustom(false);
    setValues({ ...out.settings.presets[p] });
  };
  const setValue = (id, v) => {
    setValues((vs) => ({ ...vs, [id]: v }));
    setCustom(true);
  };

  const run = async () => {
    setBusy(true);
    setError(null);
    const res = await convertItem(report.id, body);
    setBusy(false);
    if (res.ok) setResult({ data: res.body, sig });
    else setError(us(res.body?.error?.message) || "Conversion failed.");
  };

  // Bring the finished result into view and put the keyboard on Download.
  useEffect(() => {
    if (!result) return;
    const el = resultRef.current;
    el?.querySelector("a.btn")?.focus({ preventScroll: true });
    el?.scrollIntoView({ block: "nearest", behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  }, [result]);

  const presetLabel = PRESETS.find((p) => p.id === preset).label;
  const metaSummary = metadata === "keep" ? (info.gps ? "Location kept" : "Metadata kept") : metadata === "remove_gps" ? "Location removed" : "No metadata";
  const keepHelp = out.metadata_support.xmp ? "Camera details, dates and any location (EXIF and XMP)."
    : out.id === "tiff" ? "Only the main EXIF block — TIFF leaves out exposure and GPS details."
    : out.metadata_support.exif ? "Camera details, dates and any location (EXIF)."
    : `${out.name} can’t store metadata, so none is written.`;
  const formatNotes = out.notes.filter((n) => !/transparen|frames|pages|bit per channel/.test(n));

  return (
    <>
      <section className="cell cell-format">
        <label className="cell-label" htmlFor={selectId}>Convert to</label>
        <FormatSelect id={selectId} value={format} options={outputs} onChange={choose} disabled={busy} />
        {formatNotes.map((n) => <p key={n} className="muted small center">{sentence(us(n))}</p>)}
      </section>

      <section className="cell cell-controls" aria-label="Options">
        <fieldset className="plain" disabled={busy}>
          {needsFrames && (
            <fieldset className="choice">
              <legend>{info.animated ? `Animation · ${info.frames} frames` : `${info.frames} pages`}</legend>
              {out.frame_options.map((f) => (
                <label key={f.id} className="radio">
                  <input type="radio" name={`${uid}-frames`} checked={frames === f.id} onChange={() => setFrames(f.id)} />
                  <span>{us(f.label)}</span>
                </label>
              ))}
              {!frames && <p className="hint">Choose one to continue.</p>}
            </fieldset>
          )}

          {alpha?.mode === "binary" && (
            <fieldset className="choice">
              <legend>Transparency</legend>
              <p className="help">GIF pixels are either see-through or solid — nothing in between.</p>
              <label className="radio">
                <input type="radio" name={`${uid}-alpha`} checked={transparency === "keep"} onChange={() => setTransparency("keep")} />
                <span>Keep transparency<small>Soft edges blend into the background color.</small></span>
              </label>
              <label className="radio">
                <input type="radio" name={`${uid}-alpha`} checked={transparency === "flatten"} onChange={() => setTransparency("flatten")} />
                <span>Fill transparent areas<small>Uses the background color.</small></span>
              </label>
              <BackgroundColor id={`${uid}-bg`} value={background} onChange={setBackground} />
            </fieldset>
          )}
          {alpha?.mode === "none" && (
            <BackgroundColor id={`${uid}-bg`} value={background} onChange={setBackground}
              help={`Fills the transparent areas — ${out.name} can’t keep transparency.`} />
          )}
          {!alpha && out.id === "jpeg" && (
            <BackgroundColor id={`${uid}-bg`} value={background} onChange={setBackground}
              help="Only fills transparent areas. This image has none, so nothing changes." />
          )}
          {alpha?.mode === "full" && <p className="keeps"><IconCheck size={16} /> Transparency is kept.</p>}

          {cutoutOk && CUTOUT_FORMATS.includes(out.id) && (
            <div className="bgpick">
              <label className="check">
                <input type="checkbox" checked={cutout && canCut} disabled={!canCut} onChange={(e) => setCutout(e.target.checked)} />
                <span>Remove background</span>
              </label>
              <p className="help">
                {canCut ? "AI cuts out the main subject and makes the rest transparent. Takes a few seconds; works best on one clear subject."
                  : "For animations, choose “First frame only” first."}
              </p>
            </div>
          )}

          {(info.bit_depth || 8) > 8 && (
            <Notice>This image has {info.bit_depth}-bit color. The result will be 8-bit — usually not visible.</Notice>
          )}
        </fieldset>

        <button type="button" className={current ? "btn outline big" : "btn primary big"} disabled={!ready} onClick={run}>
          {busy ? "Converting…" : current ? "Convert again" : <>Convert to {out.name} <IconArrow /></>}
        </button>
        {error && <Notice tone="bad" role="alert">{error}</Notice>}

        <fieldset className="plain discs" disabled={busy}>
          <Disclosure title="Compression" changed={controls.length > 0 && (custom || preset !== "best")}
            summary={controls.length === 0 ? `None for ${out.name}` : custom ? "Custom" : presetLabel}>
            {controls.length > 0 && (
              <div className="seg" role="radiogroup" aria-label="Preset">
                {PRESETS.map((p) => (
                  <label key={p.id} className={!custom && preset === p.id ? "on" : ""}>
                    <input type="radio" name={`${uid}-preset`} checked={!custom && preset === p.id} onChange={() => pickPreset(p.id)} />
                    {p.label}
                  </label>
                ))}
              </div>
            )}
            {out.settings.note && <p className="help">{us(out.settings.note)}</p>}
            {controls.map((c) => {
              if (c.when && Object.entries(c.when).some(([k, v]) => values[k] !== v)) return null;
              const v = values[c.id];
              if (c.kind === "toggle") {
                return (
                  <label key={c.id} className="check">
                    <input type="checkbox" checked={!!v} onChange={(e) => setValue(c.id, e.target.checked)} /> <span>{us(c.label)}</span>
                  </label>
                );
              }
              if (c.kind === "choice") {
                return (
                  <label key={c.id} className="field">
                    <span>{us(c.label)}</span>
                    <select value={v} onChange={(e) => setValue(c.id, e.target.value)}>
                      {c.options.map((o) => <option key={o.id} value={o.id}>{us(o.label)}</option>)}
                    </select>
                  </label>
                );
              }
              return (
                <label key={c.id} className="field">
                  <span>{us(c.label)} <strong>{v}</strong></span>
                  <input type="range" min={c.min} max={c.max} value={v ?? c.min} onChange={(e) => setValue(c.id, Number(e.target.value))} />
                  {c.kind === "quality" && <small>Lower means a smaller file with more visible loss.</small>}
                  {c.kind === "effort" && <small>Doesn’t change the picture — only how hard the file is squeezed.</small>}
                </label>
              );
            })}
          </Disclosure>

          <Disclosure title="Target file size" changed={target.enabled} summary={target.enabled ? `At most ${target.amount} ${target.unit}` : "Off"}>
            <label className="check">
              <input type="checkbox" checked={target.enabled} onChange={(e) => setTarget({ ...target, enabled: e.target.checked })} />
              <span>Aim for a maximum file size</span>
            </label>
            {target.enabled && (
              <>
                <div className="row">
                  <label className="field narrow"><span>At most</span>
                    <input type="number" min="1" value={target.amount} onChange={(e) => setTarget({ ...target, amount: e.target.value })} /></label>
                  <label className="field narrow"><span>Unit</span>
                    <select value={target.unit} onChange={(e) => setTarget({ ...target, unit: e.target.value })}>
                      <option>KB</option><option>MB</option></select></label>
                </div>
                {knob === "quality" && (
                  <label className="field"><span>Lowest quality allowed <strong>{target.min_quality}</strong></span>
                    <input type="range" min="1" max="100" value={target.min_quality}
                      onChange={(e) => setTarget({ ...target, min_quality: Number(e.target.value) })} /></label>
                )}
                <label className="check">
                  <input type="checkbox" checked={target.allow_resize} onChange={(e) => setTarget({ ...target, allow_resize: e.target.checked })} />
                  <span>Allow smaller dimensions if needed</span>
                </label>
                <p className="help">
                  Best effort, not exact. Lowers {knob === "colors" ? "the number of colors" : knob ? "quality" : "nothing — this format is lossless"}
                  {target.allow_resize ? ", then the dimensions" : ""}. The format {target.allow_resize ? "and" : "and dimensions and"} frames never change.
                </p>
              </>
            )}
          </Disclosure>

          <Disclosure title="Resize" changed={resize.mode !== "none"}
            summary={resize.mode === "none" ? `Original · ${srcW} × ${srcH}` : `${dims.w} × ${dims.h}`}>
            <label className="field">
              <span>Resize</span>
              <select value={resize.mode} onChange={(e) => setResize({ ...resize, mode: e.target.value })}>
                <option value="none">Keep original size</option>
                <option value="width">By width</option>
                <option value="height">By height</option>
                <option value="percent">By percentage</option>
                <option value="fit">Width and height</option>
              </select>
            </label>
            {resize.mode !== "none" && (
              <>
                <div className="row">
                  {(resize.mode === "width" || resize.mode === "fit") && (
                    <label className="field narrow"><span>Width px</span>
                      <input type="number" min="1" value={resize.width} onChange={(e) => setResize({ ...resize, width: e.target.value })} /></label>
                  )}
                  {(resize.mode === "height" || resize.mode === "fit") && (
                    <label className="field narrow"><span>Height px</span>
                      <input type="number" min="1" value={resize.height} onChange={(e) => setResize({ ...resize, height: e.target.value })} /></label>
                  )}
                  {resize.mode === "percent" && (
                    <label className="field narrow"><span>Percent</span>
                      <input type="number" min="1" max="1000" value={resize.percent} onChange={(e) => setResize({ ...resize, percent: e.target.value })} /></label>
                  )}
                </div>
                {resize.mode !== "percent" && (
                  <label className="check">
                    <input type="checkbox" checked={resize.keep_aspect} onChange={(e) => setResize({ ...resize, keep_aspect: e.target.checked })} />
                    <span>Keep proportions{resize.mode === "fit" && resize.keep_aspect ? " (fits inside the box)" : ""}</span>
                  </label>
                )}
                <label className="check">
                  <input type="checkbox" checked={resize.allow_upscale} onChange={(e) => setResize({ ...resize, allow_upscale: e.target.checked })} />
                  <span>Allow enlarging beyond the original</span>
                </label>
                <p className="help">Result: <strong>{dims.w} × {dims.h}</strong>{dims.clamped ? " — limited to the original size (enlarging is off)" : ""}</p>
              </>
            )}
          </Disclosure>

          <Disclosure title="Advanced settings" changed={metadata !== "remove_gps" || icc !== "keep"}
            summary={`${metaSummary}${icc === "srgb" ? " · sRGB" : ""}`}>
            <fieldset className="choice bare">
              <legend>Metadata</legend>
              {info.gps && <p className="hint">This image contains GPS location data.</p>}
              {[["keep", "Keep metadata", keepHelp],
                ["remove_gps", "Remove location", "Removes GPS, camera maker notes and XMP. Keeps camera, date and settings."],
                ["remove_all", "Remove all metadata", "Removes all EXIF and XMP."]].map(([id, label, help]) => (
                <label key={id} className="radio">
                  <input type="radio" name={`${uid}-meta`} checked={metadata === id} onChange={() => setMetadata(id)} />
                  <span>{label}<small>{help}</small></span>
                </label>
              ))}
              <p className="help">Never copied: IPTC, comments and text blocks. Location visible in the picture itself isn’t detected.</p>
            </fieldset>
            <fieldset className="choice bare">
              <legend>Color profile</legend>
              <label className="radio">
                <input type="radio" name={`${uid}-icc`} checked={icc === "keep"} onChange={() => setIcc("keep")} />
                <span>Keep the color profile<small>{out.metadata_support.icc ? "Embedded in the new file." : `${out.name} can’t store one, so colors are converted to sRGB.`}</small></span>
              </label>
              <label className="radio">
                <input type="radio" name={`${uid}-icc`} checked={icc === "srgb"} onChange={() => setIcc("srgb")} />
                <span>Convert to sRGB<small>Looks the same without a profile.</small></span>
              </label>
              <p className="help">Source profile: {info.icc_profile || "none (sRGB assumed)"}. Rotation is always applied to the picture itself.</p>
            </fieldset>
          </Disclosure>
        </fieldset>
      </section>

      <section className="cell cell-result" aria-label="Result" ref={resultRef}>
        {busy ? <Working label="Converting…" />
          : result ? <ImageResult r={result.data} current={current} onAnother={onAnother} />
          : <EmptyOutput Icon={IconImage}>Your converted image will appear here.</EmptyOutput>}
        <p className="sr-only" role="status">{busy ? "Converting" : result ? "Conversion finished. The file is ready to download." : ""}</p>
      </section>
    </>
  );
}

function BackgroundColor({ id, value, onChange, help }) {
  return (
    <div className="bgpick">
      <label htmlFor={id}>Background color</label>
      <input id={id} type="color" value={value} onChange={(e) => onChange(e.target.value)} />
      <code>{value}</code>
      {help && <p className="help">{help}</p>}
    </div>
  );
}

const SETTING_LABELS = { quality: "quality", effort: "effort", mode: "mode", progressive: "progressive", subsampling: "chroma",
  colors: "colors", dither: "dither", compression: "compression", rle: "RLE", sizes: "icon sizes" };

function ImageResult({ r, current, onAnother }) {
  const [compare, setCompare] = useState(false);
  const single = r.files.length === 1 ? r.files[0] : null;
  const first = r.files[0];
  const change = sizeChange(r.original_size, r.output_size);
  const t = r.target;
  const pv = r.previews || {};
  const name = formatName(r.format, r.label);
  const verified = r.files.every((f) => Object.values(f.checks).every((v) => v !== false));
  const download = single
    ? { href: single.url, name: single.name, label: "Download" }
    : r.bundle && { href: r.bundle.url, name: r.bundle.name, label: `Download ZIP · ${r.files.length} files` };
  return (
    <div className="result">
      {compare && pv.before && pv.after ? (
        <Compare before={pv.before} after={pv.after} width={pv.width} height={pv.height} maxHeight={320} />
      ) : (
        <div className="preview checker">
          {pv.after
            ? <img src={pv.after} alt={`Converted ${name} image`} />
            : <div className="preview-none"><IconImage size={36} /><span>No preview available</span></div>}
        </div>
      )}
      <FileLine name={single ? single.name : `${r.files.length} ${name} files`}
        meta={[`${formatBytes(r.original_size)} → ${formatBytes(r.output_size)}`, single && `${single.width} × ${single.height}`]}>
        {change && <span className={`delta ${change.dir}`}>{change.text}</span>}
      </FileLine>

      {pv.after && single && pv.after !== single.url && <p className="muted small center">Shown as a PNG copy — browsers can’t display {name}.</p>}
      {(pv.scaled || !single) && pv.after && <p className="muted small center">{!single ? "Preview shows the first file." : "Preview scaled down to fit."}</p>}
      {!current && <Notice tone="info">Settings changed. Convert again to update this result.</Notice>}
      {r.larger_than_original && (
        <Notice>The new file is larger than the original. <a href={r.original_url} download>Download original</a></Notice>
      )}
      {t && (
        <Notice tone={t.met ? "good" : "warn"}>
          <strong>{t.met ? "Target met" : "Target not met"}</strong> — aimed for {formatBytes(t.bytes)} or less
          {single ? `, got ${formatBytes(single.size_bytes)}` : ""}. {us(t.message)}
        </Notice>
      )}
      {r.notes.length > 0 && <ul className="notes">{r.notes.map((n) => <li key={n}>{sentence(us(n))}</li>)}</ul>}

      {download && (
        <a className={current ? "btn primary big" : "btn outline big"} href={download.href} download={download.name}>
          <IconDownload /> {download.label}
        </a>
      )}
      <button type="button" className="btn outline" onClick={onAnother}>Convert another file</button>
      {pv.before && pv.after && (
        <button type="button" className="btn ghost small" aria-pressed={compare} onClick={() => setCompare((c) => !c)}>
          {compare ? "Hide comparison" : "Compare with original"}
        </button>
      )}

      <details className="mini">
        <summary>Details</summary>
        <p className={verified ? "ev real" : "ev bad"}>
          {verified ? `Checked: opens correctly as ${first.extension} (${first.mime})` : "The check of the new file failed"}
        </p>
        <dl className="used">
          <div><dt>Settings</dt><dd>
            {r.settings?.preset && <span>{r.settings.preset} preset</span>}
            {Object.entries(first.settings || {}).map(([k, v]) => <span key={k}>{SETTING_LABELS[k] || k}: {String(v)}</span>)}
          </dd></div>
          {r.metadata && <div><dt>Metadata</dt><dd>
            {Object.entries(r.metadata).map(([k, v]) => <span key={k}>{k.toUpperCase()}: {us(v)}</span>)}</dd></div>}
        </dl>
        {!single && (
          <ul className="file-links">{r.files.map((f) => (
            <li key={f.name}><a href={f.url} download={f.name}>{f.name}</a> <span className="muted small">{formatBytes(f.size_bytes)}</span></li>))}</ul>
        )}
      </details>
    </div>
  );
}
