import { useCallback, useRef, useState } from "react";
import { deleteItem, uploadFile } from "./api.js";

// One file at a time: upload → the server inspects it → report.
// phase: "idle" | "uploading" | "ready" | "error". Replacing or removing a file deletes its server copy.
export function useUpload(maxMb) {
  const [state, setState] = useState({ phase: "idle" });
  const live = useRef({ seq: 0, ctrl: null, id: null });

  // Forget whatever is in flight or stored on the server.
  const discard = useCallback(() => {
    const c = live.current;
    c.seq++;
    c.ctrl?.abort();
    c.ctrl = null;
    if (c.id) deleteItem(c.id);
    c.id = null;
  }, []);

  const start = useCallback((file) => {
    discard();
    const c = live.current;
    const seq = c.seq;
    if (file.size > maxMb * 1024 * 1024) {
      setState({ phase: "error", file, error: { code: "too_large", message: `This file is larger than ${maxMb} MB.` } });
      return;
    }
    c.ctrl = new AbortController();
    setState({ phase: "uploading", file, progress: 0 });
    uploadFile(file, (p) => {
      if (live.current.seq === seq) setState((s) => (s.phase === "uploading" ? { ...s, progress: p } : s));
    }, c.ctrl.signal).then((res) => {
      if (live.current.seq !== seq) {
        if (res.ok && res.body?.id) deleteItem(res.body.id); // finished after being replaced
        return;
      }
      c.ctrl = null;
      if (res.ok) {
        c.id = res.body.id;
        setState({ phase: "ready", file, report: res.body });
      } else {
        setState({ phase: "error", file, error: res.body?.error || {} });
      }
    });
  }, [discard, maxMb]);

  const reset = useCallback(() => {
    discard();
    setState({ phase: "idle" });
  }, [discard]);

  // Take over a file that another tab already uploaded, without sending it again.
  const adopt = useCallback((file, report) => {
    discard();
    live.current.id = report.id;
    setState({ phase: "ready", file, report });
  }, [discard]);

  // Hand the stored file to another tab: forget it here but keep the server copy.
  const release = useCallback(() => {
    live.current.id = null;
    live.current.seq++;
    setState({ phase: "idle" });
  }, []);

  return { ...state, start, reset, adopt, release };
}
