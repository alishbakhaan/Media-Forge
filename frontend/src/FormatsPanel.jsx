import { useState } from "react";

const yes = (v) => (v ? "✓" : "—");

export default function FormatsPanel({ caps }) {
  return (
    <section className="stack">
      <ImageFormats images={caps.images} />
      <VideoFormats video={caps.video} />
      <PdfTools pdf={caps.pdf} />
    </section>
  );
}

// ---- evidence badges -------------------------------------------------------
// Round-trip: our own encoder/decoder agree with each other.
// Independent: a file from other software decodes here, or our output decodes in another decoder.

function InRoundTrip({ row }) {
  const st = row.read || row.read_selftest;
  if (!st) return <span className="ev bad" title={row.notes.join("\n")}>failed</span>;
  return <span className="ev rt" title={st.detail || "Encoded a test image and decoded it again"}>pass</span>;
}

function InIndependent({ row }) {
  const r = row.real_samples;
  if (!r) return <span className="ev">—</span>;
  const tip = [...r.files, ...r.derived_files, ...r.errors].join("\n");
  if (r.passed) {
    const total = r.passed + r.failed;
    return <span className="ev real" title={tip}>{r.failed ? `${r.passed} of ${total}` : r.passed} real file{total > 1 ? "s" : ""}</span>;
  }
  if (r.derived_passed) return <span className="ev derived" title={tip}>{r.derived_passed} FFmpeg-made</span>;
  if (r.failed || r.derived_failed) return <span className="ev bad" title={tip}>sample failed</span>;
  return <span className="ev" title="No sample file in samples/">no sample</span>;
}

function PairCounts({ w }) {
  const ok = (w.tested_pairs || []).length;
  const failed = Object.entries(w.failed_pairs || {});
  if (!w.verified) return <span className="ev">—</span>;
  return (
    <div className="small">
      <span className="ev rt">{ok} passed</span>
      {failed.length > 0 && (
        <div className="pair-fail">
          {failed.map(([k, why]) => <div key={k}><code>{k.replace("+", " + ")}</code> withdrawn: {why}</div>)}
        </div>
      )}
    </div>
  );
}

function HdrResults({ hdr }) {
  if (!hdr) return null;
  const preserve = (hdr.preserve || []).map(([c, v]) => `${c.toUpperCase()}/${v.toUpperCase()}`);
  return (
    <div className="hdr-results small">
      <strong>HDR (tested on {hdr.source}).</strong>{" "}
      Keep HDR (10-bit, transfer and primaries signalled), self-tested with PQ on: {preserve.length ? preserve.join(", ") : "no path passed"}.{" "}
      Convert to SDR: {hdr.tonemap ? `passed (${hdr.detail?.tonemap})` : "failed — not offered"}.{" "}
      HLG was checked in the test suite: kept through MKV remux and WebM/VP9, MP4/AV1, MOV/HEVC re-encodes, and converted to SDR. Not tested with real HDR camera footage.
    </div>
  );
}

function OutRoundTrip({ row }) {
  const w = row.write_evidence;
  if (!w) return <span className="ev">—</span>;
  const tip = [...(w.checks || []), ...(w.errors || [])].join("\n");
  return w.passed ? <span className="ev rt" title={tip}>pass</span> : <span className="ev bad" title={tip}>failed</span>;
}

function OutIndependent({ row }) {
  const ind = row.write_evidence?.independent;
  if (!ind) return <span className="ev">—</span>;
  if (ind.passed === null) return <span className="ev" title={ind.detail}>no decoder</span>;
  return <span className={ind.passed ? "ev real" : "ev bad"} title={ind.detail}>{ind.passed ? `${ind.decoder} ✓` : `${ind.decoder} ✗`}</span>;
}

function status(f) {
  if (f.status_label) return f.status_label;
  const r = f.read?.verified, w = f.write?.verified;
  return r && w ? "Read + write" : r ? "Read only" : w ? "Write only" : "Unavailable";
}

