import { useRef, useState } from "react";
import { formatBytes, us } from "./api.js";
import { IconAlert, IconCheck, IconChevron, IconInfo } from "./icons.jsx";

export const cx = (...xs) => xs.filter(Boolean).join(" ");

// "holiday-photo-from-the-b….png": shorten long names but keep the extension visible.
export function shortName(name, max = 32) {
  if (!name || name.length <= max) return name;
  const dot = name.lastIndexOf(".");
  const ext = dot > 0 && name.length - dot <= 6 ? name.slice(dot) : "";
  return `${name.slice(0, max - ext.length - 1)}…${ext}`;
}

const an = (word) => (/^[aeiou]/i.test(word) ? "an" : "a");

export function PageHead({ lead, mark, sub, children }) {
  return (
    <header className="head">
      <h1>{lead} <mark>{mark}</mark></h1>
      {sub && <p>{sub}</p>}
      {children}
    </header>
  );
}

export const Soon = () => <span className="soon">Coming soon</span>;

const NOTICE_ICONS = { warn: IconAlert, bad: IconAlert, info: IconInfo, good: IconCheck };

export function Notice({ tone = "warn", children, ...rest }) {
  const Icon = NOTICE_ICONS[tone];
  return (
    <div className={`notice ${tone}`} {...rest}>
      <Icon size={18} />
      <div>{children}</div>
    </div>
  );
}

// value 0–1, or leave it out for work whose length is unknown.
export function Bar({ value, label }) {
  const known = typeof value === "number";
  const pct = known ? Math.round(Math.min(1, Math.max(0, value)) * 100) : undefined;
  return (
    <div className={known ? "bar" : "bar unknown"} role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct}>
      <div style={known ? { width: `${pct}%` } : undefined} />
    </div>
  );
}

// A clearly labelled collapsible section. `summary` shows the active setting while closed.
export function Disclosure({ title, summary, changed, children }) {
  return (
    <details className="disc">
      <summary>
        <span className="disc-title">{title}</span>
        {summary && <span className={changed ? "disc-sum changed" : "disc-sum"}>{summary}</span>}
        <IconChevron size={18} className="chev" />
      </summary>
      <div className="disc-body">{children}</div>
    </details>
  );
}

// A section that exists in the plan but not in the product yet: visible, labelled, not operable.
export function SoonRow({ title }) {
  return (
    <div className="disc off" aria-disabled="true">
      <div className="disc-row"><span className="disc-title">{title}</span><Soon /></div>
    </div>
  );
}

export function FileLine({ name, meta, children }) {
  return (
    <div className="fileline">
      <span className="fname" title={name}>{shortName(name)}</span>
      {meta && <span className="fmeta">{meta.filter(Boolean).join(" · ")}</span>}
      {children}
    </div>
  );
}

// Output format picker. `soon` lists planned formats: shown, but not selectable.
export function FormatSelect({ id, value, options, onChange, disabled, soon = [] }) {
  const option = (o) => <option key={o.id} value={o.id} disabled={o.disabled}>{o.name}{o.disabled ? " — same as your file" : ""}</option>;
  const main = options.filter((o) => o.priority);
  const more = options.filter((o) => !o.priority);
  return (
    <div className="fsel">
      <select id={id} value={value} onChange={(e) => onChange(e.target.value)} disabled={disabled}>
        {main.map(option)}
        {more.length > 0 && <optgroup label="More formats">{more.map(option)}</optgroup>}
        {soon.length > 0 && <optgroup label="Coming soon">{soon.map((s) => <option key={s} disabled>{s}</option>)}</optgroup>}
      </select>
      <IconChevron size={22} />
    </div>
  );
}

export function EmptyOutput({ Icon, children }) {
  return (
    <div className="empty">
      <Icon size={34} />
      <p>{children}</p>
    </div>
  );
}

export function Working({ label, value }) {
  return (
    <div className="empty working">
      <p>{label}</p>
      <Bar value={value} label={label} />
    </div>
  );
}

