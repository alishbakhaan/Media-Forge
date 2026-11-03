import { useEffect, useId, useMemo, useRef, useState } from "react";
import { cancelJob, formatBytes, formatDuration, formatName, getJob, planVideo, sentence, sizeChange, submitVideo, us } from "./api.js";
import { IconArrow, IconDownload, IconVideo } from "./icons.jsx";
import { Bar, Disclosure, EmptyOutput, FileLine, FormatSelect, Notice, PageHead, SourceCard, Working, WrongKind } from "./ui.jsx";
import { useUpload } from "./useUpload.js";

const ACTIVE = ["queued", "running", "verifying"];

// Upload → select output → convert (background job) → download, one video at a time.
const NO_SHRINK = { quality: null, height: null };

export default function VideoPage({ caps, incoming, onMove }) {
  const upload = useUpload(caps.limits.max_upload_mb);
  const [choice, setChoice] = useState(null);
  const [shrink, setShrink] = useState(NO_SHRINK); // "Reduce file size"
  const selectId = useId();
  const available = caps.video.available;
  const gifOut = Boolean(caps.video.gif?.to_gif?.verified);
  const gifIn = Boolean(caps.video.gif?.from_gif?.verified);

  // A file uploaded on another tab that turned out to be a video.
  useEffect(() => {
    if (incoming) upload.adopt(incoming.file, incoming.report);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [incoming]);

  const report = upload.phase === "ready" ? upload.report : null;
  const video = report?.kind === "video" ? report : null;
  const usable = Boolean(video && video.within_limits && video.outputs.length);
  const animatedGif = report?.kind === "image" && report.format.id === "gif" && report.image.animated && report.within_limits ? report : null;
  useEffect(() => setShrink(NO_SHRINK), [video?.id]);

  const accept = useMemo(
    () => ["video/*", ...caps.video.containers.filter((c) => c.read?.verified).flatMap((c) => c.extensions), ...(gifIn ? [".gif"] : [])].join(","),
    [caps, gifIn],
  );
  // The file's own format is disabled — converting MP4 to MP4 only makes sense when making it smaller.
  const shrinking = Boolean(shrink.quality || shrink.height);
  const outputs = useMemo(() => {
    const list = usable ? video.outputs : caps.video.containers.filter((c) => c.write?.verified);
    const all = list.map((o) => ({ ...o, name: formatName(o.id, o.label), disabled: usable && o.id === video.format.id && !shrinking }));
    if (gifOut) all.push({ id: "gif", label: "GIF", name: "GIF", priority: true });
    return all;
  }, [usable, video, caps, shrinking, gifOut]);
  const enabled = outputs.filter((o) => !o.disabled);

  // Until the user picks a format, default to the first one that differs from the upload (MP4 for most files).
  const fallback = enabled.find((o) => o.priority && o.id !== video?.format.id)?.id;
  const format = [choice, fallback].find((id) => enabled.some((o) => o.id === id)) || enabled[0]?.id;
  const out = outputs.find((o) => o.id === format);

  return (
    <div className="page">
      <PageHead lead="Convert" mark={usable ? `${formatName(video.format.id, video.format.label)} to ${out.name}` : animatedGif && gifIn ? "GIF to MP4" : "videos"}
        sub="Upload a video, choose a format, download the result." />
      {!available && <Notice tone="bad">Video conversion isn’t available on this server. {caps.video.notes?.join(" ")}</Notice>}
      <div className="bench">
        <SourceCard upload={upload} noun="video" Icon={IconVideo} accept={accept} maxMb={caps.limits.max_upload_mb} off={!available}>
          {video && <VideoSource report={video} />}
          {animatedGif && gifIn && <GifSource file={upload.file} report={animatedGif} />}
          {report?.kind === "image" && !(animatedGif && gifIn) && (
            <WrongKind title={report.format.id === "gif" ? "This GIF isn’t animated" : "This is an image"}
              text={report.format.id === "gif" ? "A still GIF is an image. Convert it on the Convert Images tab." : "Images are converted on their own tab."}
              action="Open in Convert Images" onAction={() => { upload.release(); onMove("images", upload.file, report); }} />
          )}
          {report?.kind === "pdf" && <WrongKind title="This is a PDF" text="PDFs have their own tools." action="Open Convert Document"
            onAction={() => { upload.reset(); window.location.hash = "#documents"; }} />}
        </SourceCard>

        {usable ? (
          <VideoSession key={video.id} report={video} outputs={outputs} format={format} onFormat={setChoice}
            selectId={selectId} onAnother={upload.reset} shrink={shrink} onShrink={setShrink} />
        ) : animatedGif && gifIn ? (
          <GifSession key={animatedGif.id} report={animatedGif} selectId={selectId} onAnother={upload.reset} />
        ) : (
          <>
            <section className="cell cell-format">
              <label className="cell-label" htmlFor={selectId}>Convert to</label>
              <FormatSelect id={selectId} value={format} options={outputs} onChange={setChoice} />
            </section>
            <section className="cell cell-controls">
              <button type="button" className="btn primary big" disabled>Convert to {out?.name} <IconArrow /></button>
              <p className="muted small center">Add a video to start.</p>
            </section>
            <section className="cell cell-result">
              <EmptyOutput Icon={IconVideo}>Your converted video will appear here.</EmptyOutput>
            </section>
          </>
        )}
      </div>
    </div>
  );
}

const QUALITIES = [[null, "Original quality", "No extra compression."], ["high", "High quality", "A little smaller, looks the same."],
  ["balanced", "Balanced", "Noticeably smaller, small quality loss."], ["small", "Smallest file", "Much smaller, visible quality loss."]];
const HEIGHTS = [2160, 1440, 1080, 720, 480, 360];
const GIF_WIDTHS = [240, 320, 480, 640, 800];
const GIF_FPS = [5, 8, 10, 12, 15, 20, 25];
const label = (h) => (h === 2160 ? "4K" : `${h}p`);

function VideoSource({ report }) {
  const [broken, setBroken] = useState(false);
  const v = report.video;
  const name = formatName(report.format.id, report.format.label);
  return (
    <>
      <div className="preview dark">
        {broken
          ? <div className="preview-none"><IconVideo size={36} /><span>No preview for {name} in this browser</span></div>
          : <video controls preload="metadata" src={`/api/items/${report.id}/original`} aria-label={`Original video: ${report.name}`} onError={() => setBroken(true)} />}
      </div>
      <FileLine name={report.name}
        meta={[name, formatBytes(report.size_bytes), formatDuration(v.duration), v.display_width && `${v.display_width} × ${v.display_height}`]} />
      {report.extension_mismatch && <Notice>The name ends in {report.extension}, but this is really a {name} file.</Notice>}
      {!report.within_limits && <Notice tone="bad">Too large to convert: {(v.limit_violations || []).join("; ")}.</Notice>}
    </>
  );
}

function VideoSession({ report, outputs, format, onFormat, selectId, onAnother, shrink, onShrink }) {
  const v = report.video;
  const isGif = format === "gif";
  const [gif, setGif] = useState(() => ({
    start: 0, end: Math.round((v.duration <= 15 ? v.duration : 10) * 10) / 10,
    width: Math.min(480, v.display_width), fps: Math.min(12, Math.round(v.fps || 25)),
  }));
  const audioTracks = v.streams.filter((s) => s.type === "audio");
  const out = outputs.find((o) => o.id === format);
  const uid = useId();
  const [videoCodec, setVideoCodec] = useState("auto");
  const [audioCodec, setAudioCodec] = useState("auto");
  const [tracks, setTracks] = useState(audioTracks.map((s) => s.index));
  const [subtitles, setSubtitles] = useState(true);
  const [chapters, setChapters] = useState(true);
  const [hdr, setHdr] = useState(null);
  const [confirm, setConfirm] = useState(false);
  const [metadata, setMetadata] = useState("remove_location");
  const [reencode, setReencode] = useState(false);
  const [planned, setPlanned] = useState(null); // { key, plan, error } — the server's answer for one set of choices
  const [job, setJob] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState(null); // { data, sig }
  const [error, setError] = useState(null);
  const poll = useRef(null);
  const resultRef = useRef(null);

  const choices = isGif ? { container: "gif", ...gif } : {
    container: format, video_codec: videoCodec, audio_codec: audioCodec,
    audio_tracks: audioTracks.length ? tracks : null, subtitles, chapters, hdr, metadata, force_reencode: reencode,
    quality: shrink.quality, max_height: shrink.height,
  };
  const planKey = JSON.stringify(choices);
  const body = { ...choices, confirm_drops: confirm };
  const sig = JSON.stringify(body);
  const current = result?.sig === sig;
  const hdrMissing = Boolean(v.hdr) && !hdr && !isGif;
  // Only trust an answer that belongs to the choices on screen.
  const plan = planned?.key === planKey ? planned.plan : null;
  const planError = planned?.key === planKey ? planned.error : null;

  // Ask the server what will happen whenever a choice changes.
  useEffect(() => {
    setConfirm(false);
    if (hdrMissing) return undefined; // the HDR choice comes first
    let alive = true;
    const t = setTimeout(async () => {
      const res = await planVideo(report.id, { ...choices, confirm_drops: false });
      if (!alive) return;
      setPlanned(res.ok
        ? { key: planKey, plan: res.body, error: null }
        : { key: planKey, plan: null, error: res.body?.error || { message: "Could not plan this conversion." } });
    }, 200);
    return () => { alive = false; clearTimeout(t); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [planKey]);

  useEffect(() => () => clearInterval(poll.current), []);

  const active = Boolean(job && ACTIVE.includes(job.state));
  const locked = active || submitting;
  const needsConfirm = Boolean(plan && plan.dropped.length > 0);
  const ready = plan && !planError && (!needsConfirm || confirm) && !locked && !hdrMissing;

  const choose = (id) => {
    onFormat(id);
    setVideoCodec("auto");
    setAudioCodec("auto");
    setError(null);
  };

  const start = async () => {
    setSubmitting(true);
    setError(null);
    const sent = sig;
    const res = await submitVideo(report.id, body);
    setSubmitting(false);
    if (!res.ok) { setError(us(res.body?.error?.message) || "Could not start the conversion."); return; }
    setJob(res.body);
    clearInterval(poll.current);
    poll.current = setInterval(async () => {
      const r = await getJob(res.body.id);
      if (!r.ok) return;
      setJob(r.body);
      if (!ACTIVE.includes(r.body.state)) {
        clearInterval(poll.current);
        if (r.body.state === "done") setResult({ data: r.body.result, sig: sent });
        if (r.body.state === "failed") setError(us(r.body.error?.message) || "Conversion failed.");
      }
    }, 700);
  };

  const cancel = async () => {
    if (!job) return;
    const r = await cancelJob(job.id);
    if (r.ok) setJob(r.body);
  };

  // Bring the finished result into view and put the keyboard on Download.
  useEffect(() => {
    if (!result) return;
    const el = resultRef.current;
    el?.querySelector("a.btn")?.focus({ preventScroll: true });
    el?.scrollIntoView({ block: "nearest", behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  }, [result]);

  const customCodecs = videoCodec !== "auto" || audioCodec !== "auto" || reencode;
  const metaSummary = { remove_all: "no file info", keep: "file info kept" }[metadata]; // removing location is the default
  const pct = Math.round((job?.progress || 0) * 100);

  return (
    <>
      <section className="cell cell-format">
        <label className="cell-label" htmlFor={selectId}>Convert to</label>
        <FormatSelect id={selectId} value={format} options={outputs} onChange={choose} disabled={locked} />
      </section>

      <section className="cell cell-controls" aria-label="Options">
        <fieldset className="plain" disabled={locked}>
          {isGif && (
            <fieldset className="choice">
              <legend>GIF settings</legend>
              <div className="row">
                <label className="field narrow"><span>Start (s)</span>
                  <input type="number" min="0" step="0.1" max={v.duration} value={gif.start}
                    onChange={(e) => setGif({ ...gif, start: Number(e.target.value) })} /></label>
                <label className="field narrow"><span>End (s)</span>
                  <input type="number" min="0" step="0.1" max={v.duration} value={gif.end}
                    onChange={(e) => setGif({ ...gif, end: Number(e.target.value) })} /></label>
              </div>
              <div className="row">
                <label className="field narrow"><span>Width</span>
                  <select value={gif.width} onChange={(e) => setGif({ ...gif, width: Number(e.target.value) })}>
                    {[...GIF_WIDTHS.filter((w) => w < v.display_width), v.display_width].map((w) => <option key={w} value={w}>{w} px</option>)}
                  </select></label>
                <label className="field narrow"><span>Frame rate</span>
                  <select value={gif.fps} onChange={(e) => setGif({ ...gif, fps: Number(e.target.value) })}>
                    {GIF_FPS.filter((f) => f <= Math.round(v.fps || 25)).map((f) => <option key={f} value={f}>{f} fps</option>)}
                  </select></label>
              </div>
              <p className="help">GIF has no sound. Keep it short — GIFs get big quickly (30 s at most).</p>
            </fieldset>
          )}
          {v.hdr && !isGif && (
            <fieldset className="choice">
              <legend>This video is HDR</legend>
              <label className="radio">
                <input type="radio" name={`${uid}-hdr`} checked={hdr === "preserve"} onChange={() => setHdr("preserve")} />
                <span>Keep HDR<small>Needs an HDR screen and player. Works with MP4, MOV, MKV and WebM.</small></span>
              </label>
              <label className="radio">
                <input type="radio" name={`${uid}-hdr`} checked={hdr === "tonemap"} onChange={() => setHdr("tonemap")} />
                <span>Convert to standard colors (SDR)<small>Plays everywhere.</small></span>
              </label>
              {!hdr && <p className="hint">Choose one to continue.</p>}
            </fieldset>
          )}

          {planError && <Notice tone="bad" role="alert">{us(planError.message)}</Notice>}
          {plan && !active && !current && !isGif && (
            <p className="planline">
              {plan.mode === "remux"
                ? "Quick conversion: picture and sound are copied as they are, with no quality loss."
                : `Needs re-encoding for ${out.name}, so it takes a little longer.`}
            </p>
          )}

          {needsConfirm && (
            <Notice>
              <strong>Not included in {out.name}:</strong>
              <ul>{plan.dropped.map((d) => <li key={d}>{sentence(us(d))}</li>)}</ul>
              <label className="check">
                <input type="checkbox" checked={confirm} onChange={(e) => setConfirm(e.target.checked)} />
                <span>Convert without these</span>
              </label>
            </Notice>
          )}
        </fieldset>

        {active ? (
          <div className="job">
            <div className="job-head">
              <strong>
                {job.state === "queued" && `Waiting in line${job.queue_position ? ` (position ${job.queue_position})` : ""}…`}
                {job.state === "running" && `Converting… ${pct}%`}
                {job.state === "verifying" && "Checking the result…"}
              </strong>
              <button type="button" className="btn outline small" onClick={cancel}>Cancel</button>
            </div>
            <Bar value={job.state === "verifying" ? 1 : job.state === "queued" ? 0 : job.progress || 0} label="Conversion progress" />
            {job.state === "running" && job.eta_s != null && (
              <p className="muted small">About {formatDuration(job.eta_s)} left{job.speed ? ` · ${job.speed}× speed` : ""}</p>
            )}
          </div>
        ) : (
          <button type="button" className={current ? "btn outline big" : "btn primary big"} disabled={!ready} onClick={start}>
            {submitting ? "Starting…" : current ? "Convert again" : <>Convert to {out.name} <IconArrow /></>}
          </button>
        )}
        {job?.state === "cancelled" && <Notice tone="info" role="status">Cancelled. Nothing was saved.</Notice>}
        {error && <Notice tone="bad" role="alert">{error}</Notice>}

        {!isGif && <div className="discs">
          <Disclosure title="Reduce file size" changed={Boolean(shrink.quality || shrink.height)}
            summary={shrink.quality || shrink.height
              ? [shrink.quality && QUALITIES.find(([q]) => q === shrink.quality)[1], shrink.height && `max ${label(shrink.height)}`].filter(Boolean).join(" · ")
              : "Off"}>
            <fieldset className="plain" disabled={locked}>
              <fieldset className="choice bare">
                <legend>Quality</legend>
                {QUALITIES.map(([q, name, help]) => (
                  <label key={name} className="radio">
                    <input type="radio" name={`${uid}-q`} checked={shrink.quality === q} onChange={() => onShrink({ ...shrink, quality: q })} />
                    <span>{name}<small>{help}</small></span>
                  </label>
                ))}
              </fieldset>
              <label className="field"><span>Size</span>
                <select value={shrink.height || ""} onChange={(e) => onShrink({ ...shrink, height: Number(e.target.value) || null })}>
                  <option value="">Keep {v.display_width} × {v.display_height}</option>
                  {HEIGHTS.filter((h) => h < Math.min(v.display_width, v.display_height)).map((h) => <option key={h} value={h}>{label(h)}</option>)}
                </select></label>
              <p className="help">Smaller files are re-encoded, so this takes longer. The result shows how much smaller it got.</p>
            </fieldset>
          </Disclosure>
          <Disclosure title="Advanced settings" changed={customCodecs || metadata !== "remove_location"}
            summary={[customCodecs ? "Custom codecs" : "Automatic", metaSummary].filter(Boolean).join(" · ")}>
            <fieldset className="plain" disabled={locked}>
              <fieldset className="choice bare">
                <legend>Codecs</legend>
                <label className="field"><span>Video</span>
                  <select value={videoCodec} onChange={(e) => setVideoCodec(e.target.value)}>
                    <option value="auto">Automatic (copy if possible, otherwise {out.default_video})</option>
                    {out.video_codecs.map((c) => <option key={c.id} value={c.id}>{c.label}{c.id === v.codec ? " (same as source)" : ""}</option>)}
                  </select></label>
                {v.has_audio && (
                  <label className="field"><span>Audio</span>
                    <select value={audioCodec} onChange={(e) => setAudioCodec(e.target.value)}>
                      <option value="auto">Automatic (copy if possible, otherwise {out.default_audio})</option>
                      {out.audio_codecs.map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}
                    </select></label>
                )}
                <label className="check">
                  <input type="checkbox" checked={reencode} onChange={(e) => setReencode(e.target.checked)} />
                  <span>Always re-encode, even when a straight copy would work</span>
                </label>
              </fieldset>

              {(audioTracks.length > 1 || v.counts.subtitle > 0 || v.chapters > 0) && (
                <fieldset className="choice bare">
                  <legend>Tracks to keep</legend>
                  {audioTracks.length > 1 && audioTracks.map((s) => (
                    <label key={s.index} className="check">
                      <input type="checkbox" checked={tracks.includes(s.index)}
                        onChange={(e) => setTracks((ts) => (e.target.checked ? [...ts, s.index].sort((a, b) => a - b) : ts.filter((x) => x !== s.index)))} />
                      <span>Audio #{s.index}: {s.codec}{s.language ? `, ${s.language}` : ""}{s.channels ? `, ${s.channels} ch` : ""}{s.default ? " (default)" : ""}</span>
                    </label>
                  ))}
                  {v.counts.subtitle > 0 && (
                    <label className="check"><input type="checkbox" checked={subtitles} onChange={(e) => setSubtitles(e.target.checked)} />
                      <span>Subtitles ({v.counts.subtitle})</span></label>
                  )}
                  {v.chapters > 0 && (
                    <label className="check"><input type="checkbox" checked={chapters} onChange={(e) => setChapters(e.target.checked)} />
                      <span>Chapters ({v.chapters})</span></label>
                  )}
                </fieldset>
              )}

              <fieldset className="choice bare">
                <legend>File info</legend>
                {[["remove_location", "Remove location", "Removes location tags such as phone GPS. Keeps title, dates and other tags."],
                  ["remove_all", "Remove all file info", "Track languages and titles stay."],
                  ["keep", "Keep file info", "Everything the new format supports, including any location."]].map(([id, label, help]) => (
                  <label key={id} className="radio">
                    <input type="radio" name={`${uid}-meta`} checked={metadata === id} onChange={() => setMetadata(id)} />
                    <span>{label}<small>{help}</small></span>
                  </label>
                ))}
                <p className="help">Location is found by tag name. Location hidden in vendor data streams isn’t copied — those streams are never converted.</p>
              </fieldset>
            </fieldset>

            <PlanDetails plan={plan} />
            <StreamDetails v={v} />
          </Disclosure>
        </div>}
        {isGif && plan && !current && <ul className="notes">{plan.notes.map((n) => <li key={n}>{sentence(us(n))}</li>)}</ul>}
      </section>

      <section className="cell cell-result" aria-label="Result" ref={resultRef}>
        {active ? <Working label={job.state === "running" ? `Converting… ${pct}%` : job.state === "verifying" ? "Checking the result…" : "Waiting in line…"}
            value={job.state === "running" ? job.progress || 0 : undefined} />
          : result ? <VideoResult r={result.data} current={current} onAnother={onAnother} />
          : <EmptyOutput Icon={IconVideo}>Your converted video will appear here.</EmptyOutput>}
        <p className="sr-only" role="status">
          {active ? "Converting" : result && job?.state === "done" ? "Conversion finished. The file is ready to download." : ""}
        </p>
      </section>
    </>
  );
}

// What the server will do with each stream — technical, so it lives under Advanced settings.
function PlanDetails({ plan }) {
  if (!plan) return null;
  const label = (a) => (a.action === "copy" ? "copy (no re-encoding)" : a.action === "convert" ? `convert to ${a.codec}` : `re-encode to ${a.codec}`);
  return (
    <div className="tech">
      <h4>What will happen</h4>
      <table>
        <tbody>
          <tr><th>Video</th><td>{label(plan.video)}{plan.video.action === "encode" && plan.video.reason ? <span className="muted"> — {plan.video.reason}</span> : null}</td></tr>
          {plan.audio.map((a) => (
            <tr key={a.index}><th>Audio #{a.index}</th><td>{label(a)}{a.language ? ` (${a.language})` : ""}{a.action === "encode" ? <span className="muted"> — {a.reason}</span> : null}</td></tr>
          ))}
          {plan.subtitles.map((s) => <tr key={s.index}><th>Subtitle #{s.index}</th><td>{label(s)}</td></tr>)}
          {plan.chapters?.count ? <tr><th>Chapters</th><td>{plan.chapters.action === "keep" ? `${plan.chapters.count} kept` : "not included"}</td></tr> : null}
          {plan.attachments.map((t) => <tr key={t.index}><th>Attachment</th><td>{t.filename} kept</td></tr>)}
        </tbody>
      </table>
      {plan.metadata?.summary && <p className="help">File info: {plan.metadata.summary}.</p>}
      {plan.notes.length > 0 && <ul className="notes">{plan.notes.map((n) => <li key={n}>{sentence(us(n))}</li>)}</ul>}
    </div>
  );
}

function StreamDetails({ v }) {
  const facts = [
    ["Container", v.container.label],
    ["Stored as", v.width && (v.width !== v.display_width || v.height !== v.display_height || v.rotation)
      ? `${v.width} × ${v.height}${v.rotation ? `, rotated ${v.rotation}°` : ""}${v.sar && v.sar !== "1:1" ? `, pixel aspect ${v.sar}` : ""}` : null],
    ["Video codec", v.codec ? `${v.codec}${v.bit_depth > 8 ? `, ${v.bit_depth}-bit` : ""}` : null],
    ["Audio", v.has_audio ? v.audio_codecs.join(", ") : "No audio"],
    ["Frame rate", v.fps ? `${v.fps} fps${v.vfr ? " (variable)" : ""}` : null],
    ["Dynamic range", v.hdr ? `${v.hdr} (HDR)` : v.color_transfer && v.color_transfer !== "bt709" ? v.color_transfer : null],
    ["Bitrate", v.bit_rate ? `${Math.round(v.bit_rate / 1000)} kb/s` : null],
    ["Attachments", v.attachments || null],
  ].filter(([, x]) => x !== null && x !== undefined && x !== "");
  return (
    <div className="tech">
      <h4>About this file</h4>
      <dl className="facts">{facts.map(([k, x]) => <div key={k}><dt>{k}</dt><dd>{x}</dd></div>)}</dl>
      <div className="table-wrap">
        <table>
          <thead><tr><th>#</th><th>Type</th><th>Codec</th><th>Details</th></tr></thead>
          <tbody>
            {v.streams.map((s) => (
              <tr key={s.index}>
                <td>{s.index}</td>
                <td>{s.type}{s.attached_pic ? " (cover art)" : ""}</td>
                <td>{s.codec}{s.profile ? ` · ${s.profile}` : ""}</td>
                <td>
                  {s.type === "video" && `${s.width}×${s.height} ${s.fps ? `@ ${s.fps}` : ""} ${s.pix_fmt || ""}`}
                  {s.type === "subtitle" && (s.subtitle_kind || "")}
                  {s.type === "attachment" && (s.filename || "")}
                  {s.type === "audio" && `${s.channels} ch ${s.sample_rate ? `${s.sample_rate} Hz` : ""}`}
                  {s.language ? ` · ${s.language}` : ""}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function VideoResult({ r, current, onAnother }) {
  const f = r.files[0];
  const [playable, setPlayable] = useState(null);
  const isImage = Boolean(r.preview.image);
  useEffect(() => {
    if (r.preview.image) return;
    const el = document.createElement("video");
    const answer = el.canPlayType(`${r.preview.mime}; codecs="${r.preview.codecs}"`);
    setPlayable(answer === "probably" || answer === "maybe");
  }, [r.preview.mime, r.preview.codecs]);
  const verified = Object.values(f.checks).every(Boolean);
  const change = sizeChange(r.original_size, r.output_size);
  const name = formatName(r.format, r.label);
  return (
    <div className="result">
      {isImage ? <div className="preview checker"><img src={r.preview.url} alt="The converted GIF" /></div> : <div className="preview dark">
        {playable === true && <video controls preload="metadata" src={r.preview.url} poster={r.preview.poster || undefined} aria-label={`Converted ${name} video`} />}
        {playable === false && (r.preview.poster
          ? <img src={r.preview.poster} alt="First frame of the converted video" />
          : <div className="preview-none"><IconVideo size={36} /><span>No preview available</span></div>)}
      </div>}
      <FileLine name={f.name}
        meta={[`${formatBytes(r.original_size)} → ${formatBytes(r.output_size)}`, formatDuration(f.duration)]}>
        {change && <span className={`delta ${change.dir}`}>{change.text}</span>}
      </FileLine>

      {playable === false && <p className="muted small center">This browser can’t play {name} here. Download it to watch.</p>}
      {!current && <Notice tone="info">Settings changed. Convert again to update this result.</Notice>}
      {r.larger_than_original && (
        <Notice>The new file is larger than the original. <a href={r.original_url} download>Download original</a></Notice>
      )}
      {r.notes.length > 0 && <ul className="notes">{r.notes.map((n) => <li key={n}>{sentence(us(n))}</li>)}</ul>}

      <a className={current ? "btn primary big" : "btn outline big"} href={f.url} download={f.name}><IconDownload /> Download</a>
      <button type="button" className="btn outline" onClick={onAnother}>Convert another file</button>

      <details className="mini">
        <summary>Details</summary>
        <p className={verified ? "ev real" : "ev bad"} title={Object.entries(f.checks).map(([k, x]) => `${k}: ${x ? "ok" : "FAIL"}`).join("\n")}>
          {verified ? "Checked: format, streams, size, length and playback" : "The check of the new file failed"}
        </p>
        <dl className="facts">
          <div><dt>Mode</dt><dd>{r.plan.mode === "remux" ? "Copied (no re-encoding)" : "Re-encoded"}</dd></div>
          <div><dt>Video</dt><dd>{isImage ? `GIF, ${f.frames} frames at ${f.fps} fps` : `${f.codec}${f.bit_depth > 8 ? `, ${f.bit_depth}-bit` : ""}`}</dd></div>
          <div><dt>Audio</dt><dd>{f.audio?.length ? f.audio.join(", ") : "none"}</dd></div>
          <div><dt>Size</dt><dd>{f.display_width || f.width} × {f.display_height || f.height}</dd></div>
          {f.subtitles?.length > 0 && <div><dt>Subtitles</dt><dd>{f.subtitles.join(", ")}</dd></div>}
          {f.chapters > 0 && <div><dt>Chapters</dt><dd>{f.chapters}</dd></div>}
          {f.color_transfer && f.color_transfer !== "bt709" && <div><dt>Transfer</dt><dd>{f.color_transfer}</dd></div>}
          <div><dt>Took</dt><dd>{r.elapsed_s} s</dd></div>
        </dl>
      </details>
    </div>
  );
}

function GifSource({ file, report }) {
  const [url, setUrl] = useState(null);
  useEffect(() => {
    const u = URL.createObjectURL(file);
    setUrl(u);
    return () => URL.revokeObjectURL(u);
  }, [file]);
  const i = report.image;
  return (
    <>
      <div className="preview checker">{url && <img src={url} alt={`Original GIF: ${report.name}`} />}</div>
      <FileLine name={report.name} meta={["GIF", formatBytes(report.size_bytes), `${i.width} × ${i.height}`, `${i.frames} frames`]} />
    </>
  );
}

// Animated GIF → MP4: one output, an optional background colour, then the same job/progress/result as videos.
function GifSession({ report, selectId, onAnother }) {
  const [background, setBackground] = useState("#ffffff");
  const [planned, setPlanned] = useState(null);
  const [job, setJob] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const poll = useRef(null);
  const resultRef = useRef(null);
  const body = { container: "mp4", background };
  const sig = JSON.stringify(body);
  const plan = planned?.key === sig ? planned.plan : null;
  const planError = planned?.key === sig ? planned.error : null;

  useEffect(() => {
    let alive = true;
    const t = setTimeout(async () => {
      const res = await planVideo(report.id, body);
      if (alive) setPlanned({ key: sig, plan: res.ok ? res.body : null, error: res.ok ? null : res.body?.error || { message: "Could not plan this conversion." } });
    }, 200);
    return () => { alive = false; clearTimeout(t); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sig]);
  useEffect(() => () => clearInterval(poll.current), []);
  useEffect(() => {
    if (!result) return;
    resultRef.current?.querySelector("a.btn")?.focus({ preventScroll: true });
    resultRef.current?.scrollIntoView({ block: "nearest" });
  }, [result]);

  const active = Boolean(job && ACTIVE.includes(job.state));
  const current = result?.sig === sig;
  const pct = Math.round((job?.progress || 0) * 100);

  const start = async () => {
    setSubmitting(true);
    setError(null);
    const sent = sig;
    const res = await submitVideo(report.id, body);
    setSubmitting(false);
    if (!res.ok) { setError(us(res.body?.error?.message) || "Could not start the conversion."); return; }
    setJob(res.body);
    clearInterval(poll.current);
    poll.current = setInterval(async () => {
      const r = await getJob(res.body.id);
      if (!r.ok) return;
      setJob(r.body);
      if (!ACTIVE.includes(r.body.state)) {
        clearInterval(poll.current);
        if (r.body.state === "done") setResult({ data: r.body.result, sig: sent });
        if (r.body.state === "failed") setError(us(r.body.error?.message) || "Conversion failed.");
      }
    }, 700);
  };
  const cancel = async () => { if (job) { const r = await cancelJob(job.id); if (r.ok) setJob(r.body); } };

  return (
    <>
      <section className="cell cell-format">
        <label className="cell-label" htmlFor={selectId}>Convert to</label>
        <FormatSelect id={selectId} value="mp4" options={[{ id: "mp4", name: "MP4", priority: true }]} onChange={() => {}} disabled={active} />
      </section>
      <section className="cell cell-controls" aria-label="Options">
        <fieldset className="plain" disabled={active || submitting}>
          {plan?.gif_in.transparent && (
            <div className="bgpick">
              <label htmlFor={`${report.id}-bg`}>Background color</label>
              <input id={`${report.id}-bg`} type="color" value={background} onChange={(e) => setBackground(e.target.value)} />
              <code>{background}</code>
              <p className="help">Fills the transparent areas — MP4 can’t keep transparency.</p>
            </div>
          )}
          {planError && <Notice tone="bad" role="alert">{us(planError.message)}</Notice>}
          {plan && !active && !current && <p className="planline">Every frame keeps its timing. GIFs have no sound.</p>}
        </fieldset>
        {active ? (
          <div className="job">
            <div className="job-head">
              <strong>{job.state === "running" ? `Converting… ${pct}%` : job.state === "verifying" ? "Checking the result…" : "Waiting in line…"}</strong>
              <button type="button" className="btn outline small" onClick={cancel}>Cancel</button>
            </div>
            <Bar value={job.state === "verifying" ? 1 : job.progress || 0} label="Conversion progress" />
          </div>
        ) : (
          <button type="button" className={current ? "btn outline big" : "btn primary big"} disabled={!plan || submitting} onClick={start}>
            {submitting ? "Starting…" : current ? "Convert again" : <>Convert to MP4 <IconArrow /></>}
          </button>
        )}
        {job?.state === "cancelled" && <Notice tone="info" role="status">Cancelled. Nothing was saved.</Notice>}
        {error && <Notice tone="bad" role="alert">{error}</Notice>}
      </section>
      <section className="cell cell-result" aria-label="Result" ref={resultRef}>
        {active ? <Working label={job.state === "running" ? `Converting… ${pct}%` : "Checking the result…"} value={job.progress || undefined} />
          : result ? <VideoResult r={result.data} current={current} onAnother={onAnother} />
          : <EmptyOutput Icon={IconVideo}>Your MP4 will appear here.</EmptyOutput>}
      </section>
    </>
  );
}
