import { useCallback, useEffect, useState } from "react";
import { fetchCapabilities } from "./api.js";
import DetailsPage from "./DetailsPage.jsx";
import DocumentPage from "./DocumentPage.jsx";
import { IconDoc, IconImage, IconMoon, IconSun, IconVideo } from "./icons.jsx";
import ImagePage from "./ImagePage.jsx";
import { Notice } from "./ui.jsx";
import VideoPage from "./VideoPage.jsx";

const TABS = [
  { id: "images", label: "Convert Images", Icon: IconImage },
  { id: "documents", label: "Convert Document", Icon: IconDoc },
  { id: "videos", label: "Convert Videos", Icon: IconVideo },
];
const PAGES = [...TABS.map((t) => t.id), "details"];
// Addresses from the previous layout keep working.
const OLD = { files: "images", formats: "details", system: "details/system" };

function readRoute() {
  const raw = window.location.hash.replace(/^#\/?/, "");
  const [page, sub] = (OLD[raw] || raw).split("/");
  return PAGES.includes(page) ? { page, sub: sub || null } : { page: "images", sub: null };
}

// Light or dark: the user's choice is remembered on this device; until they choose, follow the system setting.
function useTheme() {
  const system = () => (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const [theme, setTheme] = useState(() => {
    try { return localStorage.getItem("mf-theme") || system(); } catch { return system(); }
  });
  useEffect(() => { document.documentElement.dataset.theme = theme; }, [theme]);
  const toggle = () => setTheme((t) => {
    const next = t === "dark" ? "light" : "dark";
    try { localStorage.setItem("mf-theme", next); } catch { /* private mode: still switches for this visit */ }
    return next;
  });
  return [theme, toggle];
}

export default function App() {
  const [theme, toggleTheme] = useTheme();
  const [route, setRoute] = useState(readRoute);
  const [caps, setCaps] = useState(null);
  const [error, setError] = useState(null);
  const [handoff, setHandoff] = useState({}); // a file uploaded on the wrong tab, passed to the right one

  useEffect(() => {
    const ctrl = new AbortController();
    fetchCapabilities(ctrl.signal).then(setCaps).catch((e) => {
      if (e.name !== "AbortError") setError(e.message);
    });
    return () => ctrl.abort();
  }, []);

  useEffect(() => {
    const onHash = () => setRoute(readRoute());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    const tab = TABS.find((t) => t.id === route.page);
    document.title = `${tab ? tab.label : "Technical details"} · MediaForge`;
  }, [route.page]);

  const move = useCallback((page, file, report) => {
    setHandoff((h) => ({ ...h, [page]: { file, report } }));
    window.location.hash = `#${page}`;
  }, []);

  return (
    <div className="shell">
      <a className="skip" href="#main" onClick={(e) => { e.preventDefault(); document.getElementById("main")?.focus(); }}>Skip to content</a>
      <header className="topbar">
        <a className="brand" href="#images" aria-label="MediaForge home">
          <span className="logo" aria-hidden="true">M</span>
          <span>MediaForge</span>
        </a>
        <nav className="nav" aria-label="Main">
          {TABS.map(({ id, label, Icon }) => (
            <a key={id} href={`#${id}`} aria-current={route.page === id ? "page" : undefined}>
              <Icon size={18} className={`ic-${id}`} />
              <span>{label}</span>
            </a>
          ))}
        </nav>
        <button type="button" className="theme-toggle" onClick={toggleTheme}
          aria-label={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"} title={theme === "dark" ? "Light mode" : "Dark mode"}>
          {theme === "dark" ? <IconSun size={18} /> : <IconMoon size={18} />}
        </button>
      </header>

      <main id="main" className="content" tabIndex={-1}>
        {error && <Notice tone="bad" role="alert">MediaForge couldn’t start: {error}</Notice>}
        {!caps && !error && (
          <p className="starting" role="status">Getting ready… The first start can take a few minutes.</p>
        )}
        {caps && (
          <>
            {/* The three converters stay mounted so a running conversion survives a tab switch. */}
            <div hidden={route.page !== "images"}><ImagePage caps={caps} incoming={handoff.images} onMove={move} /></div>
            <div hidden={route.page !== "documents"}><DocumentPage caps={caps} toolId={route.page === "documents" ? route.sub : null} /></div>
            <div hidden={route.page !== "videos"}><VideoPage caps={caps} incoming={handoff.videos} onMove={move} /></div>
            {route.page === "details" && <DetailsPage caps={caps} section={route.sub} />}
          </>
        )}
      </main>

      <footer className="foot">
        {caps && <span>Files are deleted automatically after {caps.limits.file_ttl_minutes} minutes.</span>}
        <a href="#details">Technical details</a>
      </footer>
    </div>
  );
}
