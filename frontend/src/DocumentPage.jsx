import { useEffect, useId, useRef, useState } from "react";
import { deleteItem, formatBytes, runDocTool, sizeChange, uploadFile, us } from "./api.js";
import { FileBadge, IconArrow, IconBack, IconCompress, IconDoc, IconDownload, IconImage, IconLock, IconMerge, IconRotate, IconSplit, IconTile } from "./icons.jsx";
import { Bar, EmptyOutput, FileLine, Notice, PageHead, SourceCard, Working, WrongKind, shortName } from "./ui.jsx";
import { useUpload } from "./useUpload.js";

// The document tools. `input` is what the server must detect in the upload; `multi` tools take several files in order.
const TOOLS = [
  { id: "pdf-to-word", title: "PDF to Word", from: ["PDF", "red"], to: ["DOC", "blue"], input: "pdf", blurb: "Turn a PDF into an editable Word document.",
    note: "Gets the text out of the PDF. The layout, columns and pictures are not kept, and scanned PDFs (pictures of text) can’t be read yet." },
  { id: "word-to-pdf", title: "Word to PDF", from: ["DOC", "blue"], to: ["PDF", "red"], input: "document", noun: "Word file",
    accept: ".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document", blurb: "Save a Word document as a PDF.",
    note: "Works with .docx files. Text, headings, lists, tables and pictures are kept; the exact page layout may differ from Word." },
  { id: "jpg-to-pdf", title: "JPG to PDF", from: ["JPG", "purple"], to: ["PDF", "red"], input: "image", multi: true, noun: "photo",
    accept: "image/*", blurb: "Put your photos into one PDF." },
  { id: "merge", title: "Merge PDF", icon: [IconMerge, "red"], input: "pdf", multi: true, blurb: "Join several PDFs into one." },
  { id: "compress", title: "Compress PDF", icon: [IconCompress, "red"], input: "pdf", blurb: "Make a PDF smaller.",
    note: "Shrinks the pictures inside the PDF. A PDF that is mostly text may not get much smaller." },
  { id: "protect", title: "Protect PDF", icon: [IconLock, "green"], input: "pdf", blurb: "Add a password to a PDF." },
  { id: "pdf-to-images", title: "PDF to images", from: ["PDF", "red"], to: ["IMG", "teal"], input: "pdf", blurb: "Save each page as a picture." },
  { id: "split", title: "Split PDF", icon: [IconSplit, "red"], input: "pdf", blurb: "Separate a PDF into smaller files." },
  { id: "rotate", title: "Rotate PDF", icon: [IconRotate, "red"], input: "pdf", blurb: "Turn pages the right way up." },
];
const PDF_ACCEPT = ".pdf,application/pdf";

function ToolIcons({ tool }) {
  if (tool.icon) return <span className="tool-icons"><IconTile Icon={tool.icon[0]} tone={tool.icon[1]} /></span>;
  return (
    <span className="tool-icons">
      <FileBadge label={tool.from[0]} tone={tool.from[1]} />
      <span className="tool-arrow" aria-hidden="true">→</span>
      <FileBadge label={tool.to[0]} tone={tool.to[1]} />
    </span>
  );
}

export default function DocumentPage({ caps, toolId }) {
  const tool = TOOLS.find((t) => t.id === toolId);
  if (tool) return <ToolScreen key={tool.id} tool={tool} caps={caps} />;
  return (
    <div className="page">
      <PageHead lead="Convert" mark="documents" sub="Pick a tool, add your file, download the result." />
      <ul className="tools">
        {TOOLS.map((t) => (
          <li key={t.id}>
            <a className="tool" href={`#documents/${t.id}`}>
              <ToolIcons tool={t} />
              <span className="tool-title">{t.title}</span>
            </a>
          </li>
        ))}
      </ul>
    </div>
  );
}

const DEFAULTS = { split: { mode: "each", ranges: "" }, rotate: { angle: 90, pages: "" }, compress: { level: "balanced" },
  protect: { password: "", confirm: "" }, "pdf-to-images": { format: "png" }, "jpg-to-pdf": { page: "fit" } };

