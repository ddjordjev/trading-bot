import { useState } from "react";
import { Modules } from "./Modules";
import { Summary } from "./Summary";
import { setToken } from "../api/client";

type Section = "modules" | "auth" | "about" | "summary";

const UPPER_LINKS: { id: Section; label: string }[] = [
  { id: "modules", label: "Modules" },
  { id: "auth", label: "Authentication" },
];

const LOWER_LINKS: { id: Section; label: string }[] = [
  { id: "about", label: "About" },
  { id: "summary", label: "Summary" },
];

function AuthSection() {
  const [tokenValue, setTokenValue] = useState("");
  const [saved, setSaved] = useState(false);
  const currentToken = localStorage.getItem("dashboard_token") || "";

  const handleSave = () => {
    setToken(tokenValue);
  };

  const handleClear = () => {
    localStorage.removeItem("dashboard_token");
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
    setTokenValue("");
  };

  return (
    <div>
      <h3 style={{ color: "var(--heading)", marginBottom: "0.5rem" }}>Authentication</h3>
      <p style={{ color: "var(--text-muted)", fontSize: "0.85rem", marginBottom: "1rem" }}>
        Set the dashboard token for remote access. Required when <code>DASHBOARD_TOKEN</code> is
        configured in your <code>.env</code> file.
      </p>
      <div style={{ maxWidth: 400, display: "flex", flexDirection: "column", gap: "0.75rem" }}>
        <div>
          <label style={{ fontSize: "0.8rem", color: "var(--text-muted)", display: "block", marginBottom: "0.25rem" }}>
            Dashboard Token
          </label>
          <input
            type="password"
            placeholder={currentToken ? "••••••••" : "Enter token"}
            value={tokenValue}
            onChange={(e) => setTokenValue(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && tokenValue) handleSave(); }}
            style={{
              width: "100%", padding: "0.5rem 0.75rem",
              border: "1px solid var(--border)", borderRadius: "var(--radius)",
              background: "var(--surface)", color: "var(--text)", fontSize: "0.9rem",
              boxSizing: "border-box",
            }}
          />
        </div>
        <div style={{ display: "flex", gap: "0.5rem" }}>
          <button className="btn-primary" onClick={handleSave} disabled={!tokenValue}>
            Save & Reload
          </button>
          {currentToken && (
            <button style={{ fontSize: "0.85rem", padding: "0.4rem 0.75rem" }} onClick={handleClear}>
              Clear Token
            </button>
          )}
        </div>
        {saved && <span style={{ color: "var(--green)", fontSize: "0.8rem" }}>Token cleared</span>}
        {currentToken && (
          <span style={{ fontSize: "0.8rem", color: "var(--text-muted)" }}>
            A token is currently set.
          </span>
        )}
      </div>
    </div>
  );
}

function AboutSection() {
  return (
    <div>
      <h3 style={{ color: "var(--heading)", marginBottom: "0.5rem" }}>About</h3>
      <p style={{ color: "var(--text-muted)", fontSize: "0.9rem" }}>
        More information coming soon.
      </p>
    </div>
  );
}

export function Settings() {
  const [section, setSection] = useState<Section>("modules");

  return (
    <div style={{ display: "flex", gap: "1.5rem", minHeight: "calc(100vh - 140px)" }}>
      <nav style={{
        width: 180, flexShrink: 0,
        borderRight: "1px solid var(--border)",
        paddingRight: "1rem",
        display: "flex", flexDirection: "column",
      }}>
        <div style={{ flex: 1 }}>
          {UPPER_LINKS.map((link) => (
            <button
              key={link.id}
              onClick={() => setSection(link.id)}
              style={{
                display: "block", width: "100%", textAlign: "left",
                padding: "0.5rem 0.75rem", marginBottom: "0.25rem",
                borderRadius: "var(--radius)", border: "none",
                background: section === link.id ? "var(--surface-hover)" : "transparent",
                color: section === link.id ? "var(--heading)" : "var(--text-muted)",
                fontWeight: section === link.id ? 600 : 400,
                fontSize: "0.9rem", cursor: "pointer",
                transition: "all 0.15s",
              }}
            >
              {link.label}
            </button>
          ))}
        </div>

        <div style={{
          borderTop: "1px solid var(--border)",
          paddingTop: "0.5rem", marginTop: "0.5rem",
        }}>
          {LOWER_LINKS.map((link) => (
            <button
              key={link.id}
              onClick={() => setSection(link.id)}
              style={{
                display: "block", width: "100%", textAlign: "left",
                padding: "0.5rem 0.75rem", marginBottom: "0.25rem",
                borderRadius: "var(--radius)", border: "none",
                background: section === link.id ? "var(--surface-hover)" : "transparent",
                color: section === link.id ? "var(--heading)" : "var(--text-muted)",
                fontWeight: section === link.id ? 600 : 400,
                fontSize: "0.9rem", cursor: "pointer",
                transition: "all 0.15s",
              }}
            >
              {link.label}
            </button>
          ))}
        </div>
      </nav>

      <div style={{ flex: 1, minWidth: 0 }}>
        {section === "modules" && <Modules />}
        {section === "auth" && <AuthSection />}
        {section === "about" && <AboutSection />}
        {section === "summary" && <Summary />}
      </div>
    </div>
  );
}
