const LIMIT_LABELS = {
  max_upload_mb: ["Max upload", (v) => `${v} MB`],
  max_image_megapixels: ["Max image size", (v) => `${v} MP`],
  max_video_seconds: ["Max video length", (v) => `${Math.round(v / 60)} min`],
  max_video_width: ["Max video width", (v) => `${v} px`],
  max_video_height: ["Max video height", (v) => `${v} px`],
  max_pdf_pages: ["Max PDF pages", (v) => `${v}`],
  probe_timeout_s: ["Analysis timeout", (v) => `${v} s`],
  process_timeout_s: ["Processing timeout", (v) => `${v} s`],
  file_ttl_minutes: ["Files deleted after", (v) => `${v} min`],
  max_concurrent_probes: ["Concurrent analyses", (v) => `${v}`],
};

export default function SystemPanel({ caps }) {
  const notes = [...(caps.video.notes || []), ...(caps.pdf.notes || [])];
  return (
    <section className="stack">
      <div className="card">
        <h2>Limits</h2>
        <p className="muted small">Set with <code>MF_*</code> environment variables on the server.</p>
        <dl className="facts">
          {Object.entries(caps.limits).map(([k, v]) => (
            <div key={k}>
              <dt>{LIMIT_LABELS[k]?.[0] || k}</dt>
              <dd>{LIMIT_LABELS[k] ? LIMIT_LABELS[k][1](v) : String(v)}</dd>
            </div>
          ))}
        </dl>
      </div>

      <div className="card">
        <h2>Backends and licences</h2>
        <div className="table-wrap">
          <table>
            <thead><tr><th>Component</th><th>Version</th><th>Licence</th><th>Used for</th></tr></thead>
            <tbody>
              {caps.licenses.map((l) => (
                <tr key={l.name}><td><strong>{l.name}</strong></td><td>{l.version}</td><td>{l.license}</td><td>{l.role}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="muted small">
          Not used because of licence: {caps.pdf.rejected.map((r) => `${r.name} (${r.license})`).join(", ")}.
        </p>
      </div>

      <div className="card">
        <h2>Self-test</h2>
        <dl className="facts">
          <div><dt>Ran at</dt><dd>{caps.detected_at}</dd></div>
          <div><dt>Duration</dt><dd>{caps.detect_seconds} s</dd></div>
          <div><dt>Fingerprint</dt><dd><code>{caps.fingerprint}</code></dd></div>
          <div><dt>Real sample files</dt><dd>{caps.real_samples?.files ?? 0}</dd></div>
        </dl>
        {notes.map((n) => <p key={n} className="muted small">{n}</p>)}
      </div>
    </section>
  );
}