const ERROR_TITLES = {
  unsupported: "This file type isn’t supported",
  damaged: "This file is damaged",
  encrypted: "This file is protected",
  too_large: "This file is too big",
  limit_exceeded: "This file is over the limit",
  empty: "This file is empty",
  timeout: "This took too long",
  network: "The upload didn’t go through",
  not_ready: "Still starting up",
};

function errorText(error) {
  const msg = error?.message || "Something went wrong. Please try again.";
  // The server's generic message points at a page that now lives under "Technical details".
  if (error?.code === "unsupported" && msg.includes("Formats page")) return "We couldn’t recognise this file. Try an image, a video or a PDF.";
  return us(msg);
}

// The upload card: drop target + file picker, then the uploaded file (children) with Replace / Remove.
export function SourceCard({ upload, noun, Icon, accept, maxMb, off, children }) {
  const input = useRef(null);
  const [dragging, setDragging] = useState(false);
  const [extra, setExtra] = useState(false);
  const { phase, file, progress, error } = upload;
  const pick = () => !off && input.current?.click();
  const take = (files) => {
    if (off || !files?.length) return;
    setExtra(files.length > 1);
    upload.start(files[0]);
  };
  const button = (label, cls = "btn primary") => (
    <button type="button" className={cls} disabled={off} onClick={(e) => { e.stopPropagation(); pick(); }}>{label}</button>
  );
  return (
    <section
      className={cx("cell cell-source", dragging && "dragging", phase === "ready" && "filled", off && "off")}
      aria-label={`Your ${noun}`}
      onDragOver={(e) => { if (off) return; e.preventDefault(); setDragging(true); }}
      onDragLeave={(e) => { if (!e.currentTarget.contains(e.relatedTarget)) setDragging(false); }}
      onDrop={(e) => { e.preventDefault(); setDragging(false); take(e.dataTransfer.files); }}
      onClick={phase === "idle" && !off ? pick : undefined}
    >
      <input ref={input} type="file" accept={accept} hidden disabled={off} onClick={(e) => e.stopPropagation()}
        onChange={(e) => { take(e.target.files); e.target.value = ""; }} />

      {phase === "idle" && (
        <div className="drop">
          <span className="drop-icon"><Icon size={34} /></span>
          <p className="drop-title">Drop {an(noun)} {noun} here</p>
          <span className="drop-or">or</span>
          {button(`Choose ${noun}`)}
          {off ? <Soon /> : <p className="muted small">Up to {maxMb} MB</p>}
        </div>
      )}

      {phase === "uploading" && (
        <div className="drop">
          <FileLine name={file.name} meta={[formatBytes(file.size)]} />
          <Bar value={progress} label="Upload progress" />
          <p className="muted small" role="status">{progress < 1 ? `Uploading… ${Math.round(progress * 100)}%` : "Checking the file…"}</p>
          <button type="button" className="btn outline small" onClick={upload.reset}>Cancel</button>
        </div>
      )}

      {phase === "error" && (
        <div className="drop" role="alert">
          <span className="drop-icon bad"><IconAlert size={30} /></span>
          <p className="drop-title">{ERROR_TITLES[error?.code] || "Something went wrong"}</p>
          <p className="muted">{errorText(error)}</p>
          {button(`Choose another ${noun}`)}
        </div>
      )}

      {phase === "ready" && (
        <>
          {children}
          {extra && <p className="muted small center">One file at a time — using the first one.</p>}
          <div className="source-actions">
            {button("Replace", "btn outline small")}
            <button type="button" className="btn ghost small" onClick={upload.reset}>Remove</button>
          </div>
        </>
      )}
    </section>
  );
}

// The uploaded file belongs on another tab (or isn't convertible at all).
export function WrongKind({ title, text, action, onAction }) {
  return (
    <div className="drop">
      <span className="drop-icon"><IconInfo size={30} /></span>
      <p className="drop-title">{title}</p>
      {text && <p className="muted">{text}</p>}
      {action && <button type="button" className="btn primary" onClick={onAction}>{action}</button>}
    </div>
  );
}
