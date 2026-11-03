import { useState } from "react";

// Before/after wipe comparison. Both images are drawn at the same size; "100%" shows real pixels
// (scroll to pan) so compression artefacts are visible.
export default function Compare({ before, after, width, height, note, maxHeight = 480 }) {
  const [pos, setPos] = useState(50);
  const [actual, setActual] = useState(false);
  const fitW = Math.round((maxHeight * width) / height);
  const style = actual ? { width: `${width}px`, height: `${height}px` } : { width: `min(100%, ${fitW}px)`, aspectRatio: `${width} / ${height}` };
  return (
    <div className="compare">
      <div className="compare-bar">
        <span className="small"><strong>Before</strong> ◀ drag ▶ <strong>After</strong></span>
        <label className="check small">
          <input type="checkbox" checked={actual} onChange={(e) => setActual(e.target.checked)} /> <span>Actual size</span>
        </label>
      </div>
      <div className={actual ? "compare-view actual" : "compare-view"}>
        <div className="compare-stage" style={style}>
          <img src={before} alt="Before conversion" draggable="false" />
          <img src={after} alt="After conversion" draggable="false" style={{ clipPath: `inset(0 0 0 ${pos}%)` }} />
          <div className="compare-line" style={{ left: `${pos}%` }} />
        </div>
      </div>
      <input type="range" min="0" max="100" value={pos} onChange={(e) => setPos(Number(e.target.value))} aria-label="Comparison position" className="compare-range" />
      {note && <p className="muted small">{note}</p>}
    </div>
  );
}