// One tool, one job: files on the left, options and the result on the right.
function ToolScreen({ tool, caps }) {
  const upload = useUpload(caps.limits.max_upload_mb);
  const [list, setList] = useState([]); // multi-file tools: [{ key, file, state, report, error, progress }]
  const [opts, setOpts] = useState(DEFAULTS[tool.id] || {});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null);
  const listRef = useRef(list);
  listRef.current = list;

  // Leaving the tool removes the uploaded files from the server.
  useEffect(() => () => {
    upload.reset();
    listRef.current.forEach((x) => x.report?.id && deleteItem(x.report.id));
  }, [upload.reset]);

  const single = !tool.multi && upload.phase === "ready" && upload.report.kind === tool.input ? upload.report : null;
  const ready = tool.multi ? list.filter((x) => x.report).length >= (tool.id === "merge" ? 2 : 1) && list.every((x) => x.state !== "uploading")
    : Boolean(single && single.within_limits);
  const pwOk = tool.id !== "protect" || (opts.password.length >= 4 && opts.password === opts.confirm);
  const items = tool.multi ? list.filter((x) => x.report).map((x) => x.report.id) : single ? [single.id] : [];
  const sig = JSON.stringify([items, opts]);
  const current = result?.sig === sig;

  const run = async () => {
    setBusy(true);
    setError(null);
    const { confirm, ...options } = opts; // eslint-disable-line no-unused-vars
    const res = await runDocTool(tool.id, items, options);
    setBusy(false);
    if (res.ok) setResult({ data: res.body, sig });
    else setError(us(res.body?.error?.message) || "Something went wrong.");
  };
  const another = () => {
    setResult(null);
    setError(null);
    upload.reset();
    list.forEach((x) => x.report?.id && deleteItem(x.report.id));
    setList([]);
    if (tool.id === "protect") setOpts(DEFAULTS.protect);
  };

  const accept = tool.accept || PDF_ACCEPT;
  return (
    <div className="page">
      <a className="back" href="#documents"><IconBack size={18} /> All document tools</a>
      <PageHead lead="" mark={tool.title} sub={tool.blurb} />
      <div className="bench two">
        {tool.multi ? (
          <MultiSource tool={tool} list={list} setList={setList} accept={accept} maxMb={caps.limits.max_upload_mb} />
        ) : (
          <SourceCard upload={upload} noun={tool.noun || "PDF"} Icon={tool.input === "document" ? IconDoc : IconDoc} accept={accept} maxMb={caps.limits.max_upload_mb}>
            {single && <InputSummary report={single} />}
            {upload.phase === "ready" && upload.report.kind !== tool.input && (
              <WrongKind title={tool.input === "document" ? "This isn’t a Word file" : "This isn’t a PDF"}
                text={tool.input === "document" ? "This tool needs a .docx file." : "This tool needs a PDF file."} />
            )}
          </SourceCard>
        )}
        <section className="cell cell-controls" aria-label="Options">
          <fieldset className="plain" disabled={busy}>
            <ToolOptions tool={tool} opts={opts} setOpts={setOpts} pages={single?.pdf?.pages} />
          </fieldset>
          <button type="button" className={current ? "btn outline big" : "btn primary big"} disabled={!ready || !pwOk || busy} onClick={run}>
            {busy ? "Working…" : current ? "Do it again" : <>{tool.title} <IconArrow /></>}
          </button>
          {!ready && !busy && <p className="muted small center">{tool.multi ? (tool.id === "merge" ? "Add at least two PDFs." : "Add one or more photos.") : `Add ${tool.input === "document" ? "a Word file" : "a PDF"} to start.`}</p>}
          {error && <Notice tone="bad" role="alert">{error}</Notice>}
          {tool.note && !result && <p className="help">{tool.note}</p>}
          <div className="doc-result" aria-live="polite">
            {busy ? <Working label="Working…" />
              : result ? <DocResult r={result.data} current={current} onAnother={another} />
              : <EmptyOutput Icon={IconDoc}>Your file will appear here.</EmptyOutput>}
          </div>
        </section>
      </div>
    </div>
  );
}

function InputSummary({ report }) {
  if (report.kind === "pdf") {
    const p = report.pdf;
    const pages = `${p.pages} page${p.pages === 1 ? "" : "s"}`;
    return (
      <>
        <div className="preview short"><div className="preview-none"><FileBadge label="PDF" tone="red" /><span>{pages}</span></div></div>
        <FileLine name={report.name} meta={["PDF", formatBytes(report.size_bytes), pages]} />
        {!report.within_limits && <Notice tone="bad">Over the limit: {(p.limit_violations || []).join("; ")}.</Notice>}
      </>
    );
  }
  const d = report.document;
  return (
    <>
      <div className="preview short"><div className="preview-none"><FileBadge label="DOC" tone="blue" /><span>{d.words} words</span></div></div>
      <FileLine name={report.name} meta={["Word", formatBytes(report.size_bytes),
        d.tables ? `${d.tables} table${d.tables > 1 ? "s" : ""}` : null, d.images ? `${d.images} picture${d.images > 1 ? "s" : ""}` : null]} />
    </>
  );
}

