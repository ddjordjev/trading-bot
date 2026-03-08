import { type ReactNode, useEffect, useState } from "react";
import { BotProfiles } from "./BotProfiles";
import { Modules } from "./Modules";
import { RuntimeTuning } from "./RuntimeTuning";
import { Summary } from "./Summary";
import { get, setToken } from "../api/client";

type Section = "bots" | "modules" | "runtimeTuning" | "auth" | "about" | "summary";

const UPPER_LINKS: { id: Section; label: string }[] = [
  { id: "bots", label: "Bots" },
  { id: "modules", label: "Modules" },
  { id: "runtimeTuning", label: "Runtime Tuning" },
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
  const [version, setVersion] = useState("1.0.0");
  const BUILD_DATE = "2026-02-20";
  const REPO_URL = "https://github.com/ddjordjev/trading-bot";
  const [deployCommit, setDeployCommit] = useState("unknown");
  const shortCommit = deployCommit !== "unknown" ? deployCommit.slice(0, 8) : "unknown";

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const meta = await get<{ version?: string; deploy_commit?: string }>("/api/about");
        const resolvedVersion = String(meta?.version || "").trim();
        const commit = String(meta?.deploy_commit || "").trim();
        if (alive && resolvedVersion) setVersion(resolvedVersion);
        if (alive && commit) setDeployCommit(commit);
      } catch {
        // Keep About section resilient even if metadata endpoint is unavailable.
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  const rows: [string, string | ReactNode][] = [
    ["Name", "Trade Borg"],
    ["Version", `v${version} (${shortCommit})`],
    ["Deploy Commit", deployCommit],
    ["Build Date", BUILD_DATE],
    [
      "GitHub",
      <a key="repo-link" href={REPO_URL} target="_blank" rel="noopener noreferrer">
        {REPO_URL}
      </a>,
    ],
    ["Exchanges", "Binance\nBybit"],
    ["License", "Proprietary"],
  ];

  return (
    <div>
      <div style={{
        display: "flex", flexDirection: "column", alignItems: "center",
        padding: "2rem 0 1.5rem", borderBottom: "1px solid var(--border)", marginBottom: "1.5rem",
      }}>
        <span style={{ fontSize: "2.5rem", fontWeight: 700, color: "var(--heading)", letterSpacing: "-0.5px" }}>
          Trade Borg
        </span>
        <span style={{ fontSize: "0.9rem", color: "var(--text-muted)", marginTop: "0.25rem" }}>
          Algorithmic Trading System
        </span>
        <span style={{
          marginTop: "0.5rem", padding: "0.2rem 0.6rem",
          background: "var(--surface)", border: "1px solid var(--border)",
          borderRadius: "12px", fontSize: "0.75rem", color: "var(--accent)",
        }}>
          v{version} ({shortCommit})
        </span>
      </div>

      <table style={{ maxWidth: 420 }}>
        <tbody>
          {rows.map(([label, value]) => (
            <tr key={label}>
              <td style={{ color: "var(--text-muted)", fontWeight: 500, width: 140 }}>{label}</td>
              <td style={{ color: "var(--heading)" }}>
                {typeof value === "string"
                  ? value.includes("\n")
                    ? value.split("\n").map((line, i) => <div key={i}>{line}</div>)
                    : value
                  : value}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <div style={{
        marginTop: "2rem", paddingTop: "1.5rem",
        borderTop: "1px solid var(--border)",
        fontSize: "0.75rem", color: "#484f58", lineHeight: 1.8,
      }}>
        <p>&copy; {new Date().getFullYear()} Trade Borg. All rights reserved.</p>
        <p>
          This software is proprietary and confidential. Unauthorized copying, distribution,
          or use of this software, via any medium, is strictly prohibited.
        </p>
        <p style={{ marginTop: "0.5rem" }}>
          Trade Borg is provided &ldquo;as is&rdquo; without warranty of any kind. Trading
          cryptocurrencies involves substantial risk of loss. Past performance does not
          guarantee future results.
        </p>
      </div>
    </div>
  );
}

export function Settings() {
  const [section, setSection] = useState<Section>("bots");
  const [renderKey, setRenderKey] = useState(0);

  const navigate = (id: Section) => {
    setSection(id);
    setRenderKey((k) => k + 1);
  };

  return (
    <div style={{ display: "flex", gap: "1.5rem", minHeight: "calc(100vh - 140px)" }}>
      <nav style={{
        width: 180, flexShrink: 0,
        borderRight: "1px solid var(--border)",
        paddingRight: "1rem",
        display: "flex", flexDirection: "column",
      }}>
        <div>
          {UPPER_LINKS.map((link) => (
            <button
              key={link.id}
              onClick={() => navigate(link.id)}
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
          paddingTop: "0.5rem", marginTop: "0.25rem",
        }}>
          {LOWER_LINKS.map((link) => (
            <button
              key={link.id}
              onClick={() => navigate(link.id)}
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
        {section === "bots" && <BotProfiles key={renderKey} />}
        {section === "modules" && <Modules key={renderKey} />}
        {section === "runtimeTuning" && <RuntimeTuning key={renderKey} />}
        {section === "auth" && <AuthSection />}
        {section === "about" && <AboutSection />}
        {section === "summary" && <Summary key={renderKey} />}
      </div>
    </div>
  );
}
