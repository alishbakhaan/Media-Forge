// Small inline icon set (24 px grid, stroke follows the text colour). Decorative: always hidden from screen readers.

function Svg({ size = 24, children, ...rest }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false" {...rest}>
      {children}
    </svg>
  );
}

export const IconImage = (p) => <Svg {...p}><rect x="3" y="4" width="18" height="16" rx="3" /><circle cx="8.5" cy="9.5" r="1.5" /><path d="m21 15-4.5-4.5L7 20" /></Svg>;
export const IconVideo = (p) => <Svg {...p}><rect x="3" y="5" width="18" height="14" rx="3" /><path d="m10 9 5 3-5 3z" /></Svg>;
export const IconDoc = (p) => <Svg {...p}><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" /><path d="M14 3v5h5" /><path d="M9 13h6M9 17h4" /></Svg>;
export const IconUpload = (p) => <Svg {...p}><path d="M12 16V4" /><path d="m7 9 5-5 5 5" /><path d="M4 16v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2" /></Svg>;
export const IconDownload = (p) => <Svg {...p}><path d="M12 4v12" /><path d="m7 11 5 5 5-5" /><path d="M4 20h16" /></Svg>;
export const IconArrow = (p) => <Svg {...p}><path d="M5 12h14" /><path d="m13 6 6 6-6 6" /></Svg>;
export const IconBack = (p) => <Svg {...p}><path d="M19 12H5" /><path d="m11 6-6 6 6 6" /></Svg>;
export const IconCheck = (p) => <Svg {...p}><path d="m5 12.5 4.5 4.5L19 7.5" /></Svg>;
export const IconChevron = (p) => <Svg {...p}><path d="m6 9 6 6 6-6" /></Svg>;
export const IconAlert = (p) => <Svg {...p}><path d="M10.3 3.9 2.6 17a2 2 0 0 0 1.7 3h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" /><path d="M12 9v4" /><path d="M12 17h.01" /></Svg>;
export const IconInfo = (p) => <Svg {...p}><circle cx="12" cy="12" r="9" /><path d="M12 11v5" /><path d="M12 8h.01" /></Svg>;
export const IconLock = (p) => <Svg {...p}><rect x="5" y="11" width="14" height="9" rx="2" /><path d="M8 11V8a4 4 0 0 1 8 0v3" /></Svg>;
export const IconMerge = (p) => <Svg {...p}><path d="M6 4v3a6 6 0 0 0 6 6v7" /><path d="M18 4v3a6 6 0 0 1-6 6" /><path d="m9 17 3 3 3-3" /></Svg>;
export const IconCompress = (p) => <Svg {...p}><path d="M9 4v5H4" /><path d="m4 4 5 5" /><path d="M15 20v-5h5" /><path d="m20 20-5-5" /></Svg>;
export const IconSplit = (p) => <Svg {...p}><rect x="4" y="3" width="16" height="18" rx="2" /><path d="M4 12h1.5M8.5 12h2M13.5 12h2M18.5 12H20" /></Svg>;
export const IconRotate = (p) => <Svg {...p}><path d="M20 11a8 8 0 1 0-2.3 5.7" /><path d="M20 4v7h-7" /></Svg>;

// A small file glyph with a short type label ("PDF", "DOC"…), used on the document tool cards.
export function FileBadge({ label, tone = "red" }) {
  return (
    <span className={`fbadge tone-${tone}`} aria-hidden="true">
      <svg width="26" height="26" viewBox="0 0 24 24" focusable="false">
        <path d="M6 2h8l5 5v13a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2z" fill="currentColor" />
        <path d="M14 2v3a2 2 0 0 0 2 2h3z" fill="#fff" fillOpacity=".45" />
        <text x="11.5" y="17.4" textAnchor="middle" fontSize="6" fontWeight="800" fill="#fff" fontFamily="system-ui, sans-serif">{label}</text>
      </svg>
    </span>
  );
}

// A round tile holding one of the stroke icons above.
export function IconTile({ Icon, tone = "red" }) {
  return <span className={`fbadge tone-${tone}`} aria-hidden="true"><Icon size={22} /></span>;
}
export const IconSun = (p) => <Svg {...p}><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></Svg>;
export const IconMoon = (p) => <Svg {...p}><path d="M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5z" /></Svg>;