// Several files, in the order they will appear in the result.
function MultiSource({ tool, list, setList, accept, maxMb }) {
  const input = useRef(null);
  const [dragging, setDragging] = useState(false);
  const patch = (key, p) => setList((xs) => xs.map((x) => (x.key === key ? { ...x, ...p } : x)));
  const add = (files) => {
    const fresh = [...files].slice(0, 50).map((file) => ({ key: `${file.name}-${Math.random()}`, file, state: "uploading", progress: 0 }));
    setList((xs) => [...xs, ...fresh]);
    fresh.forEach((x) => {
      if (x.file.size > maxMb * 1024 * 1024) { patch(x.key, { state: "error", error: `Larger than ${maxMb} MB.` }); return; }
      uploadFile(x.file, (p) => patch(x.key, { progress: p })).then((res) => {
        if (!res.ok) { patch(x.key, { state: "error", error: us(res.body?.error?.message) || "Upload failed." }); return; }
        if (res.body.kind !== tool.input) {
          deleteItem(res.body.id);
          patch(x.key, { state: "error", error: tool.input === "pdf" ? "Not a PDF." : "Not a photo." });
          return;
        }
        patch(x.key, { state: "ready", report: res.body });
      });
    });
  };
  const remove = (x) => { if (x.report?.id) deleteItem(x.report.id); setList((xs) => xs.filter((y) => y.key !== x.key)); };
  const move = (i, d) => setList((xs) => { const ys = [...xs]; [ys[i], ys[i + d]] = [ys[i + d], ys[i]]; return ys; });
  const noun = tool.input === "pdf" ? "PDFs" : "photos";
  return (
    <section className={`cell cell-source${dragging ? " dragging" : ""}${list.length ? " filled" : ""}`} aria-label={`Your ${noun}`}
      onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
      onDragLeave={(e) => { if (!e.currentTarget.contains(e.relatedTarget)) setDragging(false); }}
      onDrop={(e) => { e.preventDefault(); setDragging(false); add(e.dataTransfer.files); }}>
      <input ref={input} type="file" accept={accept} multiple hidden onChange={(e) => { add(e.target.files); e.target.value = ""; }} />
      {list.length === 0 ? (
        <div className="drop">
          <span className="drop-icon">{tool.input === "pdf" ? <IconDoc size={34} /> : <IconImage size={34} />}</span>
          <p className="drop-title">Drop your {noun} here</p>
          <span className="drop-or">or</span>
          <button type="button" className="btn primary" onClick={() => input.current?.click()}>Choose {noun}</button>
          <p className="muted small">Up to {maxMb} MB each</p>
        </div>
      ) : (
        <>
          <ol className="filelist">
            {list.map((x, i) => (
              <li key={x.key} className={x.state}>
                <span className="fl-num">{i + 1}</span>
                <span className="fl-main">
                  <span className="fname" title={x.file.name}>{shortName(x.file.name, 34)}</span>
                  <span className="fmeta">
                    {x.state === "uploading" ? "Uploading…" : x.state === "error" ? x.error
                      : [formatBytes(x.file.size), x.report.pdf ? `${x.report.pdf.pages} page${x.report.pdf.pages === 1 ? "" : "s"}` : x.report.image ? `${x.report.image.width} × ${x.report.image.height}` : null].filter(Boolean).join(" · ")}
                  </span>
                  {x.state === "uploading" && <Bar value={x.progress} label={`Uploading ${x.file.name}`} />}
                </span>
                <span className="fl-actions">
                  <button type="button" className="btn ghost small" disabled={i === 0} onClick={() => move(i, -1)} aria-label={`Move ${x.file.name} up`}>↑</button>
                  <button type="button" className="btn ghost small" disabled={i === list.length - 1} onClick={() => move(i, 1)} aria-label={`Move ${x.file.name} down`}>↓</button>
                  <button type="button" className="btn ghost small" onClick={() => remove(x)} aria-label={`Remove ${x.file.name}`}>✕</button>
                </span>
              </li>
            ))}
          </ol>
          <div className="source-actions">
            <button type="button" className="btn outline small" onClick={() => input.current?.click()}>Add more</button>
          </div>
        </>
      )}
    </section>
  );
}

