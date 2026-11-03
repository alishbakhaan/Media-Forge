import FormatsPanel from "./FormatsPanel.jsx";
import { IconBack } from "./icons.jsx";
import SystemPanel from "./SystemPanel.jsx";

// Formats and System information: kept out of the main navigation, reached from the footer link.
export default function DetailsPage({ caps, section }) {
  const tab = section === "system" ? "system" : "formats";
  return (
    <div className="page details">
      <a className="back" href="#images"><IconBack size={18} /> Back to the converter</a>
      <header className="head compact">
        <h1>Technical details</h1>
        <p>What this server can read and write, and how it is set up.</p>
      </header>
      <nav className="subnav" aria-label="Technical details">
        <a href="#details" aria-current={tab === "formats" ? "page" : undefined}>Formats</a>
        <a href="#details/system" aria-current={tab === "system" ? "page" : undefined}>System</a>
      </nav>
      {tab === "formats" ? <FormatsPanel caps={caps} /> : <SystemPanel caps={caps} />}
    </div>
  );
}