function ImageFormats({ images }) {
  const [all, setAll] = useState(false);
  const label = Object.fromEntries(images.formats.map((f) => [f.id, f.label]));
  const unavailable = images.formats.filter((f) => f.priority && f.status_label);
  const rows = images.formats.filter((f) => all || f.priority);
  return (
    <div className="card">
      <h2>Images</h2>
      <div className="split">
        <div>
          <h3>Can read <span className="count">{images.readable.length}</span></h3>
          <div className="chips">{images.readable.map((id) => <span key={id} className="chip">{label[id]}</span>)}</div>
        </div>
        <div>
          <h3>Can write <span className="count">{images.writable.length}</span></h3>
          <div className="chips">{images.writable.map((id) => <span key={id} className="chip">{label[id]}</span>)}</div>
        </div>
      </div>

      {unavailable.length > 0 && (
        <div className="limits-box">
          <ul>
            {unavailable.map((f) => (
              <li key={f.id}>
                <strong>{f.label}</strong> — {f.status_label}.{" "}
                <details className="inline">
                  <summary>Details</summary>
                  <span className="muted small">{f.notes.join(" ")}</span>
                </details>
              </li>
            ))}
          </ul>
        </div>
      )}

      <dl className="legend">
        <div><dt><span className="ev rt">pass</span> Round-trip</dt><dd>This server's encoder wrote the format and its own decoder read it back (input: a test image; output: a real photo, a real transparent PNG, a real animated GIF and a real multipage TIFF, through the actual conversion pipeline).</dd></div>
        <div><dt><span className="ev real">real file</span> Independent compatibility</dt><dd>Input: files produced by other software decode here (<span className="ev derived">FFmpeg-made</span> = real content re-encoded by FFmpeg, weaker). Output: a different decoder (FFmpeg) reads our file and gets the same pixels.</dd></div>
      </dl>

      <div className="table-wrap">
        <table className="formats">
          <thead>
            <tr>
              <th rowSpan="2">Format</th><th rowSpan="2">Status</th>
              <th colSpan="2" className="group">Input</th><th colSpan="2" className="group">Output</th>
              <th rowSpan="2">Transparency</th><th rowSpan="2">Animation</th><th rowSpan="2">Multipage</th>
            </tr>
            <tr><th>Round-trip</th><th>Independent files</th><th>Round-trip</th><th>Independent decoder</th></tr>
          </thead>
          <tbody>
            {rows.map((f) => (
              <tr key={f.id} className={!f.read?.verified && !f.write?.verified ? "dim" : ""}>
                <td><strong>{f.label}</strong><div className="muted small">{f.extensions.join(" ")}</div></td>
                <td className="small">{status(f)}</td>
                <td><InRoundTrip row={f} /></td>
                <td><InIndependent row={f} /></td>
                <td>{f.write?.verified || f.write_evidence ? <OutRoundTrip row={f} /> : <span className="ev">—</span>}</td>
                <td><OutIndependent row={f} /></td>
                <td className="small">{!f.write?.verified ? "—" : f.features.alpha_binary ? "1-bit" : f.features.alpha_write ? "✓" : "—"}</td>
                <td className="small">{f.features.animation_write ? "read + write" : f.features.animation_read ? "read" : "—"}</td>
                <td className="small">{f.features.multipage_write ? "read + write" : f.features.multipage_read ? "read" : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <button className="link" onClick={() => setAll((a) => !a)}>
        {all ? "Show priority formats only" : `Show ${images.formats.length - images.formats.filter((f) => f.priority).length} additional formats`}
      </button>
    </div>
  );
}

function VideoFormats({ video }) {
  if (!video.available) {
    return (
      <div className="card"><h2>Video</h2><p>{video.notes.join(" ")}</p></div>
    );
  }
  return (
    <div className="card">
      <h2>Video</h2>
      <p className="muted small">
        Containers hold streams; codecs encode them. Every video × audio pair offered below was converted from {video.selftest?.clip || "a real clip"},
        then probed, fully decoded and checked for A/V sync ({video.selftest?.sync || "not measured"}). Pairs that failed are not offered.
      </p>
      <div className="table-wrap">
        <table>
          <thead>
            <tr><th>Container</th><th>Read</th><th>Independent files</th><th>Write</th><th>Real-clip encode</th><th>Pairs tested</th><th>Video codecs (write)</th><th>Audio codecs (write)</th></tr>
          </thead>
          <tbody>
            {video.containers.map((c) => (
              <tr key={c.id}>
                <td><strong>{c.label}</strong><div className="muted small">{c.extensions.join(" ")}</div></td>
                <td>{yes(c.read.verified)}</td>
                <td><InIndependent row={c} /></td>
                <td>{yes(c.write.verified)}</td>
                <td><OutRoundTrip row={c} /></td>
                <td><PairCounts w={c.write} /></td>
                <td>{c.write.video_codecs.map((v) => <span key={v.id} className={v.id === c.write.default_video ? "codec def" : "codec"}>{v.label}</span>)}</td>
                <td>{c.write.audio_codecs.map((a) => <span key={a.id} className={a.id === c.write.default_audio ? "codec def" : "codec"}>{a.label}</span>)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <HdrResults hdr={video.hdr} />
      <p className="muted small">Bold codec = default for that container. FFmpeg reports {video.counts.demuxers} demuxers; only the containers above are offered.</p>
    </div>
  );
}

function PdfTools({ pdf }) {
  return (
    <div className="card">
      <h2>PDF</h2>
      <div className="chips">
        {pdf.operations.map((o) => (
          <span key={o.id} className="chip disabled">
            {o.label}
            <small>{o.backend_ready ? "backend verified · tool coming soon" : "backend unavailable"}</small>
          </span>
        ))}
      </div>
      {pdf.notes.map((n) => <p key={n} className="muted small">{n}</p>)}
    </div>
  );
}