function ToolOptions({ tool, opts, setOpts, pages }) {
  const uid = useId();
  const set = (k, v) => setOpts({ ...opts, [k]: v });
  const radios = (key, choices) => choices.map(([value, label, help]) => (
    <label key={value} className="radio">
      <input type="radio" name={`${uid}-${key}`} checked={opts[key] === value} onChange={() => set(key, value)} />
      <span>{label}{help && <small>{help}</small>}</span>
    </label>
  ));
  switch (tool.id) {
    case "split":
      return (
        <fieldset className="choice"><legend>Split into</legend>
          {radios("mode", [["each", "Every page as its own PDF"], ["ranges", "Page ranges"]])}
          {opts.mode === "ranges" && (
            <label className="field"><span>Ranges{pages ? ` (1–${pages})` : ""}</span>
              <input type="text" placeholder="e.g. 1-3, 4-8" value={opts.ranges} onChange={(e) => set("ranges", e.target.value)} /></label>
          )}
        </fieldset>
      );
    case "rotate":
      return (
        <fieldset className="choice"><legend>Rotate</legend>
          {radios("angle", [[90, "90° to the right"], [180, "Upside down (180°)"], [270, "90° to the left"]])}
          <label className="field"><span>Pages{pages ? ` (1–${pages})` : ""}</span>
            <input type="text" placeholder="All pages, or e.g. 2, 5-7" value={opts.pages} onChange={(e) => set("pages", e.target.value)} /></label>
        </fieldset>
      );
    case "compress":
      return (
        <fieldset className="choice"><legend>How much</legend>
          {radios("level", [["balanced", "Balanced", "Good quality, smaller file."], ["strong", "Strong", "Smallest file; pictures look softer."]])}
        </fieldset>
      );
    case "protect":
      return (
        <fieldset className="choice"><legend>Password</legend>
          <label className="field"><span>Password</span>
            <input type="password" autoComplete="new-password" value={opts.password} onChange={(e) => set("password", e.target.value)} /></label>
          <label className="field"><span>Type it again</span>
            <input type="password" autoComplete="new-password" value={opts.confirm} onChange={(e) => set("confirm", e.target.value)} /></label>
          {opts.password && opts.password.length < 4 && <p className="hint">Use at least 4 characters.</p>}
          {opts.confirm && opts.password !== opts.confirm && <p className="hint">The passwords don’t match.</p>}
          <p className="help">Keep it safe — a forgotten password can’t be recovered. Uses AES-256 encryption.</p>
        </fieldset>
      );
    case "pdf-to-images":
      return <fieldset className="choice"><legend>Picture format</legend>{radios("format", [["png", "PNG", "Sharpest, bigger files."], ["jpg", "JPG", "Smaller files."]])}</fieldset>;
    case "jpg-to-pdf":
      return <fieldset className="choice"><legend>Page size</legend>{radios("page", [["fit", "Fit each photo", "Each page is the size of its photo."], ["a4", "A4 pages", "Photos centred on A4 paper."]])}</fieldset>;
    default:
      return null;
  }
}

function DocResult({ r, current, onAnother }) {
  const many = r.files.length > 1;
  const dl = many ? r.bundle : r.files[0];
  const change = sizeChange(r.original_size, r.output_size);
  return (
    <div className="result">
      {r.preview && <div className="preview checker"><img src={r.preview} alt="First page of the result" /></div>}
      <FileLine name={many ? `${r.files.length} files` : r.files[0].name}
        meta={[`${formatBytes(r.original_size)} → ${formatBytes(many ? r.bundle.size_bytes : r.output_size)}`,
          r.pages ? `${r.pages} page${r.pages === 1 ? "" : "s"}` : null]}>
        {r.tool === "compress" && change && <span className={`delta ${change.dir}`}>{change.text}</span>}
      </FileLine>
      {!current && <Notice tone="info">Settings changed. Run it again to update this result.</Notice>}
      {r.notes.map((n) => <Notice key={n} tone="info">{n}</Notice>)}
      <a className={current ? "btn primary big" : "btn outline big"} href={dl.url} download={dl.name}>
        <IconDownload /> {many ? `Download ZIP · ${r.files.length} files` : "Download"}
      </a>
      <button type="button" className="btn outline" onClick={onAnother}>Start again with another file</button>
      {many && (
        <details className="mini"><summary>Individual files</summary>
          <ul className="file-links">{r.files.map((f) => <li key={f.name}><a href={f.url} download={f.name}>{f.name}</a> <span className="muted small">{formatBytes(f.size_bytes)}</span></li>)}</ul>
        </details>
      )}
    </div>
  );
}
