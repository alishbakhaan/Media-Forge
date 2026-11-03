// Thin API client. The backend is same-origin (it serves this bundle).

export async function fetchCapabilities(signal) {
  // Detection runs once at server start (a few seconds, then cached); poll until ready.
  for (let attempt = 0; attempt < 120; attempt++) {
    const res = await fetch("/api/capabilities", { signal });
    if (res.status === 503) {
      await new Promise((r) => setTimeout(r, 1000));
      continue;
    }
    const body = await res.json();
    if (!res.ok) throw new Error(body?.error?.message || `Server error ${res.status}`);
    return body;
  }
  throw new Error("Capability detection did not finish in time.");
}

// Upload one file as the raw request body so the server can stream it to disk and
// enforce the size limit without buffering. Returns { ok, status, body }. Aborting `signal` cancels the upload.
export function uploadFile(file, onProgress, signal) {
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/uploads");
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.setRequestHeader("X-Filename", encodeURIComponent(file.name));
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
    };
    xhr.onload = () => {
      let body = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        body = { error: { code: "bad_response", message: `Unexpected server response (${xhr.status}).` } };
      }
      resolve({ ok: xhr.status >= 200 && xhr.status < 300, status: xhr.status, body });
    };
    xhr.onerror = () =>
      resolve({ ok: false, status: 0, body: { error: { code: "network", message: "Upload failed: network error." } } });
    xhr.onabort = () => resolve({ ok: false, status: 0, body: { error: { code: "aborted", message: "Upload cancelled." } } });
    signal?.addEventListener("abort", () => xhr.abort(), { once: true });
    xhr.send(file);
  });
}

export function formatBytes(n) {
  if (n == null) return "—";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v < 10 && i > 0 ? v.toFixed(1) : Math.round(v)} ${units[i]}`;
}

export function formatDuration(s) {
  if (s == null) return "—";
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const secNum = s % 60;
  const sec = s < 10 ? secNum.toFixed(1).padStart(4, "0") : String(Math.floor(secNum)).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
}

export function deleteItem(id) {
  return fetch(`/api/items/${id}`, { method: "DELETE" }).catch(() => {});
}

// "62% smaller" / "8% larger" — plain words for the size difference.
export function sizeChange(before, after) {
  if (!before || after == null) return null;
  const p = ((after - before) / before) * 100;
  if (Math.abs(p) < 0.5) return { text: "Same size", dir: "same" };
  const n = Math.abs(p);
  const amount = n >= 1000 ? `${Math.round(n / 100)}×` : `${n < 10 ? n.toFixed(1) : Math.round(n)}%`;
  return p < 0 ? { text: `${amount} smaller`, dir: "smaller" } : { text: `${amount} larger`, dir: "larger" };
}

// The interface uses one spelling; server-provided notes are British.
export const us = (s) => (typeof s === "string" ? s.replace(/colour/g, "color").replace(/Colour/g, "Color") : s);

// Server notes are lower-case fragments; show them as sentences.
export const sentence = (s) => `${s.charAt(0).toUpperCase()}${s.slice(1)}${/[.!?]$/.test(s) ? "" : "."}`;

// Short everyday names for the format picker.
const SHORT_NAMES = { jpeg: "JPG", mov: "MOV", mkv: "MKV", mpeg: "MPEG", ogv: "OGG", flv: "FLV", asf: "WMV" };
export const formatName = (id, label) => SHORT_NAMES[id] || label;

const OFFLINE = { ok: false, status: 0, body: { error: { code: "network", message: "Could not reach the server." } } };

async function jsonCall(method, url, body) {
  let res;
  try {
    res = await fetch(url, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    return OFFLINE;
  }
  let data;
  try {
    data = await res.json();
  } catch {
    data = { error: { code: "bad_response", message: `Unexpected server response (${res.status}).` } };
  }
  return { ok: res.ok, status: res.status, body: data };
}

export const convertItem = (id, body) => jsonCall("POST", `/api/items/${id}/convert`, body);
export const planVideo = (id, body) => jsonCall("POST", `/api/items/${id}/video/plan`, body);
export const submitVideo = (id, body) => jsonCall("POST", `/api/items/${id}/video/jobs`, body);
export const getJob = (jobId) => jsonCall("GET", `/api/jobs/${jobId}`);
export const cancelJob = (jobId) => jsonCall("POST", `/api/jobs/${jobId}/cancel`);
export const runDocTool = (tool, items, options) => jsonCall("POST", `/api/docs/${tool}`, { items, options });
